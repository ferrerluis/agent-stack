import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from showcase_policy import PolicyError, Reservations, money
from test_showcase_policy import NOW, quote
from test_showcase_prepaid import funded


class ClosedCostTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = funded()
        self.ledger = Reservations(Path(self.temp.name) / "batch", self.config)

    def receipt(self, provider="aws", seconds=100):
        cloud = self.config["providers"][provider]
        return {"provider": provider, "account_id": cloud["account_id"],
                "project_id": cloud.get("project_id", ""), "region": cloud["region"], "zone": cloud.get("zone", ""),
                "terraform_state_verified_empty": True, "cloud_queries_succeeded": 1,
                "owned_ids_checked": 3, "remaining_ids": [], "tailnet_queries_succeeded": 1,
                "tailnet_nodes_checked": 1, "remaining_tailnet_nodes": [], "verified_at": NOW + seconds}

    def reserve(self, provider="aws", run="as7-fixture01", hourly="0.10"):
        return self.ledger.reserve(provider, run, quote(provider, hourly) | {"model_prepaid": True}, NOW)

    def state(self):
        return json.loads((self.ledger.directory / "reservations.json").read_text())

    def corrupt(self, change):
        state = self.state()
        change(state)
        (self.ledger.directory / "reservations.json").write_text(json.dumps(state))

    def test_reconciliation_releases_unused_hours_but_preserves_quote_history_and_purchases(self):
        self.reserve()
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        original = self.state()
        self.assertEqual(self.ledger.committed_cash(original), money("12.30"))
        proof = self.ledger.reconcile_cleanup("as7-fixture01", NOW + 100)
        state = self.state()
        self.assertEqual(self.ledger.committed_cash(state), money("11.50"))
        self.assertEqual(state["attempts"][0]["closed_cost_bound"]["usd"], "0.20")
        self.assertEqual({k: v for k, v in state["attempts"][0].items() if k != "closed_cost_bound"}, original["attempts"][0])
        self.assertEqual(state["funding"], original["funding"])
        self.assertEqual(state["limits"], original["limits"])
        self.assertEqual(self.ledger.reconcile_cleanup("as7-fixture01", NOW + 101), proof)

    def test_released_allowances_do_not_authorize_an_extra_retry(self):
        for run in ("as7-fixture01", "as7-fixture02"):
            self.reserve(run=run)
            self.ledger.confirm_cleanup(run, self.receipt())
            self.ledger.reconcile_cleanup(run, NOW + 100)
        with self.assertRaisesRegex(PolicyError, "attempt_limit_reached"):
            self.reserve(run="as7-fixture03")
        self.assertEqual(len(self.state()["attempts"]), 2)

    def test_active_attempt_and_unproved_historical_quote_keep_full_allowance(self):
        self.reserve()
        with self.assertRaisesRegex(PolicyError, "closed_cost_proof_required"):
            self.ledger.reconcile_cleanup("as7-fixture01", NOW + 100)
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        def old_record(state):
            for key in ("price_quote", "quote_sha256", "model_usd"):
                state["attempts"][0].pop(key)
        self.corrupt(old_record)
        with self.assertRaisesRegex(PolicyError, "closed_cost_proof_required"):
            self.ledger.reconcile_cleanup("as7-fixture01", NOW + 100)
        self.assertEqual(self.ledger.committed_cash(self.state()), money("12.30"))

    def test_wrong_scope_and_incomplete_or_future_deletion_never_release_allowances(self):
        self.reserve()
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        original = self.state()
        changes = ({"account_id": "other"}, {"project_id": "other"}, {"region": "other"}, {"zone": "other"},
                   {"terraform_state_verified_empty": False}, {"remaining_ids": ["disk"]},
                   {"remaining_tailnet_nodes": ["node"]}, {"cloud_queries_succeeded": 0},
                   {"tailnet_queries_succeeded": 0}, {"verified_at": NOW - 1}, {"verified_at": float("inf")},
                   {"verified_at": NOW + 101})
        path = self.ledger.directory / "reservations.json"
        for changeset in changes:
            with self.subTest(changes=changeset):
                state = copy.deepcopy(original)
                state["attempts"][0]["cleanup_receipt"].update(changeset)
                path.write_text(json.dumps(state))
                with self.assertRaises(PolicyError):
                    self.ledger.reconcile_cleanup("as7-fixture01", NOW + 100)
                self.assertNotIn("closed_cost_bound", self.state()["attempts"][0])

    def test_edited_proof_or_its_original_receipt_quote_and_binding_are_refused_on_every_read(self):
        self.reserve()
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        self.ledger.reconcile_cleanup("as7-fixture01", NOW + 100)
        original = self.state()
        path = self.ledger.directory / "reservations.json"
        for field, changes in (("closed_cost_bound", {"usd": "0.01"}), ("closed_cost_bound", {"whole_hours_upper_bound": 0}),
                               ("cleanup_receipt", {"verified_at": NOW + 90}), ("price_quote", {"hourly_usd": "0.01"}),
                               ("cleanup_receipt", {"account_id": "other"})):
            with self.subTest(field=field, changes=changes):
                state = copy.deepcopy(original)
                state["attempts"][0][field].update(changes)
                path.write_text(json.dumps(state))
                with self.assertRaises(PolicyError):
                    self.ledger.operate(lambda state: None)
                with self.assertRaises(PolicyError):
                    self.ledger.committed_cash(state)

    def test_whole_hour_rounding_includes_hetzner_minimum_and_retains_network_reserve(self):
        self.reserve(provider="hetzner", hourly="0.40")
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt("hetzner", 3600.1))
        result = self.ledger.reconcile_cleanup("as7-fixture01", NOW + 3601)["closed_cost_bound"]
        self.assertEqual(result["whole_hours_upper_bound"], 2)
        self.assertEqual(result["usd"], "0.90")

    def test_late_cleanup_increases_bound_and_blocks_launch_above_the_original_budget(self):
        self.reserve(hourly="0.50")
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt(seconds=30 * 3600))
        result = self.ledger.reconcile_cleanup("as7-fixture01", NOW + 30 * 3600)["closed_cost_bound"]
        self.assertEqual(result["usd"], "15.10")
        self.assertGreater(money(result["usd"]), money(result["original_reserved_usd"]))
        with self.assertRaisesRegex(PolicyError, "insufficient_budget"):
            self.reserve(provider="gcp", run="as7-fixture02")

    def test_nonprepaid_model_allowance_is_retained_without_usage_proof(self):
        self.config.pop("funding")
        self.ledger.reserve("aws", "as7-fixture01", quote(), NOW)
        self.ledger.confirm_cleanup("as7-fixture01", self.receipt())
        result = self.ledger.reconcile_cleanup("as7-fixture01", NOW + 100)["closed_cost_bound"]
        self.assertEqual(result["usd"], "1.20")


if __name__ == "__main__":
    unittest.main()
