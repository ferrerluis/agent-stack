import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_terraform as runner
import showcase_live as live
import showcase_cleanup as cleanup
import test_showcase_attempt as preparation_fixtures
from test_showcase_policy import config, quote, NOW, REF

PREFIX = "as7-fixture01"
SECRET = "fixture-only-private-provider-key"


def plan_fixture(provider="aws"):
    kinds = {"aws": {"aws_vpc": ("vpc", "vpc-a1"), "aws_instance": ("instance", "i-a2"), "aws_ebs_volume": ("volume", "vol-a3")},
             "gcp": {"google_compute_network": ("network", "projects/agent-stack-fixture/global/networks/" + PREFIX + "-network"),
                     "google_compute_instance": ("instance", "projects/agent-stack-fixture/zones/us-east1-b/instances/" + PREFIX + "-instance"),
                     "google_compute_disk": ("disk", "projects/agent-stack-fixture/zones/us-east1-b/disks/" + PREFIX + "-disk")},
             "digitalocean": {"digitalocean_droplet": ("droplet", "1234"), "digitalocean_volume": ("volume", "fixture-volume")},
             "hetzner": {"hcloud_server": ("server", "5678"), "hcloud_volume": ("volume", "9012")}}[provider]
    resources, state, inventory = [], [], []
    for index, (kind, (api_kind, identifier)) in enumerate(kinds.items()):
        name = PREFIX + "-" + api_kind
        values = {"tags": {"Name": name}} if provider == "aws" else {"name": name}
        resources.append({"address": "module." + provider + "[0]." + kind + ".this", "type": kind, "mode": "managed",
                          "change": {"actions": ["create"], "before": None, "after": values}})
        state.append({"mode": "managed", "module": "module." + provider + "[0]", "type": kind, "name": "this",
                      "instances": [{"attributes": {**copy.deepcopy(values), "id": identifier}}]})
        inventory.append({"kind": api_kind, "id": str(index + 1) if provider == "gcp" else identifier, "name": name,
                          "locator": identifier.split("/", 2)[2] if provider == "gcp" else identifier})
    if provider == "digitalocean":
        project = config()["providers"][provider]["project_id"]
        resources.append({"address": "module.digitalocean[0].digitalocean_project_resources.this", "type": "digitalocean_project_resources", "mode": "managed",
                          "change": {"actions": ["create"], "before": None, "after": {"project": project, "resources": None}}})
        state.append({"module": "module.digitalocean[0]", "mode": "managed", "type": "digitalocean_project_resources", "name": "this",
                      "instances": [{"attributes": {"id": project, "project": project, "resources": ["do:droplet:1234", "do:volume:fixture-volume"]}}]})
    return {"resource_changes": resources}, {"version": 4, "resources": state}, inventory


