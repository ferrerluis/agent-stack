import importlib.util
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
SCRIPT = ROOT / "modules/common/runtime/openclaw_config.py"
spec = importlib.util.spec_from_file_location("openclaw_config", SCRIPT)
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class OpenClawSeedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "openclaw.json"
        self.seed = {"gateway_bind": "lan", "gateway_token": "private-fixture-token"}

    def test_first_start_has_local_gateway_auth_and_private_owner(self):
        self.assertTrue(config.seed_config(self.root, self.seed))
        gateway = json.loads(self.file.read_text())["gateway"]
        self.assertEqual(gateway["mode"], "local")
        self.assertEqual(gateway["bind"], "lan")
        self.assertEqual(gateway["auth"], {"mode": "token", "token": self.seed["gateway_token"]})
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(self.file.stat().st_uid, 1000 if os.geteuid() == 0 else os.geteuid())
        self.assertFalse(list(self.root.glob(".openclaw-*")))

    def test_existing_operator_config_is_preserved_even_with_invalid_seed(self):
        original = b'{ "agents": {"model": "operator-selected"} }\n'
        self.file.write_bytes(original)
        self.assertFalse(config.seed_config(self.root, None))
        self.assertEqual(self.file.read_bytes(), original)

    def test_symlink_and_nonfile_are_refused_without_touching_target(self):
        target = self.root / "operator-config"
        target.write_text("operator")
        self.file.symlink_to(target)
        with self.assertRaises(ValueError):
            config.seed_config(self.root, self.seed)
        self.assertEqual(target.read_text(), "operator")
        self.file.unlink()
        self.file.mkdir()
        with self.assertRaises(ValueError):
            config.seed_config(self.root, self.seed)

    def test_invalid_seed_does_not_create_a_config_or_echo_secret(self):
        for seed in (None, [], {}, {**self.seed, "gateway_bind": "0.0.0.0"}, {**self.seed, "gateway_token": ""}):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                config.seed_config(self.root, seed)
            self.assertFalse(self.file.exists())
        source = self.root / "seed.json"
        source.write_text(json.dumps({**self.seed, "gateway_bind": "invalid"}))
        result = subprocess.run(["python3", str(SCRIPT), "--data-dir", str(self.root), "--seed", str(source)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.seed["gateway_token"], result.stdout + result.stderr)

    def test_concurrent_operator_write_is_not_replaced(self):
        def concurrent_write(source, target):
            target.write_text("written-by-operator")
            raise FileExistsError()
        with patch.object(config.os, "link", side_effect=concurrent_write):
            self.assertFalse(config.seed_config(self.root, self.seed))
        self.assertEqual(self.file.read_text(), "written-by-operator")
        self.assertFalse(list(self.root.glob(".openclaw-*")))


class OpenClawInstallerTest(unittest.TestCase):
    def source(self):
        return (ROOT / "modules/common/templates/runtime/install-agent-stack.sh.tpl").read_text()

    def test_auto_manages_fresh_seed_and_preserves_existing_config(self):
        source = self.source()
        seed = source[source.index('OPENCLAW_CONFIG="$app/data/openclaw/openclaw.json"'):source.index("hermes_config_changed=0")]
        mode = source[source.index('OPENCLAW_CONFIG_MODE_INPUT="'):source.index('AGENT_CHANNEL="')]
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temp:
                app = Path(temp)
                data = app / "data/openclaw"
                data.mkdir(parents=True)
                original = '{"agents":{"default":"operator"}}'
                if existing:
                    (data / "openclaw.json").write_text(original)
                (app / "openclaw_config.py").write_text(SCRIPT.read_text())
                (app / "openclaw-seed.json").write_text(json.dumps({"gateway_bind": "lan", "gateway_token": "fixture"}))
                script = seed + mode + '\nprintf "%s %s" "$OPENCLAW_CONFIG_MODE_EFFECTIVE" "$openclaw_seed_changed"\n'
                values = {"openclaw_enabled": "true", "openclaw_config_mode": "auto"}
                script = re.sub(r"(?<!\$)\$\{([a-z_]+)\}", lambda m: values[m[1]], script).replace("$${", "${")
                result = subprocess.run(["bash", "-eu", "-c", script], env={**os.environ, "app": temp, "staging": temp}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "preserve 0" if existing else "manage 1")
                if existing:
                    self.assertEqual((data / "openclaw.json").read_text(), original)

    def test_failed_initial_start_removes_only_new_seed_and_restores_helper(self):
        source = self.source()
        block = source[source.index("if ! wait_agent_stack_initial_restart; then"):source.index("systemctl start openclaw || true")]
        for changed in ("0", "1"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                app = Path(temp)
                previous = app / "previous"
                previous.mkdir()
                file = app / "openclaw.json"
                file.write_text("prior-or-new-config")
                (app / "openclaw_config.py").write_text("new-helper")
                (previous / "openclaw_config.py").write_text("previous-helper")
                mocks = "wait_agent_stack_initial_restart() { return 1; }; restore_workspace_image() { :; }; systemctl() { :; }; fail() { exit 1; };\n"
                result = subprocess.run(["bash", "-eu", "-c", mocks + block], env={**os.environ, "app": temp, "previous": str(previous),
                     "OPENCLAW_CONFIG": str(file), "openclaw_seed_changed": changed, "hermes_config_changed": "0"}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(file.exists(), changed == "0")
                self.assertEqual((app / "openclaw_config.py").read_text(), "previous-helper")
