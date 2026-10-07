import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_prices as prices
import showcase_terraform as terraform
from showcase_policy import PolicyError, reserve_amount
from showcase_attempt import prepare
import test_showcase_attempt as preparation
from test_showcase_policy import config, NOW
from test_showcase_terraform import FakeSession

PREFIX = "as7-fixture01"
PRIVATE = "fixture-private-not-for-reports"
VOLUMES = '<table><tr><th>Storage</th><th>$/hour</th><th>$/month</th></tr>' \
    '<tr><td>100 GiB</td><td>$0.015</td><td>$10.00</td></tr>' \
    '<tr><td>500 GiB</td><td>$0.074</td><td>$50.00</td></tr></table>'
DROPLETS = '<p>Bundled-plan CPU Droplets are billed per second with a minimum charge of 60 seconds or $0.01, whichever is higher. ' \
    'Usage is capped at 672 hours (28 days) per month.</p><p>Additional outbound transfer is billed at $0.01 per GiB.</p>'


def price_plan(cloud, prefix=PREFIX):
    rows = []
    for kind in sorted(prices.DO_TYPES):
        values = {"name": prefix + "-" + kind.removeprefix("digitalocean_")}
        unknown = {}
        if kind == "digitalocean_droplet": values.update(size=cloud["size"], region=cloud["region"], image="ubuntu-22-04-x64", backups=False)
        if kind == "digitalocean_volume": values.update(size=20, region=cloud["region"])
        if kind == "digitalocean_vpc": values.update(region=cloud["region"], ip_range=prices.digitalocean_showcase_cidr(prefix))
        if kind == "digitalocean_project_resources": values = {"project": cloud["project_id"], "resources": None}
        if kind == "digitalocean_volume_attachment":
            values = {"droplet_id": None, "volume_id": None}; unknown = {"droplet_id": True, "volume_id": True}
        rows.append({"address": "module.digitalocean[0]." + kind + ".this", "type": kind, "mode": "managed",
                     "change": {"actions": ["create"], "before": None, "after": values, "after_unknown": unknown}})
    return {"resource_changes": rows}


class FakePublic:
    def __init__(self):
        self.calls = []
        self.documents = {"volumes": VOLUMES, "droplets": DROPLETS}

    def fetch(self, name):
        self.calls.append(name)
        document = self.documents[name]
        return {"source": prices.SOURCES[name], "body_sha256": hashlib.sha256(document.encode()).hexdigest(), "document": document}


