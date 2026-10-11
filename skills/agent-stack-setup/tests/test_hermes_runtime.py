import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "modules/common/runtime" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = load("hermes_config")
health = load("hermes_health")


class HermesConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "config.yaml"

    def test_fresh_config_uses_selected_model_and_private_permissions(self):
        self.assertTrue(config.seed_config(self.root, {"model_ref": "openai/gpt-test"}))
        self.assertEqual(json.loads(self.file.read_text())["model"], {
            "provider": "custom", "default": "gpt-test", "base_url": "https://api.openai.com/v1"
        })
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(self.file.stat().st_uid, 10000 if os.geteuid() == 0 else os.geteuid())

    def test_auto_and_preserve_leave_operator_yaml_unchanged(self):
        original = "model:\n  default: chosen-by-operator\nchannels:\n  telegram: enabled\n"
        self.file.write_text(original)
        for mode in ("auto", "preserve"):
            self.assertFalse(config.seed_config(self.root, {"model_ref": "invalid"}, mode))
            self.assertEqual(self.file.read_text(), original)

    def test_manage_changes_only_model(self):
        self.file.write_text(json.dumps({"model": {"default": "old"}, "channels": {"telegram": True}}))
        self.assertTrue(config.seed_config(self.root, {"model_ref": "anthropic/claude-test"}, "manage"))
        result = json.loads(self.file.read_text())
        self.assertEqual(result["channels"], {"telegram": True})
        self.assertEqual(result["model"], {"provider": "anthropic", "default": "claude-test"})

    def test_manage_yaml_fails_without_truncating_operator_config(self):
        original = "model:\n  default: old\n"
        self.file.write_text(original)
        with self.assertRaises(ValueError):
            config.seed_config(self.root, {"model_ref": "anthropic/test"}, "manage")
        self.assertEqual(self.file.read_text(), original)

    def test_invalid_model_does_not_create_config(self):
        for model in ("no-prefix", "other/model", "anthropic/"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                config.seed_config(self.root, {"model_ref": model})
            self.assertFalse(self.file.exists())

    def test_manage_refuses_symlink_without_changing_target(self):
        target = self.root / "operator.json"
        target.write_text('{"model":{"default":"old"}}')
        self.file.symlink_to(target)
        with self.assertRaises(ValueError):
            config.seed_config(self.root, {"model_ref": "anthropic/test"}, "manage")
        self.assertEqual(target.read_text(), '{"model":{"default":"old"}}')

    def test_missing_command_link_is_installed_once_for_the_runtime_user(self):
        self.assertTrue(config.install_command_link(self.root))
        link = self.root / ".local/bin/hermes"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), config.CLI_TARGET)
        self.assertEqual(link.lstat().st_uid, 10000 if os.geteuid() == 0 else os.geteuid())
        self.assertEqual((link.parent.stat().st_mode & 0o777), 0o700)
        before = link.lstat()
        self.assertFalse(config.install_command_link(self.root))
        self.assertEqual(link.lstat(), before)

    def test_operator_command_files_and_dangling_links_are_preserved(self):
        link = self.root / ".local/bin/hermes"
        link.parent.mkdir(parents=True)
        link.write_text("operator wrapper\n")
        link.chmod(0o750)
        before = link.stat()
        self.assertFalse(config.install_command_link(self.root))
        self.assertEqual(link.read_text(), "operator wrapper\n")
        self.assertEqual(link.stat(), before)
        link.unlink()
        link.symlink_to("operator-missing-target")
        before = link.lstat()
        self.assertFalse(config.install_command_link(self.root))
        self.assertEqual(os.readlink(link), "operator-missing-target")
        self.assertEqual(link.lstat(), before)

    def test_symlinked_home_or_command_directories_cannot_redirect_installation(self):
        outside = self.root / "outside"
        outside.mkdir()
        home = self.root / "home"
        home.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            config.install_command_link(home)
        self.assertFalse((outside / ".local").exists())
        for parent in (self.root / ".local", self.root / ".local/bin"):
            parent.parent.mkdir(exist_ok=True)
            parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                config.install_command_link(self.root)
            self.assertFalse((outside / "hermes").exists())
            parent.unlink()

    def test_preserved_operator_yaml_still_receives_the_missing_command_link(self):
        self.file.write_text("model:\n  default: operator-model\n")
        seed = self.root / "seed.json"
        seed.write_text('{"model_ref":"openai/fixture"}')
        with patch("sys.argv", ["hermes_config.py", "--data-dir", str(self.root), "--seed", str(seed), "--mode", "preserve"]), contextlib.redirect_stdout(io.StringIO()):
            config.main()
        self.assertEqual(self.file.read_text(), "model:\n  default: operator-model\n")
        self.assertEqual(os.readlink(self.root / ".local/bin/hermes"), config.CLI_TARGET)


