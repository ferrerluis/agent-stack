import base64
import copy
from http.client import HTTPConnection
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_runtime_routes as routes
import showcase_runtime_probe as probe
import showcase_runtime_environment as environment
import showcase_runtime_lifecycle as lifecycle
import showcase_model_session as session
import showcase_cleanup as cleanup
from showcase_policy import PolicyError
import test_showcase_runtime_routes as route_fixtures
import test_showcase_model_session as model_fixtures
import test_showcase_remote as remote_fixtures


class RuntimeDisarmRegressionTest(unittest.TestCase):
    def test_actual_disarm_does_not_stop_a_collected_transient_service(self):
        active = True
        calls = []
        def execute(args, timeout=10):
            nonlocal active
            calls.append(args)
            if args[:2] == ["systemctl", "show"]:
                return b"active\n" if active and args[-1].endswith('.timer') else b"inactive\n"
            if args[:2] == ["systemctl", "stop"]:
                active = False
                if len(args) != 3 or args[-1].endswith('.service'):
                    raise lifecycle.agents.ProbeError('probe_command_failed')
                return b""
            raise AssertionError('unexpected manager command')
        vm = object.__new__(lifecycle.RuntimeLifecycle)
        vm.execute = execute
        vm.disarm({'unit': 'as7-route-fixture'})
        self.assertFalse(active)
        self.assertEqual([args for args in calls if 'stop' in args], [['systemctl', 'stop', 'as7-route-fixture.timer']])

    def test_first_command_failure_survives_containment_without_raw_arguments_or_output(self):
        vm = object.__new__(lifecycle.RuntimeLifecycle)
        vm.phase, vm.failure = 'watchdog_disarm', None
        vm.raw_execute = Mock(side_effect=[lifecycle.agents.ProbeError('probe_command_failed'), RuntimeError('fixture-private-secret')])
        with self.assertRaises(lifecycle.agents.ProbeError):
            vm.command(['systemctl', 'stop', 'as7-route-fixture.timer'])
        vm.phase = 'runtime_stop'
        with self.assertRaises(RuntimeError):
            vm.command(['/usr/bin/docker', 'stop', 'fixture-private-secret'], input=b'fixture-private-secret')
        self.assertEqual(vm.failure, {'phase': 'watchdog_disarm', 'reason': 'probe_command_failed',
                                     'command': {'program': 'systemctl', 'operation': 'stop'}})
        self.assertNotIn('fixture-private-secret', json.dumps(vm.failure))

    def test_unknown_exception_and_command_arguments_are_redacted(self):
        vm = object.__new__(lifecycle.RuntimeLifecycle)
        vm.phase, vm.failure = 'native_route', None
        vm.raw_execute = Mock(side_effect=RuntimeError('fixture-private-secret'))
        with self.assertRaises(RuntimeError):
            vm.command(['/usr/bin/docker', 'exec', 'fixture-private-secret'], input=b'fixture-private-secret')
        self.assertEqual(vm.failure, {'phase': 'native_route', 'reason': 'runtime_lifecycle_unverified',
                                     'command': {'program': 'docker', 'operation': 'exec'}})
        self.assertNotIn('fixture-private-secret', json.dumps(vm.failure))
import test_showcase_agents as agent_fixtures

ROOT = Path(__file__).resolve().parents[2]
CAPABILITY = route_fixtures.CAPABILITY
CONTAINER = "c" * 64
IMAGE_ID = "sha256:" + "d" * 64
HERMES_DOCTOR = ("◆ Python Environment\n✓ valid\n◆ Required Packages\n✓ valid\n◆ Configuration Files\n✓ valid\n"
    "◆ Directory Structure\n✓ valid\n◆ API Connectivity\n✓ selected route\nAll checks passed!\n").encode()


def image(role):
    return ("ghcr.io/openclaw/openclaw" if role == "openclaw" else "ghcr.io/nousresearch/hermes-agent") + "@sha256:" + "e" * 64


def snapshot(route):
    config = routes.compile_config({}, route)
    if route["role"] == "openclaw":
        config["agents"]["defaults"].update(maxConcurrent=8, compaction={"mode": "safeguard"},
                                          subagents={"maxConcurrent": 8, "archiveAfterMinutes": 60})
    revision = "hmac-sha256:v1:" + base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")
    return {"valid": True, "configRevisionHash": revision, "appliedConfigHash": revision,
            "config": config, "issues": [], "warnings": [], "legacyIssues": []}


def resolver_result(route):
    return {"provider": "custom", "model": route["model"], "requested_provider": "as7-budget", "source": "custom_provider:as7-budget",
            "api_mode": "chat_completions" if route["provider"] == "openai" else "anthropic_messages",
            "base_url": route["base_url"] + ("/v1" if route["provider"] == "openai" else ""), "api_key": route["credential"]}


def native(route):
    if route["role"] == "openclaw": return probe.native_openclaw(snapshot(route), route)
    return probe.native_hermes(routes.compile_config({}, route), route, lambda **_: resolver_result(route))


