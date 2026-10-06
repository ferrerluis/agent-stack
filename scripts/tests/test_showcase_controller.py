import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_controller as controller
import showcase_cleanup as cleanup
import showcase_terraform as terraform
import showcase_live as live
import test_showcase_terraform as tf_fixtures
import test_showcase_remote as remote_fixtures
import test_showcase_container_probe as container_fixtures
from test_showcase_policy import NOW, quote, REF

SOURCES = ("scripts/showcase_container_probe.py", "scripts/test_containers.py")


class DeferredPreparationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("terraform"), "task-local Terraform required")
    def test_actual_committed_templates_render_and_bind_without_secret_or_cloud_use(self):
        driver = tf_fixtures.DriverTest("runTest")
        driver.fixture_sources = SOURCES + tuple("modules/common/templates/runtime/" + name for name in
            ("workspace.Dockerfile.tpl", "workspace-entrypoint.sh.tpl", "workspace-codex-update.sh.tpl", "workspace-codex-control.py.tpl")) + (
            "modules/common/runtime/workspace_cli_task.py", "skills/para-memory-drive/SKILL.md")
        driver.setUp(); self.addCleanup(driver.doCleanups)
        driver.client.terraform = shutil.which("terraform")
        original = shutil.which
        with patch.object(shutil, "which", side_effect=lambda name: None if name == "docker" else original(name)):
            proof = controller.prepare_container_deferral(driver.client)
        value = cleanup.private_read(driver.run / "container-bundle.json")
        self.assertEqual(proof["bundle_sha256"], container_fixtures.probe.validate_bundle(value))
        self.assertEqual(proof["source_revision"], driver.fixture.revision)
        self.assertEqual(set(value["contexts"]), {"legacy", "showcase"})
        self.assertIn("ARG CLAUDE_RELEASE=2.1.211", value["contexts"]["showcase"]["Dockerfile"])
        self.assertFalse((driver.run / "repo/terraform.tfstate").exists())
        driver.session.reader.assert_not_called()
        self.assertEqual(driver.session.calls, [])
        self.assertEqual(driver.commands, [])


