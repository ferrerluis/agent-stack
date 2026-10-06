import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "showcase_policy.py"
spec = importlib.util.spec_from_file_location("showcase_policy", SCRIPT)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
NOW = 1791247597
REF = "op://fixture-vault/fixture-item/fixture-field"


def config():
    value = json.loads((SCRIPT.parents[1] / "examples/showcase/execution-inputs.example.json").read_text())
    value["runner"]["ssh_cidr"] = "203.0.113.42/32"
    value["pins"].update(openclaw_version="2026.10.1", openclaw_digest="sha256:" + "a" * 64,
                         hermes_image="ghcr.io/nousresearch/hermes-agent@sha256:" + "b" * 64, codex_release="0.145.0", claude_release="2.1.211")
    value["models"].update(openclaw="openai/fixture-model", hermes="anthropic/fixture-model", codex="fixture-model", claude="fixture-model")
    value["tailnet"].update(runner_dns="runner.fixture.ts.net", name="fixture-tailnet", auth_key_ref=REF, cleanup_key_ref=REF)
    for key in value["secret_refs"]:
        value["secret_refs"][key] = REF
    for provider, cloud in value["providers"].items():
        cloud.update(account_id="123456789012" if provider == "aws" else "fixture-account", region="us-east1", size="fixture-size")
        cloud["credential_refs"] = {key: REF for key in cloud["credential_refs"]}
        if provider != "aws":
            cloud["project_id"] = {"digitalocean": "12345678-1234-1234-1234-123456789abc", "gcp": "agent-stack-fixture", "hetzner": "4242"}[provider]
        if provider == "gcp":
            cloud["zone"] = "us-east1-b"
    return value