class HermesReadinessTest(unittest.TestCase):
    def good(self):
        return {"status": "ok", "readiness": {"checks": {name: {"status": "ok"} for name in health.REQUIRED_CHECKS}}}

    def test_complete_green_readiness_passes(self):
        self.assertTrue(health.healthy(self.good()))

    def test_degraded_or_incomplete_readiness_fails(self):
        for mutate in (
            lambda p: p.update(status="degraded"),
            lambda p: p["readiness"]["checks"].pop("model"),
            lambda p: p["readiness"]["checks"]["gateway"].update(status="degraded"),
            lambda p: p["readiness"]["checks"].update(model=True),
            lambda p: p["readiness"].update(checks=[]),
        ):
            payload = self.good()
            mutate(payload)
            self.assertFalse(health.healthy(payload))
        for payload in ({"status": "ok"}, None, [], "ok"):
            self.assertFalse(health.healthy(payload))

    def test_http_200_is_not_enough_and_auth_is_bounded(self):
        response = io.BytesIO(json.dumps({"status": "degraded"}).encode())
        response.status = 200
        output = io.StringIO()
        with patch.dict(os.environ, {"API_SERVER_KEY": "private-test-key"}), patch.object(
            health.urllib.request, "urlopen", return_value=response
        ) as request, contextlib.redirect_stdout(output):
            self.assertEqual(health.main(), 1)
        args, kwargs = request.call_args
        self.assertEqual(args[0].get_header("Authorization"), "Bearer private-test-key")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertNotIn("private-test-key", output.getvalue())

    def test_missing_auth_and_raw_errors_cannot_leak_or_pass(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(health.main(), 1)
        self.assertIn("missing_api_key", output.getvalue())
        output = io.StringIO()
        with patch.dict(os.environ, {"API_SERVER_KEY": "private-test-key"}), patch.object(
            health.urllib.request, "urlopen", side_effect=OSError("private-test-key")
        ), contextlib.redirect_stdout(output):
            self.assertEqual(health.main(), 1)
        self.assertNotIn("private-test-key", output.getvalue())


class InstallerReadinessTest(unittest.TestCase):
    def check(self, hermes_state):
        source = (ROOT / "modules/common/templates/runtime/install-agent-stack.sh.tpl").read_text()
        start = source.index("wait_agent_stack_initial_restart() {")
        function = source[start:source.index("\n}\n", start) + 3]
        function = function.replace('${hermes_npm_security_patch_enabled ? 180 : 60}', '60')
        values = {"openclaw_enabled": "true", "workspace_enabled": "true", "hermes_enabled": "true",
                  "hermes_api_server_enabled": "true", "workspace_codex_auto_update_enabled": "false"}
        function = re.sub(r"(?<!\$)\$\{([a-z_]+)\}", lambda m: values.get(m[1], "test"), function).replace("$${", "${")
        mocks = '''
app=/not-a-real-deployment
systemctl() { echo active; }
seq() { echo 1; }
sleep() { :; }
docker() {
  if [ "$1" = compose ]; then
    case "${@: -1}" in hermes) [ "$HERMES_STATE" = missing ] || echo hermes;; *) echo other;; esac
  elif [ "$1" = inspect ]; then
    if [ "${@: -1}" = hermes ]; then echo "$HERMES_STATE"; else echo healthy; fi
  fi
}
'''
        return subprocess.run(["bash", "-c", mocks + function + "\nwait_agent_stack_initial_restart"],
                              env={**os.environ, "HERMES_STATE": hermes_state}, capture_output=True, text=True)

    def test_missing_and_unhealthy_hermes_block_install(self):
        for state in ("missing", "starting", "unhealthy", "running"):
            with self.subTest(state=state):
                self.assertNotEqual(self.check(state).returncode, 0)

    def test_both_runtimes_and_workspace_ready_allow_install(self):
        self.assertEqual(self.check("healthy").returncode, 0)
