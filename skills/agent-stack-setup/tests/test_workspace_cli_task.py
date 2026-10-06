import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("workspace_cli_task", ROOT / "modules/common/runtime/workspace_cli_task.py")
task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task)


@contextlib.contextmanager
def owned_home(home):
    original_stat = Path.stat
    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == Path(home):
            values = list(result); values[4] = 1000
            return os.stat_result(values)
        return result
    with patch.object(task.os, "geteuid", return_value=1000), patch.object(Path, "stat", stat):
        yield


class WorkspaceTaskTest(unittest.TestCase):
    def test_controller_challenge_is_written_exactly_before_native_execution(self):
        challenge = {"lhs": 17, "rhs": 26, "nonce": "a1" * 16}
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}), owned_home(home):
            def invoke(args, directory, env, timeout):
                self.assertEqual((directory / "input.json").read_text(), json.dumps(challenge, sort_keys=True, separators=(",", ":")))
                self.assertIn("reply with only the same JSON object", args[-1])
                (directory / "result.json").write_text(json.dumps({"sum": 43, "nonce": challenge["nonce"]}))
                return 0, self.good_output("codex")
            with patch.object(task, "invoke", side_effect=invoke), patch.object(task.secrets, "randbelow", side_effect=AssertionError("controller challenge must be used")):
                result = task.run_task("codex", "test-model", json.dumps(self.bundle()), 10, model_gateway=True, challenge=challenge)
            self.assertEqual(result["status"], "pass")
            import hashlib
            self.assertEqual(result["input_sha256"], hashlib.sha256(json.dumps(challenge, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_invalid_controller_challenges_refuse_before_files_or_execution(self):
        original = {"lhs": 17, "rhs": 26, "nonce": "a1" * 16}
        with patch.object(task.os, "geteuid", return_value=1000), patch.object(task, "invoke") as invoke, patch.object(task, "Path") as paths:
            for change in ({"lhs": True}, {"rhs": 1000000}, {"lhs": -1}, {"nonce": "stale"}, {"extra": 1}):
                result = task.run_task("codex", "test-model", json.dumps(self.bundle()), 10, model_gateway=True, challenge=original | change)
                self.assertEqual(result["reason"], "invalid_task_challenge")
            invoke.assert_not_called(); paths.assert_not_called()

    def test_native_cache_counts_are_preserved_separately_for_both_tools(self):
        for cli in ("codex", "claude"):
            value = json.loads(self.good_output(cli))
            value["usage"].update({"cached_input_tokens" if cli == "codex" else "cache_read_input_tokens": 12,
                                   "cache_write_input_tokens" if cli == "codex" else "cache_creation_input_tokens": 5})
            self.assertEqual(task.model_usage(cli, json.dumps(value)), {"input_tokens": 30, "output_tokens": 20, "cache_read_tokens": 12, "cache_write_tokens": 5})

    def test_duplicate_nonfinite_boolean_or_unbounded_native_usage_cannot_pass(self):
        for cli in ("codex", "claude"):
            raw = self.good_output(cli)
            for changed in (raw.replace('"input_tokens": 30', '"input_tokens": 30, "input_tokens": 30'),
                            raw.replace('"output_tokens": 20', '"output_tokens": NaN'),
                            raw.replace('"output_tokens": 20', '"output_tokens": true'),
                            raw.replace('"output_tokens": 20', '"output_tokens": 1000000001')):
                self.assertIsNone(task.model_usage(cli, changed))
            cache = "cached_input_tokens" if cli == "codex" else "cache_read_input_tokens"
            value = json.loads(raw); value["usage"][cache] = True
            self.assertIsNone(task.model_usage(cli, json.dumps(value)))

    def test_duplicate_extra_or_nonfinite_saved_answers_are_rejected(self):
        challenge = {"lhs": 17, "rhs": 26, "nonce": "a1" * 16}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "input.json").write_text(json.dumps(challenge))
            text = json.dumps({"sum": 43, "nonce": challenge["nonce"]})
            for raw in (text[:-1] + ',"sum":43}', text[:-1] + ',"extra":1}', text.replace('43', 'NaN')):
                (root / "result.json").write_text(raw)
                self.assertFalse(task.verify_result(root, challenge))
            (root / "result.json").write_text(text)
            (root / "input.json").write_text(json.dumps(challenge)[:-1] + ',"lhs":17}')
            self.assertFalse(task.verify_result(root, challenge))

    def bundle(self, cli="codex", host="127.0.0.1"):
        return {"role": cli, "provider": "openai" if cli == "codex" else "anthropic",
                "base_url": "http://" + host + ":12345/" + cli, "credential": "a" * 64}

    def good_output(self, cli):
        usage = {"input_tokens": 30, "output_tokens": 20}
        return json.dumps({"type": "turn.completed", "usage": usage} if cli == "codex" else {
            "type": "result", "subtype": "success", "is_error": False, "usage": usage, "permission_denials": []
        })

    def test_model_usage_requires_successful_real_task_events(self):
        for cli in ("codex", "claude"):
            self.assertEqual(task.model_usage(cli, self.good_output(cli)), {"input_tokens": 30, "output_tokens": 20,
                                                                         "cache_read_tokens": 0, "cache_write_tokens": 0})
            for output in ("", "done", "[]", "{}", '{"usage":{"input_tokens":30,"output_tokens":20}}'):
                self.assertIsNone(task.model_usage(cli, output))
        self.assertIsNone(task.model_usage("codex", '{"type":"turn.failed"}\n' + self.good_output("codex")))
        denied = json.loads(self.good_output("claude"))
        denied["permission_denials"] = [{"tool_name": "Write"}]
        self.assertIsNone(task.model_usage("claude", json.dumps(denied)))

    def test_wrong_sum_changed_nonce_or_changed_input_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            challenge = {"lhs": 14, "rhs": 7, "nonce": "fresh-nonce"}
            (root / "input.json").write_text(json.dumps(challenge))
            for output, expected in (({"sum": 21, "nonce": "fresh-nonce"}, True), ({"sum": 0, "nonce": "fresh-nonce"}, False), ({"sum": 21, "nonce": "stale"}, False)):
                (root / "result.json").write_text(json.dumps(output))
                self.assertEqual(task.verify_result(root, challenge), expected)
            (root / "input.json").write_text(json.dumps({**challenge, "lhs": 1}))
            (root / "result.json").write_text(json.dumps({"sum": 21, "nonce": "fresh-nonce"}))
            self.assertFalse(task.verify_result(root, challenge))

    def test_symlinked_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "outside.json").write_text('{"sum":3,"nonce":"test"}')
            (root / "result.json").symlink_to(root / "outside.json")
            self.assertFalse(task.verify_result(root, {"lhs": 1, "rhs": 2, "nonce": "test"}))

    def test_commands_are_unattended_without_permission_bypass(self):
        codex = task.command("codex", "test-model")
        self.assertIn('approval_policy="never"', codex)
        self.assertIn("workspace-write", codex)
        claude = task.command("claude", "test-model")
        self.assertIn("dontAsk", claude)
        self.assertIn("Read,Write,Edit", claude)
        for command in (codex, claude):
            self.assertNotIn("bypassPermissions", command)
            self.assertNotIn("danger-full-access", command)

    def test_root_and_invalid_key_are_rejected_before_execution(self):
        with patch.object(task.os, "geteuid", return_value=0), patch.object(task, "invoke") as invoke:
            self.assertEqual(task.run_task("codex", "test", "private", 180)["reason"], "workspace_user_required")
            invoke.assert_not_called()
        with patch.object(task.os, "geteuid", return_value=1000), patch.object(task, "invoke") as invoke:
            self.assertEqual(task.run_task("claude", "test", "bad key", 180)["reason"], "invalid_api_key")
            invoke.assert_not_called()

    def test_secret_is_only_in_single_cli_environment_and_result_is_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            original_stat = Path.stat
            def stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path == home:
                    values = list(result)
                    values[4] = 1000
                    return os.stat_result(values)
                return result
            for cli in ("codex", "claude"):
                def fake(command, directory, env, timeout):
                    self.assertNotIn("private-test-key", " ".join(command))
                    chosen = "CODEX_API_KEY" if cli == "codex" else "ANTHROPIC_API_KEY"
                    self.assertEqual(env[chosen], "private-test-key")
                    self.assertNotIn("OPENAI_API_KEY", env)
                    self.assertNotIn("ANTHROPIC_API_KEY" if cli == "codex" else "CODEX_API_KEY", env)
                    data = json.loads((directory / "input.json").read_text())
                    (directory / "result.json").write_text(json.dumps({"sum": data["lhs"] + data["rhs"], "nonce": data["nonce"]}))
                    return 0, self.good_output(cli)
                with patch.dict(os.environ, {"HOME": temp, "OPENAI_API_KEY": "unrelated-key"}), patch.object(
                    task.os, "geteuid", return_value=1000
                ), patch.object(Path, "stat", stat), patch.object(task, "invoke", side_effect=fake):
                    result = task.run_task(cli, "test-model", "private-test-key", 180)
                self.assertEqual(result["status"], "pass")
                self.assertTrue(result["verified"])
                self.assertNotIn("private-test-key", json.dumps(result))
                self.assertNotIn("private-test-key", "".join(p.read_text() for p in home.rglob("*.json")))

    def test_gateway_bundles_accept_only_same_role_and_private_local_route(self):
        for cli in ("codex", "claude"):
            for host in ("127.0.0.1", "host.docker.internal"):
                bundle = self.bundle(cli, host)
                self.assertEqual(task.gateway_client(json.dumps(bundle), cli), bundle)
            for changed in ({"role": "hermes"}, {"provider": "anthropic" if cli == "codex" else "openai"},
                            {"credential": "real-provider-key"}, {"credential": "A" * 64}, {"extra": "unknown"},
                            {"base_url": "https://api.openai.com/v1"}, {"base_url": "http://localhost:12345/" + cli},
                            {"base_url": "http://127.0.0.1:65536/" + cli}, {"base_url": "http://127.0.0.1:0/" + cli},
                            {"base_url": "http://user@127.0.0.1:12345/" + cli},
                            {"base_url": self.bundle(cli)["base_url"] + "?credential=private"},
                            {"base_url": self.bundle(cli)["base_url"] + "/v1"}, {"base_url": 12}):
                with self.subTest(cli=cli, fields=list(changed)), self.assertRaises(ValueError):
                    task.gateway_client(json.dumps(self.bundle(cli) | changed), cli)

    def test_ambiguous_oversized_or_wrong_input_never_starts_gateway_task(self):
        original = json.dumps(self.bundle())
        invalid = ["private-provider-key", "[]", "null", "{}", "[NaN]", " " * 4097,
                   original[:-1] + ',"credential":"' + "b" * 64 + '"}',
                   json.dumps(self.bundle() | {"credential": None}), "[" * 1000 + "]" * 1000]
        with patch.object(task.os, "geteuid", return_value=1000), patch.object(task, "invoke") as invoke:
            for raw in invalid:
                self.assertEqual(task.run_task("codex", "test-model", raw, 180, model_gateway=True)["reason"], "invalid_model_gateway")
            invoke.assert_not_called()

    def test_gateway_commands_use_explicit_auth_route_without_keys_or_automatic_codex_retries(self):
        bundle = self.bundle()
        args = task.command("codex", "test-model", bundle)
        settings = {}
        for index, arg in enumerate(args):
            if arg == "-c":
                settings.update(tomllib.loads(args[index + 1]))
        provider = settings["model_providers"][settings["model_provider"]]
        self.assertEqual(provider["base_url"], bundle["base_url"] + "/v1")
        self.assertEqual(provider["env_key"], "AGENT_STACK_MODEL_CREDENTIAL")
        self.assertFalse(provider["requires_openai_auth"])
        self.assertFalse(provider["supports_websockets"])
        self.assertEqual((provider["request_max_retries"], provider["stream_max_retries"]), (0, 0))
        self.assertEqual(settings["web_search"], "disabled")
        self.assertNotIn(bundle["credential"], " ".join(args))
        claude = task.command("claude", "test-model", self.bundle("claude"))
        self.assertNotIn("--max-budget-usd", claude)
        self.assertIn("--max-budget-usd", task.command("claude", "test-model"))
        self.assertEqual(claude[claude.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", claude)

    def test_actual_fake_cli_processes_get_isolated_homes_and_no_inherited_auth_or_proxy(self):
        # These executables are fixtures, not native compatibility proof. They
        # run as real child processes and create independently checked files.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"; home.mkdir()
            binaries = root / "bin"; binaries.mkdir()
            fixture = '''#!/usr/bin/env python3
import json, os, pathlib, sys, tomllib
cli = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
assert "inherited-private" not in str(os.environ)
assert "a" * 64 not in " ".join(args)
assert pathlib.Path(os.environ["HOME"]).parent == pathlib.Path.cwd()
assert not list(pathlib.Path(os.environ["HOME"]).iterdir())
if cli == "codex":
    settings = {}
    for i, arg in enumerate(args):
        if arg == "-c": settings.update(tomllib.loads(args[i + 1]))
    provider = settings["model_providers"][settings["model_provider"]]
    assert os.environ[provider["env_key"]] == "a" * 64
    assert provider["base_url"] == "http://127.0.0.1:12345/codex/v1"
    assert provider["requires_openai_auth"] is False
    assert os.environ["CODEX_HOME"].startswith(os.environ["HOME"] + "/")
else:
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "a" * 64
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345/claude"
    assert os.environ["CLAUDE_CONFIG_DIR"].startswith(os.environ["HOME"] + "/")
    assert os.environ["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] == "1"
    assert "--max-budget-usd" not in args
challenge = json.loads(pathlib.Path("input.json").read_text())
pathlib.Path("result.json").write_text(json.dumps({"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]}))
usage = {"input_tokens": 30, "output_tokens": 20}
print(json.dumps({"type": "turn.completed", "usage": usage} if cli == "codex" else {"type": "result", "subtype": "success", "is_error": False, "usage": usage}))
'''
            for cli in ("codex", "claude"):
                binary = binaries / cli; binary.write_text(fixture); binary.chmod(0o700)
            old = {key: "inherited-private" for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                   "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "HTTP_PROXY", "https_proxy",
                   "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "OPENAI_BASE_URL", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
                   "NODE_OPTIONS", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS")}
            old.update(HOME=str(home), PATH=str(binaries) + os.pathsep + os.environ["PATH"])
            with patch.dict(os.environ, old), owned_home(home):
                for cli in ("codex", "claude"):
                    result = task.run_task(cli, "test-model", json.dumps(self.bundle(cli)), 10, model_gateway=True)
                    self.assertEqual(result["status"], "pass", result)
                    self.assertTrue(result["verified"])
                    self.assertEqual(result["model_route"]["kind"], "budget_gateway")
                    self.assertNotIn("a" * 64, json.dumps(result))
                    directory = home / result["fixture"]
                    self.assertEqual({p.name for p in directory.iterdir()}, {"input.json", "result.json"})
                    self.assertNotIn("a" * 64, "".join(p.read_text() for p in directory.iterdir()))
            self.assertNotEqual(*[p.name for p in (home / ".agent-stack/acceptance").iterdir()])

    def test_gateway_client_home_is_removed_after_failure_timeout_or_exception(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}), owned_home(home):
            for outcome in ((1, ""), (124, ""), OSError("fixture failure")):
                homes = []
                def fake(args, directory, env, timeout):
                    homes.append(Path(env["HOME"]))
                    (homes[-1] / "native-cache").write_text("fixture private cache")
                    if isinstance(outcome, Exception): raise outcome
                    return outcome
                with patch.object(task, "invoke", side_effect=fake):
                    if isinstance(outcome, Exception):
                        with self.assertRaises(OSError):
                            task.run_task("codex", "test-model", json.dumps(self.bundle()), 10, model_gateway=True)
                    else:
                        result = task.run_task("codex", "test-model", json.dumps(self.bundle()), 10, model_gateway=True)
                        self.assertEqual(result["status"], "fail")
                self.assertEqual(len(homes), 1)
                self.assertFalse(homes[0].exists())

    def test_gateway_main_reads_private_bundle_from_stdin_and_rejects_raw_key(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["task", "--cli", "codex", "--model", "test-model", "--model-gateway"]), \
             patch.object(task.os, "geteuid", return_value=1000), \
             patch.object(sys, "stdin", io.StringIO("fixture-private-provider-key")), contextlib.redirect_stdout(output):
            status = task.main()
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "invalid_model_gateway")
        self.assertNotIn("fixture-private-provider-key", output.getvalue())