class DeferredApplyTest(unittest.TestCase):
    def setUp(self):
        self.driver = tf_fixtures.DriverTest("runTest")
        self.driver.fixture_sources = SOURCES
        self.driver.setUp()
        self.addCleanup(self.driver.doCleanups)
        self.driver.client.plan()
        self.value = container_fixtures.bundle(self.driver.fixture.revision)
        cleanup.private_write(self.driver.run / "container-bundle.json", self.value)
        self.readiness = self.driver.readiness()
        self.readiness["checks"]["container_tests"] = "deferred_owned_vm"
        self.readiness["container_deferral"] = {"mode": "first_owned_vm", "run_id": self.driver.run.name,
            "source_revision": self.driver.fixture.revision, "bundle_sha256": container_fixtures.probe.validate_bundle(self.value), "local_docker": "absent"}

    def test_deferred_owned_vm_is_recorded_pending_and_never_claims_container_pass(self):
        self.driver.reserve()
        with patch.object(terraform.shutil, "which", return_value=None):
            result = self.driver.client.apply(self.driver.ledger, self.readiness)
        record = self.driver.client.record()
        self.assertEqual(record["container_tests_gate"], "deferred_owned_vm")
        self.assertEqual(record["container_bundle_sha256"], self.readiness["container_deferral"]["bundle_sha256"])
        self.assertEqual(result["live_matrix"], "not_tested")

    def test_every_other_launch_gate_is_still_required(self):
        self.driver.reserve()
        for name in terraform.LAUNCH_CHECKS - {"container_tests"}:
            value = copy.deepcopy(self.readiness); value["checks"][name] = "pending"
            with patch.object(terraform.shutil, "which", return_value=None), self.assertRaises(terraform.PolicyError):
                self.driver.client.apply(self.driver.ledger, value)
        self.assertFalse(any(args[2] == "apply" for args, _, _ in self.driver.commands))

    def test_local_docker_availability_or_unbound_deferral_refuses_apply(self):
        self.driver.reserve()
        with patch.object(terraform.shutil, "which", return_value="/usr/bin/docker"), self.assertRaisesRegex(terraform.PolicyError, "container_deferral_invalid"):
            self.driver.client.apply(self.driver.ledger, self.readiness)
        for changes in ({"run_id": "as7-another01"}, {"source_revision": "0" * 40}, {"bundle_sha256": "0" * 64},
                        {"mode": "skip"}, {"local_docker": "present"}, {"extra": "fixture-secret"}):
            value = copy.deepcopy(self.readiness); value["container_deferral"].update(changes)
            with patch.object(terraform.shutil, "which", return_value=None), self.assertRaises(terraform.PolicyError):
                self.driver.client.apply(self.driver.ledger, value)
        self.assertFalse(any(args[2] == "apply" for args, _, _ in self.driver.commands))

    def test_changed_bundle_script_pins_or_context_refuses_apply(self):
        self.driver.reserve()
        for update in (lambda value: value.update(script=value["script"] + "# drift"),
                       lambda value: value["pins"].update(codex_release="0.146.0"),
                       lambda value: value["contexts"]["legacy"].update(Dockerfile="changed context")):
            value = copy.deepcopy(self.value); update(value)
            cleanup.private_write(self.driver.run / "container-bundle.json", value)
            with patch.object(terraform.shutil, "which", return_value=None), self.assertRaises(terraform.PolicyError):
                self.driver.client.apply(self.driver.ledger, self.readiness)
        self.assertFalse(any(args[2] == "apply" for args, _, _ in self.driver.commands))

    def test_pending_container_label_without_explicit_deferral_is_not_a_pass(self):
        self.driver.reserve()
        for label in ("pending", "skipped", "deferred_owned_vm"):
            value = self.driver.readiness(); value["checks"]["container_tests"] = label
            with self.assertRaises(terraform.PolicyError): self.driver.client.apply(self.driver.ledger, value)


class RemoteContainerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = remote_fixtures.RemoteChecksTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.checks = self.fixture.checks
        self.checks.bootstrap()
        self.value = container_fixtures.bundle(self.fixture.prepared["source_revision"])
        cleanup.private_write(self.fixture.run / "container-bundle.json", self.value)
        record = self.fixture.attempt.record()
        record.update(container_tests_gate="deferred_owned_vm", container_bundle_sha256=container_fixtures.probe.validate_bundle(self.value))
        cleanup.private_write(self.fixture.attempt.record_path, record)
        self.mutate = None
        self.dispatched = []
        original = self.checks.execute
        def execute(args, env, timeout, input=None):
            words = shlex.split(args[-1])
            if words[:5] == ["sudo", "-n", "python3", "-I", "-c"]:
                self.dispatched.append((args, env, timeout, input))
                value = json.loads(input)
                report = container_fixtures.proof(value, self.fixture.boot)
                if self.mutate: self.mutate(report)
                return json.dumps(report).encode()
            return original(args, env=env, timeout=timeout, input=input)
        self.checks.execute = execute

    def test_deferred_doctors_and_both_paid_task_types_refuse_until_container_proof(self):
        self.fixture.session.secret = Mock(return_value=tf_fixtures.SECRET)
        before = len(self.fixture.calls)
        for action in (self.checks.doctors, lambda: self.checks.runtime_task("openclaw"), lambda: self.checks.workspace_task("codex")):
            with self.assertRaisesRegex(terraform.PolicyError, "container_proof_required"): action()
        self.assertEqual(len(self.fixture.calls), before)
        self.fixture.session.secret.assert_not_called()
        result = self.checks.container_tests()
        self.assertEqual(result["status"], "pass")
        self.fixture.models.start("before_reboot")
        for role in ("openclaw", "hermes"): self.fixture.models.activate_runtime(role)
        self.checks.doctors()
        self.checks.runtime_task("openclaw")
        self.checks.workspace_task("codex")
        with self.assertRaisesRegex(terraform.PolicyError, "container_tests_already_invoked"): self.checks.container_tests()
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0][2], 3100)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", self.dispatched[0][1])
        self.assertNotIn(tf_fixtures.SECRET, json.dumps(cleanup.private_read(self.checks.path)))

    def test_stale_request_wrong_boot_or_cleanup_failure_cannot_enable_tasks(self):
        for changes in ({"request_id": "87654321-1234-4234-9234-123456789abc"}, {"boot_id": remote_fixtures.NEW_BOOT}, {"cleanup": "fail"}):
            evidence = cleanup.private_read(self.checks.path); evidence.pop("containers", None)
            cleanup.private_write(self.checks.path, evidence)
            self.mutate = lambda raw, changes=changes: raw.update(changes)
            with self.assertRaisesRegex(terraform.PolicyError, "container_acceptance_failed"): self.checks.container_tests()
            self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["container_tests"], "uncertain")
            with self.assertRaisesRegex(terraform.PolicyError, "container_proof_required"): self.checks.workspace_task("claude")

    def test_full_deadline_and_original_bundle_are_required_before_dispatch(self):
        active = self.fixture.attempt.active(self.fixture.ledger)
        self.fixture.clock = active["work_deadline"] - 3399
        with self.assertRaisesRegex(terraform.PolicyError, "container_check_time_required"): self.checks.container_tests()
        self.assertEqual(self.dispatched, [])
        self.fixture.clock = NOW
        self.value["contexts"]["legacy"]["Dockerfile"] = "drift"
        cleanup.private_write(self.fixture.run / "container-bundle.json", self.value)
        with self.assertRaisesRegex(terraform.PolicyError, "container_bundle_changed"): self.checks.container_tests()
        self.assertEqual(self.dispatched, [])

    def test_boot_changed_after_dispatch_invalidates_container_proof(self):
        self.mutate = lambda raw: setattr(self.fixture, "boot", remote_fixtures.NEW_BOOT)
        with self.assertRaisesRegex(terraform.PolicyError, "container_acceptance_failed"): self.checks.container_tests()
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["container_tests"], "uncertain")


