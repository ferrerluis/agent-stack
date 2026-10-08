import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/strict_health.py"
spec = importlib.util.spec_from_file_location("strict_health", SCRIPT)
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


class StrictHealthTest(unittest.TestCase):
    def test_workspace_subset_still_requires_both_private_application_routes(self):
        dns = "agent-stack.test.ts.net"
        good = {"TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}}, "Web": {
            dns + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:18789"}}},
            dns + ":8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9119"}}}}}
        for serve, passed in ((good, True), ({"TCP": {}, "Web": {}}, False), (good | {"AllowFunnel": {dns + ":443": True}}, False)):
            def run(command, timeout=30):
                if command[1] == "compose": return 0, "fixture-container"
                if command[1] == "inspect": return 0, json.dumps([{"State": {"Running": True, "Health": {"Status": "healthy"}}}])
                if command[-2:] == ["id", "-u"]: return 0, "1000"
                if command[:2] == ["tailscale", "status"]: return 0, json.dumps({"Self": {"Online": True, "DNSName": dns}, "Health": []})
                if command[:3] == ["tailscale", "serve", "status"]: return 0, json.dumps(serve)
                return 0, "fixture-version"
            with patch.object(doctor, "run", side_effect=run):
                report = doctor.remote_report(["workspace"], True, True, "host", ["openclaw", "hermes"])
            self.assertEqual(doctor.accepted(report, doctor.required_checks(["workspace"], True, True)), passed)
            self.assertNotIn("openclaw.doctor", {row["name"] for row in report["checks"]})

    def test_route_service_flag_refuses_empty_duplicate_unknown_or_unchecked_routes(self):
        invalid = [[], ["--check-tailscale", "--route-services", ""], ["--check-tailscale", "--route-services", "openclaw,openclaw"],
                   ["--check-tailscale", "--route-services", "workspace"], ["--route-services", "openclaw,hermes"]]
        invalid[0] = ["--check-tailscale", "--route-services", "unknown"]
        for flags in invalid:
            with patch.object(sys, "argv", ["doctor", "--remote", "--services", "workspace", *flags]), \
                 patch.object(doctor, "remote_report") as remote, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                doctor.main()
            remote.assert_not_called()

    def test_route_service_flag_is_forwarded_without_weakening_required_report_checks(self):
        report = {"schema": 1, "checks": [{"name": name, "status": "pass"} for name in sorted(doctor.required_checks(["workspace"], True, True))]}
        with patch.object(sys, "argv", ["doctor", "--json", "--services", "workspace", "--require-claude", "--check-tailscale", "--route-services", "openclaw,hermes"]), \
             patch.object(doctor.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(report))) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.main(), 0)
        command = run.call_args.args[0][-1]
        self.assertIn("--route-services openclaw,hermes", command)

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

    def test_pinned_default_coverage_requires_exact_version_counts_and_a_clean_report(self):
        report = {'schemaVersion': 1, 'ok': True, 'checksRun': 37, 'checksSkipped': 30, 'findings': []}
        for version in ('OpenClaw 2026.9.8', 'OpenClaw 2026.9.8 (fc23bc8)\n'):
            coverage = doctor.doctor_default_coverage(version)
            self.assertEqual(coverage, '2026.9.8'); self.assertTrue(doctor.doctor_healthy(report, coverage))
        for version in ('OpenClaw 2026.9.9', 'OpenClaw 2026.9.8-beta.1', 'private text', ''):
            self.assertIsNone(doctor.doctor_default_coverage(version))
            self.assertFalse(doctor.doctor_healthy(report, doctor.doctor_default_coverage(version)))
        for change in ({'checksRun': 36}, {'checksRun': True}, {'checksSkipped': 29}, {'checksSkipped': True},
                       {'schemaVersion': True}, {'ok': False}, {'findings': [{'severity': 'warning'}]},
                       {'findings': [{'severity': 'info', 'errorCode': 'OPENCLAW_STATE_LEASE_ABORTED'}]},
                       {'warnings': [{'errorCode': 'OPENCLAW_STATE_LEASE_ABORTED'}]}):
            self.assertFalse(doctor.doctor_healthy({**report, **change}, '2026.9.8'))

    def hermes_report(self):
        return "\n".join("◆ " + section + "\n  ✓ fixture passed" for section in (
            "Python Environment", "Required Packages", "Configuration Files", "Directory Structure", "API Connectivity"
        )) + "\n  All checks passed! 🎉\n"

    def test_native_hermes_requires_complete_green_summary(self):
        good = self.hermes_report()
        self.assertTrue(doctor.hermes_doctor_healthy(good))
        self.assertTrue(doctor.hermes_doctor_healthy("\x1b[32m" + good + "\x1b[0m"))
        for output in ("All checks passed!", good.replace("API Connectivity", "Missing"), good.replace("All checks passed!", "Found 1 issue(s) to address:"),
                       good + "\n✗ broken config", good + "\nAuth provider status (could not check: private-error)", good + "\nTraceback (most recent call last)"):
            self.assertFalse(doctor.hermes_doctor_healthy(output))

    def test_native_hermes_error_exit_fails_even_when_output_claims_success(self):
        def run(command, timeout=30):
            if command[-1] == "doctor":
                self.assertIn("--user", command)
                self.assertIn("10000:10000", command)
                self.assertNotIn("--fix", command)
                self.assertNotIn("--live", command)
                return 1, self.hermes_report()
            if command[1] == "compose":
                return 0, "container-fixture"
            if command[1] == "inspect":
                return 0, json.dumps([{"State": {"Running": True, "Health": {"Status": "healthy"}}}])
            return 0, '{"ok":true,"status":"pass"}'
        with patch.object(doctor, "run", side_effect=run):
            report = doctor.remote_report(["hermes"], False, False, "host")
        self.assertEqual(next(check["status"] for check in report["checks"] if check["name"] == "hermes.doctor"), "fail")
        self.assertFalse(doctor.accepted(report, doctor.required_checks(["hermes"])))

    def test_hermes_private_config_accepts_native_group_read_and_rejects_public_files(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.yaml"
            env = Path(temp) / ".env"
            config.write_text('{"model":{"default":"fixture","provider":"custom"}}')
            env.write_text("# container-seeded fixture\n")
            probe = doctor.HERMES_CONFIG_PROBE.replace("/opt/data/config.yaml", str(config))
            for config_mode, env_mode, expected in ((0o600,0o600,True),(0o640,0o600,True),(0o644,0o600,False),(0o640,0o644,False)):
                config.chmod(config_mode)
                env.chmod(env_mode)
                output = io.StringIO()
                with patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=json.loads)}), patch.dict(os.environ, {"OPENAI_API_KEY":"private-fixture-key"}), contextlib.redirect_stdout(output):
                    exec(probe, {})
                self.assertEqual(json.loads(output.getvalue())["ok"], expected)
                self.assertNotIn("private-fixture-key", output.getvalue())

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

    def test_malformed_remote_nested_state_cannot_crash_or_pass(self):
        for state in ({"State": {"Running": True, "Health": None}}, {"State": []}):
            def run(command, timeout=30):
                if command[1] == "compose":
                    return 0, "container-fixture"
                if command[1] == "inspect":
                    return 0, json.dumps([state])
                return 0, '{"Self":null,"Web":null,"AllowFunnel":null}'
            with patch.object(doctor, "run", side_effect=run):
                report = doctor.remote_report(["openclaw"], False, True, "host")
            self.assertFalse(doctor.accepted(report, doctor.required_checks(["openclaw"], False, True)))

    def test_remote_missing_container_cannot_be_green(self):
        with patch.object(doctor, "run", return_value=(0, "")):
            report = doctor.remote_report(["openclaw", "hermes", "workspace"], True, False, "host")
        self.assertFalse(doctor.accepted(report, doctor.required_checks(["openclaw", "hermes", "workspace"], True)))
        self.assertTrue(all(check["status"] == "fail" for check in report["checks"]))