class PriceTest(unittest.TestCase):
    def setUp(self):
        self.cloud = config()["providers"]["digitalocean"]
        self.cloud["size"] = "s-4vcpu-8gb"
        self.plan = price_plan(self.cloud)
        self.size = {"slug": self.cloud["size"], "memory": 8192, "disk": 160, "price_hourly": .07, "price_monthly": 48,
                     "regions": [self.cloud["region"]], "available": True}
        self.adapter = SimpleNamespace(cloud=self.cloud, pages=Mock(return_value=[self.size]))
        self.public = FakePublic()

    def measure(self, clock=None):
        return prices.digitalocean_rates(self.plan, self.cloud, PREFIX, "/tmp/fixture-private-repo", self.adapter,
                                       "a" * 40, "b" * 64, self.public, (lambda: NOW) if clock is None else clock)

    def test_bound_selection_prices_round_up_without_passing_a_launch_gate(self):
        result = self.measure()
        self.assertEqual(result["vm_and_boot_hourly_usd"], "0.071429000")
        self.assertEqual(result["data_volume_gib_hourly_usd"], "0.000150000")
        self.assertEqual(result["vm_and_disk_hourly_usd"], "0.074429000")
        self.assertEqual(result["outbound_gib_usd"], "0.010000000")
        self.assertEqual(result["selection"], {key: self.cloud[key] for key in ("account_id", "project_id", "region", "size")})
        self.assertEqual(result["plan_sha256"], "b" * 64); self.assertEqual(result["source_revision"], "a" * 40)
        self.assertIs(result["complete_quote"], False); self.assertEqual(result["launch_gate"], "unverified")
        self.assertIn("egress_volume_bound", result["not_verified"])
        self.adapter.pages.assert_called_once_with("/sizes", "sizes", "digitalocean")
        self.assertEqual(self.public.calls, ["volumes", "droplets"])
        with self.assertRaises(PolicyError): reserve_amount(result, "digitalocean", 14400, NOW)

    def test_unknown_plan_values_scope_backups_and_additional_resources_refused_before_reads(self):
        mutations = (
            lambda rows: rows["digitalocean_droplet"]["after"].update(region="wrong-region"),
            lambda rows: rows["digitalocean_droplet"]["after"].update(backups=True),
            lambda rows: rows["digitalocean_droplet"]["after"].pop("backups"),
            lambda rows: rows["digitalocean_droplet"]["after"].update(image="paid-image"),
            lambda rows: rows["digitalocean_droplet"]["after_unknown"].update(size=True),
            lambda rows: rows["digitalocean_volume"]["after"].update(size=True),
            lambda rows: rows["digitalocean_volume"]["after"].update(region="wrong-region"),
            lambda rows: rows["digitalocean_project_resources"]["after"].update(project="foreign-project"),
            lambda rows: rows["digitalocean_vpc"]["after"].update(region="wrong-region"),
            lambda rows: rows["digitalocean_vpc"]["after"].update(ip_range="10.1.0.0/16"),
            lambda rows: rows["digitalocean_vpc"]["after_unknown"].update(ip_range=True),
        )
        original = copy.deepcopy(self.plan)
        for mutation in mutations:
            self.plan = copy.deepcopy(original)
            mutation({row["type"]: row["change"] for row in self.plan["resource_changes"]})
            with self.subTest(mutation=mutation), self.assertRaises(PolicyError): self.measure()
            self.adapter.pages.assert_not_called(); self.assertEqual(self.public.calls, [])
        self.plan = copy.deepcopy(original)
        self.plan["resource_changes"].append(copy.deepcopy(self.plan["resource_changes"][0]))
        with self.assertRaises(PolicyError): self.measure()

    def test_missing_planned_firewall_key_or_unexpected_paid_kind_cannot_have_a_price_receipt(self):
        original = copy.deepcopy(self.plan)
        for kind in ("digitalocean_firewall", "digitalocean_ssh_key"):
            self.plan = {"resource_changes": [row for row in original["resource_changes"] if row["type"] != kind]}
            with self.subTest(kind=kind), self.assertRaisesRegex(PolicyError, "price_plan_shape_unsupported"): self.measure()
        self.plan = copy.deepcopy(original)
        self.plan["resource_changes"][0]["type"] = "digitalocean_loadbalancer"
        with self.assertRaises(PolicyError): self.measure()
        self.adapter.pages.assert_not_called()

    def test_v5_gpu_or_unavailable_or_duplicate_sizes_refused(self):
        for mutation in (lambda s: s.update(memory=4096), lambda s: s.update(disk=10), lambda s: s.update(available=False),
                         lambda s: s.update(regions=["foreign-region"]), lambda s: s.update(gpu_info={"count": 1})):
            row = copy.deepcopy(self.size); mutation(row); self.adapter.pages.return_value = [row]
            with self.subTest(mutation=mutation), self.assertRaisesRegex(PolicyError, "price_vm_size_unavailable"): self.measure()
        self.adapter.pages.return_value = [self.size, self.size]
        with self.assertRaisesRegex(PolicyError, "price_vm_size_unavailable"): self.measure()
        self.cloud["size"] = "s5-4vcpu-8gb-30gb"
        for row in self.plan["resource_changes"]:
            if row["type"] == "digitalocean_droplet": row["change"]["after"]["size"] = self.cloud["size"]
        with self.assertRaisesRegex(PolicyError, "bundled_cpu_size_required"): self.measure()

    def test_nonfinite_negative_zero_boolean_or_missing_prices_fail(self):
        for value in (0, -1, True, float("nan"), float("inf"), None, "not-price"):
            self.adapter.pages.return_value = [{**self.size, "price_hourly": value}]
            with self.subTest(value_type=type(value).__name__), self.assertRaises(PolicyError): self.measure()

    def test_public_rate_changes_are_measured_and_body_is_not_published(self):
        self.public.documents["droplets"] = DROPLETS.replace("$0.01 per GiB", "$0.02 per GiB") + '<script>' + PRIVATE + '</script>'
        result = self.measure()
        self.assertEqual(result["outbound_gib_usd"], "0.020000000")
        self.assertNotIn(PRIVATE, json.dumps(result)); self.assertNotIn("document", json.dumps(result))

    def test_changed_missing_duplicate_or_unbalanced_tables_and_billing_contract_fail(self):
        for document in ("", VOLUMES + VOLUMES, VOLUMES.replace("</table>", ""), VOLUMES.replace("GiB", "GB"),
                         VOLUMES.replace("$0.015", "price unavailable"), VOLUMES.replace("$/hour", "EUR/hour")):
            with self.subTest(length=len(document)), self.assertRaises(PolicyError): prices.volume_rate(document)
        for document in (DROPLETS.replace("672", "730"), DROPLETS.replace("$0.01 per GiB", "price unavailable"), DROPLETS + DROPLETS,
                         DROPLETS.replace("billed per second", "billed per month")):
            with self.subTest(length=len(document)), self.assertRaises(PolicyError): prices.droplet_terms(document)

    def test_monthly_ratio_and_highest_rounded_unit_rate_are_conservative(self):
        self.assertEqual(prices.volume_rate(VOLUMES.replace("$0.015", "$0.001")), prices.Decimal("0.000148810"))
        high = VOLUMES.replace("$0.074", "$0.080")
        self.assertEqual(prices.volume_rate(high), prices.Decimal("0.000160"))

    def test_script_prices_cannot_replace_missing_visible_prices(self):
        with self.assertRaisesRegex(PolicyError, "volume_price_table_missing_or_ambiguous"):
            prices.volume_rate("<script>" + VOLUMES + "</script>")
        with self.assertRaisesRegex(PolicyError, "egress_price_missing_or_ambiguous"):
            prices.droplet_terms("<style>" + DROPLETS + "</style>")

    def test_forged_public_origin_hash_and_expired_measurement_fail(self):
        real = self.public.fetch
        for field, value in (("source", "https://evil.test"), ("body_sha256", "c" * 64)):
            self.public.fetch = lambda name: {**real(name), field: value}
            with self.subTest(field=field), self.assertRaisesRegex(PolicyError, "price_source_changed"): self.measure()
        self.public.fetch = real
        for end in (NOW + 301, NOW - 1, float("nan")):
            clock = iter((NOW, end))
            with self.subTest(end=end), self.assertRaisesRegex(PolicyError, "price_measurement_expired"): self.measure(lambda: next(clock))