class FullRunTest(unittest.TestCase):
    def test_matching_client_and_saved_token_or_answer_claims_cannot_replace_original_provider_evidence(self):
        value = self.make(); self.assertEqual(value.controller.run_attempt(value.readiness, quote())["status"], "pass")
        original = cleanup.private_read(value.controller.path)["acceptance"]["observed"]
        original_evidence = cleanup.private_read(value.checks.path)
        for role in ("openclaw", "hermes", "codex", "claude"):
            for change in ("usage", "proof"):
                observed, evidence = copy.deepcopy(original), copy.deepcopy(original_evidence)
                row = evidence["agents" if role in ("openclaw", "hermes") else "tasks"][role]
                if change == "usage":
                    row["model_usage"]["output_tokens"] += 1
                    row["provider_task"]["model_usage"]["output_tokens"] += 1
                else: row["provider_task"]["requests_sha256"] = "0" * 64
                for key in observed[role]: observed[role][key] = copy.deepcopy(row[key])
                with self.assertRaisesRegex(terraform.PolicyError, "model_task_provider_mismatch"):
                    value.controller.verdict(observed, evidence)

    def test_matching_legacy_completion_receipts_do_not_create_missing_task_answer_proof(self):
        value = self.make(); self.assertEqual(value.controller.run_attempt(value.readiness, quote())["status"], "pass")
        observed = copy.deepcopy(cleanup.private_read(value.controller.path)["acceptance"]["observed"])
        evidence = cleanup.private_read(value.checks.path)
        def erase(state):
            for hold in state["attempts"][0]["model_requests"]: hold["completion"].pop("answer_sha256", None)
        value.ledger.operate(erase)
        holds = value.controller.reservation()["model_requests"]
        for role in ("openclaw", "hermes", "codex", "claude"):
            observed[role]["gateway_receipt"]["requests"] = [hold for hold in holds if hold["role"] == role]
            evidence["agents" if role in ("openclaw", "hermes") else "tasks"][role]["gateway_receipt"] = copy.deepcopy(observed[role]["gateway_receipt"])
        with self.assertRaisesRegex(terraform.PolicyError, "model_task_provider_mismatch"):
            value.controller.verdict(observed, evidence)

    def make(self):
        fixture = remote_fixtures.RemoteChecksTest("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # The shared construction fixture starts applied. This scenario starts
        # before its first fake reservation; no real history/state is touched.
        (fixture.fixture.batch / "reservations.json").unlink()
        fixture.attempt.state_path.unlink()
        fixture.session.resources = fixture.session.nodes = False
        record = fixture.attempt.record()
        record.update(stage="planned", apply_invocations=0)
        cleanup.private_write(fixture.attempt.record_path, record)
        hashed = tf_fixtures.preparation_fixtures.attempt.workspace_password_hash(REF, secret_reader=lambda _: "fake-flow-password")
        cleanup.private_write(fixture.run / "workspace-hash.json", {"hash": hashed})
        fixture.fail_apply = fixture.fail_destroy = False
        fixture.tf_commands = []
        def execute(args, env, timeout):
            fixture.tf_commands.append(args)
            operation = args[2]
            if operation == "plan":
                path = Path(next(arg[5:] for arg in args if arg.startswith("-out=")))
                path.write_bytes(b"fixture-delete-only-plan"); path.chmod(0o600)
                return b"planned"
            if operation == "show":
                before = terraform.state_resources(cleanup.private_read(fixture.attempt.state_path))
                return json.dumps({"resource_changes": [{"address": address, "mode": "managed", "type": row["type"],
                    "change": {"actions": ["delete"], "before": row["values"], "after": None}} for address, row in before.items()]}).encode()
            if operation == "apply":
                if args[-1].endswith("deployment.tfplan"):
                    cleanup.private_write(fixture.attempt.state_path, fixture.state)
                    fixture.session.resources = fixture.session.nodes = True
                    if fixture.fail_apply: raise terraform.PolicyError("command_timed_out")
                else:
                    if fixture.fail_destroy: raise terraform.PolicyError("fixture-cleanup-secret")
                    cleanup.private_write(fixture.attempt.state_path, {"version": 4, "resources": []})
                    fixture.session.resources = False
                return b"applied"
            raise AssertionError("Unexpected fake Terraform operation")
        fixture.attempt.execute = execute
        fixture.attempt.now = lambda: fixture.clock
        network = fixture.checks.network_checks
        fixture.checks.network_checks = lambda stage: network(stage, fixture.peer_execute)
        fixture.controller = controller.RunController(fixture.attempt, fixture.ledger, fixture.checks, now=lambda: fixture.clock, models=fixture.models)
        fixture.readiness = {"source_revision": fixture.prepared["source_revision"], "scope": cleanup.scope(fixture.config, "aws"),
                             "checks": {name: "pass" for name in terraform.LAUNCH_CHECKS}}
        return fixture

    def test_actual_components_sequence_one_apply_tasks_peer_reboot_and_cleanup(self):
        value = self.make()
        report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["cells"], {name: "pass" for name in controller.CELLS})
        self.assertEqual(report["cleanup"], "verified")
        ledger = cleanup.private_read(value.fixture.batch / "reservations.json")
        self.assertEqual(ledger["attempts"][0]["status"], "cleaned")
        self.assertEqual(ledger["attempts"][0]["reserved_usd"], "2.00")
        self.assertEqual(len([args for args in value.tf_commands if args[2] == "apply"]), 2)
        remote = cleanup.private_read(value.checks.path)
        self.assertEqual(set(remote["agents"]), {"openclaw", "hermes"})
        self.assertEqual(set(remote["tasks"]), {"claude", "codex"})
        self.assertNotEqual(remote["network"]["before_reboot"]["request_id"], remote["network"]["after_reboot"]["request_id"])
        steps = [row["name"] for row in cleanup.private_read(value.controller.path)["steps"]]
        for suffix, doctor in (("", "doctors"), ("_after_reboot", "doctors_after_reboot")):
            self.assertLess(steps.index("openclaw_activation" + suffix), steps.index(doctor))
            self.assertLess(steps.index("hermes_activation" + suffix), steps.index(doctor))
        self.assertLess(steps.index("model_channel_closed"), steps.index("reboot"))
        self.assertLess(steps.index("doctors_after_reboot"), steps.index("reboot_verified"))
        self.assertLess(steps.index("reboot_verified"), steps.index("after_reboot"))
        self.assertEqual(len(ledger["attempts"][0]["model_requests"]), 4)
        for stage in ("before_reboot", "after_reboot"):
            proof = remote["doctor_stages"][stage]
            self.assertEqual(set(proof["checks"].values()), {"pass"})
            self.assertEqual(proof["boot_id"], remote["model_channels"][stage]["boot_id"])
            self.assertEqual(set(remote["model_channels"][stage]["runtime_config_restores"]), {"openclaw", "hermes"})
        self.assertEqual(remote["checks"]["hermes.readiness"], "uncertain")
        with self.assertRaisesRegex(terraform.PolicyError, "showcase_recovery_required"):
            value.controller.run_attempt(value.readiness, quote())

    def test_matching_saved_and_observed_doctor_claims_still_need_native_identity_and_all_checks(self):
        value = self.make(); self.assertEqual(value.controller.run_attempt(value.readiness, quote())["status"], "pass")
        original = cleanup.private_read(value.checks.path)
        saved = cleanup.private_read(value.controller.path)["acceptance"]["observed"]
        for stage, receipt in (("before_reboot", "doctor_evidence"), ("after_reboot", "doctor_evidence_after_reboot")):
            for role in ("openclaw", "hermes"):
                for change in ({"uid": 0}, {"checks": {role + ".doctor": "pass"}}, {"doctor_sha256": "invalid"},
                               {"native_route": {"kind": "client_echo"}}):
                    evidence, observed = copy.deepcopy(original), copy.deepcopy(saved)
                    doctor = evidence["model_channels"][stage]["runtime_doctors"][role]; doctor.update(change)
                    proof = evidence["doctor_stages"][stage]
                    proof["runtime_reports_sha256"][role] = controller.proof_hash(doctor)
                    observed[receipt] = copy.deepcopy(proof)
                    with self.assertRaisesRegex(terraform.PolicyError, "runtime_doctor_proof_changed"):
                        value.controller.verdict(observed, evidence)

    def test_stale_shutdown_or_reused_boot_probe_cannot_pass_with_green_doctor_history(self):
        value = self.make(); self.assertEqual(value.controller.run_attempt(value.readiness, quote())["status"], "pass")
        original = cleanup.private_read(value.checks.path)
        observed = cleanup.private_read(value.controller.path)["acceptance"]["observed"]
        for changed in ("status", "restore", "nonce"):
            evidence, report = copy.deepcopy(original), copy.deepcopy(observed)
            if changed == "status": evidence["checks"]["service.hermes"] = "pass"
            elif changed == "restore": evidence["model_channels"]["after_reboot"]["runtime_config_restores"].pop("hermes")
            else:
                route = evidence["model_channels"]["after_reboot"]["runtime_routes"]["hermes"]
                route["container_probe"]["nonce"] = evidence["model_channels"]["before_reboot"]["runtime_routes"]["hermes"]["container_probe"]["nonce"]
                report["hermes_activation_after_reboot"] = copy.deepcopy(route)
                proof = evidence["doctor_stages"]["after_reboot"]
                proof["runtime_routes_sha256"]["hermes"] = controller.proof_hash(route)
                report["doctor_evidence_after_reboot"] = copy.deepcopy(proof)
            with self.assertRaises(terraform.PolicyError): value.controller.verdict(report, evidence)

    def test_native_activation_or_doctor_failure_on_either_boot_restores_and_cleans_without_task_replay(self):
        for stage in ("before_reboot", "after_reboot"):
            for action in ("verify", "doctor"):
                value = self.make(); original = value.models.runtime_lifecycle_command
                def failed(target, packet, timeout):
                    reply = original(target, packet, timeout)
                    if value.models.stage == stage and packet["action"] == action and packet["route"]["role"] == "hermes":
                        reply["status"] = "unverified"
                    return reply
                value.models.runtime_lifecycle_command = failed
                report = value.controller.run_attempt(value.readiness, quote())
                self.assertEqual(report["status"], "fail"); self.assertEqual(report["cleanup"], "verified")
                self.assertEqual(set(report["cells"].values()), {"unverified"})
                self.assertEqual(value.models.state, "closed")
                count = len(value.controller.reservation().get("model_requests", []))
                self.assertEqual(count, 0 if stage == "before_reboot" else 4)
                evidence = cleanup.private_read(value.checks.path)
                self.assertEqual(set(evidence["model_channels"][stage]["runtime_config_restores"]), {"openclaw", "hermes"})
                before = len(value.calls)
                self.assertEqual(value.controller.recover()["status"], "fail")
                self.assertEqual(len(value.calls), before)

    def test_first_owned_vm_container_path_precedes_doctors_and_model_tasks(self):
        value = self.make()
        bundle = container_fixtures.bundle(value.prepared["source_revision"])
        sha = container_fixtures.probe.validate_bundle(bundle)
        cleanup.private_write(value.run / "container-bundle.json", bundle)
        value.readiness["checks"]["container_tests"] = "deferred_owned_vm"
        value.readiness["container_deferral"] = {"mode": "first_owned_vm", "run_id": value.run.name,
            "source_revision": value.prepared["source_revision"], "bundle_sha256": sha, "local_docker": "absent"}
        original = value.checks.execute
        def execute(args, env, timeout, input=None):
            if shlex.split(args[-1])[:5] == ["sudo", "-n", "python3", "-I", "-c"]:
                return json.dumps(container_fixtures.proof(json.loads(input), value.boot)).encode()
            return original(args, env=env, timeout=timeout, input=input)
        value.checks.execute = execute
        with patch.object(terraform.shutil, "which", return_value=None):
            report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["status"], "pass")
        flow = cleanup.private_read(value.controller.path)
        steps = [row["name"] for row in flow["steps"]]
        self.assertEqual(steps[:7], ["apply", "bootstrap", "containers", "model_channel", "openclaw_activation", "hermes_activation", "doctors"])
        self.assertLess(steps.index("containers"), steps.index("openclaw"))
        self.assertEqual(value.attempt.record()["container_tests_gate"], "deferred_owned_vm")
        self.assertEqual(cleanup.private_read(value.checks.path)["checks"]["container_tests"], "pass")

    def test_partial_apply_failure_is_cleaned_without_retrying_creation(self):
        value = self.make(); value.fail_apply = True
        report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["cleanup"], "verified")
        self.assertEqual(report["failed_step"], "apply")
        self.assertEqual(set(report["cells"].values()), {"unverified"})
        self.assertEqual(len([args for args in value.tf_commands if args[-1].endswith("deployment.tfplan")]), 1)
        self.assertEqual(value.calls, [])

    def test_each_acceptance_failure_still_runs_verified_cleanup(self):
        for name in ("bootstrap", "doctors", "runtime_task", "workspace_task", "network_checks", "reboot"):
            value = self.make()
            original = getattr(value.checks, name)
            def failed(*args, original=original, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("fixture-raw-error-secret")
            setattr(value.checks, name, failed)
            report = value.controller.run_attempt(value.readiness, quote())
            self.assertEqual(report["status"], "fail", name)
            self.assertEqual(report["cleanup"], "verified", name)
            self.assertEqual(set(report["cells"].values()), {"unverified"}, name)
            self.assertNotIn("fixture-raw-error-secret", json.dumps(report))

    def test_keyboard_interrupt_cleans_and_does_not_replay_paid_steps(self):
        value = self.make()
        value.checks.workspace_task = Mock(side_effect=KeyboardInterrupt)
        report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["cleanup"], "verified")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(value.checks.workspace_task.call_count, 1)
        before = len(value.calls)
        recovered = value.controller.recover()
        self.assertEqual(recovered["cleanup"], "verified")
        self.assertEqual(recovered["status"], "fail")
        self.assertEqual(len(value.calls), before)

    def test_cleanup_failure_keeps_reservation_active_and_blocks_new_launch(self):
        value = self.make(); value.fail_destroy = True
        report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["cleanup"], "uncertain")
        self.assertEqual(set(report["cells"].values()), {"unverified"})
        self.assertEqual(value.controller.reservation()["status"], "active")
        with self.assertRaisesRegex(terraform.PolicyError, "cleanup_required_before_launch"):
            value.ledger.reserve("aws", "as7-nextfake01", quote(), now=NOW)
        self.assertNotIn("fixture-cleanup-secret", json.dumps(cleanup.private_read(value.controller.path)))

    def test_cleanup_recovery_restores_only_unchanged_completed_acceptance(self):
        value = self.make(); value.fail_destroy = True
        value.controller.run_attempt(value.readiness, quote())
        before = len(value.calls)
        value.fail_destroy = False
        report = value.controller.recover()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["cleanup"], "verified")
        self.assertEqual(len(value.calls), before)
        self.assertEqual(value.controller.reservation()["reserved_usd"], "2.00")

    def test_http_only_matching_ledger_and_client_receipts_cannot_pass_verdict(self):
        value = self.make(); self.assertEqual(value.controller.run_attempt(value.readiness, quote())["status"], "pass")
        observed = copy.deepcopy(cleanup.private_read(value.controller.path)["acceptance"]["observed"])
        evidence = cleanup.private_read(value.checks.path)
        def erase(state):
            for hold in state["attempts"][0]["model_requests"]: hold.pop("completion", None)
        value.ledger.operate(erase)
        holds = value.controller.reservation()["model_requests"]
        for role in ("openclaw", "hermes", "codex", "claude"):
            observed[role]["gateway_receipt"]["requests"] = [hold for hold in holds if hold["role"] == role]
            evidence["agents" if role in ("openclaw", "hermes") else "tasks"][role]["gateway_receipt"] = copy.deepcopy(observed[role]["gateway_receipt"])
        for status in ("provider_completions_verified", "gateway_responses_received"):
            observed["openclaw"]["gateway_receipt"]["status"] = status
            evidence["agents"]["openclaw"]["gateway_receipt"] = copy.deepcopy(observed["openclaw"]["gateway_receipt"])
            with self.assertRaisesRegex(terraform.PolicyError, "model_task_receipt_unverified"):
                value.controller.verdict(observed, evidence)

    def test_changed_evidence_or_step_receipt_cannot_restore_acceptance(self):
        for kind in ("evidence", "step"):
            value = self.make(); value.fail_destroy = True
            value.controller.run_attempt(value.readiness, quote())
            if kind == "evidence":
                record = cleanup.private_read(value.checks.path)
                record["checks"]["tailscale.acceptance"] = "uncertain"
                cleanup.private_write(value.checks.path, record)
            else:
                record = cleanup.private_read(value.controller.path)
                record["steps"][2]["proof_sha256"] = "0" * 64
                cleanup.private_write(value.controller.path, record)
            value.fail_destroy = False
            report = value.controller.recover()
            self.assertEqual(report["cleanup"], "verified")
            self.assertEqual(report["status"], "fail")
            self.assertEqual(set(report["cells"].values()), {"unverified"})

    def test_budget_refusal_creates_no_vm_or_other_run_cleanup(self):
        value = self.make()
        price = quote(); price["fixed_usd"] = "30"
        report = value.controller.run_attempt(value.readiness, price)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["reason"], "insufficient_budget")
        self.assertEqual(report["cleanup"], "not_reserved")
        self.assertEqual(value.tf_commands, [])
        self.assertEqual(value.controller.reservation(), None)

    def test_other_active_attempt_is_never_cleaned_by_this_controller(self):
        value = self.make()
        value.ledger.reserve("aws", "as7-otherfake01", quote(), now=NOW)
        report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["reason"], "cleanup_required_before_launch")
        self.assertEqual(report["cleanup"], "not_reserved")
        self.assertEqual(value.tf_commands, [])
        ledger = cleanup.private_read(value.fixture.batch / "reservations.json")
        self.assertEqual([(row["run_id"], row["status"]) for row in ledger["attempts"]], [("as7-otherfake01", "active")])

    def test_failed_post_reboot_peer_check_cleans_without_claiming_any_cell(self):
        value = self.make()
        original = value.checks.network_checks
        def execute(stage):
            result = original(stage)
            if stage == "after_reboot": raise RuntimeError("fixture-post-reboot-secret")
            return result
        value.checks.network_checks = execute
        report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["failed_step"], "after_reboot")
        self.assertEqual(report["cleanup"], "verified")
        self.assertEqual(set(report["cells"].values()), {"unverified"})
        self.assertNotIn("fixture-post-reboot-secret", json.dumps(cleanup.private_read(value.controller.path)))

    def test_changed_evidence_during_cleanup_invalidates_prior_acceptance(self):
        value = self.make()
        original = value.attempt.cleanup
        def cleanup_changed(ledger):
            result = original(ledger)
            evidence = cleanup.private_read(value.checks.path)
            evidence["checks"]["tailscale.acceptance"] = "uncertain"
            cleanup.private_write(value.checks.path, evidence)
            return result
        value.attempt.cleanup = cleanup_changed
        report = value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["cleanup"], "verified")
        self.assertEqual(report["reason"], "acceptance_evidence_changed")

    def test_loaded_controller_revision_drift_refuses_before_reservation(self):
        value = self.make()
        root = value.fixture.root / "other-controller-version"
        for name in controller.SOURCES:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((Path(controller.__file__).parents[1] / name).read_bytes())
        target = root / "scripts/showcase_policy.py"
        target.write_text(target.read_text() + "\n# different controller revision\n")
        with patch.object(controller, "__file__", str(root / "scripts/showcase_controller.py")), self.assertRaisesRegex(terraform.PolicyError, "controller_source_revision_changed"):
            value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(value.tf_commands, [])
        self.assertFalse(value.controller.path.exists())

    def test_public_cleanup_recovers_verified_acceptance_without_model_replay(self):
        import contextlib
        import io
        value = self.make(); value.fail_destroy = True
        value.controller.run_attempt(value.readiness, quote())
        value.fail_destroy = False
        before = len(value.calls)
        config_path = value.run / "inputs.json"
        argv = ["showcase_live.py", "--config", str(config_path), "--provider", "aws", "--run-id", value.run.name,
                "--batch", str(value.fixture.batch), "--phase", "cleanup"]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"AGENT_STACK_LIVE_TESTS": "1"}), \
             patch.object(live.shutil, "which", return_value="/fixture/tool"), patch.object(live, "CloudSession", return_value=value.session), \
             patch.object(live, "TerraformAttempt", return_value=value.attempt), contextlib.redirect_stdout(output):
            code = live.main()
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "cleanup_verified")
        self.assertEqual(report["acceptance"], "pass")
        self.assertEqual(len(value.calls), before)
        self.assertEqual(report["cells"], {name: "pass" for name in controller.CELLS})

    def test_running_controller_lock_refuses_concurrent_run(self):
        value = self.make()
        with controller.controller_lock(value.run), self.assertRaisesRegex(terraform.PolicyError, "showcase_run_busy"):
            value.controller.run_attempt(value.readiness, quote())
        self.assertEqual(value.tf_commands, [])