def quote(provider="aws", hourly="0.10"):
    return {"provider": provider, "currency": "USD", "verified_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            "source": "https://" + policy.PRICE_HOSTS[provider][0] + "/fixture-price", "hourly_usd": hourly, "fixed_usd": "0.10", "model_usd": "1.00", "cleanup_usd": "0.50"}


class InputTest(unittest.TestCase):
    def test_optional_existing_peer_structure_uses_only_a_key_reference(self):
        value = config()
        value['runner']['peer_ssh'] = {'host': '203.0.113.44', 'port': 22, 'username': 'fixture',
            'host_public_key': 'ssh-ed25519 YWJj', 'private_key_ref': REF}
        self.assertEqual(policy.validate_inputs(value, 'aws')['status'], 'inputs_complete')
        # Structural validation is not the wire-format/identity/access gate.
        self.assertEqual(policy.validate_inputs(value, 'aws')['live_matrix'], 'not_tested')

    def test_peer_plaintext_key_proxy_command_or_unscoped_endpoint_refused(self):
        peer = {'host': '203.0.113.44', 'port': 22, 'username': 'fixture',
            'host_public_key': 'ssh-ed25519 YWJj', 'private_key_ref': REF}
        for changes in ({'host': '127.0.0.1'}, {'host': 'hostname.test'}, {'port': True}, {'private_key_ref': 'plaintext'},
            {'username': 'fixture;command'}, {'proxy_command': 'command'}, {'host_public_key': 'private-key'}):
            value = config(); value['runner']['peer_ssh'] = {**peer, **changes}
            with self.assertRaises(policy.PolicyError): policy.validate_inputs(value, 'aws')

    def test_all_providers_have_nonsecret_structure_but_no_live_pass(self):
        for provider in policy.PROVIDERS:
            report = policy.validate_inputs(config(), provider)
            self.assertEqual(report["status"], "inputs_complete")
            self.assertEqual(report["live_matrix"], "not_tested")

    def test_placeholder_example_cannot_be_used(self):
        value = json.loads((SCRIPT.parents[1] / "examples/showcase/execution-inputs.example.json").read_text())
        with self.assertRaises(policy.PolicyError):
            policy.validate_inputs(value, "aws")

    def test_missing_other_cloud_does_not_block_a_complete_cloud(self):
        value = config()
        value["providers"].pop("gcp")
        self.assertEqual(policy.validate_inputs(value, "aws")["status"], "inputs_complete")
        with self.assertRaises(policy.PolicyError):
            policy.validate_inputs(value, "gcp")

    def test_expanded_limits_plaintext_secret_existing_project_and_mutable_pins_fail(self):
        mutations = (
            lambda c: c.update(budget_usd="25.01"), lambda c: c.update(attempt_seconds=14401), lambda c: c.update(max_attempts=3),
            lambda c: c["secret_refs"].update(openai_api_key="private-plaintext-secret"), lambda c: c["pins"].update(hermes_image="nousresearch/hermes-agent:latest"),
            lambda c: c["providers"]["gcp"].update(isolated_project=False), lambda c: c["runner"].update(ssh_cidr="0.0.0.0/0"),
            lambda c: c["providers"]["gcp"].update(existing_volume_id="operator-disk"), lambda c: c["pins"].update(openclaw_digest=[]),
        )
        for mutate in mutations:
            value = config()
            mutate(value)
            with self.assertRaises(policy.PolicyError):
                policy.validate_inputs(value, "gcp")

    def test_cli_never_prints_config_contents_and_requires_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "inputs.json"
            value = config()
            value["secret_refs"]["openai_api_key"] = "private-plaintext-secret"
            file.write_text(json.dumps(value))
            file.chmod(0o600)
            result = subprocess.run(["python3", str(SCRIPT), "--config", str(file), "--provider", "aws"], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("private-plaintext-secret", result.stdout + result.stderr)
            file.write_text(json.dumps(config()))
            file.chmod(0o644)
            result = subprocess.run(["python3", str(SCRIPT), "--config", str(file), "--provider", "aws"], capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["reason"], "private_config_required")


class ReservationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = config()
        self.ledger = policy.Reservations(Path(self.temp.name) / "batch", self.config)

    def receipt(self, **changes):
        return {"provider": "aws", "account_id": "123456789012", "project_id": "", "region": "us-east1", "zone": "", "terraform_state_verified_empty": True, "cloud_queries_succeeded": 1, "owned_ids_checked": 10, "remaining_ids": [],
                "tailnet_queries_succeeded": 1, "tailnet_nodes_checked": 1, "remaining_tailnet_nodes": [], "verified_at": NOW + 100, **changes}

    def test_only_one_attempt_and_cleanup_required_even_after_deadline(self):
        first = self.ledger.reserve("aws", "as7-fixture01", quote(), now=NOW)
        self.assertEqual(first["reserved_usd"], "2.00")
        self.assertEqual(first["deadline"] - first["work_deadline"], 900)
        with self.assertRaisesRegex(policy.PolicyError, "cleanup_required"):
            self.ledger.reserve("gcp", "as7-fixture02", quote("gcp"), now=NOW + 14500)
        for receipt in (self.receipt(remaining_ids=["operator-or-owned-disk"]), self.receipt(remaining_tailnet_nodes=["test-node"]),
                        self.receipt(cloud_queries_succeeded=0), self.receipt(account_id="wrong-account"), self.receipt(terraform_state_verified_empty=False)):
            with self.assertRaises(policy.PolicyError):
                self.ledger.confirm_cleanup("as7-fixture01", receipt)
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        self.assertEqual(self.ledger.reserve("gcp", "as7-fixture02", quote("gcp"), now=NOW)["status"], "active")

    def test_one_retry_and_full_reservations_count_after_cleanup(self):
        for run in ("as7-fixture01", "as7-fixture02"):
            self.ledger.reserve("aws", run, quote(), now=NOW)
            self.ledger.confirm_cleanup(run, self.receipt())
        with self.assertRaisesRegex(policy.PolicyError, "attempt_limit_reached"):
            self.ledger.reserve("aws", "as7-fixture03", quote(), now=NOW)
        state = json.loads((self.ledger.directory / "reservations.json").read_text())
        self.assertEqual(sum(policy.money(row["reserved_usd"]) for row in state["attempts"]), 4)

    def test_simultaneous_runners_cannot_both_reserve(self):
        def reserve(run):
            try:
                policy.Reservations(self.ledger.directory, self.config).reserve("aws", run, quote(), now=NOW)
                return "reserved"
            except policy.PolicyError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(reserve, ("as7-fixture01", "as7-fixture02")))
        self.assertEqual(sorted(statuses), ["cleanup_required_before_launch", "reserved"])
        self.assertEqual(len(json.loads((self.ledger.directory / "reservations.json").read_text())["attempts"]), 1)

    def test_failed_before_create_still_requires_successful_independent_scope_queries(self):
        self.ledger.reserve("aws", "as7-fixture01", quote(), now=NOW)
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt(owned_ids_checked=0, tailnet_nodes_checked=0))
        self.assertEqual(self.ledger.reserve("aws", "as7-fixture02", quote(), now=NOW)["status"], "active")

    def test_budget_cannot_be_reset_by_new_limits_or_account(self):
        self.config["budget_usd"] = "2.00"
        self.ledger = policy.Reservations(self.ledger.directory, self.config)
        self.ledger.reserve("aws", "as7-fixture01", quote(), now=NOW)
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        with self.assertRaisesRegex(policy.PolicyError, "insufficient_budget"):
            self.ledger.reserve("gcp", "as7-fixture02", quote("gcp"), now=NOW)
        changed = copy.deepcopy(self.config)
        changed["budget_usd"] = "25.00"
        with self.assertRaisesRegex(policy.PolicyError, "ledger_scope_mismatch"):
            policy.Reservations(self.ledger.directory, changed).reserve("gcp", "as7-fixture02", quote("gcp"), now=NOW)

    def test_later_provider_inputs_and_new_pins_preserve_prior_spend(self):
        first_config = config()
        first_config["providers"].pop("gcp")
        first = policy.Reservations(self.ledger.directory, first_config)
        first.reserve("aws", "as7-fixture01", quote(), now=NOW)
        first.confirm_cleanup("as7-fixture01", self.receipt())
        later = config()
        later["pins"]["openclaw_digest"] = "sha256:" + "c" * 64
        policy.Reservations(self.ledger.directory, later).reserve("gcp", "as7-fixture02", quote("gcp"), now=NOW)
        state = json.loads((self.ledger.directory / "reservations.json").read_text())
        self.assertEqual(len(state["attempts"]), 2)
        self.assertEqual(sum(policy.money(row["reserved_usd"]) for row in state["attempts"]), 4)

    def test_retry_cannot_silently_switch_account(self):
        self.ledger.reserve("aws", "as7-fixture01", quote(), now=NOW)
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        changed = config()
        changed["providers"]["aws"]["account_id"] = "987654321098"
        with self.assertRaisesRegex(policy.PolicyError, "provider_scope_changed"):
            policy.Reservations(self.ledger.directory, changed).reserve("aws", "as7-fixture02", quote(), now=NOW)

    def test_cleanup_is_bound_to_original_project_even_after_input_repair(self):
        self.ledger.reserve("gcp", "as7-fixture01", quote("gcp"), now=NOW)
        changed = config()
        changed["providers"]["gcp"]["project_id"] = "different-project"
        recovery = policy.Reservations(self.ledger.directory, changed)
        with self.assertRaisesRegex(policy.PolicyError, "cleanup_identity_mismatch"):
            recovery.confirm_cleanup("as7-fixture01", self.receipt(provider="gcp", account_id="fixture-account", project_id="different-project", zone="us-east1-b"))
        recovery.confirm_cleanup("as7-fixture01", self.receipt(provider="gcp", account_id="fixture-account", project_id="agent-stack-fixture", zone="us-east1-b"))

    def test_retry_and_cleanup_cannot_switch_to_empty_region_or_zone(self):
        self.ledger.reserve("aws", "as7-fixture01", quote(), now=NOW)
        with self.assertRaisesRegex(policy.PolicyError, "cleanup_identity_mismatch"):
            self.ledger.confirm_cleanup("as7-fixture01", self.receipt(region="different-region"))
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        changed = config()
        changed["providers"]["aws"]["region"] = "different-region"
        with self.assertRaisesRegex(policy.PolicyError, "provider_scope_changed"):
            policy.Reservations(self.ledger.directory, changed).reserve("aws", "as7-fixture02", quote(), now=NOW)

    def test_fresh_official_price_and_nonzero_model_cleanup_allowances_required(self):
        self.assertEqual(policy.reserve_amount(quote(hourly="0.101"), "aws", 14400, NOW), policy.money("2.01"))
        for changes in ({"currency": "EUR"}, {"verified_at": "not-a-date"}, {"source": "https://untrusted.example/price"},
                        {"model_usd": "0"}, {"cleanup_usd": "0"}, {"hourly_usd": "NaN"}, {"hourly_usd": "-1"}):
            with self.assertRaises(policy.PolicyError):
                policy.reserve_amount({**quote(), **changes}, "aws", 14400, NOW)
        with self.assertRaisesRegex(policy.PolicyError, "stale_price"):
            policy.reserve_amount(quote(), "aws", 14400, NOW + 86401)

    def test_symlink_or_world_readable_ledger_is_refused(self):
        self.ledger.directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(policy.PolicyError, "private_ledger_required"):
            self.ledger.reserve("aws", "as7-fixture01", quote(), now=NOW)
        self.ledger.directory.chmod(0o700)
        outside = Path(self.temp.name) / "outside"
        outside.write_text("must-stay-intact")
        (self.ledger.directory / "reservations.json").symlink_to(outside)
        with self.assertRaisesRegex(policy.PolicyError, "private_ledger_required"):
            self.ledger.reserve("aws", "as7-fixture01", quote(), now=NOW)
        self.assertEqual(outside.read_text(), "must-stay-intact")


