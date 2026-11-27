"""Prepared-guest checks using synthetic facts, keys and temporary files only."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("existing_guest", ROOT / "scripts/existing_guest.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


def query():
    return dict(address="guest-fixture.invalid", username="operator", identity_file="/fixture/key",
                host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOnly", port="2200",
                architecture="auto", allow_existing_data="false", require_data_mount="false")


def facts():
    return dict(os="ubuntu", version="24.04", architecture="aarch64", memory_mb=8192,
                disk_free_gb=40, data_state="missing", data_mounted=False, unsafe_path=False)


class GuestPreflight(unittest.TestCase):
    def test_normal_arm64_and_amd64_guests_need_no_hypervisor_or_loader(self):
        for machine, architecture in [("aarch64", "arm64"), ("x86_64", "amd64")]:
            f = facts()
            f["architecture"] = machine
            self.assertEqual(guest.validate_facts(query(), f)["architecture"], architecture)
        for text in ["virsh", "/dev/kvm", "tailscale", "mkfs", "write_text", "subprocess"]:
            self.assertNotIn(text, guest.REMOTE_PROBE)

    def test_wrong_guest_os_cpu_resources_or_paths_are_rejected(self):
        for name, value in [("os", "debian"), ("version", "22.04"), ("architecture", "armv7l"),
                            ("memory_mb", 1024), ("disk_free_gb", 1), ("unsafe_path", True),
                            ("data_state", "unknown")]:
            with self.subTest(name=name):
                f = facts()
                f[name] = value
                with self.assertRaises(guest.PreflightError):
                    guest.validate_facts(query(), f)

    def test_declared_architecture_must_match_guest(self):
        q = query()
        q["architecture"] = "amd64"
        with self.assertRaises(guest.PreflightError):
            guest.validate_facts(q, facts())
        q["architecture"] = "arm64"
        self.assertEqual(guest.validate_facts(q, facts())["status"], "ready")

    def test_unknown_data_requires_review_and_mount_is_optional(self):
        q, f = query(), facts()
        f["data_state"] = "unmanaged"
        with self.assertRaises(guest.PreflightError):
            guest.validate_facts(q, f)
        q["allow_existing_data"] = "true"
        self.assertEqual(guest.validate_facts(q, f)["status"], "ready")
        q["require_data_mount"] = "true"
        with self.assertRaises(guest.PreflightError):
            guest.validate_facts(q, f)
        f["data_mounted"] = True
        self.assertEqual(guest.validate_facts(q, f)["status"], "ready")

    def test_ssh_is_direct_pinned_and_rejects_injection(self):
        argv = guest.ssh_argv(query(), "/fixture/public-known-hosts")
        for value in ["/dev/null", "StrictHostKeyChecking=yes", "ForwardAgent=no", "BatchMode=yes", "2200"]:
            self.assertIn(value, argv)
        self.assertEqual(argv[-2:], ["python3", "-"])
        self.assertNotIn("-L", argv)
        self.assertNotIn("-J", argv)
        for name in ["address", "username", "host_key", "identity_file"]:
            q = query()
            q[name] = "bad\n-oProxyCommand=unexpected"
            with self.assertRaises(guest.PreflightError):
                guest.ssh_argv(q, "/fixture/known-hosts")

    def test_probe_pins_supplied_public_identity_then_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "synthetic-existing-key"
            key.write_text("synthetic fixture, not a credential")
            q = query()
            q["identity_file"] = str(key)
            seen = []
            def fake_ssh(argv, **kwargs):
                option = next(value for value in argv if value.startswith("UserKnownHostsFile="))
                known = Path(option.split("=", 1)[1])
                self.assertEqual(known.read_text(), f"{guest.ALIAS} {q['host_key']}\n")
                self.assertEqual(known.stat().st_mode & 0o777, 0o600)
                self.assertEqual(kwargs["input"], guest.REMOTE_PROBE)
                seen.append(known)
                return subprocess.CompletedProcess(argv, 0, json.dumps(facts()), "")
            output = io.StringIO()
            with patch.object(sys, "stdin", io.StringIO(json.dumps(q))), patch.object(guest.subprocess, "run", fake_ssh), contextlib.redirect_stdout(output):
                guest.main()
            self.assertEqual(json.loads(output.getvalue())["status"], "ready")
            self.assertTrue(seen)
            self.assertFalse(seen[0].exists())


class GuestStorage(unittest.TestCase):
    def run_storage(self, root, allow=False, mounted=False):
        template = (ROOT / "modules/common/templates/runtime/mount-agent-stack-volume.sh.tpl").read_text()
        body = template.split('%{ if provider_type == "existing" }', 1)[1].split('%{ else }', 1)[0]
        body = body.replace("${existing_guest_allow_existing_data}", str(allow).lower()).replace("${existing_guest_require_data_mount}", str(mounted).lower())
        body = body.replace("/opt/agent-stack", str(root / "app"))
        return subprocess.run(["bash", "-euc", body], capture_output=True, text=True)

    def test_normal_directory_bootstrap_is_idempotent_and_preserves_sentinel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.run_storage(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            data = root / "app/data"
            sentinel = data / "owned-fixture"
            sentinel.write_text("preserve")
            self.assertEqual(self.run_storage(root).returncode, 0)
            self.assertEqual(sentinel.read_text(), "preserve")
            self.assertTrue((data / ".agent-stack-existing-guest.json").is_file())

    def test_unrelated_directory_and_symlink_targets_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "app/data"
            data.mkdir(parents=True)
            sentinel = data / "unrelated"
            sentinel.write_text("preserve")
            # An app-level marker cannot adopt an unrelated replacement data
            # directory; ownership evidence must live on the selected data path.
            (root / "app/.last-apply.json").write_text('{"artifact_checksum":"fixture"}')
            self.assertNotEqual(self.run_storage(root).returncode, 0)
            self.assertEqual(sentinel.read_text(), "preserve")
            self.assertFalse((data / ".agent-stack-existing-guest.json").exists())
            self.assertEqual(self.run_storage(root, allow=True).returncode, 0)
            self.assertEqual(sentinel.read_text(), "preserve")
            (data / ".agent-stack-existing-guest.json").unlink()
            target = root / "outside"
            target.mkdir()
            data.rename(root / "original-data")
            data.symlink_to(target, target_is_directory=True)
            self.assertNotEqual(self.run_storage(root, allow=True).returncode, 0)
            self.assertEqual(list(target.iterdir()), [])

    def test_required_mount_never_falls_back_to_guest_root_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertNotEqual(self.run_storage(root, mounted=True).returncode, 0)
            self.assertFalse((root / "app/data").exists())


class GuestSshHelper(unittest.TestCase):
    def test_repo_helper_uses_guest_config_and_respects_existing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / ".ssh").mkdir()
            (root / ".ssh/config").write_text("Host agent-stack\n  HostName fixture.invalid\n  Port 2200\n")
            helper = root / "bin/agent-stack-ssh"
            shutil.copy2(ROOT / "bin/agent-stack-ssh", helper)
            for name, body in {"terraform": "print('agent-stack')", "ssh": "import json,sys;print(json.dumps(sys.argv[1:]))"}.items():
                stub = root / "bin" / name
                stub.write_text(f"#!{sys.executable}\n{body}\n")
                stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}", AGENT_STACK_SSH_KEY_PATH="", OPENCLAW_SSH_KEY_PATH="")
            key = root / "synthetic-existing-key"
            key.write_text("synthetic fixture, not a credential")
            result = subprocess.run(["bash", str(helper), "--key", str(key), "--", "fixture-command"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads(result.stdout)
            self.assertEqual(argv[:2], ["-F", str(root / ".ssh/config")])
            self.assertEqual(argv[argv.index("-i") + 1], str(key))
            self.assertEqual(argv[-2:], ["agent-stack", "fixture-command"])
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertNotIn("-L", argv)
            self.assertNotIn("-J", argv)
