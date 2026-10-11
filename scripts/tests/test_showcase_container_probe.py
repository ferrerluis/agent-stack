import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_container_probe as probe

ROOT = Path(__file__).resolve().parents[2]
REQUEST = "12345678-1234-4234-9234-123456789abc"
BOOT = "aaaaaaaa-bbbb-4ccc-9ddd-eeeeeeeeeeee"
PINS = {"codex_release": "0.145.0", "claude_release": "2.1.211", "platform": "linux/amd64"}


def bundle(revision="a" * 40, script=None):
    script = (ROOT / probe.SCRIPT_PATH).read_text() if script is None else script
    return {"schema": 1, "request_id": REQUEST, "source_revision": revision, "script": script,
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(), "pins": dict(PINS),
            "contexts": {label: {name: "fixture-only-rendered-source" for name in probe.FILES} for label in ("legacy", "showcase")}}


def profile(label):
    return {"profile": label, "status": "pass", "uid": 1000, "ssh": "pass", "persistence": "pass",
            "codex_release": PINS["codex_release"], "claude_release": PINS["claude_release"] if label == "showcase" else None,
            "platform": "linux/amd64", "model_execution": "not_tested"}


def proof(value, boot=BOOT):
    return {"schema": 1, "status": "pass", "request_id": value["request_id"], "source_revision": value["source_revision"],
            "bundle_sha256": probe.validate_bundle(value), "boot_id": boot, "profiles": [profile(label) for label in ("legacy", "showcase")],
            "cleanup": "pass", "watchdog": "disarmed", "duration_seconds": 2.5, "model_execution": "not_tested"}


class FakeChecker:
    def __init__(self):
        self.calls = []
        self.profile_calls = []
        self.collision = False
        self.list_fail = False
        self.fail_profile = False
        self.clean = True
        self.cleaned = 0

    def docker(self, *args, **kwargs):
        self.calls.append(args)
        if self.list_fail and "ls" in args:
            raise probe.PolicyError("fixture-daemon-disconnected-secret")
        output = "as-test-" + REQUEST.replace("-", "")[:16] + "-legacy\n" if self.collision and args[0] == "container" else ""
        return subprocess.CompletedProcess(args, 0, output, "")

    def profile(self, directory, codex, claude, platform, context=None):
        label = "showcase" if claude else "legacy"
        self.profile_calls.append((label, directory, context, codex, claude, platform))
        assert all((context / name).is_file() for name in probe.FILES)
        assert all((context / name).stat().st_mode & 0o077 == 0 for name in probe.FILES)
        if self.fail_profile:
            raise RuntimeError("fixture-profile-secret-must-not-leak")
        return profile(label)

    def cleanup(self):
        self.cleaned += 1
        return self.clean