class NativeProbeTest(unittest.TestCase):
    def parse_native_yaml(self, raw):
        # Exercise the actual native parser with the host's existing system
        # PyYAML. The controller/test venv does not gain a YAML dependency.
        code = "import json,sys\nns={'__name__':'fixture'}\nexec(" + repr(probe.HERMES_CONFIG_SOURCE) + ",ns)\n"
        code += "try:print(json.dumps({'ok':True,'config':ns['read_configuration'](sys.stdin.buffer.read())}))\nexcept Exception:print(json.dumps({'ok':False}))\n"
        run = subprocess.run(['/usr/bin/python3', '-I', '-c', code], input=raw, capture_output=True, timeout=5, check=True)
        return json.loads(run.stdout)

    def test_native_yaml_parser_accepts_migrated_plain_data_and_json(self):
        expected = {'_config_version': 30, 'model': {'default': 'fixture-model', 'provider': 'anthropic'}, 'api_server': {'enabled': True}}
        raw = b'_config_version: 30\nmodel:\n  default: fixture-model\n  provider: anthropic\napi_server:\n  enabled: true\n'
        self.assertEqual(self.parse_native_yaml(raw), {'ok': True, 'config': expected})
        self.assertEqual(self.parse_native_yaml(routes.encoded(expected)), {'ok': True, 'config': expected})

    def test_native_yaml_parser_refuses_aliases_duplicates_objects_nonfinite_and_non_json_types(self):
        for raw in (b'a: 1\na: 2\n', b'a: {b: 1, b: 2}\n', b'a: &anchor {b: 1}\nc: *anchor\n',
                    b'a: !!python/object:os.system {}\n', b'a: .nan\n', b'a: 2026-10-08\n',
                    b'1: value\n', b'- value\n', b'a: [' * 60 + b'x' + b']' * 60,
                    b'a: ' + b'x' * routes.LIMIT):
            with self.subTest(length=len(raw)): self.assertEqual(self.parse_native_yaml(raw), {'ok': False})

    def test_readiness_bootstrap_keeps_only_native_application_auth_and_fixed_environment(self):
        body = "import json,os;print(json.dumps({'names':sorted(os.environ),'key_matches':os.environ.get('API_SERVER_KEY')=='fixture-key'}))"
        run = subprocess.run([sys.executable, '-I', '-c', probe.hermes_readiness_source(body)],
            env={'API_SERVER_KEY': 'fixture-key', 'ANTHROPIC_API_KEY': 'fixture-provider', 'HOME': '/foreign', 'HTTPS_PROXY': 'https://foreign.invalid'},
            capture_output=True, timeout=5, check=True)
        self.assertEqual(json.loads(run.stdout), {'names': ['API_SERVER_KEY', 'HERMES_HOME', 'HOME', 'PATH'], 'key_matches': True})

    def test_revision_tokens_reject_file_hashes_bad_versions_padding_noncanonical_or_stale_values(self):
        route = route_fixtures.route()
        for bad in (None, "b" * 64, "hmac-sha256:v2:" + "A" * 43,
                    "hmac-sha256:v1:" + "A" * 43 + "=", "hmac-sha256:v1:" + "A" * 42 + "B",
                    "hmac-sha256:v1:" + "A" * 42, "hmac-sha256:v1:" + "A" * 43 + "\n"):
            value = snapshot(route); value.update(configRevisionHash=bad, appliedConfigHash=bad)
            with self.assertRaises(probe.OpenClawNativeError) as error:
                probe.native_openclaw(value, route)
            self.assertEqual(str(error.exception), "openclaw_config_revision_invalid")
            report = native(route); report["applied_config_hash"] = bad
            with self.assertRaises(ValueError): probe.native_proof_valid(route, report)
        value = snapshot(route); value["appliedConfigHash"] = "hmac-sha256:v1:" + "A" * 43
        self.assertTrue(probe.openclaw_revision_valid(value["appliedConfigHash"]))
        with self.assertRaises(probe.OpenClawNativeError) as error: probe.native_openclaw(value, route)
        self.assertEqual(str(error.exception), "openclaw_applied_revision_unverified")
        value = snapshot(route); value["appliedConfigHash"] = None
        with self.assertRaises(probe.OpenClawNativeError) as error: probe.native_openclaw(value, route)
        self.assertEqual(str(error.exception), "openclaw_applied_revision_invalid")

    def test_pinned_gateway_hmac_revisions_bind_the_exact_applied_revision(self):
        route = route_fixtures.route()
        value = snapshot(route)
        token = "hmac-sha256:v1:" + base64.urlsafe_b64encode(hashlib.sha256(b"native-fixture").digest()).decode().rstrip("=")
        value.update(configRevisionHash=token, appliedConfigHash=token)
        result = probe.native_openclaw(value, route)
        probe.native_proof_valid(route, result)
        self.assertEqual(result["applied_config_hash"], token)

    def test_openclaw_materialized_defaults_cannot_add_auxiliary_model_routes(self):
        route = route_fixtures.route()
        for key, bad in (("compaction", {"mode": "safeguard", "model": "other/model"}),
                         ("compaction", {"mode": "safeguard", "provider": "other"}),
                         ("compaction", {"mode": "default"}),
                         ("subagents", {"maxConcurrent": 8, "archiveAfterMinutes": 60, "model": "other/model"}),
                         ("subagents", {"maxConcurrent": 8, "archiveAfterMinutes": 60, "provider": "other"}),
                         ("subagents", {"maxConcurrent": 8.0, "archiveAfterMinutes": 60}),
                         ("subagents", {"maxConcurrent": 9, "archiveAfterMinutes": 60})):
            value = snapshot(route); value["config"]["agents"]["defaults"][key] = bad
            with self.assertRaises(probe.OpenClawNativeError) as error:
                probe.native_openclaw(value, route)
            self.assertEqual(str(error.exception), "openclaw_runtime_defaults_unverified")

    def test_native_validation_labels_remain_fixed_and_do_not_store_configuration(self):
        error = probe.OpenClawNativeError("fixture-private-secret")
        self.assertEqual(str(error), "openclaw_native_route_unverified")
        vm = object.__new__(lifecycle.RuntimeLifecycle)
        vm.phase, vm.failure = "native_route", None; vm.remember_failure(error)
        self.assertEqual(vm.failure, {"phase": "native_route", "reason": "openclaw_native_route_unverified"})
        self.assertNotIn("fixture-private-secret", json.dumps(vm.failure))

    def test_openclaw_accepts_the_pinned_runtime_defaults_without_alternate_routes(self):
        # v2026.9.8 config.get returns materialized config, not a disk echo:
        # src/config/materialize.ts, defaults.ts and agent-limits.ts.
        for provider in ("openai", "anthropic"):
            route = route_fixtures.route("openclaw", provider)
            value = snapshot(route)
            value["config"]["agents"]["defaults"].update(
                maxConcurrent=8, compaction={"mode": "safeguard"},
                subagents={"maxConcurrent": 8, "archiveAfterMinutes": 60})
            report = probe.native_openclaw(value, route)
            probe.native_proof_valid(route, report)
            self.assertEqual(report["model"], route["model"])

    def test_all_four_native_route_pairs_bind_model_wire_endpoint_and_full_bounds(self):
        for role in ("openclaw", "hermes"):
            for provider in ("openai", "anthropic"):
                route = route_fixtures.route(role, provider)
                report = native(route); probe.native_proof_valid(route, report)
                self.assertEqual(report["model"], route["model"])
                self.assertNotIn(CAPABILITY, json.dumps(report))

    def test_openclaw_disk_echo_stale_applied_revision_wrong_model_endpoint_or_extra_provider_fail(self):
        route = route_fixtures.route()
        mutations = [lambda value: value.pop("appliedConfigHash"), lambda value: value.update(appliedConfigHash="c" * 64),
                     lambda value: value.update(valid=False), lambda value: value.update(warnings=["fixture"]),
                     lambda value: value["config"]["models"].update(mode="merge"),
                     lambda value: value["config"]["models"]["providers"].update(other={}),
                     lambda value: value["config"]["models"]["providers"]["openai"].update(baseUrl="https://api.openai.com/v1"),
                     lambda value: value["config"]["models"]["providers"]["openai"]["models"][0].update(id="other"),
                     lambda value: value["config"]["agents"]["defaults"]["model"].update(fallbacks=["other/model"])]
        for mutation in mutations:
            value = snapshot(route); mutation(value)
            with self.assertRaises(ValueError): probe.native_openclaw(value, route)

    def test_hermes_native_pool_oauth_or_other_route_is_not_accepted_as_a_config_echo(self):
        route = route_fixtures.route("hermes", "anthropic"); config = routes.compile_config({}, route)
        for changes in ({"provider": "anthropic"}, {"requested_provider": "auto"}, {"model": "other"},
                        {"source": "pool:as7-budget"}, {"base_url": "https://api.anthropic.com"},
                        {"api_mode": "chat_completions"}, {"api_key": "other-secret"}, {"credential_pool": "fixture"},
                        {"extra_headers": {"authorization": "other-secret"}}, {"request_overrides": {"service_tier": "priority"}}):
            getter = Mock(return_value=resolver_result(route) | changes)
            with self.assertRaises(ValueError): probe.native_hermes(config, route, getter)
            getter.assert_called_once_with(requested="as7-budget", target_model=route["model"],
                                          explicit_api_key=CAPABILITY, explicit_base_url=route["base_url"])
        config["model"]["openai_runtime"] = "codex_app_server"; getter = Mock()
        with self.assertRaises(ValueError): probe.native_hermes(config, route, getter)
        getter.assert_not_called()

    def call_hermes(self, action="transport", mutation=None, uid=10000, readiness=None, status=200, factory=None,
                    config_mode=0o600, file_key=True):
        route = route_fixtures.route("hermes"); raw = routes.encoded(routes.compile_config({}, route))
        env_raw = ("OPENAI_BASE_URL=" + route["base_url"] + "/v1\nAPI_SERVER_KEY=fixture-app-readiness-key\n").encode()
        if not file_key: env_raw = env_raw.replace(b"API_SERVER_KEY=fixture-app-readiness-key\n", b"")
        packet = {"action": action, "route": route, "config_sha256": hashlib.sha256(raw).hexdigest(),
                  "environment_sha256": hashlib.sha256(env_raw).hexdigest(),
                  "source_revision": "a" * 40, "nonce": "f" * 64 if action == "transport" else ""}
        if mutation: packet.update(mutation)
        reply = json.dumps({"schema": 1, "role": "hermes", "nonce": packet["nonce"], "source_revision": "a" * 40}, separators=(",", ":")).encode()
        if action == "readiness":
            payload = {"status": "ok", "readiness": {"checks": {name: {"status": "ok"} for name in
                ("state_db", "session_store", "config", "model", "disk", "gateway", "background_queues")}}}
            reply = json.dumps(payload if readiness is None else readiness).encode() if not isinstance(readiness, bytes) else readiness
        response = Mock(status=status); response.getheader.return_value = "application/json"; response.read.return_value = reply
        connection = Mock(); connection.getresponse.return_value = response
        health = {"__name__": "fixture_checked_health"}
        exec(compile((ROOT / "modules/common/runtime/hermes_health.py").read_bytes(), "checked-hermes-health", "exec"), health)
        info = SimpleNamespace(st_uid=10000, st_gid=10000, st_nlink=1, st_size=len(raw), st_mode=0o100000 | config_mode)
        env_info = SimpleNamespace(st_uid=10000, st_gid=10000, st_nlink=1, st_size=len(env_raw), st_mode=0o100600)
        with patch.object(probe.os, "geteuid", return_value=uid), patch.object(probe.Path, "is_symlink", return_value=False), \
             patch.object(probe.Path, "stat", autospec=True, side_effect=lambda path: env_info if path.name == ".env" else info), patch.object(probe.Path, "is_file", return_value=True), \
             patch.object(probe.Path, "read_bytes", autospec=True, side_effect=lambda path: env_raw if path.name == ".env" else raw), \
             patch.object(probe, "HEALTH_MODULE", SimpleNamespace(**health)), \
             patch.object(probe, "HTTPConnection", side_effect=factory or (lambda *_, **__: connection)) as http:
            result = probe.hermes_probe(packet, Mock(return_value=resolver_result(route)))
        return result, connection, http

    def test_pinned_hermes_group_readable_config_keeps_environment_owner_only(self):
        result, _, _ = self.call_hermes(config_mode=0o640)
        self.assertEqual(result["status"], "runtime_container_route_verified")
        for mode in (0o644, 0o660, 0o400):
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.call_hermes(config_mode=mode)

    def test_readiness_uses_inherited_app_auth_only_when_file_has_no_key(self):
        with patch.dict(os.environ, {"API_SERVER_KEY": "fixture-inherited-app-key"}, clear=True):
            result, connection, _ = self.call_hermes("readiness", file_key=False)
            self.assertEqual(result["status"], "runtime_readiness_verified")
            connection.request.assert_called_once_with("GET", "/health/detailed", headers={"Authorization": "Bearer fixture-inherited-app-key"})
            _, connection, _ = self.call_hermes("readiness")
            connection.request.assert_called_once_with("GET", "/health/detailed", headers={"Authorization": "Bearer fixture-app-readiness-key"})
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError): self.call_hermes("readiness", file_key=False)

    def test_readiness_uses_only_retained_app_key_and_fixed_bounded_local_endpoint(self):
        result, connection, http = self.call_hermes("readiness")
        self.assertEqual(result["status"], "runtime_readiness_verified")
        http.assert_called_once_with("127.0.0.1", 8642, timeout=5)
        connection.request.assert_called_once_with("GET", "/health/detailed", headers={"Authorization": "Bearer fixture-app-readiness-key"})
        connection.getresponse.return_value.read.assert_called_once_with(65537)
        connection.close.assert_called_once()
        self.assertNotIn("fixture-app-readiness-key", json.dumps(result)); self.assertNotIn(CAPABILITY, json.dumps(result))

    def test_readiness_rejects_redirect_auth_failure_missing_failed_or_ambiguous_checks(self):
        for status in (302, 401, 403, 500):
            with self.assertRaises(ValueError): self.call_hermes("readiness", status=status)
        for payload in ({"status": "ok"}, {"status": "ok", "readiness": {"checks": {"model": {"status": "ok"}}}},
                        {"status": "fail"}, b'{"status":"ok","status":"ok"}', b'{"status":NaN}', b'x' * 65537):
            with self.assertRaises(ValueError): self.call_hermes("readiness", readiness=payload)

    def test_actual_loopback_readiness_accepts_authenticated_health_without_generation(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "readiness": {"checks": {name: {"status": "ok"} for name in
                    ("state_db", "session_store", "config", "model", "disk", "gateway", "background_queues")}}}).encode())
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(lambda: thread.join(2)); self.addCleanup(server.shutdown)
        original = probe.HTTPConnection
        def factory(host, port, timeout):
            self.assertEqual((host, port, timeout), ("127.0.0.1", 8642, 5))
            return original(host, server.server_address[1], timeout=timeout)
        result, _, _ = self.call_hermes("readiness", factory=factory)
        self.assertEqual(result["status"], "runtime_readiness_verified")
        self.assertEqual(requests, [("/health/detailed", "Bearer fixture-app-readiness-key")])

    def test_hermes_native_only_probe_never_connects_and_transport_uses_one_exact_nonce_without_proxy(self):
        result, connection, http = self.call_hermes("native")
        self.assertEqual(result["status"], "runtime_native_route_verified"); http.assert_not_called()
        result, connection, http = self.call_hermes()
        self.assertEqual(result["uid"], 10000); self.assertEqual(result["nonce"], "f" * 64)
        http.assert_called_once_with("172.17.0.1", 23456, timeout=3)
        self.assertEqual(connection.request.call_args.args[:2], ("POST", "/hermes/_transport"))
        self.assertEqual(json.loads(connection.request.call_args.args[2]), {"nonce": "f" * 64})
        connection.close.assert_called_once()
        self.assertNotIn(CAPABILITY, json.dumps(result))
        for mutation in ({"config_sha256": "c" * 64}, {"environment_sha256": "c" * 64}, {"nonce": "bad"}, {"action": "paid"}, {"source_revision": "b" * 64}):
            with self.assertRaises(ValueError): self.call_hermes(mutation=mutation)
        with self.assertRaises(ValueError): self.call_hermes(uid=0)

    def test_python_probe_and_complete_inline_loader_fail_redacted_without_vm_module_files(self):
        commands = [[sys.executable, "-B", "-I", "-c", (ROOT / "scripts/showcase_runtime_probe.py").read_text()],
                    lifecycle.source_command(ROOT)[2:]]
        # Substitute only the local executable; the complete checked
        # loader really executes, without installed VM modules or cache files.
        commands[1][0] = sys.executable
        for command in commands:
            result = subprocess.run(command, input=json.dumps({"credential": CAPABILITY}).encode(), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1); self.assertNotIn(CAPABILITY.encode(), result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["schema"], 1)


