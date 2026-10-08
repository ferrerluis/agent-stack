import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_runtime_routes as routes
import showcase_model_session as session
import showcase_cleanup as cleanup
from showcase_policy import PolicyError
from showcase_model_session_fixtures import FixtureGateway, FixtureTransport, CAPABILITY
import test_showcase_remote as fixtures

ROOT = Path(__file__).resolve().parents[2]


def binding():
    return {"run_id": "as7-fixture01", "source_revision": "a" * 40,
            "boot_id": fixtures.BOOT, "checksum": fixtures.CHECKSUM}


def route(role="openclaw", provider="openai"):
    return {"role": role, "provider": provider, "model": "fixture-model", "credential": CAPABILITY,
            "base_url": "http://172.17.0.1:23456/" + role, "bridge_ipv4": "172.17.0.1",
            "context_tokens": 1050000, "output_tokens": 128}


def changed_access_time(info, count):
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
    return SimpleNamespace(**{name: getattr(info, name) for name in fields},
                           st_atime=info.st_atime + count, st_atime_ns=info.st_atime_ns + count)


class CompileTest(unittest.TestCase):
    def test_temporary_acceptance_tools_are_disabled_without_changing_original_app_settings(self):
        for role, original in (("openclaw", {"tools": {"profile": "full", "deny": ["browser"]}}),
                               ("hermes", {"platform_toolsets": {"api_server": ["terminal"], "cli": ["web", "terminal"], "telegram": ["web"]}, "mcp_servers": {"personal": {"command": "fixture"}}})):
            before = copy.deepcopy(original); value = routes.compile_config(original, route(role))
            if role == "openclaw": self.assertEqual(value["tools"], {"profile": "full", "deny": ["*"]})
            else:
                self.assertEqual(value["platform_toolsets"], {"api_server": [], "cli": [], "telegram": ["web"]})
                self.assertEqual(value["mcp_servers"], {})
            self.assertEqual(original, before)

    def test_openclaw_preserves_gateway_and_workspace_but_replaces_all_model_routes(self):
        original = {"gateway": {"auth": {"token": "fixture-app-token"}}, "agents": {"defaults": {"workspace": "/workspace",
                    "model": "old/model", "models": {"old/model": {"agentRuntime": {"id": "codex"}}}}},
                    "models": {"providers": {"unselected": {"baseUrl": "https://unselected.invalid"}}}}
        unchanged = copy.deepcopy(original)
        for provider in ("openai", "anthropic"):
            result = routes.compile_config(original, route(provider=provider))
            self.assertEqual(result["gateway"], original["gateway"])
            self.assertEqual(result["agents"]["defaults"]["workspace"], "/workspace")
            self.assertEqual(result["models"]["mode"], "replace")
            self.assertEqual(set(result["models"]["providers"]), {provider})
            value = result["models"]["providers"][provider]
            self.assertEqual(value["api"], "openai-completions" if provider == "openai" else "anthropic-messages")
            self.assertEqual(value["baseUrl"], route()["base_url"] + ("/v1" if provider == "openai" else ""))
            self.assertEqual(value["models"][0]["contextWindow"], 1050000)
            self.assertEqual(value["models"][0]["maxTokens"], 128)
            self.assertEqual(result["agents"]["defaults"]["model"]["fallbacks"], [])
            self.assertNotIn("unselected.invalid", json.dumps(result))
            self.assertNotIn("agentRuntime", json.dumps(result))
        self.assertEqual(original, unchanged)

    def test_hermes_named_route_preserves_dashboard_and_disables_discovery_and_fallbacks(self):
        original = {"dashboard": {"enabled": True}, "custom_providers": [{"name": "old"}],
                    "fallback_providers": [{"provider": "anthropic"}], "model": {"openai_runtime": "codex_app_server"}}
        for provider in ("openai", "anthropic"):
            value = routes.compile_config(original, route("hermes", provider))
            self.assertEqual(value["dashboard"], {"enabled": True})
            self.assertEqual(value["model"]["provider"], "as7-budget")
            self.assertEqual(value["model"]["api_mode"], "chat_completions" if provider == "openai" else "anthropic_messages")
            self.assertEqual(value["providers"]["as7-budget"]["discover_models"], False)
            self.assertEqual(value["providers"]["as7-budget"]["api_key"], CAPABILITY)
            self.assertEqual(value["providers"]["as7-budget"]["models"], ["fixture-model"])
            self.assertEqual(value["fallback_providers"], [])
            self.assertNotIn("custom_providers", value); self.assertNotIn("openai_runtime", value["model"])

    def test_unexpected_per_agent_auxiliary_and_endpoint_overrides_fail_before_migration(self):
        values = [("openclaw", {"agents": {"list": [{"id": "personal"}]}}),
                  ("openclaw", {"agents": {"defaults": {"agentRuntime": {"id": "codex"}}}}),
                  ("openclaw", {"agents": {"defaults": {"cliBackends": {"personal": {}}}}}),
                  ("openclaw", {"agents": {"defaults": {"compaction": {"model": "other"}}}}),
                  ("hermes", {"auxiliary": {"compression": {"provider": "other"}}}),
                  ("hermes", {"model_routes": {"other": "route"}}), ("hermes", {"model_aliases": {"a": "b"}}),
                  ("hermes", {"secrets": {"sources": [{"type": "op"}]}})]
        for role, original in values:
            with self.subTest(role=role, keys=list(original)), self.assertRaises(ValueError):
                routes.compile_config(original, route(role))

    def test_private_route_schema_caps_and_limits_are_strict(self):
        for changes in ({"role": "codex"}, {"provider": "other"}, {"model": "bad model"}, {"credential": "provider-key"},
                        {"bridge_ipv4": "8.8.8.8"}, {"bridge_ipv4": "127.0.0.1"}, {"bridge_ipv4": "169.254.169.254"},
                        {"bridge_ipv4": "::1"}, {"bridge_ipv4": 2886795265}, {"role": []},
                        {"base_url": "https://api.openai.com/v1"},
                        {"base_url": "http://172.17.0.1:65536/openclaw"}, {"base_url": route()["base_url"] + "?override=1"},
                        {"base_url": "http://172.17.0.1:23456/hermes"}, {"context_tokens": True},
                        {"context_tokens": 4095}, {"context_tokens": 10000001}, {"output_tokens": 15},
                        {"output_tokens": 4097}, {"output_tokens": 16.0}, {"extra": "override"}):
            with self.subTest(keys=list(changes)), self.assertRaises(ValueError): routes.compile_config({}, route() | changes)
        routes.binding_valid(binding() | {"run_id": "as7-aws-fixture-01"})
        for changes in ({"run_id": "../../personal"}, {"run_id": "as7-not_allowed"}, {"run_id": "as7-short"},
                        {"source_revision": "b" * 64}, {"boot_id": "bad"},
                        {"checksum": "b" * 40}, {"extra": "override"}):
            with self.assertRaises(ValueError): routes.binding_valid(binding() | changes)
        for raw in (b'[]', b'{"a":1,"a":2}', b'{"a":NaN}', b'x' * (routes.LIMIT + 1)):
            with self.assertRaises(ValueError): routes.decoded(raw)