class BundleTest(unittest.TestCase):
    def test_bundle_and_proof_bind_source_pins_context_boot_and_request(self):
        value = bundle()
        report = proof(value)
        self.assertEqual(probe.validate_proof(report, value, BOOT)["proof_sha256"], probe.digest(report))
        for changes in ({"request_id": "87654321-1234-4234-9234-123456789abc"}, {"source_revision": "b" * 40},
                        {"bundle_sha256": "0" * 64}, {"boot_id": "b" + BOOT[1:]}, {"cleanup": "fail"},
                        {"watchdog": "armed"}, {"duration_seconds": 2940}, {"duration_seconds": float('nan')},
                        {"model_execution": "pass"}, {"extra": "fixture-secret"}):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                probe.validate_proof({**report, **changes}, value, BOOT)

    def test_incomplete_profiles_wrong_pins_uid_and_missing_legacy_are_rejected(self):
        value = bundle()
        for changes in ({"uid": True}, {"uid": 0}, {"codex_release": "0.146.0"}, {"ssh": "fail"},
                        {"persistence": "fail"}, {"model_execution": "pass"}):
            report = proof(value)
            report["profiles"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(probe.PolicyError):
                probe.validate_proof(report, value, BOOT)
        report = proof(value); report["profiles"].reverse()
        with self.assertRaises(probe.PolicyError): probe.validate_proof(report, value, BOOT)

    def test_bundle_refuses_traversal_extra_contexts_source_drift_and_large_input(self):
        value = bundle()
        changes = []
        changed = copy.deepcopy(value); changed["contexts"]["legacy"]["../auth.json"] = "secret"; changes.append(changed)
        changed = copy.deepcopy(value); changed["contexts"].pop("showcase"); changes.append(changed)
        changed = copy.deepcopy(value); changed["script"] += "# drift"; changes.append(changed)
        changed = copy.deepcopy(value); changed["pins"]["platform"] = "linux/arm64"; changes.append(changed)
        changed = copy.deepcopy(value); changed["contexts"]["legacy"]["Dockerfile"] = "x" * 65536; changes.append(changed)
        for changed in changes:
            with self.assertRaises(Exception): probe.validate_bundle(changed)

    @unittest.skipUnless(shutil.which("terraform"), "task-local Terraform required")
    def test_actual_render_uses_only_locals_and_produces_both_checked_contexts(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-private-key", "TF_VAR_gateway_token": "fixture-token", "SSH_AUTH_SOCK": "/fixture/agent"}):
            value = probe.render_bundle(ROOT, "a" * 40, PINS, shutil.which("terraform"))
        probe.validate_bundle(value)
        self.assertIn("ARG CODEX_RELEASE=" + PINS["codex_release"], value["contexts"]["legacy"]["Dockerfile"])
        self.assertNotIn("ARG CLAUDE_RELEASE", value["contexts"]["legacy"]["Dockerfile"])
        self.assertIn("ARG CLAUDE_RELEASE=" + PINS["claude_release"], value["contexts"]["showcase"]["Dockerfile"])
        self.assertNotIn("fixture-private-key", json.dumps(value))
        self.assertNotIn("fixture-token", json.dumps(value))

    def test_remote_source_has_no_eager_repository_import_dependency(self):
        source = (ROOT / probe.PROBE_PATH).read_text()
        # Isolated Python has no repo path. Invalid input must produce the
        # fixed failure report, rather than an import error or traceback.
        result = subprocess.run([sys.executable, "-I", "-c", source], input=b"{}", capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"schema": 1, "status": "fail", "cleanup": "uncertain", "watchdog": "unverified"})
        self.assertEqual(result.stderr, b"")


class VMFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.checker = FakeChecker()
        self.env = None
        self.commands = []
        self.unit_collision = False
        self.arm_failed = False
        self.disarm_failed = False
        self.times = iter((10.0, 12.5))

    def execute(self, args, env, timeout):
        self.commands.append(args)
        self.env = env
        if "--property=LoadState" in args:
            return b"loaded" if self.unit_collision else b"not-found"
        if args[0] == "systemd-run":
            if self.arm_failed: raise RuntimeError("fixture-timer-secret")
            return b""
        if "is-active" in args: return b"active"
        if "--property=ActiveState" in args: return b"active" if self.disarm_failed else b"inactive"
        return b""

    def run_vm(self):
        def factory():
            return types.SimpleNamespace(ContainerChecks=lambda env, token: self.checker,
                                         CheckFailed=probe.checked_module(ROOT).CheckFailed)
        with patch.object(probe.os, "geteuid", return_value=0):
            return probe.run_vm(bundle(), execute=self.execute, now=lambda: next(self.times), suite_factory=factory, scratch=self.temp.name)

    def test_both_profiles_cleaned_and_timer_disarmed_before_pass(self):
        report = self.run_vm()
        self.assertEqual(report["status"], "pass")
        self.assertEqual([call[0] for call in self.checker.profile_calls], ["legacy", "showcase"])
        self.assertEqual(self.checker.cleaned, 1)
        self.assertEqual(report["cleanup"], "pass")
        self.assertEqual(report["watchdog"], "disarmed")
        self.assertEqual(set(self.env), {"PATH", "HOME", "LANG", "TZ", "DOCKER_CONFIG"})
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_name_collision_or_failed_list_never_arms_or_builds(self):
        for name in ("collision", "list_fail"):
            setattr(self.checker, name, True)
            report = self.run_vm()
            self.assertEqual(report["status"], "fail")
            self.assertEqual(self.checker.profile_calls, [])
            self.assertFalse(any(args[0] == "systemd-run" for args in self.commands))
            setattr(self.checker, name, False)

    def test_unit_collision_never_replaces_or_stops_existing_timer(self):
        self.unit_collision = True
        report = self.run_vm()
        self.assertEqual(report["status"], "fail")
        self.assertEqual(self.checker.profile_calls, [])
        self.assertFalse(any(args[0] == "systemd-run" or "stop" in args for args in self.commands))

    def test_arm_failure_cleans_and_cannot_start_profiles(self):
        self.arm_failed = True
        report = self.run_vm()
        self.assertEqual(report["status"], "fail")
        self.assertEqual(self.checker.profile_calls, [])
        self.assertEqual(self.checker.cleaned, 1)
        self.assertNotIn("fixture-timer-secret", json.dumps(report))

    def test_failed_profile_cleans_and_redacts_failure(self):
        self.checker.fail_profile = True
        report = self.run_vm()
        self.assertEqual(report["status"], "fail")
        self.assertEqual(self.checker.cleaned, 1)
        self.assertEqual(report["cleanup"], "pass")
        self.assertNotIn("fixture-profile-secret", json.dumps(report))

    def test_failed_suite_command_survives_cleanup_with_private_diagnostics(self):
        suite = probe.checked_module(ROOT)
        def fail(*args, **kwargs):
            suite.execute([sys.executable, "-c", "import sys;sys.stderr.write('build-failed');sys.exit(42)"], self.env)
        # Use the same checked exception class that run_vm receives.
        factory = lambda: types.SimpleNamespace(ContainerChecks=lambda env, token: self.checker, CheckFailed=suite.CheckFailed)
        with patch.object(self.checker, "profile", side_effect=fail), patch.object(probe.os, "geteuid", return_value=0):
            report = probe.run_vm(bundle(), execute=self.execute, now=lambda: next(self.times), suite_factory=factory, scratch=self.temp.name)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["cleanup"], "pass")
        self.assertEqual(report["watchdog"], "disarmed")
        self.assertEqual(report["diagnostics"]["phase"], "legacy")
        self.assertEqual(report["diagnostics"]["command"]["stderr"], "build-failed")
        self.assertEqual(report["diagnostics"]["command"]["exit_code"], 42)
        self.assertEqual(self.checker.cleaned, 1)
        with self.assertRaises(probe.PolicyError):
            probe.validate_proof(report, bundle(), BOOT)

    def test_malformed_suite_diagnostics_cannot_emit_arbitrary_data(self):
        suite = probe.checked_module(ROOT)
        failure = suite.CheckFailed("workspace_ssh_timeout", {"secret": "fixture-private-value"})
        factory = lambda: types.SimpleNamespace(ContainerChecks=lambda env, token: self.checker, CheckFailed=suite.CheckFailed)
        with patch.object(self.checker, "profile", side_effect=failure), patch.object(probe.os, "geteuid", return_value=0):
            report = probe.run_vm(bundle(), execute=self.execute, now=lambda: next(self.times), suite_factory=factory, scratch=self.temp.name)
        self.assertEqual(report["diagnostics"], {"phase": "legacy", "reason": "workspace_ssh_timeout"})
        self.assertNotIn("fixture-private-value", json.dumps(report))

    def test_unverified_cleanup_preserves_captured_timer_program(self):
        self.checker.clean = False
        report = self.run_vm()
        self.assertEqual(report, {"schema": 1, "status": "fail", "cleanup": "uncertain", "watchdog": "armed",
                                  "diagnostics": {"phase": "cleanup", "reason": "container_cleanup_unverified"}})
        retained = list(Path(self.temp.name).glob("*/cleanup.py"))
        self.assertEqual(len(retained), 1)
        self.assertFalse(any("stop" in args for args in self.commands))

    def test_unverified_disarm_cannot_pass_or_remove_timer_program(self):
        self.disarm_failed = True
        report = self.run_vm()
        self.assertEqual(report, {"schema": 1, "status": "fail", "cleanup": "pass", "watchdog": "armed",
                                  "diagnostics": {"phase": "cleanup", "reason": "container_watchdog_disarm_unverified"}})
        self.assertEqual(len(list(Path(self.temp.name).glob("*/cleanup.py"))), 1)

    def test_watchdog_script_refuses_foreign_labels_and_never_prunes(self):
        token = "a" * 16
        namespace = {"__name__": "fixture_watchdog"}
        def run(args, **kwargs):
            if "inspect" in args:
                info = {"Labels": {"agent-stack.container-test": "foreign"}}
                if args[1] != "volume": info = {"Config": info}
                return subprocess.CompletedProcess(args, 0, json.dumps([info]).encode(), b"")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        with patch.dict(os.environ, dict(os.environ)), patch.object(probe.subprocess, "run", side_effect=run) as commands:
            exec(compile(probe.cleanup_source(token), "captured-cleanup.py", "exec"), namespace)
        self.assertTrue(commands.called)
        self.assertFalse(any("rm" in call.args[0] or "prune" in call.args[0] for call in commands.call_args_list))