class LifecycleTest(unittest.TestCase):
    def make(self, role="openclaw", provider="openai"):
        if getattr(self, "files", None): self.files.doCleanups()
        files = route_fixtures.TransactionTest("runTest"); files.setUp(); self.addCleanup(files.doCleanups)
        self.files, self.role, self.route = files, role, route_fixtures.route(role, provider)
        self.original_environment = b"OPENAI_API_KEY=as7-model-gateway-not-active\nANTHROPIC_API_KEY=as7-model-gateway-not-active\nAPI_SERVER_KEY=fixture-server-key\n"
        self.environment_path = files.root / "hermes/.env"
        self.environment_path.write_bytes(self.original_environment); self.environment_path.chmod(0o600)
        self.running, self.timer, self.clock, self.health = True, False, 0.0, "healthy"
        self.identifier, self.image_ref, self.image_id, self.restarts = CONTAINER, image(role), IMAGE_ID, 0
        self.calls = []; self.native_mutation = self.probe_mutation = None
        self.doctor_output = (json.dumps({"schemaVersion": 1, "ok": True, "checksRun": 12, "checksSkipped": 0,
            "findings": [], "warnings": []}).encode() if role == "openclaw" else HERMES_DOCTOR)
        self.doctor_failure = self.doctor_change = False
        self.stop_on_disarm = False
        self.task_mutation = None; self.task_failure = self.bad_help = False
        self.vm = lifecycle.RuntimeLifecycle(route_fixtures.binding(), self.execute, files.transaction,
                                             now=lambda: self.clock, sleep=self.sleep)
        patcher = patch.object(lifecycle, "PROBE_SOURCE", (ROOT / "scripts/showcase_runtime_probe.py").read_text())
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(lifecycle, "TASK_SOURCE", (ROOT / "scripts/showcase_runtime_task.py").read_text())
        patcher.start(); self.addCleanup(patcher.stop)
        namespace = {"__name__": "fixture_native_doctor", "__file__": str(ROOT / remote_fixtures.remote.DOCTOR_PATH)}
        exec(compile((ROOT / remote_fixtures.remote.DOCTOR_PATH).read_bytes(), namespace["__file__"], "exec"), namespace)
        parser = patch.object(lifecycle, "DOCTOR_MODULE", SimpleNamespace(**namespace)); parser.start(); self.addCleanup(parser.stop)
        return self.vm

    def sleep(self, amount): self.clock += amount

    def execute(self, args, timeout=10, input=None):
        self.calls.append((args, timeout, input))
        if args[:len(lifecycle.COMPOSE)] == lifecycle.COMPOSE: return self.identifier.encode()
        if args[:2] == ["/usr/bin/docker", "inspect"]:
            return json.dumps({"id": self.identifier, "image_id": self.image_id, "image_ref": self.image_ref,
                "state": {"Running": self.running, "Restarting": False, "Dead": False, "Health": {"Status": self.health}},
                "restarts": self.restarts, "labels": {"com.docker.compose.project": "agent-stack", "com.docker.compose.service": self.role},
                "mounts": [{"Destination": "/home/node/.openclaw" if self.role == "openclaw" else "/opt/data", "Type": "bind", "RW": True,
                            "Source": str(self.files.root / self.role)}]}).encode()
        if args[:2] == ["/usr/bin/docker", "stop"]: self.running = False; return CONTAINER.encode()
        if args[:2] == ["/usr/bin/docker", "start"]: self.running = True; return CONTAINER.encode()
        if args[0] == "systemd-run": self.timer = True; return b""
        if args[:2] == ["systemctl", "is-active"]: return b"active" if self.timer else b"inactive"
        if args[:2] == ["systemctl", "stop"]:
            self.timer = False
            if self.stop_on_disarm: self.running = False
            return b""
        if args[:2] == ["systemctl", "show"]: return b"active" if self.timer else b"inactive"
        if args[:2] == ["/usr/bin/docker", "exec"]:
            if input is not None and json.loads(input).get('action') == 'source_configuration':
                self.assertEqual(self.role, 'hermes'); self.assertIn('10000', args); self.assertIn('-I', args)
                packet = json.loads(input); raw = self.files.target(self.role).read_bytes()
                self.assertEqual(packet['config_sha256'], routes.sha(raw))
                config = self.yaml_configuration if hasattr(self, 'yaml_configuration') else routes.decoded(raw)
                return routes.encoded({'schema': 1, 'status': 'hermes_source_configuration_verified',
                    'config_sha256': routes.sha(raw), 'config': config})
            if "--help" in args:
                return b"unsupported" if self.bad_help else b"--expect-url --expect-final --params --timeout --json"
            if "agent" in args or (input is not None and "challenge" in json.loads(input)):
                self.assertEqual(timeout, 190); self.assertTrue(self.timer)
                self.assertEqual(args[args.index("--user") + 1], str(routes.OWNERS[self.role]))
                self.assertIn(CONTAINER, args); self.assertIn("env", args); self.assertIn("-i", args)
                if self.task_failure: raise lifecycle.agents.ProbeError("fixture timeout")
                value = agent_fixtures.response(self.role)
                if self.role == "openclaw":
                    params = json.loads(args[args.index("--params") + 1])
                    self.assertEqual(params["sessionId"], agent_fixtures.ID); self.assertEqual(params["idempotencyKey"], agent_fixtures.ID)
                    self.assertFalse(params["deliver"])
                    value["result"]["meta"]["agentMeta"]["provider"] = self.route["provider"]
                else:
                    value["runtime"]["requested"] = {"provider": "as7-budget", "model": self.route["model"]}
                    value.update(completed=True, partial=False, interrupted=False)
                    value = lifecycle.agents.parse_turn(self.role, value, self.route["provider"] + "/" + self.route["model"],
                        agent_fixtures.CHALLENGE, agent_fixtures.ID, agent_fixtures.RUN, native_provider="custom")
                if self.task_mutation: self.task_mutation(value)
                return json.dumps(value).encode()
            if "doctor" in args:
                self.assertNotIn("--fix", args); self.assertNotIn("--live", args); self.assertIn("-i", args)
                self.assertEqual(timeout, 90)
                if self.doctor_failure: raise lifecycle.agents.ProbeError("fixture timeout")
                if self.doctor_change: self.files.target(self.role).write_bytes(b'{"operator":"change"}\n')
                return self.doctor_output
            if "config.get" in args:
                value = snapshot(self.route)
                if self.native_mutation: self.native_mutation(value)
                return json.dumps(value).encode()
            packet = json.loads(input)
            self.assertEqual(packet["route"], self.route)
            if self.role == 'hermes' and packet['action'] == 'readiness':
                self.assertNotIn('env', args); self.assertIn("key=os.environ.get('API_SERVER_KEY')", args[-1])
            else:
                self.assertIn("env", args); self.assertIn("-i", args)
            value = {"schema": 1, "status": {"native": "runtime_native_route_verified", "transport": "runtime_container_route_verified", "readiness": "runtime_readiness_verified"}[packet["action"]],
                "role": self.role, "provider": self.route["provider"], "uid": routes.OWNERS[self.role],
                "config_sha256": packet["config_sha256"], "source_revision": packet["source_revision"], "nonce": packet["nonce"]}
            if self.role == "hermes": value["native_route"] = native(self.route)
            if self.role == "hermes": value["environment_sha256"] = packet["environment_sha256"]
            if packet["action"] == "readiness": value["readiness_sha256"] = "a" * 64
            if self.probe_mutation: self.probe_mutation(value)
            return json.dumps(value).encode()
        raise AssertionError("unexpected fixture command")

    def prepare(self): return self.vm.prepare(self.route, image(self.role), "f" * 32)

    def test_slow_openclaw_cli_can_verify_doctor_and_task_without_relaxing_routes(self):
        self.make(); execute = self.execute
        def slow_cli(args, timeout=10, **options):
            if 'config.get' in args or '--help' in args:
                if timeout < 12: raise lifecycle.agents.ProbeError('probe_command_timeout')
                self.clock += 12
            return execute(args, timeout=timeout, **options)
        self.vm.raw_execute = slow_cli
        self.prepare(); self.vm.verify(self.route, 'f' * 64)
        self.vm.doctor(self.route, 'a' * 32)
        report = self.task()
        self.assertEqual(report['status'], 'runtime_task_verified')
        self.assertFalse(self.timer); self.assertTrue(self.running)
        self.assertNotIn(CAPABILITY, json.dumps(report))
        self.vm.restore('openclaw')
        self.assertEqual(self.files.target().read_bytes(), self.files.before['openclaw'])

    def test_native_cli_timeout_remains_bounded_and_preserves_recovery(self):
        self.make(); execute = self.execute
        def stuck_cli(args, timeout=10, **options):
            if 'config.get' in args:
                self.assertLessEqual(timeout, 30)
                raise lifecycle.agents.ProbeError('probe_command_timeout')
            return execute(args, timeout=timeout, **options)
        self.vm.raw_execute = stuck_cli
        with self.assertRaises(lifecycle.agents.ProbeError): self.prepare()
        self.assertFalse(self.running); self.assertTrue(self.timer)
        self.assertEqual(self.vm.failure['phase'], 'native_route')
        self.assertEqual(self.vm.failure['reason'], 'probe_command_timeout')
        self.assertFalse(any('agent' in args for args, _, _ in self.calls))
        self.vm.restore('openclaw')
        self.assertEqual(self.files.target().read_bytes(), self.files.before['openclaw'])

    def test_pinned_hermes_yaml_is_staged_by_exact_native_hash_and_restored_byte_for_byte(self):
        self.make('hermes')
        raw = b'_config_version: 30\nmodel:\n  default: fixture-model\n  provider: anthropic\n'
        self.yaml_configuration = {'_config_version': 30, 'model': {'default': 'fixture-model', 'provider': 'anthropic'}}
        self.files.target('hermes').write_bytes(raw)
        ready = self.prepare(); self.assertEqual(ready['status'], 'runtime_reload_verified')
        self.vm.restore('hermes'); self.assertEqual(self.files.target('hermes').read_bytes(), raw)

    def test_hermes_configuration_changed_during_stop_is_not_overwritten_by_native_parse(self):
        self.make('hermes'); execute = self.execute
        changed = b'{"operator":"changed-after-source-read"}\n'
        def stop_edit(args, **options):
            result = execute(args, **options)
            if args[:2] == ['/usr/bin/docker', 'stop']: self.files.target('hermes').write_bytes(changed)
            return result
        self.vm.raw_execute = stop_edit
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.files.target('hermes').read_bytes(), changed)
        self.assertFalse(self.files.journal('hermes').exists())
        self.assertEqual(self.vm.failure['phase'], 'configuration_install')

    def test_pinned_hermes_restart_permission_does_not_reject_same_configuration_bytes(self):
        self.make('hermes'); self.prepare()
        self.files.target('hermes').chmod(0o640)
        self.vm.current_files(self.vm.load('hermes'), self.route)
        self.files.target('hermes').chmod(0o644)
        with self.assertRaises(ValueError): self.vm.current_files(self.vm.load('hermes'), self.route)

    def test_all_four_reload_probe_restore_paths_use_exact_container_and_private_original(self):
        for role in ("openclaw", "hermes"):
            for provider in ("openai", "anthropic"):
                self.make(role, provider)
                ready = self.prepare(); self.assertEqual(ready["watchdog"], "armed"); self.assertTrue(self.timer)
                report = self.vm.verify(self.route, "f" * 64)
                self.assertEqual(report["status"], "runtime_route_verified"); self.assertFalse(self.timer)
                self.assertTrue(self.running); self.assertNotIn(CAPABILITY, json.dumps(report))
                restored = self.vm.restore(role)
                self.assertEqual(restored["status"], "runtime_config_restored")
                self.assertFalse(self.running); self.assertFalse(self.timer)
                self.assertEqual(self.files.target(role).read_bytes(), self.files.before[role])
                self.assertEqual(self.environment_path.read_bytes(), self.original_environment)
                self.assertFalse(self.vm.path(role).exists())
                for args, _, _ in self.calls:
                    if args[:2] in (["/usr/bin/docker", "stop"], ["/usr/bin/docker", "start"]): self.assertEqual(args[-1], CONTAINER)
                self.assertFalse(any("pull" in args or "restart" in args for args, _, _ in self.calls))

    def test_repeated_prepare_and_verify_never_replays_reload_or_nonce(self):
        self.make(); self.prepare(); count = len(self.calls)
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(len(self.calls), count)
        self.vm.verify(self.route, "f" * 64); count = len(self.calls)
        with self.assertRaises(ValueError): self.vm.verify(self.route, "c" * 64)
        self.assertEqual(len(self.calls), count)

    def test_wrong_image_replacement_container_and_automatic_restart_fail_without_other_resource_stop(self):
        for change in ("image", "id", "restart"):
            self.make(); self.prepare()
            if change == "image": self.image_ref = image("hermes")
            elif change == "id": self.identifier = "a" * 64
            else: self.restarts = 1
            with self.assertRaises(ValueError): self.vm.verify(self.route, "f" * 64)
            self.assertTrue(self.timer)
            self.assertFalse(any(args[:2] == ["/usr/bin/docker", "stop"] and args[-1] != CONTAINER for args, _, _ in self.calls))

    def test_stale_native_revision_or_container_uid_nonce_failure_preserves_guardian_and_stops(self):
        for change in ("revision", "uid", "nonce"):
            self.make(); self.prepare()
            if change == "revision": self.native_mutation = lambda value: value.update(appliedConfigHash="c" * 64)
            else: self.probe_mutation = lambda value: value.update({change: 0 if change == "uid" else "bad"})
            with self.assertRaises(ValueError): self.vm.verify(self.route, "f" * 64)
            self.assertFalse(self.running); self.assertTrue(self.timer)
            self.assertEqual(json.loads(self.vm.path(self.role).read_text())["status"], "uncertain")
            self.vm.restore(self.role); self.assertFalse(self.timer); self.assertFalse(self.running)

    def test_bounded_unhealthy_reload_leaves_captured_container_stopped_and_original_restorable(self):
        self.make(); original = self.execute
        def unhealthy(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ["/usr/bin/docker", "start"]: self.health = "starting"
            return result
        self.vm.execute = unhealthy
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.clock, 90); self.assertFalse(self.running); self.assertTrue(self.timer)
        self.vm.restore(self.role)
        self.assertEqual(self.files.target().read_bytes(), self.files.before[self.role])
        self.assertFalse(self.running)

    def test_changed_original_or_context_binding_never_overwrites_unknown_configuration(self):
        self.make(); self.prepare(); self.vm.verify(self.route, "f" * 64)
        self.files.target().write_text('{"operator":"change"}\n')
        with self.assertRaises(ValueError): self.vm.restore(self.role)
        self.assertEqual(self.files.target().read_text(), '{"operator":"change"}\n'); self.assertFalse(self.running)
        context = json.loads(self.vm.path(self.role).read_text()); context["binding"]["source_revision"] = "c" * 40
        self.vm.path(self.role).write_bytes(routes.encoded(context))
        with self.assertRaises(ValueError): self.vm.load(self.role)

    def test_both_native_doctors_parse_unchanged_output_bind_identity_and_cannot_replay(self):
        for role in ("openclaw", "hermes"):
            for provider in ("openai", "anthropic"):
                self.make(role, provider); self.prepare(); ready = self.vm.verify(self.route, "f" * 64)
                report = self.vm.doctor(self.route, "a" * 32)
                self.assertEqual(report["status"], "runtime_doctor_verified"); self.assertEqual(report["health"], "healthy")
                self.assertEqual(report["native_route"], ready["native_route"]); self.assertFalse(self.timer)
                self.assertEqual(report["doctor_sha256"], routes.sha(self.doctor_output)); self.assertNotIn(CAPABILITY, json.dumps(report))
                before = len(self.calls)
                with self.assertRaises(ValueError): self.vm.doctor(self.route, "b" * 32)
                self.assertEqual(len(self.calls), before)
                self.vm.restore(role); self.assertFalse(self.running); self.assertEqual(self.environment_path.read_bytes(), self.original_environment)

    def test_native_failed_incomplete_doctor_or_timeout_keeps_recovery_and_stops_captured_container(self):
        for role in ("openclaw", "hermes"):
            for failure in ("failed", "incomplete", "timeout", "malformed"):
                self.make(role); self.prepare(); self.vm.verify(self.route, "f" * 64)
                if failure == "timeout": self.doctor_failure = True
                elif role == "hermes": self.doctor_output = HERMES_DOCTOR.replace(b"All checks passed!", b"Found 1 issue" if failure == "failed" else b"unfinished")
                else: self.doctor_output = json.dumps({"schemaVersion": True if failure == "malformed" else 1, "ok": failure != "failed", "checksRun": 12, "checksSkipped": 1 if failure == "incomplete" else 0, "findings": []}).encode()
                with self.assertRaises((ValueError, lifecycle.agents.ProbeError)): self.vm.doctor(self.route, "a" * 32)
                self.assertFalse(self.running); self.assertTrue(self.timer)
                self.assertEqual(json.loads(self.vm.path(role).read_text())["status"], "uncertain")
                self.vm.restore(role); self.assertFalse(self.timer); self.assertFalse(self.running)

    def test_doctor_changed_configuration_or_environment_cannot_keep_old_green_or_overwrite(self):
        for role in ("openclaw", "hermes"):
            self.make(role); self.prepare(); self.vm.verify(self.route, "f" * 64); self.doctor_change = True
            with self.assertRaises(ValueError): self.vm.doctor(self.route, "a" * 32)
            self.assertFalse(self.running); self.assertTrue(self.timer)
            with self.assertRaises(ValueError): self.vm.restore(role)
            self.assertEqual(self.files.target(role).read_bytes(), b'{"operator":"change"}\n')
        self.make("hermes"); self.prepare(); self.vm.verify(self.route, "f" * 64)
        self.environment_path.write_bytes(b"OPENAI_BASE_URL=https://foreign.invalid\n")
        with self.assertRaises(ValueError): self.vm.doctor(self.route, "a" * 32)
        with self.assertRaises(ValueError): self.vm.restore("hermes")
        self.assertEqual(self.environment_path.read_bytes(), b"OPENAI_BASE_URL=https://foreign.invalid\n")
        for action in ("route", "doctor"):
            self.make("hermes"); self.prepare()
            if action == "doctor": self.vm.verify(self.route, "f" * 64)
            self.stop_on_disarm = True
            with self.assertRaises(ValueError):
                if action == "doctor": self.vm.doctor(self.route, "a" * 32)
                else: self.vm.verify(self.route, "f" * 64)
            self.assertFalse(self.running)
            self.assertEqual(json.loads(self.vm.path("hermes").read_text())["status"], "uncertain")
            self.vm.restore("hermes")

    def ready_task(self, role="openclaw", provider="openai"):
        self.make(role, provider); self.prepare(); self.vm.verify(self.route, "f" * 64)
        return self.vm.doctor(self.route, "a" * 32)

    def task(self): return self.vm.task(self.route, agent_fixtures.CHALLENGE, agent_fixtures.ID)

    def test_four_task_routes_bind_same_container_doctor_and_fresh_answer_then_restore_original(self):
        for role in ("openclaw", "hermes"):
            for provider in ("openai", "anthropic"):
                doctor = self.ready_task(role, provider); report = self.task()
                self.assertEqual(report["status"], "runtime_task_verified"); self.assertFalse(self.timer)
                self.assertEqual(report["doctor_sha256"], doctor["doctor_sha256"])
                self.assertEqual(report["container_id"], doctor["container_id"])
                self.assertEqual(report["challenge_sha256"], routes.sha(routes.encoded(agent_fixtures.CHALLENGE)))
                self.assertEqual(report["turn"]["answer"]["sum"], 826364)
                self.assertNotIn(CAPABILITY, json.dumps(report))
                calls = len(self.calls)
                with self.assertRaises(ValueError): self.task()
                self.assertEqual(len(self.calls), calls)
                self.vm.restore(role); self.assertFalse(self.running)
                self.assertEqual(self.files.target(role).read_bytes(), self.files.before[role])
                self.assertEqual(self.environment_path.read_bytes(), self.original_environment)

    def test_task_without_fresh_native_doctor_never_dispatches_or_arms_timer(self):
        self.make(); self.prepare(); self.vm.verify(self.route, "f" * 64)
        calls = len(self.calls)
        with self.assertRaises(ValueError): self.task()
        self.assertEqual(len(self.calls), calls); self.assertFalse(self.timer)

    def test_lost_task_reply_bad_answer_or_pinned_help_stops_exact_container_and_cannot_repeat(self):
        for role in ("openclaw", "hermes"):
            for failure in ("timeout", "answer", "help"):
                if role == "hermes" and failure == "help": continue
                self.ready_task(role); self.task_failure = failure == "timeout"; self.bad_help = failure == "help"
                if failure == "answer":
                    self.task_mutation = (lambda value: value["result"]["payloads"][0].update(text='{"sum":0,"nonce":"a"}')) if role == "openclaw" else (lambda value: value["answer"].update(sum=0))
                with self.assertRaises((ValueError, lifecycle.agents.ProbeError)): self.task()
                self.assertFalse(self.running)
                self.assertEqual(json.loads(self.vm.path(role).read_text())["status"], "uncertain")
                calls = len(self.calls)
                with self.assertRaises(ValueError): self.task()
                self.assertEqual(len(self.calls), calls)
                self.vm.restore(role); self.assertFalse(self.timer)

    def test_task_changed_files_or_watchdog_stop_cannot_keep_success_or_overwrite_operator_change(self):
        for role in ("openclaw", "hermes"):
            for failure in ("config", "environment", "timer"):
                if role == "openclaw" and failure == "environment": continue
                self.ready_task(role)
                if failure == "timer": self.stop_on_disarm = True
                else:
                    changed = self.environment_path if failure == "environment" else self.files.target(role)
                    self.task_mutation = lambda value: changed.write_bytes(b'{"operator":"change"}\n')
                with self.assertRaises(ValueError): self.task()
                self.assertFalse(self.running)
                if failure != "timer":
                    with self.assertRaises(ValueError): self.vm.restore(role)
                    self.assertEqual(changed.read_bytes(), b'{"operator":"change"}\n')
                else: self.vm.restore(role)

    def test_task_replaced_image_and_expired_task_duration_are_not_accepted_as_fresh_proof(self):
        for failure in ("image", "duration"):
            self.ready_task()
            if failure == "image": self.task_mutation = lambda value: setattr(self, "image_id", "sha256:" + "a" * 64)
            else: self.task_mutation = lambda value: setattr(self, "clock", 286)
            with self.assertRaises((ValueError, lifecycle.agents.ProbeError)): self.task()
            self.assertEqual(json.loads(self.vm.path("openclaw").read_text())["status"], "uncertain")
            if failure == "image":
                self.assertFalse(any(args[:2] == ["/usr/bin/docker", "stop"] and args[-1] != CONTAINER for args, _, _ in self.calls))
            else: self.assertFalse(self.running)


