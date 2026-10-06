import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/strict_health.py"
spec = importlib.util.spec_from_file_location("strict_health", SCRIPT)
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


class StrictHealthTest(unittest.TestCase):
    def report(self):
        return {"schema": 1, "checks": [{"name": name, "status": "pass"} for name in sorted(
            doctor.required_checks(["openclaw", "hermes", "workspace"], True, True)
        )]}

    def invoke(self, output, code=0, timeout=False):
        with tempfile.TemporaryDirectory() as temp:
            wrapper = Path(temp) / "ssh-fixture"
            wrapper.write_text("#!/usr/bin/env python3\nimport sys,time\nsys.stdin.read()\n" + (
                "time.sleep(1)\n" if timeout else ""
            ) + "print(" + repr(output) + ")\nprint('private-secret-in-stderr',file=sys.stderr)\nsys.exit(" + str(code) + ")\n")
            wrapper.chmod(0o700)
            return subprocess.run(["python3", str(SCRIPT), "--strict", "--json", "--ssh-wrapper", str(wrapper),
                                   "--services", "openclaw,hermes,workspace", "--require-claude", "--check-tailscale",
                                   "--timeout", "0.1" if timeout else "5"], capture_output=True, text=True)

    def test_complete_report_passes_without_remote_secrets(self):
        report = self.report()
        report["secret"] = "private-secret-in-report"
        report["checks"][0]["detail"] = "private-secret-in-check"
        result = self.invoke(json.dumps(report))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")
        self.assertNotIn("private-secret", result.stdout + result.stderr)

    def test_empty_malformed_partial_and_failed_reports_fail(self):
        reports = ["", "not-json", "[]", json.dumps({"schema": 1, "checks": []})]
        partial = self.report()
        partial["checks"].pop()
        reports.append(json.dumps(partial))
        duplicate = self.report()
        duplicate["checks"].append(duplicate["checks"][0])
        reports.append(json.dumps(duplicate))
        failed = self.report()
        failed["checks"][0]["status"] = "fail"
        reports.append(json.dumps(failed))
        for report in reports:
            with self.subTest(report=report):
                result = self.invoke(report)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout)["status"], "fail")

    def test_failed_ssh_and_timeout_do_not_pass(self):
        for result in (self.invoke(json.dumps(self.report()), code=1), self.invoke("", timeout=True)):
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("private-secret", result.stdout + result.stderr)

    def test_native_doctor_requires_inspected_checks(self):
        good = {"schemaVersion": 1, "ok": True, "checksRun": 5, "checksSkipped": 0, "findings": []}
        self.assertTrue(doctor.doctor_healthy(good))
        for change in ({"checksRun": 0}, {"checksSkipped": 1}, {"warnings": [{"errorCode": "OPENCLAW_STATE_LEASE_ABORTED"}]}, {"findings": [{"severity": "warning"}]}, {"ok": False}, {"schemaVersion": 9}):
            self.assertFalse(doctor.doctor_healthy({**good, **change}))
        self.assertFalse(doctor.doctor_healthy([]))
        self.assertFalse(doctor.doctor_healthy({"findings": []}))

    def test_routes_require_correct_dns_https_ports_backends_and_private_access(self):
        dns = "agent-stack.test.ts.net"
        serve = {"TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}}, "Web": {
            dns + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:18789"}}},
            dns + ":8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9119"}}},
        }}
        self.assertTrue(doctor.routes_healthy(serve, dns + ".", ["openclaw", "hermes"]))
        self.assertFalse(doctor.routes_healthy(serve, "another.test.ts.net", ["openclaw", "hermes"]))
        self.assertFalse(doctor.routes_healthy({**serve, "AllowFunnel": {dns + ":443": True}}, dns, ["openclaw", "hermes"]))
        serve["Web"][dns + ":8443"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:18789"
        self.assertFalse(doctor.routes_healthy(serve, dns, ["openclaw", "hermes"]))

    def test_remote_missing_container_cannot_be_green(self):
        with patch.object(doctor, "run", return_value=(0, "")):
            report = doctor.remote_report(["openclaw", "hermes", "workspace"], True, False, "host")
        self.assertFalse(doctor.accepted(report, doctor.required_checks(["openclaw", "hermes", "workspace"], True)))
        self.assertTrue(all(check["status"] == "fail" for check in report["checks"]))