class WatchdogDisarmTest(unittest.TestCase):
    def disarm(self, states, stop_error=None):
        calls = []
        def execute(args, env, timeout):
            calls.append(args)
            if "show" in args:
                return next(states)
            if stop_error is not None:
                raise stop_error
            return b""
        probe.disarm_watchdog("owned-fixture", execute, {})
        return calls

    def test_stopping_timer_collects_never_started_service(self):
        calls = self.disarm(iter((b"active\n", b"inactive\n", b"inactive\n")))
        self.assertEqual([args for args in calls if "stop" in args], [["systemctl", "stop", "owned-fixture.timer"]])

    def test_running_service_must_also_be_stopped_and_verified(self):
        calls = self.disarm(iter((b"active\n", b"inactive\n", b"active\n", b"inactive\n")))
        self.assertEqual([args[-1] for args in calls if "stop" in args], ["owned-fixture.timer", "owned-fixture.service"])

    def test_unit_collected_between_show_and_stop_requires_fresh_proof(self):
        self.disarm(iter((b"active\n", b"inactive\n", b"inactive\n")), probe.PolicyError("container_command_failed"))
        for result in (b"active\n", b"", b"unknown\n"):
            with self.subTest(result=result), self.assertRaisesRegex(probe.PolicyError, "container_watchdog_disarm_unverified"):
                self.disarm(iter((b"active\n", result)), probe.PolicyError("container_command_failed"))

    def test_deadline_and_unavailable_state_cannot_be_ignored(self):
        with self.assertRaisesRegex(probe.PolicyError, "container_deadline_reached"):
            self.disarm(iter((b"active\n",)), probe.PolicyError("container_deadline_reached"))
        with self.assertRaisesRegex(probe.PolicyError, "container_command_failed"):
            probe.disarm_watchdog("owned-fixture", Mock(side_effect=probe.PolicyError("container_command_failed")), {})