class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Real temporary files/locks/fsync/renames; logical root/runtime owners
        # are replaced because the local runner is UID 1000, not VM root.
        uid, gid = os.geteuid(), os.getegid()
        checked = routes.checked_path
        def owner_check(path, owner=None, directory=False):
            return checked(path, owner=uid if owner == 0 else owner, directory=directory)
        fstat = os.fstat
        def logical_root_stat(fd):
            value = list(fstat(fd)); value[4] = value[5] = 0
            return os.stat_result(value)
        self.chown = Mock()
        for patcher in (patch.object(routes.os, "geteuid", return_value=0),
                        patch.dict(routes.OWNERS, {"openclaw": uid, "hermes": uid}),
                        patch.object(routes, "checked_path", side_effect=owner_check),
                        patch.object(routes.os, "fchown", self.chown), patch.object(routes.os, "fstat", side_effect=logical_root_stat)):
            patcher.start(); self.addCleanup(patcher.stop)
        self.before = {}
        for role, filename in routes.FILES.items():
            path = self.root / filename; path.parent.mkdir()
            self.before[role] = (json.dumps({"gateway": {"mode": "local"}, "model": {"provider": "custom", "default": "original"}}, indent=2) + "\n").encode()
            path.write_bytes(self.before[role]); path.chmod(0o640)
        self.transaction = routes.ConfigurationTransaction(self.root)

    def target(self, role="openclaw"): return self.root / routes.FILES[role]
    def journal(self, role="openclaw"): return self.transaction.journal / (role + ".json")

    def secure_original(self):
        config = {'gateway': {'mode': 'local', 'bind': 'loopback',
            'auth': {'mode': 'token', 'token': 'fixture-original-gateway-secret'},
            'controlUi': {'allowedOrigins': ['https://fixture', 'https://fixture.test.ts.net']}}}
        self.before['openclaw'] = routes.encoded(config); self.target().write_bytes(self.before['openclaw'])
        return config

    def test_secretrefs_keep_credentials_out_of_config_and_restore_removes_only_owned_secret_file(self):
        self.secure_original(); original_mode = stat.S_IMODE(self.target().stat().st_mode)
        with self.transaction.lock(): result = self.transaction._install(binding(), route(), file_secrets=True)
        target = self.transaction.verify_secret_file(binding())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(routes.decoded(target.read_bytes()), {'gateway': 'fixture-original-gateway-secret', 'model': CAPABILITY})
        config = routes.decoded(self.target().read_bytes())
        self.assertNotIn(CAPABILITY, self.target().read_text()); self.assertNotIn('fixture-original-gateway-secret', self.target().read_text())
        self.assertEqual(config['gateway']['publicOrigin'], 'https://fixture.test.ts.net')
        self.assertEqual(config['models']['providers']['openai']['apiKey'], {'source': 'file', 'provider': 'as7-budget', 'id': '/model'})
        self.assertEqual(config['agents']['defaults']['utilityModel'], '')
        self.assertFalse(config['memory']['search']['enabled'])
        self.assertEqual(config['skills']['workshop']['autonomous']['mode'], 'off')
        self.assertNotIn(CAPABILITY, json.dumps(result))
        self.transaction.restore(binding(), 'openclaw')
        self.assertEqual(self.target().read_bytes(), self.before['openclaw'])
        self.assertEqual(stat.S_IMODE(self.target().stat().st_mode), original_mode)
        self.assertFalse(target.exists()); self.assertFalse(self.journal().exists())

    def test_existing_or_changed_secret_file_cannot_be_overwritten_or_removed(self):
        self.secure_original(); secret = self.root / routes.OPENCLAW_SECRET_FILE
        secret.write_bytes(b'operator data'); secret.chmod(0o600)
        with self.assertRaises(ValueError): self.transaction._install(binding(), route(), file_secrets=True)
        self.assertFalse(self.journal().exists()); self.assertEqual(secret.read_bytes(), b'operator data')
        secret.unlink(); self.transaction._install(binding(), route(), file_secrets=True)
        installed = self.target().read_bytes(); secret.write_bytes(b'operator change')
        with self.assertRaises(ValueError): self.transaction.restore(binding(), 'openclaw')
        self.assertEqual(self.target().read_bytes(), installed); self.assertEqual(secret.read_bytes(), b'operator change')
        self.assertTrue(self.journal().exists())

    def test_crash_before_secret_or_config_write_keeps_recoverable_exact_original(self):
        for fail_secret in (True, False):
            self.secure_original(); original = self.target().read_bytes()
            write = routes.atomic_write; secret = self.root / routes.OPENCLAW_SECRET_FILE
            def crash(path, *args, **kwargs):
                if path == (secret if fail_secret else self.target()): raise RuntimeError('fixture interrupted write')
                return write(path, *args, **kwargs)
            with patch.object(routes, 'atomic_write', side_effect=crash), self.assertRaises(RuntimeError):
                self.transaction._install(binding(), route(), file_secrets=True)
            self.assertTrue(self.journal().exists()); self.assertEqual(self.target().read_bytes(), original)
            self.transaction.restore(binding(), 'openclaw')
            self.assertEqual(self.target().read_bytes(), original); self.assertFalse(secret.exists())

    def test_secure_profile_refuses_nonlocal_bind_ambiguous_origin_and_preexisting_secret_providers(self):
        original = self.secure_original()
        cases = [copy.deepcopy(original) for _ in range(3)]
        cases[0]['gateway']['bind'] = 'lan'
        cases[1]['gateway']['controlUi']['allowedOrigins'].append('https://other.test.ts.net')
        cases[2]['secrets'] = {'providers': {'operator': {'source': 'env'}}}
        for config in cases:
            self.target().write_bytes(routes.encoded(config))
            with self.assertRaises(ValueError): self.transaction._install(binding(), route(), file_secrets=True)
            self.assertFalse(self.journal().exists())

    def test_real_private_journal_precedes_atomic_change_and_exact_original_bytes_mode_are_restored(self):
        for role in routes.OWNERS:
            result = self.transaction.install(binding(), route(role))
            self.assertEqual(result["status"], "runtime_config_installed")
            self.assertEqual(result["native_contract"], "unverified")
            self.assertNotIn(CAPABILITY, json.dumps(result))
            self.assertEqual(stat.S_IMODE(self.journal(role).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.target(role).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.transaction.journal.stat().st_mode), 0o700)
            self.assertEqual(result["config_sha256"], hashlib.sha256(self.target(role).read_bytes()).hexdigest())
            restored = self.transaction.restore(binding(), role)
            self.assertEqual(restored["status"], "runtime_config_restored")
            self.assertEqual(self.target(role).read_bytes(), self.before[role])
            self.assertEqual(stat.S_IMODE(self.target(role).stat().st_mode), 0o640)
            self.assertFalse(self.journal(role).exists())
        self.assertFalse(list(self.root.rglob(".as7-route-*")))

    def test_config_reads_may_change_access_time_without_becoming_operator_edits(self):
        checked = routes.checked_path
        for role in routes.OWNERS:
            calls = []
            def reading(path, **kwargs):
                info = checked(path, **kwargs)
                if Path(path) == self.target(role):
                    calls.append(path); return changed_access_time(info, len(calls))
                return info
            with patch.object(routes, "checked_path", side_effect=reading):
                result = self.transaction.install(binding(), route(role))
            self.assertEqual(result["status"], "runtime_config_installed")
            self.assertGreaterEqual(len(calls), 2)
            self.transaction.restore(binding(), role)
            self.assertEqual(self.target(role).read_bytes(), self.before[role])

    def test_same_config_bytes_with_changed_permissions_or_file_identity_refuse_install(self):
        atomic = routes.atomic_write
        for role, kind in (("openclaw", "permissions"), ("hermes", "replacement")):
            original_inode = self.target(role).stat().st_ino
            def edit(path, *args, **kwargs):
                result = atomic(path, *args, **kwargs)
                if path == self.journal(role):
                    if kind == "permissions": self.target(role).chmod(0o600)
                    else:
                        replacement = self.root / "same-bytes-replacement"
                        replacement.write_bytes(self.before[role]); replacement.chmod(0o640)
                        replacement.replace(self.target(role))
                return result
            with patch.object(routes, "atomic_write", side_effect=edit), self.assertRaises(ValueError):
                self.transaction.install(binding(), route(role))
            self.assertEqual(self.target(role).read_bytes(), self.before[role]); self.assertTrue(self.journal(role).exists())
            if kind == "permissions": self.assertEqual(stat.S_IMODE(self.target(role).stat().st_mode), 0o600)
            else: self.assertNotEqual(self.target(role).stat().st_ino, original_inode)

    def test_install_is_single_use_and_unknown_changed_file_is_never_overwritten(self):
        self.transaction.install(binding(), route())
        with self.assertRaises(ValueError): self.transaction.install(binding(), route())
        self.target().write_text('{"changed":"by-runtime"}\n')
        with self.assertRaises(ValueError): self.transaction.restore(binding(), "openclaw")
        self.assertEqual(self.target().read_text(), '{"changed":"by-runtime"}\n')
        self.assertTrue(self.journal().exists())

    def test_crash_after_durable_journal_before_replace_is_recoverable_without_install_replay(self):
        atomic = routes.atomic_write
        def crash(path, *args, **kwargs):
            if path == self.target(): raise OSError("fixture crash")
            return atomic(path, *args, **kwargs)
        with patch.object(routes, "atomic_write", side_effect=crash), self.assertRaises(OSError):
            self.transaction.install(binding(), route())
        self.assertEqual(self.target().read_bytes(), self.before["openclaw"])
        self.assertTrue(self.journal().exists())
        self.transaction.restore(binding(), "openclaw")
        self.assertFalse(self.journal().exists())

    def test_wrong_source_boot_run_snapshot_hash_or_public_journal_refuses_restore(self):
        self.transaction.install(binding(), route())
        after = self.target().read_bytes()
        for changes in ({"source_revision": "b" * 40}, {"boot_id": fixtures.NEW_BOOT}, {"run_id": "as7-other"}):
            with self.assertRaises(ValueError): self.transaction.restore(binding() | changes, "openclaw")
            self.assertEqual(self.target().read_bytes(), after)
        snapshot = self.journal().read_bytes()
        value = json.loads(snapshot); value["original_sha256"] = "b" * 64
        self.journal().write_bytes(routes.encoded(value))
        with self.assertRaises(ValueError): self.transaction.restore(binding(), "openclaw")
        self.journal().write_bytes(snapshot); self.journal().chmod(0o640)
        with self.assertRaises(ValueError): self.transaction.restore(binding(), "openclaw")
        self.assertEqual(self.target().read_bytes(), after)

    def test_symlink_hardlink_and_parent_symlink_are_refused_before_journal_or_write(self):
        original = self.target().read_bytes(); path = self.target()
        path.unlink(); other = self.root / "operator.json"; other.write_bytes(original); other.chmod(0o600)
        path.symlink_to(other)
        with self.assertRaises(ValueError): self.transaction.install(binding(), route())
        path.unlink(); os.link(other, path)
        with self.assertRaises(ValueError): self.transaction.install(binding(), route())
        path.unlink(); path.parent.rmdir(); path.parent.symlink_to(self.root / "hermes", target_is_directory=True)
        with self.assertRaises(ValueError): self.transaction.install(binding(), route())
        self.assertEqual(other.read_bytes(), original); self.assertFalse(self.journal().exists())

    def test_second_transaction_cannot_wait_or_modify_while_first_holds_lock(self):
        with self.transaction.lock(), self.assertRaises(BlockingIOError): self.transaction.install(binding(), route())
        self.assertEqual(self.target().read_bytes(), self.before["openclaw"])
        self.assertFalse(self.journal().exists())

    def test_native_yaml_duplicate_keys_and_public_originals_are_not_silently_migrated(self):
        for raw in (b'model:\n  provider: custom\n', b'{"model":{},"model":{}}'):
            self.target("hermes").write_bytes(raw)
            with self.assertRaises(ValueError): self.transaction.install(binding(), route("hermes"))
            self.assertEqual(self.target("hermes").read_bytes(), raw)
            self.assertFalse(self.journal("hermes").exists())
        self.target().chmod(0o644)
        with self.assertRaises(ValueError): self.transaction.install(binding(), route())
        self.assertEqual(self.target().read_bytes(), self.before["openclaw"])


