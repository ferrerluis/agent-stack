#!/usr/bin/env python3
"""Exercise the rendered trial guard with real packets in disposable namespaces."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import re
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest


TEMPLATE = Path(__file__).resolve().parents[3] / "modules/common/templates/cloud_init.yaml.tpl"


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def endpoint():
    """Hold a second, isolated network namespace and accept controlled UDP work."""
    read_fd, write_fd = os.pipe()
    back_read, back_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd); os.close(back_read)
        os.unshare(os.CLONE_NEWNET)
        stream = os.fdopen(back_write, "w", buffering=1)
        stream.write(json.dumps({"ready": True}) + "\n")
        incoming = os.fdopen(read_fd)
        receiver = None
        for line in incoming:
            row = json.loads(line)
            if row["action"] == "listen":
                receiver = socket.socket(socket.AF_INET6 if ":" in row["address"] else socket.AF_INET, socket.SOCK_DGRAM)
                receiver.bind((row["address"], 19007)); receiver.settimeout(.3)
                result = True
            elif row["action"] == "send":
                with socket.socket(socket.AF_INET6 if ":" in row["address"] else socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    sender.sendto(bytes(row["bytes"]), (row["address"], 19007))
                result = True
            elif row["action"] == "listen_tcp":
                listener = socket.socket(socket.AF_INET6 if ":" in row["address"] else socket.AF_INET, socket.SOCK_STREAM)
                listener.bind((row["address"], 19008)); listener.listen()
                def serve(listener=listener):
                    while True:
                        client, _ = listener.accept()
                        with client:
                            if client.recv(4) == b"ping":
                                client.sendall(b"pong")
                threading.Thread(target=serve, daemon=True).start()
                result = True
            elif row["action"] == "connect":
                result = tcp_exchange(row["address"])
            else:
                try:
                    result = len(receiver.recv(4096))
                except socket.timeout:
                    result = None
            stream.write(json.dumps(result) + "\n")
        os._exit(0)
    os.close(read_fd); os.close(back_write)
    output = os.fdopen(back_read)
    assert json.loads(output.readline()) == {"ready": True}
    return pid, os.fdopen(write_fd, "w", buffering=1), output


def request(peer, **row):
    peer[1].write(json.dumps(row) + "\n")
    return json.loads(peer[2].readline())


def send(address: str, size: int, may_drop: bool = False):
    with socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_DGRAM) as sender:
        try:
            sender.sendto(bytes(size), (address, 19007))
        except PermissionError:
            # A locally generated packet dropped by POSTROUTING can report
            # EPERM directly. Forwarded drops instead disappear at the router.
            if not may_drop:
                raise


def tcp_exchange(address: str):
    with socket.create_connection((address, 19008), timeout=.75) as sender:
        sender.sendall(b"ping")
        return sender.recv(4).decode()


def network_fixture(script_path: Path, tcp: bool = False):
    # All interfaces, sysctls and firewall rules below belong to the unshared
    # namespace. No real host network, Docker daemon or cloud is involved.
    peers = []
    try:
        outside, container = endpoint(), endpoint(); peers = [outside, container]
        for name, child_name, peer, host, other in (
            ("eth0", "wan", outside, "192.0.2.1", "192.0.2.2"),
            ("docker0", "inside", container, "198.51.100.1", "198.51.100.2"),
        ):
            command("ip", "link", "add", name, "type", "veth", "peer", "name", child_name)
            command("ip", "link", "set", child_name, "netns", str(peer[0]))
            command("ip", "addr", "add", host + "/24", "dev", name)
            command("ip", "link", "set", name, "up")
            prefix = ("nsenter", "-t", str(peer[0]), "-n")
            command(*prefix, "ip", "addr", "add", other + "/24", "dev", child_name)
            command(*prefix, "ip", "link", "set", child_name, "up")
            command(*prefix, "ip", "link", "set", "lo", "up")
            command(*prefix, "ip", "route", "add", "default", "via", host)
            subnet = "1" if name == "eth0" else "2"
            command("ip", "-6", "addr", "add", "2001:db8:" + subnet + "::1/64", "dev", name, "nodad")
            command(*prefix, "ip", "-6", "addr", "add", "2001:db8:" + subnet + "::2/64", "dev", child_name, "nodad")
            remote_mac = json.loads(command(*prefix, "ip", "-j", "link", "show", child_name))[0]["address"]
            command("ip", "-6", "neigh", "add", "2001:db8:" + subnet + "::2", "lladdr", remote_mac, "dev", name, "nud", "permanent")
            local_mac = json.loads(command("ip", "-j", "link", "show", name))[0]["address"]
            command(*prefix, "ip", "-6", "neigh", "add", "2001:db8:" + subnet + "::1", "lladdr", local_mac, "dev", child_name, "nud", "permanent")
            command(*prefix, "ip", "-6", "route", "add", "default", "via", "2001:db8:" + subnet + "::1")
        command("ip", "link", "set", "lo", "up")
        command("ip", "route", "add", "default", "via", "192.0.2.2", "dev", "eth0")
        Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
        Path("/proc/sys/net/ipv6/conf/all/forwarding").write_text("1\n")
        # Static neighbours replace discovery in this controlled fabric. Keep
        # asynchronous multicast reports out of the tiny packet-count test.
        command("ip6tables", "-t", "raw", "-I", "OUTPUT", "1", "-d", "ff00::/8", "-j", "DROP")
        subprocess.run(["bash", str(script_path)], check=True, capture_output=True, timeout=5)
        # Real host and forwarded connections must work over either protocol.
        # Blocking IPv6 broke Docker Hub token requests on a dual-stack VM.
        if tcp:
            for address in ("192.0.2.2", "2001:db8:1::2"):
                assert request(outside, action="listen_tcp", address=address)
                assert tcp_exchange(address) == "pong"
                assert request(container, action="connect", address=address) == "pong"
            for firewall in ("iptables", "ip6tables"):
                counters = command(firewall, "-t", "mangle", "-L", "AS7_EGRESS", "-nvx")
                accepted = re.findall(r"^\s*([0-9]+)\s+([0-9]+)\s+ACCEPT\b", counters, re.MULTILINE)
                assert len(accepted) == 1 and int(accepted[0][0]) > 0 and 0 < int(accepted[0][1]) < 32768
            print(json.dumps({"status": "pass", "host_and_forwarded_tcp": "both_protocols", "total_quota_bytes": 65536}))
            return
        assert request(container, action="listen", address="198.51.100.2") is True
        assert request(outside, action="listen", address="192.0.2.2") is True
        # A forwarded download exceeds the entire outbound quota and must
        # still arrive. This reproduces the historical broad-hook failure.
        assert request(outside, action="send", address="198.51.100.2", bytes=2048) is True
        assert request(container, action="receive") == 2048, "inbound container download consumed outbound quota"
        # Each family receives half the 512-byte total. Host and forwarded
        # traffic share that family's allowance, despite earlier filter ACCEPTs.
        accepted_total = 0
        for firewall, external, internal, packet_bytes in (
            ("iptables", "192.0.2.2", "198.51.100.2", 92),
            ("ip6tables", "2001:db8:1::2", "2001:db8:2::2", 112),
        ):
            for chain in ("OUTPUT", "FORWARD"):
                command(firewall, "-I", chain, "1", "-j", "ACCEPT")
            request(outside, action="listen", address=external)
            request(container, action="listen", address=internal)
            request(outside, action="send", address=internal, bytes=2048)
            assert request(container, action="receive") == 2048, "inbound download consumed " + firewall + " quota"
            try:
                send(external, 64)
            except OSError:
                raise AssertionError(firewall + " host packet missing: " + command(firewall, "-t", "mangle", "-L", "AS7_EGRESS", "-nvx")) from None
            assert request(outside, action="receive") == 64
            request(container, action="send", address=external, bytes=64)
            assert request(outside, action="receive") == 64, firewall + " forwarded packet missing: " + command(firewall, "-t", "mangle", "-L", "AS7_EGRESS", "-nvx")
            for origin in ("host", "container"):
                if origin == "host":
                    send(external, 64, may_drop=True)
                else:
                    request(container, action="send", address=external, bytes=64)
                assert request(outside, action="receive") is None, origin + " bypassed " + firewall + " quota"
            send(internal, 64)
            assert request(container, action="receive") == 64, "internal traffic blocked after exhaustion"
            counters = command(firewall, "-t", "mangle", "-L", "AS7_EGRESS", "-nvx")
            accepted = re.findall(r"^\s*([0-9]+)\s+([0-9]+)\s+ACCEPT\b", counters, re.MULTILINE)
            assert len(accepted) == 1 and int(accepted[0][0]) == 2 and int(accepted[0][1]) == 2 * packet_bytes
            accepted_total += int(accepted[0][1])
        assert accepted_total == 408 and accepted_total <= 512
        print(json.dumps({"status": "pass", "inbound_container_bytes": 2048,
                          "shared_outbound_bytes": accepted_total, "quota_bytes": 512,
                          "host_and_forwarded_overflow": "dropped", "internal_after_overflow": "passed",
                          "external_ipv6": "metered", "ipv6_accept_bypass": "dropped",
                          "internal_ipv6": "passed"}))
    finally:
        for peer in peers:
            peer[1].close(); peer[2].close()
        for peer in peers:
            os.waitpid(peer[0], 0)


def boot_script(root: Path, template: str, quota: int = 512) -> Path:
    script = textwrap.dedent(template.split("bootcmd:\n  - |\n", 1)[1].split("%{ endif }", 1)[0])
    script = script.replace("/etc/systemd/system", str(root / "systemd"))
    script = script.replace("/var/lib/agent-stack-trial", str(root / "trial"))
    script = script.replace("${showcase_trial_limits.egress_cap_bytes}", str(quota))
    script = script.replace("${showcase_trial_limits.deadline_utc}", "2099-01-01 00:00:00 UTC")
    # A malformed guard may never shut down the real test host.
    shutdown = root / "shutdown"
    shutdown.write_text("#!/bin/sh\nexit 97\n"); shutdown.chmod(0o700)
    script = script.replace("/sbin/shutdown", str(shutdown))
    (root / "systemd").mkdir()
    (root / "bin").mkdir()
    systemctl = root / "bin/systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n"); systemctl.chmod(0o700)
    path = root / "boot.sh"
    path.write_text("export PATH=" + str(root / "bin") + ':"$PATH"\n' + script)
    return path


class TrialNetworkTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux" and hasattr(os, "unshare"), "Linux namespaces required")
    def test_downloads_do_not_consume_the_real_shared_outbound_quota(self):
        self.run_network_fixture()

    @unittest.skipUnless(sys.platform == "linux" and hasattr(os, "unshare"), "Linux namespaces required")
    def test_host_and_forwarded_tcp_work_over_both_metered_protocols(self):
        self.run_network_fixture(tcp=True)

    def run_network_fixture(self, tcp: bool = False):
        if not all(shutil.which(name) for name in ("unshare", "nsenter", "ip", "iptables", "ip6tables")):
            self.skipTest("namespace/firewall tools unavailable")
        probe = subprocess.run(["unshare", "-Urn", "true"], capture_output=True)
        if probe.returncode:
            self.skipTest("host prohibits disposable user/network namespaces")
        with tempfile.TemporaryDirectory() as directory:
            path = boot_script(Path(directory), TEMPLATE.read_text(), quota=65536 if tcp else 512)
            process = subprocess.Popen(["unshare", "-Urn", sys.executable, "-B", __file__, "--network-fixture", str(path), *( ["tcp"] if tcp else [] )],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                output, errors = process.communicate(timeout=15)
            finally:
                # Include forked endpoints when an assertion or command stalls.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            result = subprocess.CompletedProcess(process.args, process.returncode, output, errors)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "pass")

    def test_uncertain_external_interface_stops_before_installing_any_firewall(self):
        for routes in ("", "default via 192.0.2.1 dev eth0\ndefault via 192.0.2.3 dev eth1\n",
                       "default via 192.0.2.1 dev eth+\n", "default dev lo\n"):
            with self.subTest(routes=routes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); script = boot_script(root, TEMPLATE.read_text())
                ip = root / "bin/ip"
                ip.write_text("#!/bin/sh\ncat <<'ROUTES'\n" + routes + "ROUTES\n")
                ip.chmod(0o700)
                for name in ("iptables", "ip6tables"):
                    firewall = root / ("bin/" + name)
                    firewall.write_text("#!/bin/sh\ntouch " + str(root / "firewall-used") + "\n")
                    firewall.chmod(0o700)
                result = subprocess.run(["bash", str(script)], capture_output=True, timeout=2)
                self.assertEqual(result.returncode, 97)
                self.assertFalse((root / "firewall-used").exists())


if __name__ == "__main__":
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--network-fixture":
        network_fixture(Path(sys.argv[2]), tcp=len(sys.argv) == 4)
    else:
        unittest.main()
