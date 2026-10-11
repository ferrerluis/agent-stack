import copy
from http.client import HTTPConnection, IncompleteRead
import json
import os
from pathlib import Path
import pwd
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_model_transport as transport
import showcase_model_relay as relay
import showcase_model_gateway as gateway
import showcase_model_spend as spend
from showcase_policy import PolicyError, Reservations
from showcase_attempt import verify_prepared, KEY_PATH
from test_showcase_policy import config, quote, NOW
import test_showcase_attempt as preparation
import test_showcase_model_spend as terms
from test_showcase_model_gateway import Response, body, PRIVATE, SSE_FIRST, SSE_DONE

ROOT = Path(__file__).resolve().parents[2]
RUN = "as7-fixture01"
BOOT = "11111111-1111-1111-1111-111111111111"
CHECKSUM = "a" * 64


class RelayContractTest(unittest.TestCase):
    def policy_raw(self):
        return b'gatewayports no\nallowtcpforwarding remote\ndisableforwarding no\npubkeyauthentication yes\npermitlisten any\nusedns no\nhostkey fixture-one\nhostkey fixture-two\n'

    def test_effective_ssh_match_policy_is_read_before_forwarding_and_public_binds_are_refused(self):
        connection = {"SSH_CONNECTION": "203.0.113.42 50123 10.0.0.1 22"}
        with patch.dict(os.environ, connection), patch.object(relay.subprocess, "run", return_value=SimpleNamespace(stdout=self.policy_raw())) as run:
            self.assertEqual(relay.server_policy()["gateway_ports"], "no")
            self.assertEqual(run.call_args.args[0][:5], ["sudo", "-n", "/usr/sbin/sshd", "-T", "-C"])
            self.assertIn("host=203.0.113.42,addr=203.0.113.42,laddr=10.0.0.1,lport=22", run.call_args.args[0][5])
        invalid = [self.policy_raw().replace(before, after) for before, after in (
            (b'gatewayports no', b'gatewayports yes'), (b'gatewayports no', b'gatewayports clientspecified'),
            (b'disableforwarding no', b'disableforwarding yes'), (b'allowtcpforwarding remote', b'allowtcpforwarding local'),
            (b'permitlisten any', b'permitlisten none'), (b'pubkeyauthentication yes', b'pubkeyauthentication no'),
            (b'usedns no', b'usedns yes'))]
        invalid += [self.policy_raw() + b'gatewayports no\n', b'x' * 65537]
        for raw in invalid:
            with patch.dict(os.environ, connection), patch.object(relay.subprocess, "run", return_value=SimpleNamespace(stdout=raw)), self.assertRaises(ValueError):
                relay.server_policy()
    def network(self):
        return [{"Name": "bridge", "Driver": "bridge", "Internal": False,
                 "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}}]

    def test_bridge_discovery_has_one_actual_private_ipv4_and_no_endpoint_override(self):
        rows = self.network()
        with patch.object(relay.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(rows).encode())) as run:
            self.assertEqual(relay.bridge_address(), "172.17.0.1")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/docker", "network", "inspect", "bridge"])
        invalid = []
        for changed in ({"Name": "foreign"}, {"Driver": "overlay"}, {"Internal": True},
                        {"IPAM": {"Config": []}}, {"IPAM": {"Config": rows[0]["IPAM"]["Config"] * 2}},
                        {"IPAM": {"Config": [{"Subnet": "8.8.8.0/24", "Gateway": "8.8.8.8"}]}},
                        {"IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.18.0.1"}]}},
                        {"IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.0"}]}}):
            invalid.append(json.dumps([rows[0] | changed]).encode())
        invalid += [b'[]', b'[NaN]', b'[{"Name":"bridge","Name":"foreign"}]', b'x' * 65537]
        for raw in invalid:
            with patch.object(relay.subprocess, "run", return_value=SimpleNamespace(stdout=raw)), self.assertRaises((ValueError, AttributeError, TypeError)):
                relay.bridge_address()

    def packet(self):
        return {"run_id": RUN, "boot_id": BOOT, "checksum": CHECKSUM, "forward_port": 12345,
                "sent_at": NOW, "work_deadline": NOW + 100}

    def owned(self, packet, listener="127.0.0.1:12345"):
        applied = {"status": "success", "checksum": CHECKSUM, "services": ["hermes", "openclaw", "workspace"]}
        with patch.object(Path, "read_text", return_value=BOOT), patch.object(Path, "read_bytes", return_value=json.dumps(applied).encode()), \
             patch.object(relay.subprocess, "run", return_value=SimpleNamespace(stdout=("LISTEN 0 128 " + listener + " 0.0.0.0:*\n").encode())):
            return relay.owned_binding(packet, now=lambda: NOW)

    def test_original_boot_checksum_clock_deadline_and_loopback_forward_are_required(self):
        self.assertEqual(self.owned(self.packet()), 95)
        for changed in ({"extra": True}, {"run_id": "foreign"}, {"boot_id": "2" * 36}, {"checksum": "b" * 64},
                        {"forward_port": True}, {"forward_port": 65536}, {"sent_at": NOW - 6}, {"sent_at": float("nan")},
                        {"work_deadline": NOW + 10}, {"work_deadline": NOW + 14401}):
            with self.subTest(fields=list(changed)), self.assertRaises(ValueError): self.owned(self.packet() | changed)
        for address in ("0.0.0.0:12345", "172.17.0.1:12345", "[::]:12345", "127.0.0.1:11111"):
            with self.assertRaises(ValueError): self.owned(self.packet(), address)

    def test_duplicate_nonfinite_malformed_control_and_public_addresses_are_refused(self):
        for raw in (b'[]', b'null', b'{"a":1,"a":2}', b'{"a":NaN}', b'x' * 8193):
            with self.assertRaises(ValueError): relay.decoded(raw)
        for address in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "169.254.1.2", "::1", "100.64.0.1", None):
            with self.assertRaises(PolicyError): transport.private_ipv4(address)
        for address in ("10.0.0.1", "172.17.0.1", "192.168.1.1"):
            self.assertEqual(transport.private_ipv4(address), address)

    def test_control_reader_bounds_partial_frames_and_observes_eof_without_blocking(self):
        reader_fd, writer_fd = os.pipe()
        self.addCleanup(os.close, reader_fd); self.addCleanup(os.close, writer_fd)
        reader = relay.ControlReader(reader_fd)
        self.assertIsNone(reader.packet(.01))
        os.write(writer_fd, b'{"a":1}\n{"b":2}\n')
        self.assertEqual(reader.packet(), {"a": 1}); self.assertEqual(reader.packet(), {"b": 2})
        os.write(writer_fd, b'{"partial":')
        self.assertIsNone(reader.packet(.01)); reader.started = time.monotonic() - 6
        with self.assertRaises(ValueError): reader.packet(.01)
        eof_reader, eof_writer = os.pipe(); self.addCleanup(os.close, eof_reader); os.close(eof_writer)
        with self.assertRaises(EOFError): relay.ControlReader(eof_reader).packet(.1)

    def test_invalid_lifetimes_or_ports_create_no_listener(self):
        for port, life in ((0, 20), (65536, 20), (True, 20), (1234, 0), (1234, 14400), (1234, float("inf"))):
            with patch.object(relay.socket, "socket") as make, self.assertRaises(ValueError): relay.PrivateRelay("127.0.0.1", port, life)
            make.assert_not_called()