class FakeSession:
    def __init__(self, value):
        self.config = value
        self.adapter = Mock()
        self.adapter.cloud = value["providers"]["aws"]
        self.adapter.call.return_value = {"Account": value["providers"]["aws"]["account_id"]}
        self.reader = Mock(return_value="fixture-password-never-saved")
        self.resources = False
        self.nodes = False
        self.fail_queries = False
        self.fail_node_delete = False
        self.calls = []
        self.environments = []

    def environment(self, run, min_lifetime=0):
        env = runner.isolated_environment(run)
        env["AWS_SECRET_ACCESS_KEY"] = SECRET
        self.environments.append(min_lifetime)
        return env

    def secret(self, reference):
        return SECRET

    def preflight(self, prefix):
        self.calls.append("preflight")
        return {"status": "read_checks_passed", "launch_ready": False}

    def inventory(self, prefix, previous=None):
        self.calls.append("inventory")
        if self.fail_queries:
            raise runner.PreflightError("provider_http_403")
        rows = plan_fixture()[2] if self.resources else []
        return {"scope": cleanup.scope(self.config, "aws"), "run_id": prefix, "identity": "verified", "resources": rows,
                "unknown_resources": [], "nodes": [{"id": "owned-node", "name": prefix + ".fixture.ts.net", "hostname": prefix}] if self.nodes else [],
                "cloud_queries": 3, "tailnet_queries": 1, "verified_at": NOW + 100}

    def delete_nodes(self, record):
        self.calls.append("delete_nodes")
        if self.fail_node_delete:
            raise runner.PreflightError("tailnet_delete_failed")
        self.nodes = False


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixtures.PreparationTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in getattr(self, "fixture_sources", ()):
            target = self.fixture.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((Path(runner.__file__).parents[1] / name).read_bytes())
        if getattr(self, "fixture_sources", ()):
            self.fixture.git("add", ".")
            self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "controller source fixture")
            self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip()
        self.fixture.prepare()
        self.config = config()
        self.run = self.fixture.batch / "attempts" / PREFIX
        self.session = FakeSession(self.config)
        self.commands = []
        self.fail_apply = False
        self.fail_destroy = False
        self.destroy_mutation = None
        self.client = runner.TerraformAttempt(self.run, self.config, "aws", self.session, execute=self.execute, now=lambda: NOW)
        self.ledger = cleanup.Reservations(self.fixture.batch, self.config)

    def execute(self, args, env, timeout):
        self.commands.append((args, dict(env), timeout))
        operation = args[2]
        if operation == "version":
            return b'{"terraform_version":"1.16.5"}'
        if operation == "init":
            (self.run / "repo/.terraform.lock.hcl").write_text("fixture-provider-lock")
            return b"initialized"
        if operation == "plan":
            path = Path(next(arg[5:] for arg in args if arg.startswith("-out=")))
            path.write_bytes(b"fixture-private-binary-plan")
            path.chmod(0o600)
            return b"planned"
        if operation == "show":
            if args[-1].endswith("destroy.tfplan"):
                before = runner.state_resources(cleanup.private_read(self.run / "repo/terraform.tfstate"))
                plan = {"resource_changes": [{"address": address, "mode": "managed", "type": row["type"],
                         "change": {"actions": ["delete"], "before": row["values"], "after": None}} for address, row in before.items()]}
                if self.destroy_mutation:
                    self.destroy_mutation(plan)
                return json.dumps(plan).encode()
            return json.dumps(plan_fixture()[0]).encode()
        if operation == "apply":
            if args[-1].endswith("deployment.tfplan"):
                cleanup.private_write(self.run / "repo/terraform.tfstate", plan_fixture()[1])
                self.session.resources = self.session.nodes = True
                if self.fail_apply:
                    raise runner.PolicyError("command_timed_out")
            else:
                if self.fail_destroy:
                    raise runner.PolicyError("command_timed_out")
                cleanup.private_write(self.run / "repo/terraform.tfstate", {"version": 4, "resources": []})
                self.session.resources = False
            return b"applied"
        raise AssertionError("Unexpected fixture operation")

    def readiness(self):
        return {"source_revision": self.fixture.revision, "scope": cleanup.scope(self.config, "aws"),
                "checks": {key: "pass" for key in runner.LAUNCH_CHECKS}}

    def reserve(self):
        return self.ledger.reserve("aws", PREFIX, quote(), now=NOW)

    def applied(self):
        self.client.plan()
        self.reserve()
        self.client.apply(self.ledger, self.readiness())

    def test_saved_plan_apply_and_independently_verified_cleanup_keep_reservation_history(self):
        self.applied()
        self.assertEqual(self.client.record()["stage"], "applied")
        result = self.client.cleanup(self.ledger)
        self.assertEqual(result["cleanup"], "verified")
        self.assertEqual(self.client.record()["stage"], "cleaned")
        self.assertEqual(self.session.calls, ["preflight", "preflight", "inventory", "delete_nodes", "inventory"])
        ledger = cleanup.private_read(self.fixture.batch / "reservations.json")
        self.assertEqual(ledger["attempts"][0]["status"], "cleaned")
        self.assertEqual(ledger["attempts"][0]["reserved_usd"], "2.00")
        self.assertEqual(ledger["attempts"][0]["cleanup_receipt"]["terraform_state_proof"], "destroyed_empty_local_state")
        self.session.reader.assert_called_once()
        for path in self.run.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"fixture-password-never-saved", path.read_bytes())
        self.assertTrue(any("TF_VAR_workspace_password_hash" in env for _, env, _ in self.commands))
        self.assertTrue(all("TF_VAR_workspace_password" not in env and "-auto-approve" not in args for args, env, _ in self.commands))

    def test_missing_readiness_or_reservation_never_starts_apply(self):
        self.client.plan()
        for proof in (None, {}, {**self.readiness(), "checks": {}}):
            with self.assertRaises(runner.PolicyError):
                self.client.apply(self.ledger, proof)
        with self.assertRaises(runner.PolicyError):
            self.client.apply(self.ledger, self.readiness())
        self.assertFalse(any(args[2] == "apply" for args, _, _ in self.commands))

    def test_changed_binary_plan_or_provider_lock_refuses_apply(self):
        self.client.plan()
        self.reserve()
        for name in ("deployment.tfplan", "repo/.terraform.lock.hcl"):
            path = self.run / name
            old = path.read_bytes()
            path.write_bytes(b"changed")
            with self.assertRaises(runner.PolicyError):
                self.client.apply(self.ledger, self.readiness())
            path.write_bytes(old)
        self.assertFalse(any(args[2] == "apply" for args, _, _ in self.commands))

    def test_partial_apply_is_recorded_uncertain_and_cleanup_still_runs(self):
        self.client.plan()
        self.reserve()
        self.fail_apply = True
        with self.assertRaises(runner.PolicyError):
            self.client.apply(self.ledger, self.readiness())
        self.assertEqual(self.client.record()["stage"], "apply_uncertain")
        self.assertEqual(self.client.cleanup(self.ledger)["cleanup"], "verified")
        self.assertFalse(self.session.resources)

    def test_second_apply_is_refused_and_cleanup_works_after_deadline(self):
        self.applied()
        with self.assertRaises(runner.PolicyError):
            self.client.apply(self.ledger, self.readiness())
        self.client.now = lambda: NOW + 15000
        self.assertEqual(self.client.cleanup(self.ledger)["cleanup"], "verified")

    def test_expired_unapplied_reservation_can_be_cancelled_without_fake_state_or_destroy(self):
        self.client.plan()
        self.reserve()
        self.client.now = lambda: NOW + 15000
        with self.assertRaisesRegex(runner.PolicyError, "deadline_reached"):
            self.client.apply(self.ledger, self.readiness())
        self.assertEqual(self.client.record()["stage"], "planned")
        self.assertEqual(self.client.cleanup(self.ledger)["cleanup"], "verified")
        self.assertFalse(self.client.state_path.exists())
        self.assertFalse(any(args[2] == "apply" for args, _, _ in self.commands))
        proof = cleanup.private_read(self.run / "cleanup-receipt.json")
        self.assertEqual(proof["terraform_state_proof"], "never_applied_verified_absent")

    def test_failed_destroy_or_node_deletion_retains_recovery_and_blocks_new_launch(self):
        self.applied()
        self.fail_destroy = True
        with self.assertRaises(runner.PolicyError):
            self.client.cleanup(self.ledger)
        self.assertEqual(self.client.record()["stage"], "cleanup_failed")
        self.assertTrue((self.run / "inventory.json").exists())
        with self.assertRaises(runner.PolicyError):
            self.ledger.reserve("aws", "as7-other-fixture", quote(), now=NOW)
        self.fail_destroy = False
        self.session.fail_node_delete = True
        with self.assertRaises(runner.PreflightError):
            self.client.cleanup(self.ledger)
        self.assertTrue(self.session.nodes)
        self.session.fail_node_delete = False
        self.assertEqual(self.client.cleanup(self.ledger)["cleanup"], "verified")

    def test_owned_orphan_or_failed_independent_query_cannot_confirm_cleanup(self):
        self.applied()
        original = self.session.inventory
        def orphan(*args, **kwargs):
            result = original(*args, **kwargs)
            if not self.session.resources:
                result["resources"] = [plan_fixture()[2][2]]
            return result
        with patch.object(self.session, "inventory", side_effect=orphan), self.assertRaises(runner.PreflightError):
            self.client.cleanup(self.ledger)
        self.assertEqual(self.client.record()["stage"], "cleanup_failed")
        self.session.fail_queries = True
        with self.assertRaises(runner.PreflightError):
            self.client.cleanup(self.ledger)
        self.assertEqual(cleanup.private_read(self.fixture.batch / "reservations.json")["attempts"][0]["status"], "active")

    def test_unplanned_foreign_state_or_create_during_destroy_never_runs_destroy_apply(self):
        self.applied()
        path = self.run / "repo/terraform.tfstate"
        original = cleanup.private_read(path)
        changed = copy.deepcopy(original)
        changed["resources"][0]["instances"][0]["attributes"]["id"] = "vpc-ffffff"
        cleanup.private_write(path, changed)
        with self.assertRaises(runner.PolicyError):
            self.client.cleanup(self.ledger)
        cleanup.private_write(path, original)
        self.destroy_mutation = lambda plan: plan["resource_changes"][0]["change"].update(actions=["delete", "create"])
        with self.assertRaises(runner.PolicyError):
            self.client.cleanup(self.ledger)
        self.assertFalse(any(args[2] == "apply" and args[-1].endswith("destroy.tfplan") for args, _, _ in self.commands))

    def test_missing_state_after_apply_is_uncertain_and_keeps_captured_ids(self):
        self.applied()
        self.client.state_path.unlink()
        with self.assertRaisesRegex(runner.PolicyError, "missing_state_after_apply"):
            self.client.cleanup(self.ledger)
        self.assertEqual(len(cleanup.private_read(self.run / "inventory.json")["resources"]), 3)
        self.assertFalse((self.run / "cleanup-receipt.json").exists())

    def test_swapped_destroy_plan_before_execution_never_runs_apply(self):
        self.applied()
        original = self.session.environment
        def environment(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.client.record()["stage"] == "destroy_started":
                (self.run / "destroy.tfplan").write_bytes(b"swapped-binary-plan")
            return result
        self.session.environment = environment
        with self.assertRaisesRegex(runner.PolicyError, "saved_plan_changed"):
            self.client.cleanup(self.ledger)
        self.assertEqual(self.client.record()["stage"], "cleanup_failed")
        self.assertFalse(any(args[2] == "apply" and args[-1].endswith("destroy.tfplan") for args, _, _ in self.commands))

    def test_live_entrypoint_runs_saved_plan_and_cleanup_without_printing_private_output(self):
        inputs = self.fixture.root / "private-inputs.json"
        cleanup.private_write(inputs, self.config)
        common = ["live", "--provider", "aws", "--run-id", PREFIX, "--batch", str(self.fixture.batch), "--config", str(inputs)]
        def invoke(phase):
            output = io.StringIO()
            with patch.dict(os.environ, {"AGENT_STACK_LIVE_TESTS": "1"}), patch.object(sys, "argv", common + ["--phase", phase]), \
                 patch.object(live.signal, "signal"), \
                 patch.object(live, "CloudSession", return_value=self.session), patch.object(live, "TerraformAttempt", return_value=self.client), \
                 contextlib.redirect_stdout(output):
                status = live.main()
            self.assertNotIn(SECRET, output.getvalue())
            return status, json.loads(output.getvalue())
        status, report = invoke("plan")
        self.assertEqual(status, 0, report)
        self.assertEqual(report["status"], "planned")
        self.assertFalse(report["launch_ready"])
        self.assertFalse(report["spend_reserved"])
        self.assertFalse((self.fixture.batch / "reservations.json").exists())
        status, report = invoke("cleanup")
        self.assertEqual(status, 1)
        self.assertEqual(report["reason"], "active_attempt_scope_required")
        self.reserve()
        self.client.apply(self.ledger, self.readiness())
        status, report = invoke("cleanup")
        self.assertEqual(status, 0)
        self.assertEqual(report["cleanup"], "verified")
        self.assertEqual(cleanup.private_read(self.fixture.batch / "reservations.json")["attempts"][0]["status"], "cleaned")

    def test_record_exists_before_apply_child_and_exclusive_lock_refuses_concurrent_attempt(self):
        self.client.plan()
        self.reserve()
        original = self.client.execute
        def execute(args, **kwargs):
            if args[2] == "apply":
                self.assertEqual(self.client.record()["stage"], "apply_started")
                with self.assertRaisesRegex(runner.PolicyError, "attempt_busy"):
                    self.client.apply(self.ledger, self.readiness())
            return original(args, **kwargs)
        self.client.execute = execute
        self.client.apply(self.ledger, self.readiness())


class GuardTest(unittest.TestCase):
    def record(self, provider="aws"):
        return {"schema": 1, "scope": cleanup.scope(config(), provider), "run_id": PREFIX, "captured_at": NOW,
                "resources": plan_fixture(provider)[2], "tailnet": {"name": "fixture-tailnet", "runner_dns": "runner.fixture.ts.net", "devices": []}}

    def test_gcp_full_project_zone_and_global_locators_match_actual_state(self):
        plan, state, _ = plan_fixture("gcp")
        self.assertEqual(len(runner.guard_state(state, plan, config(), "gcp", PREFIX, "/tmp/private-run", self.record("gcp"))), 3)
        state["resources"][0]["instances"][0]["attributes"]["id"] = "projects/foreign-project/global/networks/" + PREFIX + "-network"
        with self.assertRaises(runner.PolicyError):
            runner.guard_state(state, plan, config(), "gcp", PREFIX, "/tmp/private-run", self.record("gcp"))

    def test_do_project_assignments_and_hetzner_ids_are_owned(self):
        for provider in ("digitalocean", "hetzner"):
            plan, state, _ = plan_fixture(provider)
            self.assertEqual(len(runner.guard_state(state, plan, config(), provider, PREFIX, "/tmp/private-run", self.record(provider))),
                             3 if provider == "digitalocean" else 2)
            changed = copy.deepcopy(state)
            changed["resources"][0]["instances"][0]["attributes"]["id"] = "unowned-id"
            with self.assertRaisesRegex(runner.PolicyError, "state_resource_not_in_inventory"):
                runner.guard_state(changed, plan, config(), provider, PREFIX, "/tmp/private-run", self.record(provider))
        plan, state, _ = plan_fixture("digitalocean")
        for changes in ({"project": "foreign-project"}, {"resources": ["do:droplet:9999"]}, {"resources": ["do:database:1234"]}):
            changed = copy.deepcopy(state)
            changed["resources"][-1]["instances"][0]["attributes"].update(changes)
            with self.assertRaisesRegex(runner.PolicyError, "foreign_project_assignment"):
                runner.guard_state(changed, plan, config(), "digitalocean", PREFIX, "/tmp/private-run", self.record("digitalocean"))

    def test_foreign_attachment_is_checked(self):
        plan, state, _ = plan_fixture()
        address = "module.aws[0].aws_volume_attachment.data"
        plan["resource_changes"].append({"address": address, "type": "aws_volume_attachment", "mode": "managed",
            "change": {"actions": ["create"], "before": None, "after": {"volume_id": None, "instance_id": None}, "after_unknown": {"volume_id": True, "instance_id": True}}})
        state["resources"].append({"module": "module.aws[0]", "mode": "managed", "type": "aws_volume_attachment", "name": "data",
                                  "instances": [{"attributes": {"id": "attachment-id", "volume_id": "vol-a3", "instance_id": "i-foreign"}}]})
        with self.assertRaisesRegex(runner.PolicyError, "foreign_state_relationship"):
            runner.guard_state(state, plan, config(), "aws", PREFIX, "/tmp/private-run", self.record())

    def test_route_association_requires_independent_id_and_subnet_match(self):
        plan, state, _ = plan_fixture()
        record = self.record()
        for kind, identifier in (("subnet", "subnet-a4"), ("route_table", "rtb-a5")):
            record["resources"].append({"kind": kind, "id": identifier, "locator": identifier, "name": PREFIX + "-" + kind})
        plan["resource_changes"].append({"address": "module.aws[0].aws_route_table_association.public", "type": "aws_route_table_association", "mode": "managed",
            "change": {"actions": ["create"], "before": None, "after": {"subnet_id": None, "route_table_id": None},
                       "after_unknown": {"subnet_id": True, "route_table_id": True}}})
        state["resources"].append({"module": "module.aws[0]", "mode": "managed", "type": "aws_route_table_association", "name": "public",
            "instances": [{"attributes": {"id": "rtbassoc-a6", "subnet_id": "subnet-a4", "route_table_id": "rtb-a5"}}]})
        adapter = Mock()
        table = {"RouteTableId": "rtb-a5", "Associations": [{"RouteTableAssociationId": "rtbassoc-a6", "SubnetId": "subnet-a4"}]}
        adapter.call.return_value = {"RouteTables": [table]}
        self.assertEqual(len(runner.guard_state(state, plan, config(), "aws", PREFIX, "/tmp/private-run", record, adapter)), 4)
        adapter.call.assert_called_once_with(adapter.ec2, "describe_route_tables", RouteTableIds=["rtb-a5"])
        for changed in ({"RouteTableAssociationId": "rtbassoc-ff", "SubnetId": "subnet-a4"},
                        {"RouteTableAssociationId": "rtbassoc-a6", "SubnetId": "subnet-ff"}):
            adapter.call.return_value = {"RouteTables": [{**table, "Associations": [changed]}]}
            with self.assertRaisesRegex(runner.PolicyError, "foreign_state_relationship"):
                runner.guard_state(state, plan, config(), "aws", PREFIX, "/tmp/private-run", record, adapter)

    def test_changed_state_names_and_invalid_ids_fail_before_deletion(self):
        plan, state, _ = plan_fixture()
        for identifier in (None, True, ""):
            changed = copy.deepcopy(state)
            changed["resources"][0]["instances"][0]["attributes"]["id"] = identifier
            with self.assertRaises(runner.PolicyError):
                runner.guard_state(changed, plan, config(), "aws", PREFIX, "/tmp/private-run", self.record())
        state["resources"][0]["instances"][0]["attributes"]["tags"]["Name"] = PREFIX + "-different"
        with self.assertRaisesRegex(runner.PolicyError, "name_changed"):
            runner.guard_state(state, plan, config(), "aws", PREFIX, "/tmp/private-run", self.record())


class SessionAndCliTest(unittest.TestCase):
    def test_selected_cloud_credentials_are_cached_and_other_clouds_are_never_read(self):
        value = config()
        value["providers"]["aws"]["region"] = "us-east-1"
        reader = Mock(return_value=SECRET)
        factory = Mock(return_value=Mock())
        session = runner.CloudSession(value, "aws", reader, aws_factory=factory)
        reader.assert_called_once_with(REF)
        with tempfile.TemporaryDirectory() as temp:
            env = session.environment(Path(temp))
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], SECRET)
        self.assertNotIn("DIGITALOCEAN_TOKEN", env)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", env)
        self.assertNotIn("TF_VAR_aws_secret_key", env)

    def test_google_token_is_refreshed_before_a_long_command_without_saving_json_key(self):
        session = runner.CloudSession.__new__(runner.CloudSession)
        session.provider = "gcp"
        session.adapter = Mock(token="old", expires_at=NOW + 500, now=lambda: NOW)
        def refresh():
            session.adapter.expires_at = NOW + 3600
            return "new-token"
        session.adapter.access_token.side_effect = refresh
        with tempfile.TemporaryDirectory() as temp:
            env = session.environment(Path(temp), min_lifetime=1860)
        self.assertIsNone(session.adapter.token)
        self.assertEqual(env["GOOGLE_OAUTH_ACCESS_TOKEN"], "new-token")
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", env)
        self.assertNotIn("GOOGLE_CREDENTIALS", env)

    def test_short_google_token_lifetime_refuses_command_environment(self):
        session = runner.CloudSession.__new__(runner.CloudSession)
        session.provider = "gcp"
        session.adapter = Mock(token=None, expires_at=NOW + 120, now=lambda: NOW)
        session.adapter.access_token.return_value = "too-short-token"
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(runner.PolicyError, "google_token_lifetime_insufficient"):
            session.environment(Path(temp), min_lifetime=1860)

    def test_reference_repairs_and_late_cloud_inputs_preserve_original_cleanup_scope_and_pins(self):
        original, current = config(), config()
        current["providers"]["aws"]["credential_refs"]["secret_key"] = "op://vault/changed-item/key"
        current["pins"]["codex_release"] = "0.146.0"
        recovered = live.recovery_inputs(original, current, "aws")
        self.assertEqual(recovered["pins"], original["pins"])
        self.assertEqual(recovered["providers"]["aws"]["credential_refs"], current["providers"]["aws"]["credential_refs"])
        current["providers"]["aws"]["account_id"] = "111111111111"
        with self.assertRaises(runner.PolicyError):
            live.recovery_inputs(original, current, "aws")

    def test_live_cli_requires_opt_in_and_has_no_launch_or_readiness_override(self):
        script = Path(__file__).resolve().parents[1] / "showcase_live.py"
        args = [sys.executable, str(script), "--phase", "plan", "--provider", "aws", "--run-id", PREFIX,
                "--config", "/tmp/missing-private-inputs", "--batch", "/tmp/missing-private-batch"]
        result = subprocess.run(args, env={**os.environ, "AGENT_STACK_LIVE_TESTS": "0"}, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "live_opt_in_required")
        result = subprocess.run(args + ["--readiness", "fake-proof.json"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_ssh_batch_ignores_personal_client_config_and_keeps_changed_key_refusal(self):
        script = Path(__file__).resolve().parents[2] / "bin/agent-stack-ssh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "ssh"
            executable.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            executable.chmod(0o700)
            key = root / "test key"
            key.touch()
            env = {**os.environ, "PATH": str(root) + ":" + os.environ["PATH"]}
            result = subprocess.run([str(script), "--batch", "--known-hosts", str(root / "known hosts"), "--key", str(key),
                                     "--host", "admin@203.0.113.10", "--", "true"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(result.stdout)
            self.assertEqual(args[:2], ["-F", "/dev/null"])
            self.assertIn("StrictHostKeyChecking=accept-new", args)
            self.assertIn("BatchMode=yes", args)
            self.assertIn(str(key), args)
            self.assertEqual(args[-2:], ["admin@203.0.113.10", "true"])


class NodeDeletionTest(unittest.TestCase):
    def http(self, status=200, body=b""):
        response = io.BytesIO(body)
        response.status = status
        result = Mock(queries=0)
        result.opener.open.return_value = response
        return result

    def test_delete_uses_exact_device_tls_endpoint_and_bounded_request(self):
        http = self.http()
        runner.tailnet_delete(http, SECRET, "owned-device_123")
        request = http.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.tailscale.com/api/v2/device/owned-device_123")
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(http.opener.open.call_args.kwargs, {"timeout": 20})
        self.assertEqual(http.queries, 1)
        self.assertNotIn(SECRET, request.full_url)
        self.assertTrue(request.headers["Authorization"].startswith("Basic "))

    def test_invalid_ids_failed_or_redirected_requests_cannot_pass(self):
        for identifier in ("../runner", "id?next=runner", "", None):
            http = self.http()
            with self.assertRaisesRegex(runner.PolicyError, "invalid_tailnet_deletion_target"):
                runner.tailnet_delete(http, SECRET, identifier)
            http.opener.open.assert_not_called()
        for http in (self.http(403), self.http(body=b"x" * 4097)):
            with self.assertRaisesRegex(runner.PolicyError, "tailnet_delete_failed"):
                runner.tailnet_delete(http, SECRET, "owned-device")
            self.assertEqual(http.queries, 0)
        for error in (HTTPError("https://fixture.invalid", 404, "private raw error", {}, io.BytesIO(SECRET.encode())),
                      URLError("private raw error"), runner.PreflightError("provider_redirect_refused")):
            http = self.http()
            http.opener.open.side_effect = error
            with self.assertRaises(runner.PreflightError) as caught:
                runner.tailnet_delete(http, SECRET, "owned-device")
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(http.queries, 0)

    def session_and_record(self):
        session = runner.CloudSession.__new__(runner.CloudSession)
        session.config, session.provider, session.http = config(), "aws", Mock()
        session.secret = Mock(return_value=SECRET)
        record = GuardTest().record()
        record["tailnet"]["devices"] = [{"id": "original-node", "name": PREFIX + ".fixture.ts.net", "hostname": PREFIX}]
        session.inventory = Mock(return_value={"nodes": [
            {"id": "original-node", "name": PREFIX + ".fixture.ts.net"},
            {"id": "new-node", "name": PREFIX + ".fixture.ts.net"},
            {"id": "runner-node", "name": "runner.fixture.ts.net"}]})
        return session, record

    def test_only_previously_captured_node_id_is_removed_never_new_node_or_runner(self):
        session, record = self.session_and_record()
        with patch.object(runner, "tailnet_delete") as delete:
            session.delete_nodes(record)
            delete.assert_called_once_with(session.http, SECRET, "original-node")
        session.inventory.assert_called_once_with(PREFIX, record)
        session.inventory.return_value = {"nodes": []}
        with patch.object(runner, "tailnet_delete") as delete:
            session.delete_nodes(record)
            delete.assert_not_called()

    def test_wrong_scope_runner_or_unapproved_second_node_refuse_deletion(self):
        for changes in ({"name": "runner.fixture.ts.net"}, {"name": PREFIX + "-browser.fixture.ts.net"}):
            session, record = self.session_and_record()
            record["tailnet"]["devices"][0].update(changes)
            with patch.object(runner, "tailnet_delete") as delete, self.assertRaises((runner.PolicyError, runner.PreflightError)):
                session.delete_nodes(record)
            delete.assert_not_called()
        session, record = self.session_and_record()
        record["scope"]["account_id"] = "000000000000"
        with self.assertRaisesRegex(runner.PreflightError, "inventory_scope_mismatch"):
            session.delete_nodes(record)
        session.inventory.assert_not_called()