class SessionReloadTest(unittest.TestCase):
    def make(self):
        helper = route_fixtures.SessionStagingTest("runTest"); fixture = helper.make(); self.addCleanup(helper.doCleanups)
        self.fixture = fixture; fixture.reload_calls = []
        fixture.prepare_mutation = fixture.probe_mutation = None; fixture.observe = True
        def command(target, packet, timeout):
            fixture.reload_calls.append(packet)
            role = packet.get("role") or packet["route"]["role"]
            if packet["action"] == "restore":
                self.assertFalse(fixture.models.gateway.alive); self.assertFalse(fixture.models.transport.alive)
                return {"schema": 1, "status": "runtime_config_restored", "role": role, "binding": packet["binding"], "original_sha256": "c" * 64}
            route = packet["route"]
            value = {"schema": 1, "status": "runtime_reload_verified", "role": role, "binding": packet["binding"],
                "container_id": CONTAINER, "image_ref": "ghcr.io/openclaw/openclaw@" + fixture.config["pins"]["openclaw_digest"] if role == "openclaw" else fixture.config["pins"]["hermes_image"],
                "image_id": IMAGE_ID, "config_sha256": "b" * 64, "native_route": native(route), "watchdog": "armed"}
            if role == "hermes": value["environment_sha256"] = "e" * 64
            if packet["action"] == "prepare":
                self.assertEqual(timeout, 180)
                if fixture.prepare_mutation: fixture.prepare_mutation(value)
            elif packet["action"] == "doctor":
                self.assertEqual(timeout, 180); self.assertIn(role, fixture.models.gateway.diagnostic_roles)
                self.assertNotIn(role, fixture.models.gateway.started_roles)
                value.update(status="runtime_doctor_verified", uid=1000 if role == "openclaw" else 10000,
                    health="healthy", watchdog="disarmed", doctor_sha256="d" * 64,
                    checks={"service." + role: "pass", role + ".doctor": "pass"})
                if role == "hermes": value["checks"].update({"hermes.config": "pass", "hermes.readiness": "pass"}); value["readiness_sha256"] = "a" * 64
                if fixture.probe_mutation: fixture.probe_mutation(value)
            else:
                self.assertEqual(timeout, 60)
                value.update(status="runtime_route_verified", watchdog="disarmed")
                value["container_probe"] = {"schema": 1, "status": "runtime_container_route_verified", "role": role,
                    "provider": route["provider"], "uid": 1000 if role == "openclaw" else 10000, "config_sha256": "b" * 64,
                    "source_revision": packet["binding"]["source_revision"], "nonce": packet["nonce"]}
                if role == "hermes": value["container_probe"]["native_route"] = value["native_route"]
                if role == "hermes": value["container_probe"]["environment_sha256"] = "e" * 64
                if fixture.observe: fixture.models.gateway.observed.add((role, packet["nonce"]))
                if fixture.probe_mutation: fixture.probe_mutation(value)
            return value
        fixture.models.runtime_lifecycle_command = command
        return fixture

    def test_both_native_reload_routes_require_independent_probe_and_cannot_authorize_runtime_task_yet(self):
        for role in ("openclaw", "hermes"):
            fixture = self.make()
            evidence = cleanup.private_read(fixture.checks.path); evidence["checks"][role + ".doctor"] = "pass"
            cleanup.private_write(fixture.checks.path, evidence)
            report = fixture.models.activate_runtime(role)
            self.assertEqual(report["status"], "runtime_route_verified")
            self.assertEqual(cleanup.private_read(fixture.checks.path)["checks"][role + ".doctor"], "uncertain")
            self.assertEqual(fixture.models.gateway.started_roles, set())
            with self.assertRaisesRegex(PolicyError, "runtime_model_route_required"): fixture.models.runtime_client(role)
            fixture.models.close()
            evidence = cleanup.private_read(fixture.checks.path)
            self.assertNotIn(CAPABILITY, json.dumps(evidence))
            self.assertEqual(evidence["model_channels"]["before_reboot"]["shutdown"], "verified")
            self.assertEqual([packet["action"] for packet in fixture.reload_calls], ["prepare", "verify", "restore"])

    def test_parent_verification_budget_covers_slow_cli_and_transport_checks(self):
        fixture = self.make(); command = fixture.models.runtime_lifecycle_command
        def slow_verification(target, packet, timeout):
            if packet['action'] == 'verify' and timeout < 45:
                raise PolicyError('fixture_verification_deadline')
            return command(target, packet, timeout)
        fixture.models.runtime_lifecycle_command = slow_verification
        result = fixture.models.activate_runtime('openclaw')
        self.assertEqual(result['status'], 'runtime_route_verified')
        self.assertEqual(fixture.models.gateway.started_roles, set())
        fixture.models.close()

    def test_forged_reload_receipt_or_echoed_unobserved_nonce_revokes_and_restores(self):
        for changed in ("image", "native", "schema", "schema_bool", "uid", "observe"):
            fixture = self.make()
            if changed == "image": fixture.prepare_mutation = lambda value: value.update(image_ref=image("hermes"))
            elif changed == "native": fixture.prepare_mutation = lambda value: value["native_route"].update(base_url_sha256="a" * 64)
            elif changed in ("schema", "schema_bool"): fixture.prepare_mutation = lambda value: value.update(schema=True if changed == "schema_bool" else 2)
            elif changed == "uid": fixture.probe_mutation = lambda value: value["container_probe"].update(uid=0)
            else: fixture.observe = False
            with self.assertRaisesRegex(PolicyError, "runtime_route_unverified"): fixture.models.activate_runtime("openclaw")
            self.assertEqual(fixture.models.state, "closed")
            evidence = cleanup.private_read(fixture.checks.path)
            self.assertEqual(evidence["model_channels"]["before_reboot"]["runtime_routes"]["openclaw"]["status"], "uncertain")
            self.assertEqual(fixture.reload_calls[-1]["action"], "restore")

    def test_session_native_doctor_uses_denied_generation_original_account_and_clears_current_green_on_close(self):
        for role in ("openclaw", "hermes"):
            fixture = self.make(); fixture.models.activate_runtime(role)
            report = fixture.models.runtime_doctor(role)
            self.assertEqual(report["status"], "runtime_doctor_verified"); self.assertEqual(fixture.models.gateway.started_roles, set())
            self.assertEqual(fixture.models.gateway.diagnostic_roles, set())
            evidence = cleanup.private_read(fixture.checks.path)
            self.assertEqual(evidence["checks"][role + ".doctor"], "pass"); self.assertEqual(evidence["checks"]["service." + role], "pass")
            with self.assertRaisesRegex(PolicyError, "runtime_doctor_already_invoked"): fixture.models.runtime_doctor(role)
            with self.assertRaisesRegex(PolicyError, "runtime_model_route_required"): fixture.models.runtime_client(role)
            fixture.models.close(); self.assertEqual(cleanup.private_read(fixture.checks.path)["checks"][role + ".doctor"], "uncertain")

    def test_session_forged_doctor_uid_native_hash_health_and_schema_revoke_and_restore(self):
        for changed in ("uid", "native", "hash", "health", "schema"):
            fixture = self.make(); fixture.models.activate_runtime("hermes")
            def mutate(value):
                if changed == "native": value["native_route"]["base_url_sha256"] = "a" * 64
                elif changed == "uid": value["uid"] = 0
                elif changed == "hash": value["doctor_sha256"] = "invalid"
                elif changed == "health": value["health"] = "stopped"
                elif changed == "schema": value["schema"] = True
            fixture.probe_mutation = mutate
            with self.assertRaisesRegex(PolicyError, "runtime_doctor_unverified"): fixture.models.runtime_doctor("hermes")
            self.assertEqual(fixture.models.state, "closed"); self.assertIn("hermes", fixture.models.gateway.sealed)
            self.assertEqual(fixture.reload_calls[-1]["action"], "restore")

    def test_session_any_new_diagnostic_hold_is_uncertain_and_never_refunded(self):
        fixture = self.make(); fixture.models.activate_runtime("hermes")
        fixture.probe_mutation = lambda value: fixture.models.spend.reserve("hermes", "c" * 64, 1)
        with self.assertRaisesRegex(PolicyError, "runtime_doctor_unverified"): fixture.models.runtime_doctor("hermes")
        account = fixture.models.checks.ledger.operate(lambda state: fixture.models.spend.account(state))
        self.assertEqual(len(account["model_requests"]), 1)
        self.assertEqual(fixture.models.state, "closed")


