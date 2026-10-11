import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("runtime_access", ROOT / "scripts/read_runtime_access.py")
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


class RuntimeAccessTest(unittest.TestCase):
    def query(self):
        return {"repo_root": str(ROOT), "key_path": ".ssh/test-key", "host": "203.0.113.1", "user": "admin"}

    def test_actual_dns_is_collected_with_bounded_key_authenticated_ssh(self):
        payload = {"schema": 1, "tailscale_dns": "agent-stack.real-tailnet.ts.net", "secret": "must-not-escape"}
        with patch.object(access.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")) as ssh:
            result = access.collect(self.query())
        self.assertEqual(result, {"status": "observed", "tailscale_dns": payload["tailscale_dns"]})
        self.assertEqual(ssh.call_args.kwargs["timeout"], 25)
        self.assertIn("--batch", ssh.call_args.args[0])
        self.assertIn("admin@203.0.113.1", ssh.call_args.args[0])

    def test_failed_missing_and_malformed_access_never_fabricates_url(self):
        for code, output in ((1, ""), (0, ""), (0, "[]"), (0, '{"schema":1,"tailscale_dns":"guessed-short-name"}')):
            with patch.object(access.subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "private-secret")):
                self.assertEqual(access.collect(self.query()), {"status": "unavailable", "tailscale_dns": ""})

    def test_timeout_returns_unavailable_so_cleanup_can_proceed(self):
        with patch.object(access.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 25)):
            self.assertEqual(access.collect(self.query())["status"], "unavailable")

    def test_invalid_hosts_are_rejected_before_ssh(self):
        for host in ("-oProxyCommand=bad", "user@host", "host;bad", "host with spaces"):
            with patch.object(access.subprocess, "run") as ssh:
                self.assertEqual(access.collect({**self.query(), "host": host})["status"], "unavailable")
                ssh.assert_not_called()
