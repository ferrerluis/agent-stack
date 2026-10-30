"""Run the installer functions and check every Dockerfile COPY source."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "modules/common/templates/runtime"


class WorkspaceImageContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.previous = self.root / "previous"
        self.previous.mkdir()
        source = (RUNTIME / "install-agent-stack.sh.tpl").read_text()
        self.functions = "\n".join(re.search(r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S)[0]
                                   for name in ("build_workspace_image", "restore_workspace_image"))
        self.functions = self.functions.replace("${workspace_enabled}", "true")
        for name in ("workspace.Dockerfile", "workspace-entrypoint.sh", "workspace-codex-update.sh", "workspace-codex-control.py"):
            (self.app / name).write_text((RUNTIME / (name + ".tpl")).read_text())
        (self.app / "workspace-para-memory-drive-skill.md").write_text("fixture skill")
        (self.app / "codex-install.py").write_bytes((ROOT / "modules/common/runtime/codex_install.py").read_bytes())
        self.codex_installer = (ROOT / "modules/common/runtime/codex_install.py").read_bytes()
        self.task = (ROOT / "modules/common/runtime/workspace_cli_task.py").read_bytes()
        (self.app / "workspace-cli-task.py").write_bytes(self.task)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        docker = self.tools / "docker"
        docker.write_text("#!/usr/bin/env python3\n" + '''import hashlib,json,os,pathlib,shlex,sys
if sys.argv[1] != 'build':
    sys.exit(1)
context=pathlib.Path(sys.argv[-1])
sources=[]
for line in (context/'Dockerfile').read_text().splitlines():
    if line.startswith('COPY '):
        for source in shlex.split(line)[1:-1]:
            path=context/source
            assert path.is_file(), 'missing Dockerfile COPY source: '+source
            sources.append(source)
task=context/'workspace-cli-task.py'
(context.parent/'context-proof.json').write_text(json.dumps({'sources':sources,
    'task_sha256':hashlib.sha256(task.read_bytes()).hexdigest() if task.exists() else None,
    'task_mode':task.stat().st_mode & 0o777 if task.exists() else None}))
sys.exit(int(os.environ.get('FIXTURE_DOCKER_EXIT','0')))
''')
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))

    def run_function(self, command, **env):
        script = ("set -euo pipefail\napp=" + shlex.quote(str(self.app)) + "\nprevious="
                  + shlex.quote(str(self.previous)) + "\n" + self.functions + "\n" + command + "\n")
        return subprocess.run(["bash", "-c", script], env=self.env | env, capture_output=True, timeout=15)

    def test_fresh_image_and_restore_package_every_declared_source(self):
        for command in ("build_workspace_image", "restore_workspace_image"):
            with self.subTest(command=command):
                result = self.run_function(command)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                proof = json.loads((self.app / "context-proof.json").read_text())
                self.assertIn("codex-install.py", proof["sources"])
                self.assertEqual((self.app / ".workspace-image-context/codex-install.py").exists(), False)
                self.assertEqual((self.app / "codex-install.py").read_bytes(), self.codex_installer)
                self.assertIn("workspace-cli-task.py", proof["sources"])
                self.assertEqual(proof["task_sha256"], hashlib.sha256(self.task).hexdigest())
                self.assertEqual(proof["task_mode"], 0o755)
                self.assertEqual((self.app / "workspace-cli-task.py").read_bytes(), self.task)
                self.assertFalse((self.app / ".workspace-image-context").exists())

    def test_missing_current_task_source_refuses_build_even_inside_a_condition(self):
        (self.app / "workspace-cli-task.py").unlink()
        for function in ("build_workspace_image", "restore_workspace_image"):
            with self.subTest(function=function):
                result = self.run_function("if " + function + "; then exit 81; fi")
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertFalse((self.app / "context-proof.json").exists())

    def test_failed_docker_build_cannot_be_hidden_by_context_cleanup(self):
        for function in ("build_workspace_image", "restore_workspace_image"):
            with self.subTest(function=function):
                result = self.run_function("if " + function + "; then exit 81; fi", FIXTURE_DOCKER_EXIT="42")
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertTrue((self.app / ".workspace-image-context/workspace-cli-task.py").is_file())

    def test_old_rollback_dockerfile_can_build_without_a_task_helper(self):
        (self.app / "workspace-cli-task.py").unlink()
        path = self.app / "workspace.Dockerfile"
        path.write_text("\n".join(line for line in path.read_text().splitlines()
                                   if "workspace-cli-task" not in line) + "\n")
        result = self.run_function("restore_workspace_image")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        proof = json.loads((self.app / "context-proof.json").read_text())
        self.assertNotIn("workspace-cli-task.py", proof["sources"])

    def test_missing_current_codex_installer_refuses_build(self):
        (self.app / "codex-install.py").unlink()
        for function in ("build_workspace_image", "restore_workspace_image"):
            with self.subTest(function=function):
                result = self.run_function("if " + function + "; then exit 81; fi")
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertFalse((self.app / "context-proof.json").exists())

    def test_old_rollback_dockerfile_can_build_without_codex_installer(self):
        (self.app / "codex-install.py").unlink()
        path = self.app / "workspace.Dockerfile"
        path.write_text("\n".join(line for line in path.read_text().splitlines()
                                   if "codex-install.py" not in line) + "\n")
        result = self.run_function("restore_workspace_image")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn("codex-install.py", json.loads((self.app / "context-proof.json").read_text())["sources"])