class GatewayStartTest(unittest.TestCase):
    def make(self):
        helper = model_fixtures.OriginalAccountSessionTest("runTest"); fixture = helper.make(real_gateway=True)
        self.addCleanup(helper.doCleanups)
        return fixture, helper

    def test_actual_gateway_generation_requires_controller_task_start_before_hold_key_or_upstream(self):
        fixture, helper = self.make()
        self.assertEqual(helper.request(fixture), 403)
        self.assertEqual(helper.account(fixture)["model_requests"], [])
        fixture.reader.assert_not_called(); fixture.opener.open.assert_not_called()
        started = fixture.models.begin_task("codex")
        self.assertEqual(helper.request(fixture), 200)
        self.assertEqual(fixture.models.task_receipt("codex", started)["status"], "provider_completions_verified")
        with self.assertRaises(PolicyError): fixture.models.gateway.authorize_role("codex")

    def test_runtime_nonce_before_task_start_uses_actual_http_without_hold_key_or_authorization(self):
        fixture, helper = self.make(); bundle = fixture.models.gateway.new_probe("hermes", kind="runtime")
        connection = HTTPConnection("127.0.0.1", fixture.models.gateway.server.server_port, timeout=3)
        try:
            connection.request("POST", "/hermes/_transport", json.dumps({"nonce": bundle["nonce"]}, separators=(",", ":")),
                {"Authorization": "Bearer " + bundle["credential"], "Content-Type": "application/json"})
            result = connection.getresponse(); self.assertEqual(result.status, 200); result.read()
        finally: connection.close()
        self.assertTrue(fixture.models.gateway.probe_observed("hermes", bundle["nonce"]))
        self.assertEqual(helper.account(fixture)["model_requests"], [])
        fixture.reader.assert_not_called(); fixture.opener.open.assert_not_called()
        self.assertEqual(fixture.models.gateway.started_roles, set())
        with self.assertRaises(PolicyError): fixture.models.gateway.new_probe("hermes", kind="runtime")
        with self.assertRaises(PolicyError): fixture.models.gateway.new_probe("codex", kind="runtime")

    def test_actual_gateway_diagnostic_window_blocks_racing_start_and_generation_then_seals_on_failure(self):
        fixture, helper = self.make(); gateway = fixture.models.gateway
        for role in ("openclaw", "hermes"):
            with gateway.diagnostic_window(role):
                self.assertEqual(helper.request(fixture, role), 403)
                with self.assertRaisesRegex(PolicyError, "model_diagnostic_in_progress"): gateway.authorize_role(role)
                with self.assertRaisesRegex(PolicyError, "model_diagnostic_refused"):
                    with gateway.diagnostic_window(role): pass
            self.assertFalse(gateway.dispatch_allowed(role)); self.assertEqual(gateway.diagnostic_roles, set())
        with self.assertRaises(RuntimeError):
            with gateway.diagnostic_window("hermes"): raise RuntimeError("fixture failed doctor")
        self.assertNotIn("hermes", gateway.tokens)
        with self.assertRaises(PolicyError): gateway.authorize_role("hermes")
        self.assertEqual(helper.account(fixture)["model_requests"], [])
        fixture.reader.assert_not_called(); fixture.opener.open.assert_not_called()
        gateway.authorize_role("openclaw"); self.assertTrue(gateway.dispatch_allowed("openclaw"))


if __name__ == "__main__": unittest.main()
