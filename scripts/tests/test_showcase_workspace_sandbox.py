import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_workspace_sandbox as sandbox
import showcase_contents as contents

NONCE = "1a" * 16


def report(nonce=NONCE, boot="11111111-1111-1111-1111-111111111111"):
    return {"schema": 1, "status": "workspace_sandbox_verified", "nonce": nonce, "uid": 1000, "paid_requests": 0,
            "checks": {key: True for key in sandbox.CHECKS}, "unchanged_sentinels": True, "capabilities_zero": True,
            "seccomp_enabled": True, "no_new_privileges": True, "apparmor": "agent-stack-workspace-codex (enforce)",
            "inside_sha256": hashlib.sha256(nonce.encode()).hexdigest(), "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64, "boot_id": boot}


class WorkspaceConfinementEvidenceTest(unittest.TestCase):
    def test_timeout_stops_the_probe_process_group_before_returning_failure(self):
        child = Mock(pid=1234)
        child.communicate.side_effect = [sandbox.subprocess.TimeoutExpired("codex", 40), (b"", b"")]
        with patch.object(sandbox.subprocess, "Popen", return_value=child) as popen, patch.object(sandbox.os, "killpg") as stop:
            with self.assertRaises(sandbox.subprocess.TimeoutExpired): sandbox.invoke(["codex"], "/fixture", {})
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        stop.assert_called_once_with(1234, sandbox.signal.SIGKILL)
        self.assertEqual(child.communicate.call_count, 2)

    def test_complete_native_receipt_matches_fresh_nonce_and_file(self):
        value = report(); self.assertEqual(sandbox.verify(value, NONCE), value)

    def test_claimed_cli_success_cannot_hide_unsafe_or_forged_evidence(self):
        changes = [("uid", 0), ("uid", 1000.0), ("paid_requests", 1), ("paid_requests", False),
                   ("unchanged_sentinels", False), ("capabilities_zero", False), ("seccomp_enabled", False),
                   ("no_new_privileges", False), ("apparmor", "unconfined"), ("inside_sha256", "c" * 64), ("nonce", "2b" * 16)]
        for key, bad in changes:
            with self.subTest(key=key, bad=bad):
                value = report(); value[key] = bad
                with self.assertRaises(AssertionError): sandbox.verify(value, NONCE)
        for key in sandbox.CHECKS:
            for bad in (False, 1):
                value = report(); value["checks"][key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(AssertionError): sandbox.verify(value, NONCE)

    def test_workspace_policy_requires_exact_loaded_bytes_and_outer_confinement(self):
        expected = {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": []}
        native = {"State": {"Running": True}, "AppArmorProfile": "agent-stack-workspace-codex", "HostConfig": {
            "Privileged": False, "CapAdd": None, "SecurityOpt": ["no-new-privileges:true", "apparmor=agent-stack-workspace-codex", "seccomp=" + json.dumps(expected)]}}
        self.assertEqual(contents.workspace_container_policy(native, json.dumps(expected)), contents.digest(expected))
        for key, bad in (("Privileged", True), ("CapAdd", ["SYS_ADMIN"]), ("SecurityOpt", ["seccomp=unconfined"]),
                         ("SecurityOpt", ["no-new-privileges", "apparmor=agent-stack-workspace-codex", 'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}'])):
            with self.subTest(key=key):
                value = copy.deepcopy(native); value["HostConfig"][key] = bad
                with self.assertRaises(AssertionError): contents.workspace_container_policy(value, json.dumps(expected))
