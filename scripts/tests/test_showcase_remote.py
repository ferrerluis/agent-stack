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
import showcase_terraform as terraform
import showcase_cleanup as cleanup
import test_showcase_attempt as preparation_fixtures
import test_showcase_terraform as terraform_fixtures
from test_showcase_policy import config, quote, NOW
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
PREFIX = terraform_fixtures.PREFIX
IP = "8.8.8.8"  # Fixture only: every SSH/provider operation below is replaced.
CHECKSUM = "a" * 64
BOOT = "11111111-1111-1111-1111-111111111111"
NEW_BOOT = "22222222-2222-2222-2222-222222222222"
DISK = "33333333-3333-3333-3333-333333333333"


class RemoteChecksTest(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixtures.PreparationTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("bin/agent-stack-ssh", remote.DOCTOR_PATH):
            target = self.fixture.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
            target.chmod(0o755)
        self.fixture.git("add", ".")
        self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "remote fixture")
        self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip()
        self.prepared = self.fixture.prepare()
        self.run = self.fixture.batch / "attempts" / PREFIX
        self.config = config()
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
        self.checks = remote.RemoteChecks(self.attempt, self.ledger, execute=self.execute, now=lambda: self.clock, sleep=self.sleep)

    def sleep(self, amount):
        self.clock += amount

    def execute(self, args, env, timeout, input):
        self.calls.append({"args": args, "env": env, "input": input, "timeout": timeout})
        words = shlex.split(args[-1])
        if input == remote.BOOTSTRAP.encode():
            return json.dumps({"schema": 1, "status": "pass", "checksum": CHECKSUM, "dns": PREFIX + ".fixture.ts.net",
                "boot_id": self.boot, "disk_uuid": self.disk, "data_volume_id": "vol-a3"}).encode()
        if "--remote" in words:
            module = self.doctor()
            names = module.required_checks(["openclaw", "hermes", "workspace"], True, True)
            report = {"schema": 1, "status": "pass", "checks": [{"name": name, "status": "pass"} for name in sorted(names)]}
            if self.fail_doctor:
                report["checks"][0]["status"] = "fail"
            return json.dumps(report).encode()
        if "--cli" in words:
            cli = words[words.index("--cli") + 1]
            report = {"schema": 1, "cli": cli, "status": "pass", "verified": True,
                "model_usage": {"input_tokens": 30, "output_tokens": 20}, "fixture": ".agent-stack/acceptance/" + cli + "-fresh0001",
                "input_sha256": ("b" if cli == "codex" else "c") * 64, "duration_seconds": 1.25}
            if self.task_mutation:
                self.task_mutation(report)
            return json.dumps(report).encode()
        if remote.VERIFY_FILES in words:
            if self.fail_verifier:
                raise terraform.PolicyError("command_failed")
            digest = ("d" if "codex-" in words[-2] else "e") * 64
            report = {"schema": 1, "status": "pass", "uid": 1000, "input_sha256": words[-1], "result_sha256": digest}
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
        for cli in ("codex", "claude"):
            self.checks.workspace_task(cli)

    def test_measured_bootstrap_both_doctors_tasks_and_reboot_keep_disk_and_files(self):
        self.baseline()
        self.assertEqual(self.checks.reboot(), {"reboot": "pass", "workspace_files": "preserved", "doctors": "pass"})
        evidence = cleanup.private_read(self.checks.path)
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
        self.assertTrue(all(call["input"] == terraform_fixtures.SECRET.encode() and call["timeout"] == 195 for call in model_calls))

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
        self.assertNotIn("workspace.codex.execution", evidence["checks"])
        before = len(self.calls)
        with self.assertRaisesRegex(terraform.PolicyError, "workspace_task_already_invoked"):
            self.checks.workspace_task("codex")
        self.assertEqual(len(self.calls), before)

    def test_malformed_usage_wrong_cli_fixture_path_or_hash_cannot_pass(self):
        self.checks.bootstrap()
        changes = ({"model_usage": {}}, {"cli": "claude"}, {"fixture": "../outside"}, {"input_sha256": ""}, {"verified": False})
        for change in changes:
            evidence = cleanup.private_read(self.checks.path)
            evidence["tasks"] = {}  # test-only independent fresh invocation fixture
            cleanup.private_write(self.checks.path, evidence)
            self.task_mutation = lambda report: report.update(change)
            with self.assertRaisesRegex(terraform.PolicyError, "workspace_task_failed"):
                self.checks.workspace_task("codex")
            self.assertEqual(cleanup.private_read(self.checks.path)["tasks"]["codex"]["status"], "uncertain")

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
        self.fail_doctor = False
        self.assertEqual(self.checks.reboot(resume=True)["reboot"], "pass")
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
