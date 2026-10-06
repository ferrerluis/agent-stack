#!/usr/bin/env python3
"""Internal owned-VM SSH channel; native container routing is a later gate.

No public CLI, provider credentials, arbitrary forward/bind or management
operation is exposed. The caller must close both this channel and its gateway.
"""
from collections import deque
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import stat
import subprocess
import threading
import time

from showcase_attempt import KEY_PATH, command, isolated_environment, remaining_timeout, verify_prepared
from showcase_model_gateway import BudgetGateway
from showcase_policy import PolicyError, require

RELAY = "scripts/showcase_model_relay.py"


def private_ipv4(value):
    try: address = ipaddress.ip_address(value)
    except (TypeError, ValueError): raise PolicyError("model_bridge_address_unverified") from None
    require(address.version == 4 and any(address in ipaddress.ip_network(net) for net in
            ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")), "model_bridge_address_unverified")
    return str(address)


def report(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "model_transport_report_invalid"); value[key] = item
        return value
    try: value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False, "model_transport_report_invalid"))
    except (ValueError, UnicodeError): raise PolicyError("model_transport_report_invalid") from None
    require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1,
            "model_transport_report_invalid")
    return value


class SSHModelTransport:
    def __init__(self, checks, gateway, popen=subprocess.Popen, execute=command):
        require(isinstance(gateway, BudgetGateway), "model_gateway_required")
        self.checks, self.gateway, self.popen, self.execute = checks, gateway, popen, execute
        self.state, self.process, self.pump = "new", None, None
        self.condition = threading.Condition(); self.records = deque(); self.allocated = None
        self.deadline, self.ready, self.proof = None, None, None
        self.shutdown_verified = None

    def healthy(self):
        return self.state in ("running", "verified") and self.process is not None and self.process.poll() is None \
            and self.pump is not None and self.pump.is_alive() and self.checks.now() < self.deadline - 5

    def drain(self, port):
        buffers = {self.process.stdout: bytearray(), self.process.stderr: bytearray()}
        counts = {stream: 0 for stream in buffers}
        try:
            for stream in buffers: os.set_blocking(stream.fileno(), False)
            while buffers:
                if self.state not in ("stopping", "closed") and self.checks.now() >= self.deadline - 5:
                    raise PolicyError("model_transport_deadline_reached")
                readable, _, _ = select.select(list(buffers), [], [], .05)
                for stream in readable:
                    raw = os.read(stream.fileno(), 4096)
                    if not raw:
                        require(not buffers[stream], "model_transport_report_invalid")
                        del buffers[stream]; continue
                    counts[stream] += len(raw)
                    require(counts[stream] <= 8192, "model_transport_output_limit")
                    buffer = buffers[stream]; buffer.extend(raw)
                    while b"\n" in buffer:
                        line, _, tail = buffer.partition(b"\n"); buffer[:] = tail
                        with self.condition:
                            if stream is self.process.stdout:
                                require(len(self.records) < 4, "model_transport_output_limit")
                                self.records.append(report(bytes(line)))
                            else:
                                # OpenSSH's native diagnostic uses CRLF even
                                # without a terminal. Never trim JSON stdout.
                                if line.endswith(b"\r"): line = line[:-1]
                                match = re.fullmatch(rb"Allocated port ([1-9][0-9]{0,4}) for remote forward to 127\.0\.0\.1:" + str(port).encode(), line)
                                if match:
                                    require(self.allocated is None and int(match[1]) <= 65535, "model_forward_allocation_invalid")
                                    self.allocated = int(match[1])
                            self.condition.notify_all()
                if not readable and self.process.poll() is not None:
                    # Drain the remaining EOFs before confirming exit.
                    continue
        except BaseException:
            with self.condition:
                self.state = "failed"; self.condition.notify_all()
            try: self.process.terminate()
            except OSError: pass
        finally:
            with self.condition: self.condition.notify_all()

    def read(self, status, seconds=15):
        end = time.monotonic() + seconds
        with self.condition:
            while True:
                require(self.state != "failed", "model_transport_failed")
                if status == "allocation" and self.allocated is not None: return self.allocated
                if status != "allocation" and self.records:
                    value = self.records.popleft()
                    require(value.get("status") == status, "model_transport_report_invalid"); return value
                require(self.process.poll() is None and time.monotonic() < end, "model_transport_failed")
                self.condition.wait(min(.05, max(0, end - time.monotonic())))

    def send(self, value):
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        require(len(raw) <= 8192 and self.process.poll() is None, "model_transport_failed")
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            require(os.write(self.process.stdin.fileno(), raw) == len(raw), "model_transport_failed")
        except (OSError, ValueError): raise PolicyError("model_transport_failed") from None

    def __enter__(self):
        require(self.state == "new", "model_transport_already_used"); self.state = "starting"
        try:
            prepared, active = self.checks.ensure()
            remaining_timeout(active, 75, now=self.checks.now())
            require(active["work_deadline"] - self.checks.now() >= 75, "model_transport_deadline_reached")
            spend = self.gateway.spend
            require(spend.ledger is self.checks.ledger and spend.run_id == self.checks.run.name
                    and spend.provider == self.checks.provider and spend.policy["source_revision"] == prepared["source_revision"],
                    "model_transport_scope_changed")
            evidence = self.checks.evidence(); self.checks.require_container_gate(evidence)
            baseline = evidence.get("after_reboot") or evidence.get("baseline")
            require(evidence["checks"].get("launch_ssh") == "pass" and isinstance(baseline, dict), "bootstrap_proof_required")
            target = self.checks.bind()
            require(baseline.get("checksum") == target["checksum"] and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", baseline.get("boot_id", "")),
                    "model_transport_boot_changed")
            checkout = self.checks.attempt.checkout; root = Path(__file__).resolve().parents[1]
            for name in (RELAY, "bin/agent-stack-ssh"):
                source = checkout / name
                require(source.is_file() and not source.is_symlink() and source.read_bytes() == (root / name).read_bytes(),
                        "model_transport_source_changed")
            hosts = checkout / ".ssh/known_hosts"
            require(hosts.is_file() and not hosts.is_symlink() and hosts.stat().st_size > 0
                    and hosts.stat().st_uid == os.geteuid() and stat.S_IMODE(hosts.stat().st_mode) == 0o600, "private_known_hosts_required")
            verify_prepared(self.checks.run, self.checks.config, self.checks.provider)
            self.deadline = active["work_deadline"]
            # Revoke dispatch while this owned channel is being established.
            # A failed startup cannot leave a standalone gateway usable.
            self.gateway.require_transport(self.healthy)
            # Check effective Match rules before asking SSH to bind anything.
            # A server with GatewayPorts=yes could force a wildcard listener.
            base = [str(checkout / "bin/agent-stack-ssh"), "--batch", "--known-hosts", str(hosts), "--key", str(checkout / KEY_PATH),
                    "--host", target["host"]]
            policy = report(self.execute(base + ["--pinned", "--", shlex.join(["python3", "-B", "-c", (checkout / RELAY).read_text(), "--check-forwarding"])],
                            env=isolated_environment(self.checks.run), timeout=remaining_timeout(active, 15, now=self.checks.now())))
            require(set(policy) == {"schema", "status", "gateway_ports", "allow_tcp_forwarding", "disable_forwarding"}
                    and policy["status"] == "forwarding_policy_verified" and policy["gateway_ports"] == "no"
                    and policy["disable_forwarding"] == "no" and policy["allow_tcp_forwarding"] in ("yes", "all", "remote"),
                    "model_forwarding_policy_unverified")
            port = self.gateway.server.server_port
            args = base + ["--model-forward", str(port), "--",
                    shlex.join(["sudo", "-n", "python3", "-B", "-u", "-c", (checkout / RELAY).read_text()])]
            self.process = self.popen(args, env=isolated_environment(self.checks.run), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self.pump = threading.Thread(target=self.drain, args=(port,), daemon=True); self.pump.start()
            allocated = self.read("allocation")
            self.send({"run_id": self.checks.run.name, "boot_id": baseline["boot_id"], "checksum": target["checksum"],
                       "forward_port": allocated, "sent_at": self.checks.now(), "work_deadline": self.deadline})
            ready = self.read("ready")
            require(set(ready) == {"schema", "status", "bridge_ipv4", "bridge_port", "boot_id", "checksum", "forward_port"}
                    and ready["boot_id"] == baseline["boot_id"] and ready["checksum"] == target["checksum"]
                    and type(ready["forward_port"]) is int and ready["forward_port"] == allocated
                    and type(ready["bridge_port"]) is int and 1 <= ready["bridge_port"] <= 65535, "model_transport_report_invalid")
            ready["bridge_ipv4"] = private_ipv4(ready["bridge_ipv4"]); self.ready = ready; self.state = "running"
            probe = self.gateway.new_probe("codex")
            self.send({name: probe[name] for name in ("role", "nonce", "credential")})
            result = self.read("probe", seconds=10)
            expected = {"schema": 1, "role": "codex", "nonce": probe["nonce"], "source_revision": prepared["source_revision"]}
            require(set(result) == {"schema", "status", "reply"} and result["reply"] == expected
                    and self.gateway.probe_observed("codex", probe["nonce"]), "model_transport_probe_unverified")
            self.state = "verified"
            self.proof = {"schema": 1, "status": "private_channel_verified", "run_id": self.checks.run.name,
                          "source_revision": prepared["source_revision"], "boot_id": baseline["boot_id"], "checksum": target["checksum"],
                          "bridge_ipv4": ready["bridge_ipv4"], "bridge_port": ready["bridge_port"],
                          "relay_source_sha256": hashlib.sha256((checkout / RELAY).read_bytes()).hexdigest(),
                          "probe_nonce": probe["nonce"], "native_routes": "unverified"}
            return self
        except BaseException:
            self.close(); raise

    def client(self, role):
        require(self.state == "verified" and self.healthy(), "model_transport_required")
        self.checks.ensure()
        return {**self.gateway.client(role), "base_url": "http://host.docker.internal:" + str(self.ready["bridge_port"]) + "/" + role}

    def close(self):
        if self.state == "closed": return
        require(self.state != "closed_unverified", "model_transport_shutdown_unverified")
        self.state = "stopping"
        if self.process is None: self.state = "closed"; return
        try:
            self.send({"stop": True})
        except PolicyError: pass
        try: self.process.stdin.close()
        except (OSError, ValueError): pass
        verified = False
        try:
            closed = self.read("closed", seconds=5)
            code = self.process.wait(timeout=3)
            verified = code == 0 and set(closed) == {"schema", "status", "reason", "listeners_closed", "connections_closed"} \
                and closed["reason"] == "controller_stop" and closed["listeners_closed"] is True and closed["connections_closed"] is True
        except (PolicyError, subprocess.TimeoutExpired): pass
        finally:
            if self.process.poll() is None:
                try: os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    self.process.wait(timeout=2)
            if self.pump is not None: self.pump.join(timeout=1)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try: stream.close()
                except (OSError, ValueError): pass
            self.shutdown_verified = verified and (self.pump is None or not self.pump.is_alive())
            self.state = "closed" if self.shutdown_verified else "closed_unverified"
        require(self.pump is None or not self.pump.is_alive(), "model_transport_shutdown_unverified")
        require(verified, "model_transport_shutdown_unverified")

    def __exit__(self, *args): self.close()
