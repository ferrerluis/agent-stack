import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("workspace_sandbox", ROOT / "modules/common/runtime/workspace_sandbox.py")
sandbox = importlib.util.module_from_spec(spec); spec.loader.exec_module(sandbox)
BUNDLE = json.loads((ROOT / "modules/common/runtime/workspace_sandbox_profiles.json").read_text())


class WorkspaceSandboxTest(unittest.TestCase):
    def test_scoped_profile_preserves_docker_denials_and_capability_conditions(self):
        policy, aa = sandbox.render(BUNDLE)
        policy = json.loads(policy); baseline = json.loads(BUNDLE["seccomp"])
        self.assertEqual(policy["defaultAction"], "SCMP_ACT_ERRNO")
        self.assertEqual(policy["syscalls"][:-1], baseline["syscalls"])
        unconditional = {name for rule in policy["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("includes") and not rule.get("args") for name in rule["names"]}
        self.assertFalse(unconditional.intersection({"bpf", "keyctl", "perf_event_open", "kexec_load", "init_module", "finit_module", "open_by_handle_at", "reboot"}))
        self.assertTrue(set(sandbox.NAMESPACE_SYSCALLS) <= unconditional)
        self.assertIn(b"deny @{PROC}/sysrq-trigger", aa)
        self.assertIn(b"deny /sys/kernel/security", aa)
        self.assertNotIn(b"flags=(unconfined)", aa)

    def test_abi4_explicitly_allows_user_namespace_and_unix_networking(self):
        _, aa = sandbox.render(BUNDLE, abi4=True)
        self.assertIn(b"abi <abi/4.0>", aa)
        self.assertIn(b"  userns,", aa)
        self.assertIn(b"  network unix,", aa)
        self.assertIn(b"  pivot_root,", aa)

    def test_changed_upstream_profile_or_revision_is_refused(self):
        for key in ("seccomp", "apparmor", "upstream_revision"):
            value = copy.deepcopy(BUNDLE); value[key] += "changed"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "bundle_refused"): sandbox.render(value)

    def test_non_root_install_is_refused_before_reading_files(self):
        with patch.object(sandbox.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(ValueError, "root_required"): sandbox.install()

    def layout(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); app = root / "app"; aa = root / "apparmor"; app.mkdir(mode=0o700); aa.mkdir(mode=0o700)
        (app / sandbox.BUNDLE).write_text(json.dumps(BUNDLE)); (app / sandbox.BUNDLE).chmod(0o600)
        profiles = root / "profiles"; profiles.write_text(sandbox.PROFILE + " (enforce)\n")
        return app, aa, profiles

    def test_install_is_repeatable_and_retains_enforced_profile(self):
        app, aa, profiles = self.layout()
        with patch.object(sandbox, "require_root"), patch.object(sandbox, "parser") as parser:
            first = sandbox.install(app, aa, profiles); second = sandbox.install(app, aa, profiles)
        self.assertEqual(first, second)
        self.assertEqual(parser.call_count, 2)
        self.assertEqual((app / sandbox.SECCOMP).stat().st_mode & 0o777, 0o600)
        self.assertEqual((aa / sandbox.PROFILE).stat().st_mode & 0o777, 0o600)

    def test_non_enforcing_profile_restores_previous_durable_files(self):
        app, aa, profiles = self.layout()
        previous = sandbox.MARKER.encode() + b"previous managed profile\n"
        (aa / sandbox.PROFILE).write_bytes(previous); (app / sandbox.SECCOMP).write_bytes(b"previous policy\n")
        (aa / sandbox.PROFILE).chmod(0o600); (app / sandbox.SECCOMP).chmod(0o600)
        profiles.write_text(sandbox.PROFILE + " (complain)\n")
        with patch.object(sandbox, "require_root"), patch.object(sandbox, "parser"):
            with self.assertRaisesRegex(ValueError, "not_enforcing"): sandbox.install(app, aa, profiles)
        self.assertEqual((aa / sandbox.PROFILE).read_bytes(), previous)
        self.assertEqual((app / sandbox.SECCOMP).read_bytes(), b"previous policy\n")

    def test_unmanaged_profile_and_symlink_are_refused(self):
        app, aa, profiles = self.layout()
        path = aa / sandbox.PROFILE; path.write_text("operator owned profile"); path.chmod(0o600)
        with patch.object(sandbox, "require_root"), self.assertRaisesRegex(ValueError, "unmanaged_profile"): sandbox.install(app, aa, profiles)
        path.unlink(); path.symlink_to(profiles)
        with patch.object(sandbox, "require_root"), self.assertRaisesRegex(ValueError, "path_refused"): sandbox.install(app, aa, profiles)

    def test_group_writable_bundle_is_refused(self):
        app, aa, profiles = self.layout(); (app / sandbox.BUNDLE).chmod(0o660)
        with patch.object(sandbox, "require_root"), self.assertRaisesRegex(ValueError, "file_refused"): sandbox.install(app, aa, profiles)
