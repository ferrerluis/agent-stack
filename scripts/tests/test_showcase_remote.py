import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_remote as remote
import showcase_controller as controller
import showcase_terraform as terraform
import showcase_cleanup as cleanup
import test_showcase_attempt as preparation_fixtures
import test_showcase_terraform as terraform_fixtures
from test_showcase_policy import config, quote, NOW
from unittest.mock import Mock
import test_showcase_peer as peer_fixtures
from showcase_model_session_fixtures import FixtureSession, PRIVATE_ROUTE

ROOT = Path(__file__).resolve().parents[2]
PREFIX = terraform_fixtures.PREFIX
IP = "8.8.8.8"  # Fixture only: every SSH/provider operation below is replaced.
CHECKSUM = "a" * 64
BOOT = "11111111-1111-1111-1111-111111111111"
NEW_BOOT = "22222222-2222-2222-2222-222222222222"
DISK = "33333333-3333-3333-3333-333333333333"


class RemoteChecksTest(unittest.TestCase):
    def test_stale_provider_answers_fail_all_roles_and_cannot_replay_paid_work(self):
        for role in ("openclaw", "hermes", "codex", "claude"):
            fresh = RemoteChecksTest("runTest"); fresh.setUp(); self.addCleanup(fresh.doCleanups)
            fresh.checks.bootstrap(); fresh.checks.doctors()
            original = fresh.models.fake_response
            def stale(role, body, answer=None, usage=None):
                original(role, body, {**answer, "nonce": "b2" * 16}, usage)
            fresh.models.fake_response = stale
            method = fresh.checks.runtime_task if role in ("openclaw", "hermes") else fresh.checks.workspace_task
            with self.assertRaisesRegex(terraform.PolicyError, "model_task_provider_mismatch"): method(role)
            evidence = cleanup.private_read(fresh.checks.path)
            self.assertEqual(evidence["agents" if role in ("openclaw", "hermes") else "tasks"][role]["status"], "uncertain")
            self.assertEqual(fresh.models.state, "closed")
            holds = fresh.ledger.operate(fresh.models.spend.account)["model_requests"]
            self.assertEqual(len(holds), 1); self.assertEqual(holds[0]["status"], "response_received")
            before = len(fresh.calls)
            with self.assertRaises(terraform.PolicyError): method(role)
            self.assertEqual(len(fresh.calls), before)

    def test_native_token_mismatch_seals_runtime_and_coding_tasks(self):
        for role in ("openclaw", "hermes", "codex", "claude"):
            fresh = RemoteChecksTest("runTest"); fresh.setUp(); self.addCleanup(fresh.doCleanups)
            fresh.checks.bootstrap(); fresh.checks.doctors()
            def changed(report): report["model_usage"]["cache_read_tokens"] += 1
            if role in ("openclaw", "hermes"): fresh.agent_mutation = changed
            else: fresh.task_mutation = changed
            method = fresh.checks.runtime_task if role in ("openclaw", "hermes") else fresh.checks.workspace_task
            with self.assertRaisesRegex(terraform.PolicyError, "model_task_provider_mismatch"): method(role)
            self.assertEqual(fresh.models.state, "closed")

    def test_independent_file_answer_hash_must_match_controller_challenge(self):
        self.checks.bootstrap()
        self.file_mutation = lambda report: report.update(answer_sha256="0" * 64)
        with self.assertRaisesRegex(terraform.PolicyError, "workspace_file_verification_failed"):
            self.checks.workspace_task("codex")
        self.assertEqual(self.models.state, "closed")

    def setUp(self):
        self.fixture = preparation_fixtures.PreparationTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("bin/agent-stack-ssh", remote.DOCTOR_PATH, remote.AGENT_PATH, remote.BROWSER_PATH, remote.PEER_PATH,
                     *controller.SOURCES):
            target = self.fixture.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
            target.chmod(0o755)
        self.fixture.git("add", ".")
        self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "remote fixture")
        self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip()
        self.config = config()
        self.config["runner"]["peer_ssh"] = peer_fixtures.access()
        self.prepared = preparation_fixtures.attempt.prepare(self.config, "aws", PREFIX, self.fixture.batch,
            self.fixture.revision, self.fixture.repo)
        self.run = self.fixture.batch / "attempts" / PREFIX
        self.plan, self.state, _ = terraform_fixtures.plan_fixture()
        self.plan["resource_changes"].append({"address": "terraform_data.runtime_apply", "mode": "managed", "type": "terraform_data",
            "change": {"actions": ["create"], "before": None, "after": {"input": CHECKSUM}}})
        self.state["resources"].append({"type": "terraform_data", "mode": "managed", "name": "runtime_apply", "instances": [
            {"attributes": {"id": "runtime-local-id", "input": {"value": CHECKSUM, "type": "string"}}}]})
        self.state["resources"][1]["instances"][0]["attributes"]["public_ip"] = IP
        cleanup.private_write(self.run / "repo/terraform.tfstate", self.state)
        cleanup.private_write(self.run / "deployment-plan.json", self.plan)
        (self.run / "deployment.tfplan").write_bytes(b"private fixture binary")
        (self.run / "deployment.tfplan").chmod(0o600)
        (self.run / "repo/.terraform.lock.hcl").write_text("fixture provider lock")
        cleanup.private_write(self.run / "terraform.json", {"schema": 1, "stage": "applied", "run_id": PREFIX,
            "scope": cleanup.scope(self.config, "aws"), "source_revision": self.prepared["source_revision"], "apply_invocations": 1,
            "container_tests_gate": "pass",
            "plan_sha256": hashlib.sha256((self.run / "deployment.tfplan").read_bytes()).hexdigest(),
            "plan_json_sha256": hashlib.sha256((self.run / "deployment-plan.json").read_bytes()).hexdigest(),
            "provider_lock_sha256": hashlib.sha256((self.run / "repo/.terraform.lock.hcl").read_bytes()).hexdigest()})
        self.session = terraform_fixtures.FakeSession(self.config)
        self.session.resources = self.session.nodes = True
        self.api_ip = IP
        def provider(client, operation, **kwargs):
            if operation == "get_caller_identity":
                return {"Account": self.config["providers"]["aws"]["account_id"]}
            if operation == "describe_instances":
                self.assertEqual(kwargs, {"InstanceIds": ["i-a2"]})
                return {"Reservations": [{"Instances": [{"InstanceId": "i-a2", "State": {"Name": "running"}, "PublicIpAddress": self.api_ip}]}]}
            raise AssertionError("unexpected API operation")
        self.session.adapter.call.side_effect = provider
        self.session.http = Mock()
        self.session.http.request.return_value = {"devices": [{"name": self.config["tailnet"]["runner_dns"], "authorized": True}]}
        self.attempt = terraform.TerraformAttempt(self.run, self.config, "aws", self.session)
        self.ledger = cleanup.Reservations(self.fixture.batch, self.config)
        self.ledger.reserve("aws", PREFIX, quote(), now=NOW)
        self.clock = NOW
        self.calls = []
        self.boot, self.disk = BOOT, DISK
        self.change_boot = True
        self.fail_doctor = False
        self.fail_verifier = False
        self.rebooted = False
        self.task_mutation = None
        self.file_mutation = None
        self.agent_mutation = None
        self.private_mutation, self.peer_mutation = None, None
        self.peer_calls = []
        self.checks = remote.RemoteChecks(self.attempt, self.ledger, execute=self.execute, now=lambda: self.clock, sleep=self.sleep)
        self.models = FixtureSession(self.checks)
        self.checks.models = self.models  # Test-only lazy fixture channel.

    def sleep(self, amount):
        self.clock += amount

    def execute(self, args, env, timeout, input):
        self.calls.append({"args": args, "env": env, "input": input, "timeout": timeout})
        words = shlex.split(args[-1])
        if "-c" in words and "workspace_model_route_unverified" in words[-1]:
            return json.dumps(self.models.fake_probe(json.loads(input))).encode()
        if input == remote.BOOTSTRAP.encode():
            return json.dumps({"schema": 1, "status": "pass", "checksum": CHECKSUM, "dns": PREFIX + ".fixture.ts.net",
                "boot_id": self.boot, "disk_uuid": self.disk, "data_volume_id": "vol-a3"}).encode()
        if input == peer_fixtures.peer.VM_SOURCE.encode():
            report = {"schema": 1, "status": "pass", "boot_id": self.boot,
                "browser_url": "http://127.0.0.1:18789/#bootstrapToken=fake-handoff&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789",
                "hermes_username": "fixture", "hermes_password": terraform_fixtures.SECRET,
                "host_public_key": peer_fixtures.KEY, "workspace_public_key": peer_fixtures.KEY}
            if self.private_mutation: self.private_mutation(report)
            return json.dumps(report).encode()
        if "--remote" in words:
            module = self.doctor()
            names = module.required_checks(["openclaw", "hermes", "workspace"], True, True)
            report = {"schema": 1, "status": "pass", "checks": [{"name": name, "status": "pass"} for name in sorted(names)]}
            if self.fail_doctor:
                report["checks"][0]["status"] = "fail"
            return json.dumps(report).encode()
        if input == (ROOT / remote.AGENT_PATH).read_bytes():
            _, _, _, _, runtime, model, request_id, challenge_json = words
            challenge = json.loads(challenge_json)
            answer = {"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]}
            usage = {"input_tokens": 30, "output_tokens": 25, "cache_read_tokens": 0, "cache_write_tokens": 0}
            self.models.fake_response(runtime, challenge_json.encode(), answer, usage)
            report = {"schema": 1, "status": "pass", "runtime": runtime, "served_model": model,
                "request_id": request_id, "watchdog": "disarmed", "duration_seconds": 1.25,
                "transport": "gateway_rpc" if runtime == "openclaw" else "api_runs",
                "answer": answer, "model_usage": usage}
            if self.agent_mutation:
                self.agent_mutation(report)
            return json.dumps(report).encode()
        if "--cli" in words:
            cli = words[words.index("--cli") + 1]
            bundle = json.loads(input)
            self.assertIn("--model-gateway", words)
            self.assertEqual(bundle["base_url"], PRIVATE_ROUTE + cli)
            challenge = json.loads(words[words.index("--challenge") + 1])
            answer = {"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]}
            if not hasattr(self, "workspace_challenges"): self.workspace_challenges = {}
            self.workspace_challenges[cli] = challenge
            usage = {"input_tokens": 30, "output_tokens": 20, "cache_read_tokens": 0, "cache_write_tokens": 0}
            self.models.fake_response(cli, input, answer, usage)
            report = {"schema": 1, "cli": cli, "status": "pass", "verified": True,
                "model_usage": usage, "fixture": ".agent-stack/acceptance/" + cli + "-fresh0001",
                "input_sha256": remote.task_evidence.input_hash(challenge), "duration_seconds": 1.25,
                "model_route": {"kind": "budget_gateway", "role": cli, "provider": bundle["provider"],
                                "base_url_sha256": hashlib.sha256(bundle["base_url"].encode()).hexdigest()}}
            if self.task_mutation:
                self.task_mutation(report)
            return json.dumps(report).encode()
        if remote.VERIFY_FILES in words:
            if self.fail_verifier:
                raise terraform.PolicyError("command_failed")
            cli = "codex" if "codex-" in words[-2] else "claude"
            challenge = self.workspace_challenges[cli]
            answer = {"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]}
            digest = hashlib.sha256(json.dumps(answer).encode()).hexdigest()
            report = {"schema": 1, "status": "pass", "uid": 1000, "input_sha256": words[-1], "result_sha256": digest,
                      "answer_sha256": remote.task_evidence.expected_answer(challenge)}
            if self.file_mutation:
                self.file_mutation(report)
            return json.dumps(report).encode()
        if words == ["sudo", "-n", "systemctl", "reboot"]:
            self.rebooted = True
            if self.change_boot:
                self.boot = NEW_BOOT
            raise terraform.PolicyError("command_failed")  # expected disconnect
        raise AssertionError("unexpected SSH command")

    def doctor(self):
        module = Mock()
        namespace = {"__name__": "fixture_doctor", "__file__": str(ROOT / remote.DOCTOR_PATH)}
        exec(compile((ROOT / remote.DOCTOR_PATH).read_bytes(), str(ROOT / remote.DOCTOR_PATH), "exec"), namespace)
        module.required_checks, module.accepted = namespace["required_checks"], namespace["accepted"]
        return module

    def baseline(self):
        self.checks.bootstrap()
        self.checks.doctors()
        for runtime in ("openclaw", "hermes"):
            self.checks.runtime_task(runtime)
        for cli in ("codex", "claude"):
            self.checks.workspace_task(cli)
        self.checks.network_checks("before_reboot", self.peer_execute)
        self.models.close()

    def peer_execute(self, access, command, payload):
        self.peer_calls.append((access, command, payload))
        request = payload["browser"]
        peer = {"runner_dns": request["runner_dns"], "target_dns": request["target_dns"], "address": "100.100.2.3",
            "runner_addresses": ["100.100.1.2"], "traffic": "tailscale_ping", "dns": "matches_peer"}
        ssh = {role: {**peer_fixtures.ssh_proof(role), "nonce": payload["ssh"]["nonce"], "boot_id": payload["ssh"]["boot_id"]}
            for role in ("host", "workspace")}
        report = {"schema": 1, "status": "pass", "request_id": request["request_id"], "peer": peer, "ssh": ssh,
            "duration_seconds": 1.5, "browser_duration_seconds": 1, "apps": {
                "openclaw": {"origin": "https://" + request["target_dns"], "authentication": "gateway_hello", "rendered": True, "read_rpc": True},
                "hermes": {"origin": "https://" + request["target_dns"] + ":8443", "authentication": "basic_session", "rendered": True, "gateway_running": True, "version": "fixture"}}}
        if self.peer_mutation: self.peer_mutation(report)
        return json.dumps(report).encode()

    def test_both_runtime_challenges_are_fresh_single_invocation_and_private(self):
        self.checks.bootstrap()
        self.checks.doctors()
        proofs = [self.checks.runtime_task(runtime) for runtime in ("openclaw", "hermes")]
        self.assertTrue(all(proof["status"] == "pass" for proof in proofs))
        self.assertNotEqual(proofs[0]["request_id"], proofs[1]["request_id"])
        evidence = cleanup.private_read(self.checks.path)
        self.assertNotEqual(evidence["agents"]["openclaw"]["challenge"]["nonce"], evidence["agents"]["hermes"]["challenge"]["nonce"])
        self.assertNotIn(terraform_fixtures.SECRET, json.dumps(evidence))
        self.assertFalse(list((self.run / "repo").rglob("__pycache__")))
        for runtime in ("openclaw", "hermes"):
            with self.assertRaisesRegex(terraform.PolicyError, "runtime_task_already_invoked"):
                self.checks.runtime_task(runtime)
        agent_calls = [call for call in self.calls if call["input"] == (ROOT / remote.AGENT_PATH).read_bytes()]
        self.assertEqual(len(agent_calls), 2)
        self.assertTrue(all(call["timeout"] == 300 for call in agent_calls))

    def test_bad_runtime_answer_invalidates_health_evidence_and_blocks_repeat(self):
        self.checks.bootstrap()
        self.checks.doctors()
        self.agent_mutation = lambda report: report["answer"].update(sum=0)
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_task_failed"):
            self.checks.runtime_task("hermes")
        evidence = cleanup.private_read(self.checks.path)
        self.assertEqual(evidence["agents"]["hermes"]["status"], "uncertain")
        self.assertEqual(evidence["checks"]["hermes.agent_turn"], "uncertain")
        self.assertEqual(evidence["checks"]["service.hermes"], "uncertain")
        self.assertTrue(all(value == "uncertain" for key, value in evidence["checks"].items() if key.startswith("hermes.")))
        for cli in ("codex", "claude"):
            with self.assertRaisesRegex(terraform.PolicyError, "model_session_required"):
                self.checks.workspace_task(cli)
        with self.assertRaisesRegex(terraform.PolicyError, "persistence_baseline_required"):
            self.checks.reboot()
        self.assertFalse(self.rebooted)
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_doctor_proof_required"):
            self.checks.runtime_task("hermes")
        self.checks.doctors()
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_task_already_invoked"):
            self.checks.runtime_task("hermes")

    def test_runtime_task_needs_doctors_and_full_containment_time_before_dispatch(self):
        self.checks.bootstrap()
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_doctor_proof_required"):
            self.checks.runtime_task("openclaw")
        self.checks.doctors()
        self.clock = self.attempt.active(self.ledger)["work_deadline"] - 299
        before = len(self.calls)
        with self.assertRaisesRegex(terraform.PolicyError, "agent_task_time_required"):
            self.checks.runtime_task("openclaw")
        self.assertEqual(len(self.calls), before)
        self.assertEqual(cleanup.private_read(self.checks.path)["agents"], {})

    def test_measured_bootstrap_both_doctors_tasks_and_reboot_keep_disk_and_files(self):
        self.baseline()
        self.assertEqual(self.checks.reboot(), {"reboot": "pass", "workspace_files": "preserved", "doctors": "pass"})
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["tailscale.acceptance"], "uncertain")
        self.checks.network_checks("after_reboot", self.peer_execute)
        evidence = cleanup.private_read(self.checks.path)
        self.assertEqual(evidence["checks"]["tailscale.acceptance"], "pass")
        self.assertNotEqual(evidence["network"]["before_reboot"]["request_id"], evidence["network"]["after_reboot"]["request_id"])
        self.assertEqual(evidence["network"]["after_reboot"]["boot_id"], NEW_BOOT)
        self.assertEqual(evidence["source_revision"], self.prepared["source_revision"])
        self.assertEqual(evidence["baseline"]["boot_id"], BOOT)
        self.assertEqual(evidence["after_reboot"]["boot_id"], NEW_BOOT)
        self.assertEqual(evidence["after_reboot"]["disk_uuid"], DISK)
        self.assertTrue(all(task["status"] == "pass" for task in evidence["tasks"].values()))
        self.assertNotIn(terraform_fixtures.SECRET, json.dumps(evidence))
        self.assertFalse(list((self.run / "repo").rglob("__pycache__")))
        for call in self.calls:
            self.assertNotIn(terraform_fixtures.SECRET, " ".join(call["args"]))
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", call["env"])
            self.assertIn("--batch", call["args"])
            self.assertIn("admin@" + IP, call["args"])
        model_calls = [call for call in self.calls if "--cli" in shlex.split(call["args"][-1])]
        self.assertEqual(len(model_calls), 2)
        self.assertTrue(all(call["timeout"] == 195 and json.loads(call["input"])["role"] in ("codex", "claude") for call in model_calls))
        self.assertTrue(all(terraform_fixtures.SECRET.encode() not in call["input"] for call in model_calls))

    def test_network_private_inputs_source_nonce_and_evidence_are_scoped(self):
        self.checks.bootstrap(); self.checks.doctors()
        proof = self.checks.network_checks("before_reboot", self.peer_execute)
        self.assertEqual(proof["status"], "pass")
        _, command, payload = self.peer_calls[0]
        self.assertIn("python3 -I", command)
        self.assertNotIn(terraform_fixtures.SECRET, command)
        self.assertNotIn(payload["ssh"]["private_key"], command)
        self.assertTrue(payload["browser"]["openclaw_url"].startswith("https://" + PREFIX + ".fixture.ts.net/#"))
        saved = json.dumps(cleanup.private_read(self.checks.path))
        self.assertNotIn(terraform_fixtures.SECRET, saved)
        self.assertNotIn("fake-handoff", saved)
        self.assertNotIn("BEGIN OPENSSH", saved)
        self.assertFalse(list(self.attempt.checkout.rglob("__pycache__")))
        self.session.http.request.assert_called_once()

    def test_missing_peer_transport_refuses_before_secret_collection(self):
        self.checks.bootstrap(); self.checks.doctors()
        self.config["runner"].pop("peer_ssh")
        cleanup.private_write(self.run / "inputs.json", self.config)
        before = len(self.calls)
        with self.assertRaisesRegex(terraform.PolicyError, "existing_peer_access_required"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        self.assertEqual(len(self.calls), before)
        self.session.http.request.assert_not_called()
        self.assertEqual(self.peer_calls, [])

    def test_network_requires_doctors_correct_boot_stage_and_full_deadline(self):
        self.checks.bootstrap()
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_doctor_proof_required"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        self.checks.doctors()
        with self.assertRaisesRegex(terraform.PolicyError, "network_boot_stage_invalid"):
            self.checks.network_checks("after_reboot", self.peer_execute)
        active = self.attempt.active(self.ledger); self.clock = active["work_deadline"] - 749
        before = len(self.calls)
        with self.assertRaisesRegex(terraform.PolicyError, "network_check_time_required"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        self.assertEqual(before, len(self.calls))
        self.assertEqual(self.peer_calls, [])

    def test_revoked_runner_refuses_private_gateway_collection(self):
        self.checks.bootstrap(); self.checks.doctors()
        self.session.http.request.return_value["devices"][0]["authorized"] = False
        with self.assertRaisesRegex(terraform.PolicyError, "network_acceptance_failed"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        self.assertFalse(any(call["input"] == peer_fixtures.peer.VM_SOURCE.encode() for call in self.calls))
        self.assertEqual(self.peer_calls, [])

    def test_bad_private_handoff_key_or_changed_boot_never_dispatched(self):
        self.checks.bootstrap(); self.checks.doctors()
        for changes in ({"browser_url": "https://external.test/#bootstrapToken=secret"}, {"host_public_key": "ssh-ed25519 bad"},
            {"hermes_password": 0}, {"boot_id": NEW_BOOT}, {"extra": terraform_fixtures.SECRET}):
            self.private_mutation = lambda raw, changes=changes: raw.update(changes)
            with self.assertRaisesRegex(terraform.PolicyError, "network_acceptance_failed"):
                self.checks.network_checks("before_reboot", self.peer_execute)
            self.assertEqual(self.peer_calls, [])
            self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["tailscale.acceptance"], "uncertain")

    def test_stale_network_proof_replaces_prior_green_and_blocks_reboot(self):
        self.baseline()
        before = cleanup.private_read(self.checks.path)["network"]["before_reboot"]["request_id"]
        self.peer_mutation = lambda raw: raw.update(request_id=before)
        with self.assertRaisesRegex(terraform.PolicyError, "network_acceptance_failed"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        saved = cleanup.private_read(self.checks.path)
        self.assertEqual(saved["network"]["before_reboot"]["status"], "uncertain")
        self.assertEqual(saved["checks"]["tailscale.acceptance"], "uncertain")
        with self.assertRaisesRegex(terraform.PolicyError, "peer_baseline_required"):
            self.checks.reboot()

    def test_changed_initial_boot_invalidates_prior_network_proof_before_peer_dispatch(self):
        self.baseline()
        self.boot = NEW_BOOT
        before = len(self.peer_calls)
        with self.assertRaisesRegex(terraform.PolicyError, "network_acceptance_failed"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        saved = cleanup.private_read(self.checks.path)
        self.assertEqual(saved["network"]["before_reboot"]["status"], "uncertain")
        self.assertEqual(saved["checks"]["tailscale.acceptance"], "uncertain")
        self.assertEqual(len(self.peer_calls), before)
        with self.assertRaisesRegex(terraform.PolicyError, "peer_baseline_required"):
            self.checks.reboot()

    def test_peer_budget_and_boot_are_rechecked_after_collection_and_dispatch(self):
        self.checks.bootstrap(); self.checks.doctors()
        work_deadline = self.attempt.active(self.ledger)["work_deadline"]
        self.private_mutation = lambda raw: setattr(self, 'clock', work_deadline - 249)
        with self.assertRaisesRegex(terraform.PolicyError, "network_acceptance_failed"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        self.assertEqual(self.peer_calls, [])
        self.clock, self.private_mutation = NOW, None
        self.peer_mutation = lambda raw: setattr(self, 'boot', NEW_BOOT)
        with self.assertRaisesRegex(terraform.PolicyError, "network_acceptance_failed"):
            self.checks.network_checks("before_reboot", self.peer_execute)
        self.assertEqual(cleanup.private_read(self.checks.path)["network"]["before_reboot"]["status"], "uncertain")

    def test_secret_reader_failure_stops_and_raw_errors_are_redacted(self):
        self.checks.bootstrap(); self.checks.doctors()
        def locked(*args):
            raise remote.PreflightError('onepassword_unavailable_or_locked')
        with self.assertRaisesRegex(terraform.PolicyError, '^onepassword_unavailable_or_locked$'):
            self.checks.network_checks("before_reboot", locked)
        def raw_error(*args):
            raise RuntimeError(terraform_fixtures.SECRET)
        with self.assertRaisesRegex(terraform.PolicyError, '^network_acceptance_failed$') as caught:
            self.checks.network_checks("before_reboot", raw_error)
        self.assertNotIn(terraform_fixtures.SECRET, str(caught.exception))
        self.assertNotIn(terraform_fixtures.SECRET, json.dumps(cleanup.private_read(self.checks.path)))

    def test_edited_public_ip_unowned_state_or_modified_source_fail_before_ssh(self):
        self.api_ip = "1.1.1.1"
        with self.assertRaisesRegex(terraform.PolicyError, "owned_vm_address_unverified"):
            self.checks.bootstrap()
        self.assertEqual(self.calls, [])
        self.api_ip = IP
        self.state["resources"][1]["instances"][0]["attributes"]["id"] = "i-ffffff"
        cleanup.private_write(self.attempt.state_path, self.state)
        with self.assertRaisesRegex(terraform.PolicyError, "state_resource_not_in_inventory"):
            self.checks.bootstrap()
        self.assertEqual(self.calls, [])
        (self.run / "repo/bin/agent-stack-ssh").write_text("modified")
        with self.assertRaisesRegex(terraform.PolicyError, "prepared_source_changed"):
            self.checks.bootstrap()

    def test_failed_doctor_and_missing_file_verification_never_mark_checks_passed(self):
        self.checks.bootstrap()
        self.fail_doctor = True
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_doctor_failed"):
            self.checks.doctors()
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["openclaw.doctor"], "uncertain")
        self.fail_verifier = True
        with self.assertRaises(terraform.PolicyError):
            self.checks.workspace_task("codex")
        evidence = cleanup.private_read(self.checks.path)
        self.assertEqual(evidence["tasks"]["codex"]["status"], "uncertain")
        self.assertEqual(evidence["checks"]["workspace.codex.execution"], "uncertain")
        before = len(self.calls)
        with self.assertRaisesRegex(terraform.PolicyError, "workspace_task_already_invoked"):
            self.checks.workspace_task("codex")
        self.assertEqual(len(self.calls), before)

    def test_malformed_usage_wrong_cli_fixture_path_or_hash_cannot_pass(self):
        changes = ({"model_usage": {}}, {"cli": "claude"}, {"fixture": "../outside"}, {"input_sha256": ""}, {"verified": False})
        for change in changes:
            fresh = RemoteChecksTest("runTest"); fresh.setUp(); self.addCleanup(fresh.doCleanups)
            fresh.checks.bootstrap()
            fresh.task_mutation = lambda report: report.update(change)
            with self.assertRaisesRegex(terraform.PolicyError, "workspace_task_failed"):
                fresh.checks.workspace_task("codex")
            self.assertEqual(cleanup.private_read(fresh.checks.path)["tasks"]["codex"]["status"], "uncertain")

    def test_root_verifier_cannot_pass(self):
        self.checks.bootstrap()
        self.file_mutation = lambda report: report.update(uid=0)
        with self.assertRaisesRegex(terraform.PolicyError, "workspace_file_verification_failed"):
            self.checks.workspace_task("codex")

    def test_reboot_needs_new_boot_id_and_unchanged_disk_and_results(self):
        self.baseline()
        self.change_boot = False
        with self.assertRaisesRegex(terraform.PolicyError, "new_boot_not_verified"):
            self.checks.reboot()
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["reboot"], "uncertain")
        with self.assertRaisesRegex(terraform.PolicyError, "reboot_already_invoked"):
            self.checks.reboot()

    def test_changed_persistent_disk_after_reboot_fails(self):
        self.baseline()
        execute = self.execute
        def changed(*args, **kwargs):
            result = execute(*args, **kwargs)
            if self.rebooted and kwargs.get("input") == remote.BOOTSTRAP.encode():
                report = json.loads(result)
                report["disk_uuid"] = "44444444-4444-4444-4444-444444444444"
                return json.dumps(report).encode()
            return result
        self.checks.execute = changed
        with self.assertRaisesRegex(terraform.PolicyError, "persistent_disk_or_runtime_changed"):
            self.checks.reboot()

    def test_failed_rechecks_replace_previous_green_evidence(self):
        self.checks.bootstrap()
        self.checks.doctors()
        self.fail_doctor = True
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_doctor_failed"):
            self.checks.doctors()
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["openclaw.doctor"], "uncertain")
        self.boot = NEW_BOOT
        with self.assertRaisesRegex(terraform.PolicyError, "persistence_baseline_changed"):
            self.checks.bootstrap()
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["launch_ssh"], "uncertain")

    def test_interrupted_reboot_can_resume_verification_without_rebooting_again(self):
        self.baseline()
        self.fail_doctor = True
        with self.assertRaisesRegex(terraform.PolicyError, "runtime_doctor_failed"):
            self.checks.reboot()
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["reboot"], "uncertain")
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["openclaw.doctor"], "uncertain")
        self.fail_doctor = False
        self.assertEqual(self.checks.reboot(resume=True)["reboot"], "pass")
        self.assertEqual(cleanup.private_read(self.checks.path)["checks"]["openclaw.doctor"], "pass")
        reboot_commands = [call for call in self.calls if shlex.split(call["args"][-1]) == ["sudo", "-n", "systemctl", "reboot"]]
        self.assertEqual(len(reboot_commands), 1)

    def test_expired_or_incomplete_apply_refuses_remote_work(self):
        self.clock = NOW + 15000
        with self.assertRaisesRegex(terraform.PolicyError, "deadline_reached"):
            self.checks.bootstrap()
        self.clock = NOW
        record = cleanup.private_read(self.run / "terraform.json")
        record["stage"] = "apply_uncertain"
        cleanup.private_write(self.run / "terraform.json", record)
        with self.assertRaisesRegex(terraform.PolicyError, "applied_attempt_required"):
            self.checks.bootstrap()
        self.assertEqual(self.calls, [])


class AddressTest(unittest.TestCase):
    def test_do_and_hetzner_addresses_must_match_running_observed_vm(self):
        adapter = Mock()
        for provider in ("digitalocean", "hetzner"):
            vm = {"id": 1234, "name": PREFIX, "status": "active" if provider == "digitalocean" else "running"}
            vm.update(networks={"v4": [{"ip_address": IP, "type": "public"}]} if provider == "digitalocean" else {})
            if provider == "hetzner":
                vm["public_net"] = {"ipv4": {"ip": IP}}
            key = "droplet" if provider == "digitalocean" else "server"
            adapter.get.return_value = {key: vm}
            values = {"id": "1234", "name": PREFIX, "ipv4_address": IP}
            self.assertEqual(remote.matching_address(adapter, provider, values), IP)
            with self.assertRaisesRegex(terraform.PolicyError, "owned_vm_address_unverified"):
                remote.matching_address(adapter, provider, {**values, "ipv4_address": "1.1.1.1"})

    def test_gcp_instance_zone_and_nat_address_are_requeried_in_original_project(self):
        adapter = Mock()
        adapter.cloud = config()["providers"]["gcp"]
        name = PREFIX + "-instance"
        identifier = "projects/agent-stack-fixture/zones/us-east1-b/instances/" + name
        adapter.get.return_value = {"name": name, "status": "RUNNING", "networkInterfaces": [{"accessConfigs": [{"natIP": IP}]}]}
        values = {"id": identifier, "name": name, "network_interface": [{"access_config": [{"nat_ip": IP}]}]}
        self.assertEqual(remote.matching_address(adapter, "gcp", values), IP)
        adapter.get.assert_called_once_with("/projects/agent-stack-fixture/zones/us-east1-b/instances/" + name)
        with self.assertRaisesRegex(terraform.PolicyError, "owned_vm_scope_mismatch"):
            remote.matching_address(adapter, "gcp", {**values, "id": identifier.replace("us-east1-b", "us-east1-c")})


@unittest.skipUnless(os.geteuid() == 1000, "independent workspace file verifier runs as UID 1000")
class FileVerifierTest(unittest.TestCase):
    def test_actual_verifier_rejects_duplicate_nonfinite_or_ambiguous_files_and_hashes_canonical_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); fixture = ".agent-stack/acceptance/codex-fresh0001"
            directory = home / fixture; directory.mkdir(parents=True)
            source, result = directory / "input.json", directory / "result.json"
            challenge = {"lhs": 17, "rhs": 26, "nonce": "a1" * 16}
            answer = {"sum": 43, "nonce": challenge["nonce"]}
            source.write_text(json.dumps(challenge, sort_keys=True, separators=(",", ":")))
            code = remote.VERIFY_FILES.replace("/home/user", temp)
            def invoke():
                return subprocess.run([sys.executable, "-c", code, fixture, hashlib.sha256(source.read_bytes()).hexdigest()],
                                      env={**os.environ, "HOME": temp}, capture_output=True, timeout=10)
            for text in (json.dumps(answer), json.dumps(answer, sort_keys=True, separators=(",", ":"))):
                result.write_text(text)
                report = json.loads(invoke().stdout)
                self.assertEqual(report["answer_sha256"], remote.task_evidence.expected_answer(challenge))
                self.assertEqual(report["result_sha256"], hashlib.sha256(text.encode()).hexdigest())
            text = json.dumps(answer)
            for invalid in (text[:-1] + ',"sum":43}', text.replace('43', 'NaN'), text[:-1] + ',"extra":1}', text.replace('43', 'true')):
                result.write_text(invalid); self.assertEqual(invoke().returncode, 1)
            result.write_text(text)
            source.write_text(json.dumps(challenge)[:-1] + ',"lhs":17}')
            self.assertEqual(invoke().returncode, 1)

    def test_actual_verifier_checks_nonce_sum_input_hash_and_regular_owned_files(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixture = ".agent-stack/acceptance/codex-fresh0001"
            directory = home / fixture
            directory.mkdir(parents=True)
            source = directory / "input.json"
            result = directory / "result.json"
            source.write_text(json.dumps({"lhs": 7, "rhs": 8, "nonce": "a" * 32}))
            original = source.read_bytes()
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            code = remote.VERIFY_FILES.replace("/home/user", temp)
            def invoke():
                process = subprocess.run([sys.executable, "-c", code, fixture, digest], env={**os.environ, "HOME": temp}, capture_output=True, timeout=10)
                return process.returncode, json.loads(process.stdout)
            result.write_text(json.dumps({"sum": 15, "nonce": "a" * 32}))
            self.assertEqual(invoke()[1]["status"], "pass")
            for change in ({"sum": 0, "nonce": "a" * 32}, {"sum": 15, "nonce": "b" * 32}, {"sum": 15, "nonce": "a" * 32, "extra": True}):
                result.write_text(json.dumps(change))
                self.assertEqual(invoke()[0], 1)
            result.write_text(json.dumps({"sum": 15, "nonce": "a" * 32}))
            source.write_text(json.dumps({"lhs": 8, "rhs": 7, "nonce": "a" * 32}))
            self.assertEqual(invoke()[0], 1)
            source.write_bytes(original)
            result.unlink()
            result.symlink_to(source)
            self.assertEqual(invoke()[0], 1)

    def test_parent_symlink_or_changed_input_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "outside").mkdir()
            (home / ".agent-stack").symlink_to(home / "outside")
            fixture = ".agent-stack/acceptance/codex-fresh0001"
            directory = home / fixture
            directory.mkdir(parents=True)
            source, result = directory / "input.json", directory / "result.json"
            source.write_text(json.dumps({"lhs": 7, "rhs": 8, "nonce": "a" * 32}))
            result.write_text(json.dumps({"sum": 15, "nonce": "a" * 32}))
            code = remote.VERIFY_FILES.replace("/home/user", temp)
            process = subprocess.run([sys.executable, "-c", code, fixture, hashlib.sha256(source.read_bytes()).hexdigest()],
                                     env={**os.environ, "HOME": temp}, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 1)
