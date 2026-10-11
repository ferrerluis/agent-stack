import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("hermes_npm_patch", ROOT / "modules/common/runtime/hermes_npm_patch.py")
npm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(npm)


class HermesNpmPatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "hermes"
        self.root.mkdir(mode=0o755)
        (self.root / ".hermes_build_sha").write_text(npm.REVISION + "\n")
        (self.root / ".hermes_build_sha").chmod(0o644)
        (self.root / ".npmrc").write_bytes(b"engine-strict=true\n")
        (self.root / ".npmrc").chmod(0o644)
        self.bundle = Path(temp.name) / "bundle.json"
        self.data = {"schema": 1, "upstream_revision": npm.REVISION, "files": {},
                     "installed_versions": {"node_modules/example": "1.2.3"},
                     "npm_config_sha256": npm.sha(b"engine-strict=true\n")}
        for name in npm.FILES:
            path = self.root / name
            path.parent.mkdir(mode=0o755, exist_ok=True)
            original, corrected = b'{"name":"original"}\n', '{"name":"patched"}\n'
            path.write_bytes(original)
            path.chmod(0o644)
            self.data["files"][name] = {"original_sha256": npm.sha(original),
                "patched_sha256": npm.sha(corrected.encode()), "content": corrected}
        self.bundle.write_text(json.dumps(self.data))
        self.bundle.chmod(0o644)
        self.addCleanup(patch.stopall)
        patch.object(npm, "BUNDLE_SHA256", npm.sha(self.bundle.read_bytes())).start()

    def instance(self):
        return npm.Patch(self.root, self.bundle, owner=os.geteuid())

    def install(self, command, cwd):
        path = self.root / "node_modules/example/package.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version":"1.2.3"}')
        for name in npm.OUTPUTS:
            directory = self.root / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "entry.js").write_text("built from corrected dependencies\n")
        for path in self.root.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)

    def test_repair_and_reboot_verify_built_outputs_without_reinstalling(self):
        runner = Mock(side_effect=self.install)
        result = self.instance().apply(runner)
        self.assertEqual(result["bundle_sha256"], npm.BUNDLE_SHA256)
        self.assertEqual(runner.call_count, 3)
        before = (self.root / npm.MARKER).stat()
        runner.reset_mock()
        self.assertEqual(self.instance().apply(runner), result)
        runner.assert_not_called()
        self.assertEqual((self.root / npm.MARKER).stat(), before)
        (self.root / npm.OUTPUTS[0] / "entry.js").write_text("changed after verification")
        with self.assertRaises(ValueError):
            self.instance().apply(runner)
        runner.assert_not_called()

    def test_wrong_source_or_modified_manifest_refuses_before_writing(self):
        (self.root / "package.json").write_text("operator change")
        before = (self.root / "package-lock.json").read_bytes()
        with self.assertRaises(ValueError):
            self.instance()
        self.assertEqual((self.root / "package-lock.json").read_bytes(), before)
        (self.root / "package.json").write_text('{"name":"original"}\n')
        (self.root / ".hermes_build_sha").write_text("other revision")
        with self.assertRaises(ValueError):
            self.instance()

    def test_missing_or_modified_project_npm_policy_refuses_before_manifest_changes(self):
        before = (self.root / "package-lock.json").read_bytes()
        (self.root / ".npmrc").write_text("engine-strict=false\n")
        with self.assertRaises(ValueError):
            self.instance()
        self.assertEqual((self.root / "package-lock.json").read_bytes(), before)
        (self.root / ".npmrc").unlink()
        with self.assertRaises(FileNotFoundError):
            self.instance()
        self.assertEqual((self.root / "package-lock.json").read_bytes(), before)

    def test_untrusted_bundle_and_symlinked_parent_cannot_change_external_files(self):
        self.bundle.write_text(self.bundle.read_text() + " ")
        with self.assertRaises(ValueError):
            self.instance()
        self.bundle.write_text(json.dumps(self.data))
        directory = self.root / "web"
        external = self.root.parent / "external"
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
        before = (external / "package.json").read_bytes()
        with self.assertRaises(ValueError):
            self.instance()
        self.assertEqual((external / "package.json").read_bytes(), before)

    def test_failed_build_or_wrong_installed_version_never_writes_success_marker(self):
        runner = Mock(side_effect=[None, ValueError("build failed")])
        with self.assertRaises(ValueError):
            self.instance().apply(runner)
        self.assertFalse((self.root / npm.MARKER).exists())
        self.install([], self.root)
        (self.root / "node_modules/example/package.json").write_text('{"version":"1.2.2"}')
        with self.assertRaises(ValueError):
            self.instance().apply(Mock())
        self.assertFalse((self.root / npm.MARKER).exists())

    def test_children_receive_no_app_credentials_or_operator_npm_configuration(self):
        home = self.root.parent / "npm-home"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "private-fixture", "OPENAI_API_KEY": "private-fixture",
                                    "npm_config_userconfig": "/operator/credentials"}):
            runner = npm.Runner(home, owner=os.geteuid())
        process = Mock(returncode=0)
        process.communicate.return_value = (b"", None)
        with patch.object(npm.subprocess, "Popen", return_value=process) as launch:
            runner(["npm", "ci"], self.root)
        environment = launch.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(environment["npm_config_userconfig"], str(home / "user.npmrc"))
        self.assertEqual(environment["npm_config_fetch_retries"], "2")
        self.assertEqual(environment["npm_config_fetch_retry_mintimeout"], "1000")
        self.assertEqual(environment["npm_config_fetch_retry_maxtimeout"], "3000")
        self.assertEqual(environment["npm_config_fetch_timeout"], "30000")
        self.assertEqual((home / "user.npmrc").read_bytes(), b"")
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_timeout_kills_installation_process_group_and_cannot_continue(self):
        runner = npm.Runner(self.root.parent / "npm-home", seconds=1, owner=os.geteuid())
        process = Mock(pid=1234567)
        process.communicate.side_effect = [subprocess.TimeoutExpired("npm", 1), (b"", None)]
        with patch.object(npm.subprocess, "Popen", return_value=process), patch.object(npm.os, "killpg") as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                runner(["npm", "ci"], self.root)
        kill.assert_called_once_with(process.pid, npm.signal.SIGKILL)

    def test_retries_share_the_install_and_build_deadline(self):
        with patch.object(npm.time, "monotonic", return_value=100):
            runner = npm.Runner(self.root.parent / "npm-home", seconds=480, owner=os.geteuid())
        process = Mock(returncode=0)
        process.communicate.return_value = (b"", None)
        with patch.object(npm.subprocess, "Popen", return_value=process) as launch, \
                patch.object(npm.time, "monotonic", side_effect=[570, 581]):
            runner(["npm", "ci"], self.root)
            process.communicate.assert_called_once_with(timeout=10)
            with self.assertRaises(ValueError):
                runner(["npm", "run", "build"], self.root)
        launch.assert_called_once()

    def test_failure_details_report_exact_stage_and_numeric_facts_without_exception_text(self):
        runner = npm.Runner(self.root.parent / "npm-home", owner=os.geteuid())
        process = Mock(returncode=-9)
        process.communicate.return_value = (b"", None)
        with patch.object(npm.subprocess, "Popen", return_value=process):
            with self.assertRaises(ValueError) as failure:
                runner(["npm", "ci"], self.root)
        self.assertEqual(npm.failure_details(failure.exception, "npm_install", runner),
                         {"stage": "npm_install", "kind": "command_exit", "returncode": -9})
        self.assertEqual(npm.failure_details(subprocess.TimeoutExpired("private-fixture", 1), "web_build", None),
                         {"stage": "web_build", "kind": "timeout"})
        self.assertEqual(npm.failure_details(FileNotFoundError(2, "private-fixture"), "installed_verify", runner),
                         {"stage": "installed_verify", "kind": "os_error", "errno": 2})
        instance = self.instance()
        def fail(command, cwd):
            raise PermissionError(13, "private-fixture")
        with patch.object(npm, "Patch", return_value=instance), patch.object(npm, "Runner", return_value=fail), \
                patch.object(npm.os, "geteuid", return_value=0), patch.object(npm.sys, "argv", ["patch"]), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(npm.main(), 1)
        self.assertEqual(instance.stage, "npm_install")
        self.assertIn('"stage": "npm_install"', output.getvalue())
        self.assertIn('"errno": 13', output.getvalue())
        self.assertNotIn("private-fixture", output.getvalue())
        self.assertFalse((self.root / npm.MARKER).exists())

    def test_repository_bundle_contains_only_verified_public_patch_inputs(self):
        path = ROOT / "modules/common/runtime/hermes_npm_patch.json"
        data = json.loads(path.read_bytes())
        self.assertEqual(set(data["files"]), npm.FILES)
        self.assertEqual(data["upstream_revision"], npm.REVISION)
        for entry in data["files"].values():
            self.assertEqual(npm.sha(entry["content"].encode()), entry["patched_sha256"])
        root_package = json.loads(data["files"]["package.json"]["content"])
        for name in ("undici", "yaml"):
            self.assertEqual(root_package["devDependencies"][name], data["installed_versions"]["node_modules/" + name])
        source = (ROOT / "modules/common/runtime/hermes_npm_patch.py").read_text()
        self.assertIn(npm.sha(path.read_bytes()), source)


if __name__ == "__main__":
    unittest.main()
