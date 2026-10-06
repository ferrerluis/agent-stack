import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("workspace_cli_task", ROOT / "modules/common/runtime/workspace_cli_task.py")
task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task)


class WorkspaceTaskTest(unittest.TestCase):
    def good_output(self, cli):
        usage = {"input_tokens": 30, "output_tokens": 20}
        return json.dumps({"type": "turn.completed", "usage": usage} if cli == "codex" else {
            "type": "result", "subtype": "success", "is_error": False, "usage": usage, "permission_denials": []
        })

    def test_model_usage_requires_successful_real_task_events(self):
        for cli in ("codex", "claude"):
            self.assertEqual(task.model_usage(cli, self.good_output(cli)), {"input_tokens": 30, "output_tokens": 20})
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