class FreshPlanTest(unittest.TestCase):
    def plan(self):
        return {"resource_changes": [{"address": "module.aws[0]." + kind + ".this", "mode": "managed", "type": kind,
                    "change": {"actions": ["create"], "before": None, "after": {"tags": {"Name": "as7-fixture01-resource"}}}}
                    for kind in policy.ESSENTIAL_TYPES["aws"]]}

    def test_only_owned_new_vm_network_and_disk_pass(self):
        self.assertEqual(policy.validate_fresh_plan(self.plan(), "aws", "as7-fixture01", "/tmp/private-run")["status"], "fresh_plan_checked")

    def test_similar_prefix_is_not_owned_by_the_attempt(self):
        plan = self.plan()
        plan["resource_changes"][0]["change"]["after"]["tags"]["Name"] = "as7-fixture01someone-else"
        with self.assertRaisesRegex(policy.PolicyError, "foreign_resource_name"):
            policy.validate_fresh_plan(plan, "aws", "as7-fixture01", "/tmp/private-run")

    def test_reuse_import_update_delete_replace_foreign_name_and_other_cloud_fail(self):
        mutations = (
            lambda p: p["resource_changes"][0]["change"].update(actions=["update"]),
            lambda p: p["resource_changes"][0]["change"].update(actions=["delete", "create"]),
            lambda p: p["resource_changes"][0]["change"].update(before={"id": "existing"}),
            lambda p: p["resource_changes"][0]["change"].update(importing={"id": "existing"}),
            lambda p: p["resource_changes"][0]["change"]["after"]["tags"].update(Name="operator-vpc"),
            lambda p: p["resource_changes"][0].update(address="module.gcp[0].google_compute_instance.this"),
            lambda p: p["resource_changes"].pop(),
        )
        for mutate in mutations:
            plan = self.plan()
            mutate(plan)
            with self.assertRaises(policy.PolicyError):
                policy.validate_fresh_plan(plan, "aws", "as7-fixture01", "/tmp/private-run")

    def test_attachments_cannot_name_existing_ids(self):
        plan = self.plan()
        attachment = {"address": "module.aws[0].aws_volume_attachment.this", "mode": "managed", "type": "aws_volume_attachment",
                      "change": {"actions": ["create"], "before": None, "after": {"volume_id": None, "instance_id": None}, "after_unknown": {"volume_id": True, "instance_id": True}}}
        plan["resource_changes"].append(attachment)
        policy.validate_fresh_plan(plan, "aws", "as7-fixture01", "/tmp/private-run")
        attachment["change"]["after"]["volume_id"] = "vol-operator-disk"
        with self.assertRaisesRegex(policy.PolicyError, "existing_resource_reference"):
            policy.validate_fresh_plan(plan, "aws", "as7-fixture01", "/tmp/private-run")

    def test_local_files_must_remain_inside_private_checkout(self):
        plan = self.plan()
        file = {"address": "local_file.repo_ssh_config", "mode": "managed", "type": "local_file",
                "change": {"actions": ["create"], "before": None, "after": {"filename": ".ssh/config"}}}
        plan["resource_changes"].append(file)
        policy.validate_fresh_plan(plan, "aws", "as7-fixture01", "/tmp/private-run")
        file["change"]["after"]["filename"] = "../../operator-config"
        with self.assertRaisesRegex(policy.PolicyError, "private_output_required"):
            policy.validate_fresh_plan(plan, "aws", "as7-fixture01", "/tmp/private-run")