class RelaySocketTest(unittest.TestCase):
    def setUp(self):
        self.upstream = socket.socket(); self.upstream.bind(("127.0.0.1", 0)); self.upstream.listen(8); self.upstream.settimeout(3)
        self.addCleanup(self.upstream.close)
        self.relay = relay.PrivateRelay("127.0.0.1", self.upstream.getsockname()[1], 20)
        self.addCleanup(self.relay.close)

    def pair(self):
        client = socket.create_connection(("127.0.0.1", self.relay.port), timeout=3)
        server, _ = self.upstream.accept(); server.settimeout(3)
        self.addCleanup(client.close); self.addCleanup(server.close)
        return client, server

    def test_actual_full_duplex_streaming_and_half_close_preserve_bytes(self):
        client, server = self.pair()
        client.sendall(b'fixture request'); self.assertEqual(server.recv(100), b'fixture request')
        server.sendall(SSE_FIRST); self.assertEqual(client.recv(1000), SSE_FIRST)
        client.shutdown(socket.SHUT_WR); self.assertEqual(server.recv(100), b'')
        server.sendall(SSE_DONE); self.assertEqual(client.recv(1000), SSE_DONE)
        server.shutdown(socket.SHUT_WR); self.assertEqual(client.recv(100), b'')

    def test_connections_buffers_and_total_forwarded_bytes_are_bounded(self):
        pairs = [self.pair() for _ in range(4)]
        with socket.create_connection(("127.0.0.1", self.relay.port), timeout=3) as extra:
            self.assertEqual(extra.recv(10), b'')
        self.assertEqual(len(self.relay.connections), 4)
        with patch.object(relay, "MAX_BYTES", 8):
            client, server = pairs[0]; client.sendall(b'x' * 9)
            self.assertEqual(server.recv(20), b'')
        self.assertEqual(relay.MAX_BUFFER, 65536)

    def test_deadline_closes_existing_connections_and_listener_cleanup_is_verified(self):
        client, server = self.pair(); self.relay.deadline = time.monotonic() - 1
        end = time.monotonic() + 2
        while self.relay.healthy() and time.monotonic() < end: time.sleep(.01)
        self.assertEqual(client.recv(100), b''); self.assertEqual(server.recv(100), b'')
        self.relay.close()
        self.assertFalse(self.relay.thread.is_alive()); self.assertFalse(self.relay.connections); self.assertFalse(self.relay.workers)
        with self.assertRaises(OSError): socket.create_connection(("127.0.0.1", self.relay.port), timeout=.1)

    def test_stop_closes_all_sessions_and_refuses_new_connections(self):
        pairs = [self.pair() for _ in range(2)]
        self.relay.close()
        for client, server in pairs:
            self.assertEqual(client.recv(10), b''); self.assertEqual(server.recv(10), b'')
        with self.assertRaises(OSError): socket.create_connection(("127.0.0.1", self.relay.port), timeout=.1)