class VMGuardTest(unittest.TestCase):
    def test_main_checks_boot_checksum_and_inactive_runtime_before_any_transaction(self):
        packet = {"action": "install", "binding": binding(), "route": route()}
        applied = {"status": "success", "checksum": fixtures.CHECKSUM, "services": ["hermes", "openclaw", "workspace"]}
        for changed_boot, changed_checksum, inactive in ((True, False, True), (False, True, True), (False, False, False)):
            runtime = Mock(side_effect=None if inactive else ValueError("fixture-not-stopped"))
            with patch.object(routes.os, "geteuid", return_value=0), \
                 patch.object(routes.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(packet).encode()))), \
                 patch.object(routes.sys, "stdout", io.StringIO()) as output, \
                 patch.object(routes.Path, "read_text", return_value=fixtures.NEW_BOOT if changed_boot else fixtures.BOOT), \
                 patch.object(routes.Path, "read_bytes", return_value=json.dumps(applied | ({"checksum": "b" * 64} if changed_checksum else {})).encode()), \
                 patch.object(routes, "inactive_runtime", runtime), patch.object(routes, "ConfigurationTransaction") as transaction:
                self.assertEqual(routes.main(), 1)
                transaction.assert_not_called()
                self.assertEqual(json.loads(output.getvalue()), {"schema": 1, "status": "runtime_config_route_unverified"})
                self.assertNotIn(CAPABILITY, output.getvalue())

    def test_running_restarting_unknown_or_multiple_containers_refuse_all_file_writes(self):
        identifier = "b" * 64
        for state in ({"Running": True, "Restarting": False}, {"Running": False, "Restarting": True},
                      {"Running": 0, "Restarting": False}, {}):
            execute = Mock(side_effect=[Mock(stdout=identifier.encode(), stderr=b""), Mock(stdout=json.dumps(state).encode(), stderr=b"")])
            with patch.object(routes.subprocess, "run", execute), self.assertRaises(ValueError): routes.inactive_runtime("openclaw")
        for identifier in ("", "b" * 12, "b" * 64 + "\n" + "c" * 64):
            with patch.object(routes.subprocess, "run", return_value=Mock(stdout=identifier.encode(), stderr=b"")), self.assertRaises(ValueError):
                routes.inactive_runtime("hermes")

    def test_exact_stopped_container_is_checked_with_no_shell_retry_or_secret_input(self):
        execute = Mock(side_effect=[Mock(stdout=("b" * 64 + "\n").encode(), stderr=b""),
                                   Mock(stdout=b'{"Running":false,"Restarting":false}', stderr=b"")])
        with patch.object(routes.subprocess, "run", execute): routes.inactive_runtime("hermes")
        self.assertEqual(execute.call_count, 2)
        for call in execute.call_args_list:
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL); self.assertEqual(call.kwargs["timeout"], 10)
            self.assertNotIn("shell", call.kwargs)

    def test_actual_checked_source_bad_stdin_is_redacted_without_vm_mutations(self):
        source = (ROOT / session.RUNTIME_CONFIG).read_text()
        for packet in (b'{"credential":"' + CAPABILITY.encode() + b'"}', b'x' * (routes.LIMIT + 1)):
            result = subprocess.run([sys.executable, "-B", "-I", "-c", source], input=packet, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout), {"schema": 1, "status": "runtime_config_route_unverified"})
            self.assertNotIn(CAPABILITY.encode(), result.stdout + result.stderr)