class TransportTest(unittest.TestCase):
    def transport(self, raw=None):
        public = prices.PublicPricing()
        response = Mock(status=200)
        response.read.return_value = VOLUMES.encode() if raw is None else raw
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        public.opener.open = Mock(return_value=response)
        return public, response

    def test_public_fixed_get_without_auth_and_bound_body_hash(self):
        public, response = self.transport()
        result = public.fetch("volumes")
        request = public.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, prices.SOURCES["volumes"]); self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("authorization", {key.lower(): value for key, value in request.header_items()})
        response.read.assert_called_once_with(prices.MAX_RESPONSE + 1)
        self.assertEqual(result["body_sha256"], hashlib.sha256(VOLUMES.encode()).hexdigest())
        self.assertEqual(public.opener.open.call_args.kwargs, {"timeout": 20})

    def test_unapproved_source_and_redirect_refused_without_followup(self):
        public, _ = self.transport()
        with self.assertRaisesRegex(PolicyError, "price_source_refused"): public.fetch("https://evil.test/" + PRIVATE)
        public.opener.open.assert_not_called()
        with self.assertRaisesRegex(PolicyError, "price_redirect_refused") as caught:
            prices.NoRedirect().redirect_request(None, None, 302, PRIVATE, {}, "https://evil.test/" + PRIVATE)
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_oversized_invalid_utf8_network_and_http_errors_redacted_without_retry(self):
        for raw in (b"x" * (prices.MAX_RESPONSE + 1), b"\xff"):
            public, _ = self.transport(raw)
            with self.assertRaises(PolicyError): public.fetch("volumes")
        for error in (URLError(PRIVATE), HTTPError("https://evil.test/" + PRIVATE, 403, PRIVATE, {}, io.BytesIO(PRIVATE.encode()))):
            public, _ = self.transport(); public.opener.open.side_effect = error
            with self.assertRaisesRegex(PolicyError, "price_request_failed") as caught: public.fetch("volumes")
            self.assertNotIn(PRIVATE, str(caught.exception)); self.assertEqual(public.opener.open.call_count, 1)


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation.PreparationTest("runTest"); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        for name in ("showcase_prices", "showcase_terraform", "showcase_policy", "showcase_preflight"):
            target = self.fixture.repo / "scripts" / (name + ".py")
            target.parent.mkdir(exist_ok=True)
            target.write_bytes((Path(terraform.__file__).parent / (name + ".py")).read_bytes())
        self.fixture.git("add", ".")
        self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "checked price sources")
        self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip()
        self.config = config(); self.config["providers"]["digitalocean"]["size"] = "s-4vcpu-8gb"
        prepare(self.config, "digitalocean", PREFIX, self.fixture.batch, self.fixture.revision, self.fixture.repo)
        self.run = self.fixture.batch / "attempts" / PREFIX
        session = FakeSession(self.config)
        cloud = self.config["providers"]["digitalocean"]
        session.adapter = SimpleNamespace(cloud=cloud, pages=Mock(return_value=[{
            "slug": cloud["size"], "memory": 8192, "disk": 160, "price_hourly": .07, "price_monthly": 48,
            "regions": [cloud["region"]], "available": True}]))
        self.client = terraform.TerraformAttempt(self.run, self.config, "digitalocean", session, now=lambda: NOW)
        self.plan = price_plan(cloud)
        def tf(*args, **kwargs):
            if args[0] == "version": return json.dumps({"terraform_version": "1.16.5"}).encode()
            if args[0] == "init": (self.client.checkout / ".terraform.lock.hcl").write_text("fixture-provider-lock")
            if args[0] == "plan":
                path = self.run / "deployment.tfplan"; path.write_bytes(b"fixture-binary-plan"); path.chmod(0o600)
            if args[0] == "show": return json.dumps(self.plan).encode()
            return b""
        self.client.tf = Mock(side_effect=tf)
        self.client.plan()

    def test_saved_receipt_binds_original_plan_and_commit_and_is_private_without_reservation(self):
        result = self.client.resource_unit_prices(FakePublic())
        path = self.run / "resource-prices.json"
        self.assertEqual(json.loads(path.read_text()), result); self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(result["plan_sha256"], self.client.record()["plan_sha256"])
        self.assertEqual(result["source_revision"], self.fixture.revision)
        self.assertFalse((self.fixture.batch / "reservations.json").exists())
        self.assertEqual(self.client.record()["stage"], "planned")

    def test_modified_plan_or_source_fails_before_public_or_cloud_price_reads(self):
        public = FakePublic()
        (self.run / "deployment.tfplan").write_bytes(b"changed")
        with self.assertRaisesRegex(PolicyError, "saved_plan_changed"): self.client.resource_unit_prices(public)
        self.client.session.adapter.pages.assert_not_called(); self.assertEqual(public.calls, [])
        (self.run / "deployment.tfplan").write_bytes(b"fixture-binary-plan")
        (self.client.checkout / "main.tf").write_text("changed")
        with self.assertRaisesRegex(PolicyError, "prepared_source_changed"): self.client.resource_unit_prices(public)
        self.client.session.adapter.pages.assert_not_called(); self.assertEqual(public.calls, [])

    def test_older_prepared_price_controller_refused_before_reads(self):
        target = self.fixture.repo / "scripts/showcase_prices.py"
        target.write_bytes(target.read_bytes() + b'\n# fixture older implementation\n')
        self.fixture.git("add", ".")
        self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "older source fixture")
        revision = self.fixture.git("rev-parse", "HEAD").strip()
        prefix = PREFIX + "x"
        prepare(self.config, "digitalocean", prefix, self.fixture.batch, revision, self.fixture.repo)
        session, tf = self.client.session, self.client.tf
        self.client = terraform.TerraformAttempt(self.fixture.batch / "attempts" / prefix, self.config, "digitalocean", session, now=lambda: NOW)
        self.client.tf = tf
        self.run = self.client.run
        self.plan = price_plan(self.config["providers"]["digitalocean"], prefix)
        self.client.plan()
        public = FakePublic()
        with self.assertRaisesRegex(PolicyError, "price_controller_source_changed"): self.client.resource_unit_prices(public)
        session.adapter.pages.assert_not_called(); self.assertEqual(public.calls, [])

    def test_public_plan_entrypoint_saves_measured_partial_prices_without_launch_or_reservation(self):
        import showcase_live as live
        from showcase_cleanup import private_write
        self.client.record_path.unlink()
        inputs = self.fixture.root / "private-inputs.json"; private_write(inputs, self.config)
        argv = ['showcase_live.py', '--phase', 'plan', '--provider', 'digitalocean', '--run-id', PREFIX,
                '--config', str(inputs), '--batch', str(self.fixture.batch)]
        output = io.StringIO()
        public = FakePublic()
        with patch.dict(live.os.environ, {"AGENT_STACK_LIVE_TESTS": "1"}), patch.object(sys, "argv", argv), \
             patch.object(live.shutil, "which", return_value="/fixture/tool"), patch.object(live.signal, "signal"), \
             patch.object(live, "CloudSession", return_value=self.client.session), patch.object(live, "TerraformAttempt", return_value=self.client), \
             patch.object(prices, "PublicPricing", return_value=public), contextlib.redirect_stdout(output):
            code = live.main()
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0, report); self.assertEqual(report["status"], "planned")
        self.assertFalse(report["launch_ready"]); self.assertFalse(report["spend_reserved"])
        self.assertEqual(report["resource_unit_prices"]["launch_gate"], "unverified")
        self.assertFalse((self.fixture.batch / "reservations.json").exists())
        self.assertFalse((self.client.checkout / "terraform.tfstate").exists())
        self.assertTrue((self.run / "resource-prices.json").is_file())
