"""Exercise the real layout script and native directory installation."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class WorkspaceLayoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.data = self.app / "data"
        self.legacy = self.root / "legacy"
        self.script = self.root / "migrate.sh"
        self.script.write_text((ROOT / "modules/common/templates/runtime/agent-stack-migrate-layout.sh.tpl").read_text().replace("$${", "${"))
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "AGENT_STACK_APP_ROOT": str(self.app), "AGENT_STACK_DATA_ROOT": str(self.data),
                    "AGENT_STACK_LEGACY_ROOT": str(self.legacy)}

    def migrate(self):
        result = subprocess.run(["bash", str(self.script)], env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def install_workspace_home(self):
        result = subprocess.run(["install", "-d", "-m", "0755", str(self.data / "workspace"),
                                 str(self.data / "workspace/home")], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_fresh_volume_can_install_persistent_workspace_before_openclaw_starts(self):
        self.migrate()
        self.assertTrue((self.data / "workspace").is_symlink())
        self.install_workspace_home()
        home = self.data / "workspace/home"
        (home / "result.txt").write_text("fresh coding workspace result")
        self.assertEqual(home.resolve(), self.data / "openclaw/workspace/home")
        self.migrate()
        self.install_workspace_home()
        self.assertEqual((home / "result.txt").read_text(), "fresh coding workspace result")

    def test_legacy_workspace_moves_with_its_existing_files_and_home(self):
        old = self.data / "workspace"
        (old / "home").mkdir(parents=True)
        (old / "notes.md").write_text("existing agent notes")
        (old / "home/task.txt").write_text("existing coding task")
        self.migrate()
        self.install_workspace_home()
        self.assertTrue(old.is_symlink())
        self.assertEqual((self.data / "openclaw/workspace/notes.md").read_text(), "existing agent notes")
        self.assertEqual((old / "home/task.txt").read_text(), "existing coding task")
        self.migrate()
        self.assertEqual((old / "notes.md").read_text(), "existing agent notes")

    def test_existing_canonical_workspace_is_not_overwritten(self):
        target = self.data / "openclaw/workspace"
        target.mkdir(parents=True)
        (target / "existing.txt").write_text("keep this")
        self.migrate()
        self.install_workspace_home()
        self.assertEqual((self.data / "workspace/existing.txt").read_text(), "keep this")

    def test_unrecognized_existing_link_is_preserved(self):
        self.data.mkdir(parents=True)
        target = self.root / "operator-owned"
        target.mkdir()
        (target / "keep.txt").write_text("operator data")
        (self.data / "workspace").symlink_to(target)
        (self.data / ".agent-stack-layout-version").write_text("1\n")
        self.migrate()
        self.assertEqual((self.data / "workspace").resolve(), target)
        self.assertEqual((target / "keep.txt").read_text(), "operator data")