class SessionStagingTest(unittest.TestCase):
    def make(self):
        fixture = fixtures.RemoteChecksTest("runTest"); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.checks.bootstrap()
        fixture.models = session.ModelSession(fixture.checks, fixture.models.policy, FixtureGateway, FixtureTransport)
        fixture.checks.models = None
        fixture.models.start("before_reboot")
        self.addCleanup(lambda: fixture.models.close() if fixture.models.state not in ("closed", "closed_unverified") else None)
        original = fixture.checks.execute; source = (ROOT / session.RUNTIME_CONFIG).read_text()
        fixture.config_calls = []; fixture.install_mutation = fixture.restore_mutation = None
        def execute(args, **kwargs):
            words = shlex.split(args[-1])
            if words[-1] != source: return original(args, **kwargs)
            packet = json.loads(kwargs["input"]); fixture.config_calls.append(packet)
            self.assertIn("--pinned", args); self.assertEqual(kwargs["timeout"], 20)
            if packet["action"] == "install":
                role = packet["route"]["role"]
                report = {"schema": 1, "status": "runtime_config_installed", "role": role, "binding": packet["binding"],
                    "config_sha256": "b" * 64, "base_url_sha256": hashlib.sha256(packet["route"]["base_url"].encode()).hexdigest(),
                    "native_contract": "unverified"}
                if fixture.install_mutation: fixture.install_mutation(report)
            else:
                self.assertFalse(fixture.models.gateway.alive); self.assertFalse(fixture.models.transport.alive)
                report = {"schema": 1, "status": "runtime_config_restored", "role": packet["role"],
                          "binding": packet["binding"], "original_sha256": "c" * 64}
                if fixture.restore_mutation: fixture.restore_mutation(report)
            return json.dumps(report).encode()
        fixture.checks.execute = execute
        return fixture

    def test_source_checked_staging_retains_original_limits_and_cannot_authorize_paid_runtime(self):
        fixture = self.make()
        result = fixture.models.stage_runtime_config("openclaw")
        self.assertEqual(result["native_contract"], "unverified")
        packet = fixture.config_calls[0]
        self.assertEqual(packet["route"]["context_tokens"], fixture.models.policy["roles"]["openclaw"]["context_tokens_upper_bound"])
        self.assertEqual(packet["route"]["base_url"], "http://172.17.0.1:23456/openclaw")
        with self.assertRaisesRegex(PolicyError, "runtime_model_route_required"): fixture.models.runtime_client("openclaw")
        with self.assertRaisesRegex(PolicyError, "runtime_config_already_invoked"): fixture.models.stage_runtime_config("openclaw")
        fixture.models.close()
        value = cleanup.private_read(fixture.checks.path)
        channel = value["model_channels"]["before_reboot"]
        self.assertEqual(channel["runtime_config_restores"]["openclaw"]["status"], "runtime_config_restored")
        self.assertEqual(channel["shutdown"], "verified")
        self.assertNotIn(CAPABILITY, json.dumps(value))
        self.assertEqual(fixture.models.runtime_configs, {})
        self.assertEqual(cleanup.private_read(fixture.ledger.directory / "reservations.json")["attempts"][0]["model_requests"], [])

    def test_bad_install_proof_revokes_and_restores_instead_of_passing_or_repeating(self):
        fixture = self.make(); fixture.install_mutation = lambda report: report.update(native_contract="verified")
        with self.assertRaisesRegex(PolicyError, "runtime_config_route_unverified"): fixture.models.stage_runtime_config("hermes")
        self.assertEqual(fixture.models.state, "closed")
        self.assertEqual([packet["action"] for packet in fixture.config_calls], ["install", "restore"])
        channel = cleanup.private_read(fixture.checks.path)["model_channels"]["before_reboot"]
        self.assertEqual(channel["runtime_configs"]["hermes"]["status"], "uncertain")
        self.assertEqual(channel["shutdown"], "verified")

    def test_uncertain_restore_closes_capabilities_but_cannot_claim_verified_shutdown(self):
        fixture = self.make(); fixture.models.stage_runtime_config("hermes")
        fixture.restore_mutation = lambda report: report.update(binding=binding())
        with self.assertRaisesRegex(PolicyError, "model_session_shutdown_unverified"): fixture.models.close()
        self.assertFalse(fixture.models.gateway.alive); self.assertFalse(fixture.models.transport.alive)
        self.assertEqual(fixture.models.state, "closed_unverified")
        channel = cleanup.private_read(fixture.checks.path)["model_channels"]["before_reboot"]
        self.assertEqual(channel["shutdown"], "uncertain")
        self.assertEqual(channel["runtime_config_restores"]["hermes"], {"status": "uncertain"})
        with self.assertRaises(PolicyError): fixture.models.start("after_reboot")

    def test_staging_after_reboot_is_fresh_source_boot_bound_and_keeps_the_same_allocation(self):
        fixture = self.make(); fixture.models.stage_runtime_config("openclaw"); fixture.models.close()
        before = cleanup.private_read(fixture.ledger.directory / "reservations.json")["attempts"][0]
        evidence = cleanup.private_read(fixture.checks.path)
        evidence["after_reboot"] = evidence["baseline"] | {"boot_id": fixtures.NEW_BOOT}
        evidence["checks"]["reboot"] = "pass"; cleanup.private_write(fixture.checks.path, evidence)
        fixture.boot = fixtures.NEW_BOOT
        fixture.models.start("after_reboot"); result = fixture.models.stage_runtime_config("openclaw")
        self.assertEqual(result["binding"]["boot_id"], fixtures.NEW_BOOT)
        fixture.models.close()
        after = cleanup.private_read(fixture.ledger.directory / "reservations.json")["attempts"][0]
        self.assertEqual(after, before)
        self.assertEqual([packet["action"] for packet in fixture.config_calls], ["install", "restore", "install", "restore"])

    def test_lost_install_and_work_deadline_revoke_even_when_failure_evidence_cannot_be_reloaded(self):
        fixture = self.make()
        original = fixture.models.runtime_config_command
        def lost_install(target, packet):
            if packet["action"] == "install":
                fixture.clock += 20000
                raise OSError("private fixture failure")
            return original(target, packet)
        fixture.models.runtime_config_command = lost_install
        with self.assertRaisesRegex(PolicyError, "model_session_shutdown_unverified"):
            fixture.models.stage_runtime_config("openclaw")
        self.assertFalse(fixture.models.gateway.alive); self.assertFalse(fixture.models.transport.alive)
        self.assertEqual(fixture.models.state, "closed_unverified")
        channel = cleanup.private_read(fixture.checks.path)["model_channels"]["before_reboot"]
        self.assertEqual(channel["runtime_configs"]["openclaw"]["status"], "uncertain")
        self.assertEqual(channel["shutdown"], "uncertain")


if __name__ == "__main__": unittest.main()