class NativeSSHTransportTest(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation.PreparationTest("runTest"); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        for name in (transport.RELAY, "bin/agent-stack-ssh"):
            target = self.fixture.repo / name; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes()); target.chmod(0o755)
        self.fixture.git("add", "."); self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "native channel fixture")
        self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip(); self.fixture.prepare()
        self.config = config(); self.run = self.fixture.batch / "attempts" / RUN; self.checkout = self.run / "repo"
        self.ledger = Reservations(self.fixture.batch, self.config)
        self.active = self.ledger.reserve("aws", RUN, quote(), now=NOW)
        policy = terms.policy(self.config); policy["source_revision"] = self.fixture.revision
        self.reader = Mock(return_value=PRIVATE); self.opener = SimpleNamespace(open=Mock(side_effect=lambda *a, **k: Response()))
        model = spend.ModelSpend(self.ledger, RUN, "aws", self.fixture.revision, policy, now=lambda: NOW)
        self.gateway = gateway.BudgetGateway(model, self.reader, self.opener); self.gateway.__enter__(); self.addCleanup(self.gateway.close)
        self.native = self.fixture.root / "native"; self.native.mkdir(mode=0o700)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.native / "host")], check=True)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0)); self.port = reservation.getsockname()[1]
        user = pwd.getpwuid(os.geteuid()).pw_name
        # Only ownership, Docker interface and wall clock are fixtures. This
        # runs the real relay validation/data/lifecycle through real SSH.
        source = '''import sys,json,os,shlex,subprocess
sys.dont_write_bytecode=True
from pathlib import Path
sys.path.insert(0, ROOT)
import showcase_model_relay as relay
if shlex.split(os.environ['SSH_ORIGINAL_COMMAND'])[-1]=='--check-forwarding':
    original_run=relay.subprocess.run
    def policy_run(args,**kwargs):
        assert args[:5]==['sudo','-n','/usr/sbin/sshd','-T','-C']
        return original_run(['/usr/sbin/sshd','-T','-f',CONFIG,'-C',args[5]],**kwargs)
    relay.subprocess.run=policy_run
    relay.emit(relay.server_policy()); sys.exit(0)
original_text,original_bytes=Path.read_text,Path.read_bytes
Path.read_text=lambda path,*a,**k: BOOT if str(path)=='/proc/sys/kernel/random/boot_id' else original_text(path,*a,**k)
Path.read_bytes=lambda path,*a,**k: json.dumps({'status':'success','checksum':CHECKSUM,'services':['hermes','openclaw','workspace']}).encode() if str(path)=='/opt/agent-stack/.last-apply.json' else original_bytes(path,*a,**k)
original=relay.owned_binding
relay.owned_binding=lambda packet: original(packet,now=lambda:packet['sent_at'])
relay.bridge_address=lambda:'127.0.0.1'
sys.exit(relay.main())
'''.replace("ROOT", repr(str(ROOT / "scripts"))).replace("BOOT", repr(BOOT)).replace("CHECKSUM", repr(CHECKSUM)).replace("CONFIG", repr(str(self.native / "sshd.conf")))
        (self.native / "remote.py").write_text(source)
        self.force = shlex.join([sys.executable, "-B", str(self.native / "remote.py")])
        cfg = f'ListenAddress 127.0.0.1\nPort {self.port}\nHostKey {self.native}/host\nPidFile {self.native}/pid\nAuthorizedKeysFile {self.checkout}/{KEY_PATH}.pub\nAllowUsers {user}\nUsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nAuthenticationMethods publickey\nStrictModes no\nPermitUserEnvironment no\nAllowTcpForwarding remote\nGatewayPorts no\nLogLevel ERROR\nForceCommand {self.force}\n'
        (self.native / "sshd.conf").write_text(cfg)
        public = (self.native / "host.pub").read_text().split()
        self.hosts = self.checkout / ".ssh/known_hosts"
        self.hosts.write_text(f'[127.0.0.1]:{self.port} {public[0]} {public[1]}\n'); self.hosts.chmod(0o600)
        self.server = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(self.native / "sshd.conf")],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_server)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            self.assertIsNone(self.server.poll(), "fixture SSH server stopped")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.1): break
            except OSError: time.sleep(.02)
        shim = self.native / "ssh"; shim.write_text('#!/bin/sh\nexec /usr/bin/ssh -p ' + str(self.port) + ' "$@"\n'); shim.chmod(0o700)
        self.env = patch.dict(os.environ, {"PATH": str(self.native) + os.pathsep + os.environ["PATH"]}); self.env.start(); self.addCleanup(self.env.stop)
        self.ip = patch.object(transport, "private_ipv4", side_effect=lambda value: value if value == "127.0.0.1" else transport.require(False, "fixture_bridge_invalid"))
        self.ip.start(); self.addCleanup(self.ip.stop)
        self.checks = SimpleNamespace(run=self.run, config=self.config, provider="aws", ledger=self.ledger, now=lambda: NOW,
            attempt=SimpleNamespace(checkout=self.checkout),
            ensure=lambda: (verify_prepared(self.run, self.config, "aws"), self.active),
            evidence=lambda: {"checks": {"launch_ssh": "pass"}, "baseline": {"checksum": CHECKSUM, "boot_id": BOOT}},
            require_container_gate=Mock(), bind=lambda: {"host": user + "@127.0.0.1", "checksum": CHECKSUM})

    def stop_server(self):
        self.server.terminate()
        try: self.server.wait(timeout=3)
        except subprocess.TimeoutExpired: self.server.kill(); self.server.wait(timeout=3)

    def channel(self):
        value = transport.SSHModelTransport(self.checks, self.gateway); value.__enter__(); self.addCleanup(self.close_channel, value); return value

    def close_channel(self, value):
        if value.state == "closed_unverified":
            self.assertIsNotNone(value.process.poll()); self.assertFalse(value.pump.is_alive())
        else: value.close()

    def account(self): return json.loads((self.fixture.batch / "reservations.json").read_text())["attempts"][0]

    def test_native_dynamic_reverse_forward_nonce_is_independently_seen_without_model_spend(self):
        value = self.channel()
        self.assertEqual(value.proof["status"], "private_channel_verified")
        self.assertEqual(value.proof["native_routes"], "unverified")
        self.assertTrue(self.gateway.probe_observed("codex", value.proof["probe_nonce"]))
        self.reader.assert_not_called(); self.opener.open.assert_not_called(); self.assertEqual(self.account()["model_requests"], [])
        self.assertTrue(value.client("codex")["base_url"].startswith("http://host.docker.internal:"))
        self.assertNotIn(PRIVATE, json.dumps(value.proof))
        port = value.ready["bridge_port"]; forward = value.allocated
        value.close(); self.assertTrue(value.shutdown_verified); self.assertFalse(self.gateway.healthy())
        for closed in (port, forward):
            with self.assertRaises(OSError): socket.create_connection(("127.0.0.1", closed), timeout=.1)
        self.assertFalse(value.pump.is_alive()); self.assertIsNotNone(value.process.poll())
        with self.assertRaises(PolicyError): value.client("codex")
        with self.assertRaises(PolicyError): value.__enter__()

    def test_native_ssh_streaming_remains_incremental_and_records_original_full_hold(self):
        value = self.channel(); allow, waiting = threading.Event(), threading.Event()
        def later():
            waiting.set(); self.assertTrue(allow.wait(3)); return SSE_DONE
        self.opener.open.side_effect = lambda *a, **k: Response([SSE_FIRST, later], "text/event-stream")
        client = HTTPConnection("127.0.0.1", value.ready["bridge_port"], timeout=3)
        try:
            token = value.client("codex")["credential"]
            client.request("POST", "/codex/v1/responses", body(), {"Content-Type": "application/json", "Authorization": "Bearer " + token})
            response = client.getresponse(); self.assertEqual(response.read1(len(SSE_FIRST)), SSE_FIRST)
            self.assertFalse(allow.is_set()); self.assertTrue(waiting.wait(1)); allow.set()
            self.assertEqual(response.read(), SSE_DONE)
        finally: allow.set(); client.close()
        rows = self.account()["model_requests"]; self.assertEqual(len(rows), 1); self.assertEqual(rows[0]["status"], "response_received")
        self.assertNotIn(PRIVATE, json.dumps(rows)); self.reader.assert_called_once()

    def test_lost_native_ssh_session_revokes_gateway_and_does_not_refund_held_work(self):
        value = self.channel()
        client = HTTPConnection("127.0.0.1", value.ready["bridge_port"], timeout=3)
        try:
            client.request("POST", "/codex/v1/responses", body(), {"Content-Type": "application/json",
                           "Authorization": "Bearer " + value.client("codex")["credential"]})
            response = client.getresponse(); self.assertEqual(response.status, 200); response.read()
        finally: client.close()
        before = self.account()["model_requests"]; self.assertEqual(len(before), 1)
        value.process.terminate(); value.process.wait(timeout=3)
        end = time.monotonic() + 2
        while value.pump.is_alive() and time.monotonic() < end: time.sleep(.02)
        self.assertFalse(self.gateway.healthy())
        with self.assertRaises(PolicyError): value.client("codex")
        with self.assertRaises(PolicyError): value.close()
        self.assertFalse(value.shutdown_verified)
        self.reader.assert_called_once(); self.opener.open.assert_called_once()
        self.assertEqual(self.account()["model_requests"], before)

    def test_missing_pin_or_changed_scope_never_launches_ssh_or_reads_a_model_key(self):
        for mutation in ("pin", "scope"):
            before = self.hosts.read_bytes()
            value = transport.SSHModelTransport(self.checks, self.gateway, popen=Mock())
            if mutation == "pin": self.hosts.write_bytes(b'')
            else: self.checks.provider = "gcp"
            with self.assertRaises(PolicyError): value.__enter__()
            value.popen.assert_not_called(); self.reader.assert_not_called()
            self.hosts.write_bytes(before); self.checks.provider = "aws"

    def test_unknown_native_host_key_fails_without_adding_a_pin_or_spending(self):
        original = self.hosts.read_bytes(); self.hosts.write_text('fixture.invalid ssh-ed25519 AAAA\n')
        value = transport.SSHModelTransport(self.checks, self.gateway)
        with self.assertRaises(PolicyError): value.__enter__()
        self.assertEqual(self.hosts.read_text(), 'fixture.invalid ssh-ed25519 AAAA\n')
        self.assertFalse(self.gateway.healthy()); self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.hosts.write_bytes(original)

    def test_independent_gateway_nonce_observation_is_required_before_channel_acceptance(self):
        value = transport.SSHModelTransport(self.checks, self.gateway)
        with patch.object(self.gateway, "probe_observed", return_value=False), self.assertRaisesRegex(PolicyError, "model_transport_probe_unverified"):
            value.__enter__()
        self.assertIsNone(value.proof); self.assertTrue(value.shutdown_verified)
        self.assertFalse(self.gateway.healthy()); self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_original_work_deadline_revokes_a_running_native_channel(self):
        value = self.channel()
        self.checks.now = lambda: self.active["work_deadline"]
        self.assertFalse(self.gateway.healthy())
        end = time.monotonic() + 3
        while value.process.poll() is None and time.monotonic() < end: time.sleep(.02)
        self.assertIsNotNone(value.process.poll())
        with self.assertRaises(PolicyError): value.close()
        self.assertFalse(value.shutdown_verified)
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_oversized_remote_output_is_bounded_redacted_and_cannot_authenticate_a_channel(self):
        policy = {"schema": 1, "status": "forwarding_policy_verified", "gateway_ports": "no", "allow_tcp_forwarding": "remote", "disable_forwarding": "no"}
        (self.native / "remote.py").write_text('import os,shlex,json\nif shlex.split(os.environ["SSH_ORIGINAL_COMMAND"])[-1]=="--check-forwarding":\n print(' + repr(json.dumps(policy)) + ',flush=True)\nelse:\n print(' + repr(PRIVATE * 500) + ',flush=True)\n')
        value = transport.SSHModelTransport(self.checks, self.gateway)
        with self.assertRaises(PolicyError) as error: value.__enter__()
        self.assertNotIn(PRIVATE, str(error.exception)); self.assertIsNone(value.proof)
        self.assertIsNotNone(value.process.poll()); self.assertFalse(value.pump.is_alive())
        self.assertFalse(self.gateway.healthy()); self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_refused_effective_server_policy_starts_no_forward_or_paid_request(self):
        bad = {"schema": 1, "status": "forwarding_policy_verified", "gateway_ports": "yes", "allow_tcp_forwarding": "remote", "disable_forwarding": "no"}
        popen = Mock(); execute = Mock(return_value=json.dumps(bad).encode())
        value = transport.SSHModelTransport(self.checks, self.gateway, popen=popen, execute=execute)
        with self.assertRaisesRegex(PolicyError, "model_forwarding_policy_unverified"): value.__enter__()
        popen.assert_not_called(); self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.assertIn("--pinned", execute.call_args.args[0]); self.assertNotIn("--model-forward", execute.call_args.args[0])


class SSHWrapperContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "key"; self.key.write_text('fixture-not-a-real-key'); self.key.chmod(0o600)
        self.hosts = self.root / "pins"; self.hosts.write_text('fixture.invalid ssh-ed25519 AAAA\n'); self.hosts.chmod(0o600)
        binary = self.root / "ssh"
        binary.write_text('#!/bin/sh\nexec /usr/bin/ssh -G "$@"\n'); binary.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"])

    def invoke(self, options):
        return subprocess.run([str(ROOT / "bin/agent-stack-ssh"), "--key", str(self.key), *options],
                              env=self.env, capture_output=True, timeout=3)

    def required(self): return ["--batch", "--host", "user@127.0.0.1", "--known-hosts", str(self.hosts)]

    def test_native_config_pins_auth_and_only_loopback_forward_with_correct_option_precedence(self):
        result = self.invoke(self.required() + ["--model-forward", "49152"])
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        config = {}
        for line in result.stdout.decode().splitlines():
            key, _, value = line.partition(" "); config.setdefault(key, []).append(value)
        for key in ("stricthostkeychecking", "batchmode", "exitonforwardfailure"):
            self.assertIn(config[key][0], ("yes", "true"))
        self.assertEqual(config["identityagent"], ["none"])
        self.assertEqual(config["serveraliveinterval"], ["5"]); self.assertEqual(config["serveralivecountmax"], ["2"])
        self.assertEqual(config["globalknownhostsfile"], ["/dev/null"])
        self.assertEqual(config["forwardagent"], ["no"]); self.assertEqual(config["forwardx11"], ["no"])
        self.assertEqual(config["remoteforward"], ["[127.0.0.1]:0 [127.0.0.1]:49152"])

    def test_invalid_arbitrary_public_or_duplicate_forward_input_is_refused(self):
        for port in ("0", "65536", "-1", "01", "host:1234", "0.0.0.0:1234", "1234:foreign:5678", "private fixture"):
            result = self.invoke(self.required() + ["--model-forward", port]); self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(port, result.stderr.decode())
        self.assertNotEqual(self.invoke(self.required() + ["--model-forward", "1234", "--model-forward", "5678"]).returncode, 0)

    def test_forwarding_requires_explicit_batch_target_and_preexisting_host_pin(self):
        for options in (["--batch", "--model-forward", "1234"],
                        ["--host", "user@127.0.0.1", "--known-hosts", str(self.hosts), "--model-forward", "1234"],
                        ["--batch", "--host", "user@127.0.0.1", "--model-forward", "1234"]):
            self.assertNotEqual(self.invoke(options).returncode, 0)
        self.hosts.write_text('')
        self.assertNotEqual(self.invoke(self.required() + ["--model-forward", "1234"]).returncode, 0)

    def test_pinned_paths_with_spaces_work_and_ssh_expansion_paths_are_refused(self):
        original = self.hosts
        self.hosts = self.root / "pins with spaces"; self.hosts.write_text(original.read_text())
        result = self.invoke(self.required() + ["--pinned"])
        self.assertEqual(result.returncode, 0)
        self.assertIn(str(self.hosts), result.stdout.decode())
        self.assertNotIn('remoteforward', result.stdout.decode())
        for name in ('pins%h', 'pins$USER', 'pins~', 'pins"quoted', 'pins\\escaped'):
            self.hosts = self.root / name; self.hosts.write_text(original.read_text())
            result = self.invoke(self.required() + ["--pinned"])
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(str(self.hosts), result.stderr.decode())
