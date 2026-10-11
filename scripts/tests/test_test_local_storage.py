"""Exercise runner storage and cleanup with local command stand-ins, not providers."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("git"), "git required for the isolated worktree")
class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-stack-storage-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "fixture repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/test-local.sh", self.repo / "scripts/test-local.sh")
        (self.repo / ".gitignore").write_text(".tmp/\n")
        (self.repo / "variables.tf").write_text("# fixture\n")
        # bash -n stays native; populate only the files this runner checks.
        runtime = self.repo / "modules/common/templates/runtime"
        runtime.mkdir(parents=True)
        for name in ("install-agent-stack", "agent-stack-migrate-layout", "mount-agent-stack-volume",
                     "agent-stack-tailscale-watchdog", "tailscale-bootstrap", "host-tailscale-bootstrap",
                     "agent-stack-vpn", "agent-stack-vpn-openvpn", "workspace-codex-update",
                     "agent-stack-workspace-codex-update", "workspace-entrypoint",
                     "agent-stack-diagnostics", "agent-stack-diagnostics-ssh"):
            (runtime / (name + ".sh.tpl")).write_text("#!/bin/bash\n")
        (runtime / "workspace-codex-control.py.tpl").write_text("# fixture\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.trace = self.base / "trace.jsonl"
        # Use the actual interpreter by absolute path, so the python3 stand-in
        # cannot recurse. Both stand-ins record the runner's real environment.
        import sys
        body = "#!" + sys.executable + "\n" + '''import json,os,sys,time
from pathlib import Path
fields={key:os.environ[key] for key in ("TMPDIR","TF_DATA_DIR","TF_PLUGIN_CACHE_DIR")}
with open(os.environ["STORAGE_TRACE"],"a") as stream: stream.write(json.dumps(fields)+"\\n")
cache=Path(fields["TF_PLUGIN_CACHE_DIR"])
marker=cache/"reused-provider.fixture"
if not marker.exists(): marker.write_text("original cache entry")
Path(fields["TMPDIR"],"disposable.fixture").write_text("temporary build output")
if os.environ.get("STORAGE_STALL")=="1": time.sleep(30)
if Path(sys.argv[0]).name=="terraform" and os.environ.get("STORAGE_FAIL")=="1": sys.exit(7)
'''
        for name in ("terraform", "python3"):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], STORAGE_TRACE=str(self.trace))
        for key in ("AGENT_STACK_TEST_ROOT", "AGENT_STACK_TEST_KEEP_WORK", "STORAGE_FAIL", "STORAGE_STALL"):
            self.env.pop(key, None)

    def run_runner(self, **extra):
        return subprocess.run(["bash", str(self.repo / "scripts/test-local.sh")], env=self.env | extra,
                              capture_output=True, text=True, timeout=30)

    def rows(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def test_default_reuses_disk_cache_and_removes_two_distinct_completed_workspaces(self):
        for _ in range(2):
            result = self.run_runner()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        root = self.repo / ".tmp/agent-stack-tests"
        rows = self.rows()
        self.assertEqual({row["TF_PLUGIN_CACHE_DIR"] for row in rows}, {str(root / "provider-cache")})
        workspaces = {Path(row["TF_DATA_DIR"]).parent for row in rows}
        self.assertEqual(len(workspaces), 2)
        self.assertTrue(all(work.parent == root and not work.exists() for work in workspaces))
        self.assertTrue(all(Path(row["TMPDIR"]).parent in workspaces for row in rows))
        self.assertEqual((root / "provider-cache/reused-provider.fixture").read_text(), "original cache entry")
        self.assertEqual((root / "provider-cache").stat().st_mode & 0o077, 0)
        self.assertIn("repo idempotence | pass", result.stdout)

    def test_failed_run_cleans_its_workspace_and_preserves_shared_cache(self):
        result = self.run_runner(STORAGE_FAIL="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        root = self.repo / ".tmp/agent-stack-tests"
        self.assertFalse(list(root.glob("work.*")))
        self.assertTrue((root / "provider-cache/reused-provider.fixture").is_file())

    def test_explicit_keep_retains_private_workspace_for_diagnosis(self):
        result = self.run_runner(AGENT_STACK_TEST_KEEP_WORK="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        work = Path(self.rows()[0]["TF_DATA_DIR"]).parent
        self.assertTrue((work / "tmp/disposable.fixture").is_file())
        self.assertEqual(work.stat().st_mode & 0o077, 0)
        self.assertIn("Test workspace retained: " + str(work), result.stdout)

    def test_terminated_run_removes_only_its_workspace_and_preserves_cache(self):
        process = subprocess.Popen(["bash", str(self.repo / "scripts/test-local.sh")],
                                   env=self.env | {"STORAGE_STALL": "1"}, start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            marker = self.repo / ".tmp/agent-stack-tests/provider-cache/reused-provider.fixture"
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "fixture did not reach the test workspace")
            os.killpg(process.pid, signal.SIGTERM)
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, out + err)
            root = self.repo / ".tmp/agent-stack-tests"
            self.assertFalse(list(root.glob("work.*")))
            self.assertTrue((root / "provider-cache/reused-provider.fixture").is_file())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)

    def test_unignored_root_inside_checkout_cannot_copy_cache_as_source(self):
        root = self.repo / "unignored storage"
        result = self.run_runner(AGENT_STACK_TEST_ROOT=str(root))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Git-ignored", result.stderr)
        self.assertFalse(self.trace.exists())
        self.assertFalse(list(root.glob("work.*")))

    def test_linux_refuses_memory_filesystem_before_starting_test_commands(self):
        # Simulate this host's reported filesystem, without downloading or
        # building anything in a RAM directory. Native runner executes the guard.
        for name, output in (("uname", "Linux"), ("stat", "tmpfs")):
            path = self.bin / name
            path.write_text("#!/bin/sh\nprintf '%s\\n' '" + output + "'\n")
            path.chmod(0o700)
        result = self.run_runner()
        self.assertEqual(result.returncode, 2)
        self.assertIn("RAM-backed", result.stderr)
        self.assertFalse(self.trace.exists())
        self.assertFalse(list((self.repo / ".tmp/agent-stack-tests").glob("work.*")))


if __name__ == "__main__":
    unittest.main()
