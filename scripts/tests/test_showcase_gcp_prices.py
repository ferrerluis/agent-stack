import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_gcp_prices as prices
import showcase_terraform as terraform
from showcase_attempt import prepare
from showcase_policy import PolicyError, reserve_amount
import test_showcase_attempt as preparation
from test_showcase_policy import config, NOW
from test_showcase_terraform import FakeSession

PREFIX = "as7-fixture01"
PRIVATE = "fixture-private-never-for-reports"


def cloud_config():
    value = config(); value["providers"]["gcp"]["size"] = "e2-standard-2"
    return value


def plan():
    return json.loads((Path(__file__).parent / "fixtures/gcp-price-shape.json").read_text())


def image():
    name = "ubuntu-2204-jammy-v20261001"
    return {"name": name, "id": "1234567890123456789", "family": "ubuntu-2204-lts", "status": "READY", "architecture": "X86_64",
            "licenses": [prices.LICENSE], "selfLink": "https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/" + name}


def adapter(cloud):
    return SimpleNamespace(cloud=cloud, access_token=Mock(return_value=PRIVATE),
        get=Mock(return_value={"name": cloud["size"], "architecture": "X86_64", "guestCpus": 2, "memoryMb": 8192}),
        http=SimpleNamespace(request=Mock(return_value=image())))


def sku(kind, region="us-east1"):
    ids = {"cpu": "CF4E-A0C7-E3BF", "ram": "F449-33EC-A5EF", "disk": "6AE1-525F-8B80", "ipv4": "C054-7F72-A02E"}
    descriptions = {"cpu": "E2 Instance Core running in Americas", "ram": "E2 Instance Ram running in Americas",
                    "disk": "Balanced PD Capacity", "ipv4": "External IP Charge on a Standard VM"}
    units = {"cpu": ("h", "s", 3600, 20000000), "ram": ("GiBy.h", "By.s", 2 ** 30 * 3600, 3000000),
             "disk": ("GiBy.mo", "By.s", 2 ** 30 * 730 * 3600, 100000000), "ipv4": ("h", "s", 3600, 5000000)}
    unit, base, factor, nanos = units[kind]
    tiers = [{"startUsageAmount": 0, "unitPrice": {"currencyCode": "USD", "nanos": nanos}}]
    if kind == "ipv4":
        tiers = [{"unitPrice": {"currencyCode": "USD"}}, {"startUsageAmount": 1, "unitPrice": {"currencyCode": "USD", "nanos": nanos}}]
    return {"name": prices.SERVICE + "/skus/" + ids[kind], "skuId": ids[kind], "description": descriptions[kind],
        "category": {"serviceDisplayName": "Compute Engine", "resourceFamily": "Storage" if kind == "disk" else "Network" if kind == "ipv4" else "Compute",
                     "resourceGroup": kind.upper(), "usageType": "OnDemand"}, "serviceProviderName": "Google",
        "serviceRegions": ["global"] if kind == "ipv4" else [region],
        "geoTaxonomy": {"type": "GLOBAL" if kind == "ipv4" else "REGIONAL", "regions": [] if kind == "ipv4" else [region]},
        "pricingInfo": [{"effectiveTime": datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat(), "currencyConversionRate": 1,
            "pricingExpression": {"usageUnit": unit, "baseUnit": base, "baseUnitConversionFactor": factor,
                                  "displayQuantity": 1, "tieredRates": tiers}}]}


def catalog_rows():
    return [sku(kind) for kind in ("cpu", "ram", "disk", "ipv4")]


class FakeHTTP:
    def __init__(self, pages=None):
        self.pages = [{"skus": catalog_rows()}] if pages is None else pages
        self.calls = []
    def page(self, token, page_token=None):
        self.calls.append((token, page_token)); value = self.pages[len(self.calls) - 1]
        if isinstance(value, Exception): raise value
        raw = json.dumps(value).encode()
        return value, len(raw), hashlib.sha256(raw).hexdigest()


class PriceTest(unittest.TestCase):
    def setUp(self):
        self.cloud = cloud_config()["providers"]["gcp"]
        self.plan = plan(); self.adapter = adapter(self.cloud); self.http = FakeHTTP()
        self.catalog = prices.GCPCatalog(self.adapter, self.http, now=lambda: NOW)

    def measure(self, now=lambda: NOW):
        return prices.gcp_rates(self.plan, self.cloud, PREFIX, "/tmp/fixture-repo", self.adapter,
                                "a" * 40, "b" * 64, self.catalog, now)

    def test_real_mock_plan_shape_cpu_ram_both_disks_and_ip_without_free_allowance(self):
        result = self.measure()
        self.assertEqual(result["vm_hourly_usd_upper_bound"], "0.064000000")
        self.assertEqual(result["vm_disks_ipv4_hourly_usd_upper_bound"], "0.082392858")
        self.assertEqual(result["external_ipv4_hourly_usd_upper_bound"], "0.005000000")
        self.assertEqual(result["selection"]["ram_gib"], 8); self.assertEqual(result["selection"]["boot_volume_gib"], 40)
        self.assertEqual(result["selection"]["data_volume_gib"], 50)
        self.assertEqual(result["source_revision"], "a" * 40); self.assertEqual(result["plan_sha256"], "b" * 64)
        self.assertFalse(result["complete_quote"]); self.assertEqual(result["launch_gate"], "unverified")
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.adapter.http.request.assert_called_once_with(prices.IMAGE_ENDPOINT + image()["name"], "Bearer " + PRIVATE)
        with self.assertRaises(PolicyError): reserve_amount(result, "gcp", 14400, NOW)

    def test_resolved_image_in_refreshed_saved_plan_state_keeps_exact_vm_binding(self):
        self.plan["prior_state"] = {"values": self.plan.pop("planned_values")}
        result = self.measure()
        self.assertEqual(result["selection"]["image_id"], image()["id"])
        self.adapter.http.request.assert_called_once_with(prices.IMAGE_ENDPOINT + image()["name"], "Bearer " + PRIVATE)

    def test_conflicting_image_copies_or_malformed_refreshed_state_refuse_before_reads(self):
        original = copy.deepcopy(self.plan)
        changes = [{}, {"values": None}, {"values": {"root_module": None}}]
        for prior in changes:
            self.plan = copy.deepcopy(original); self.plan["prior_state"] = prior
            with self.assertRaisesRegex(PolicyError, "gcp_resolved_image_plan_required"): self.measure()
        for key, value in (("image_id", "1234567890123456790"), ("family", "foreign"), ("self_link", "https://foreign.invalid/image")):
            self.plan = copy.deepcopy(original); self.plan["prior_state"] = {"values": copy.deepcopy(self.plan["planned_values"])}
            self.plan["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"][key] = value
            with self.assertRaisesRegex(PolicyError, "gcp_resolved_image_plan_required"): self.measure()
        self.adapter.get.assert_not_called(); self.adapter.http.request.assert_not_called(); self.adapter.access_token.assert_not_called()
        self.assertEqual(self.http.calls, [])

    def test_changed_scope_unknown_values_extra_disks_spot_gpu_or_foreign_network_fail_before_reads(self):
        original = copy.deepcopy(self.plan)
        changes = (
            lambda r: r["google_compute_instance"]["after"].update(project="foreign-project"),
            lambda r: r["google_compute_disk"]["after"].update(zone="us-east1-c"),
            lambda r: r["google_compute_subnetwork"]["after"].update(region="foreign-region"),
            lambda r: r["google_compute_disk"]["after_unknown"].update(size=True),
            lambda r: r["google_compute_instance"]["after"]["boot_disk"][0]["initialize_params"][0].update(image="ubuntu-pro-os-cloud/paid"),
            lambda r: r["google_compute_instance"]["after_unknown"]["boot_disk"][0]["initialize_params"][0].update(size=True),
            lambda r: r["google_compute_instance"]["after"].update(scheduling=[{"preemptible": True}]),
            lambda r: r["google_compute_instance"]["after"].update(guest_accelerator=[{"count": 1}]),
            lambda r: r["google_compute_instance"]["after"]["network_interface"][0]["access_config"][0].update(nat_ip="203.0.113.40"),
            lambda r: r["google_compute_instance"]["after"]["attached_disk"][0].update(source="foreign-disk"),
            lambda r: r["google_compute_disk"]["after"].update(type="hyperdisk-balanced"),
            lambda r: r["google_compute_disk"]["after"].update(size=True),
        )
        for change in changes:
            self.plan = copy.deepcopy(original)
            change({r["type"]: r["change"] for r in self.plan["resource_changes"]})
            with self.subTest(change=change), self.assertRaises(PolicyError): self.measure()
            self.adapter.get.assert_not_called(); self.assertEqual(self.http.calls, [])

    def test_missing_or_extra_firewall_or_disk_refused(self):
        original = copy.deepcopy(self.plan)
        for kind in ("google_compute_disk", "google_compute_firewall"):
            self.plan = copy.deepcopy(original)
            self.plan["resource_changes"] = [r for r in self.plan["resource_changes"] if r["type"] != kind]
            with self.subTest(kind=kind), self.assertRaises(PolicyError): self.measure()
        self.plan = copy.deepcopy(original); self.plan["resource_changes"].append(copy.deepcopy(original["resource_changes"][0]))
        with self.assertRaises(PolicyError): self.measure()

    def test_shared_core_arm_custom_or_changed_cpu_ram_vm_size_refused(self):
        for size in ("e2-micro", "e2-custom-2-8192", "t2a-standard-2", "n2-standard-2"):
            self.cloud["size"] = size
            for resource in self.plan["resource_changes"]:
                if resource["type"] == "google_compute_instance": resource["change"]["after"]["machine_type"] = size
            with self.subTest(size=size), self.assertRaisesRegex(PolicyError, "gcp_e2_plan_scope_required"): self.measure()
        self.cloud["size"] = "e2-standard-2"
        self.plan = plan()
        for mutation in ({"architecture": "ARM64"}, {"guestCpus": 4}, {"memoryMb": 4096}, {"memoryMb": 16384}):
            self.adapter.get.return_value = {"name": self.cloud["size"], "architecture": "X86_64", "guestCpus": 2, "memoryMb": 8192, **mutation}
            with self.subTest(mutation=mutation), self.assertRaisesRegex(PolicyError, "gcp_price_vm_size_unavailable"): self.measure()
        self.assertEqual(self.http.calls, [])

    def test_wrong_image_family_license_owner_or_arm_refused_before_catalog(self):
        for mutation in ({"family": "ubuntu-pro-2204-lts"}, {"architecture": "ARM64"}, {"status": "DEPRECATED"},
                         {"licenses": [PRIVATE]}, {"selfLink": "https://foreign.invalid/image"}):
            self.adapter.http.request.return_value = {**image(), **mutation}
            with self.subTest(mutation=mutation), self.assertRaisesRegex(PolicyError, "gcp_price_ubuntu_image_required"): self.measure()
        self.assertEqual(self.http.calls, [])

    def test_family_or_different_immutable_boot_image_refused_before_cloud_reads(self):
        for selected in ("ubuntu-os-cloud/ubuntu-2204-lts", prices.IMAGE_LINK + "ubuntu-2204-jammy-v20261002",
                         "https://foreign.invalid/" + image()["name"]):
            self.plan = plan()
            vm = next(row["change"]["after"] for row in self.plan["resource_changes"] if row["type"] == "google_compute_instance")
            vm["boot_disk"][0]["initialize_params"][0]["image"] = selected
            with self.subTest(selected=selected), self.assertRaises(PolicyError): self.measure()
        self.adapter.get.assert_not_called(); self.adapter.http.request.assert_not_called(); self.assertEqual(self.http.calls, [])

    def test_missing_duplicate_unresolved_or_foreign_planned_image_refused_before_reads(self):
        changes = (
            lambda p: p.pop("planned_values"),
            lambda p: p["planned_values"]["root_module"]["child_modules"].append(copy.deepcopy(p["planned_values"]["root_module"]["child_modules"][0])),
            lambda p: p["planned_values"]["root_module"]["child_modules"][0].update(address="module.foreign[0]"),
            lambda p: p["planned_values"]["root_module"]["child_modules"][0]["resources"].append(copy.deepcopy(p["planned_values"]["root_module"]["child_modules"][0]["resources"][0])),
        )
        for change in changes:
            self.plan = plan(); change(self.plan)
            with self.subTest(change=change), self.assertRaisesRegex(PolicyError, "gcp_resolved_image_plan_required"): self.measure()
        for mutation in ({"project": "foreign-project"}, {"family": "paid-pro-family"}, {"status": "PENDING"},
                         {"image_id": None}, {"image_id": True}, {"image_id": "0"}, {"image_id": str(2 ** 64)},
                         {"self_link": "https://foreign.invalid/image"}, {"licenses": [PRIVATE]}):
            self.plan = plan()
            self.plan["planned_values"]["root_module"]["child_modules"][0]["resources"][0]["values"].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(PolicyError, "gcp_resolved_image_plan_required"): self.measure()
        self.plan = plan()
        data = self.plan["planned_values"]["root_module"]["child_modules"][0]["resources"][0]
        self.plan["resource_changes"].append({key: data[key] for key in ("address", "mode", "type")} | {
            "change": {"actions": ["read"], "after": data["values"], "after_unknown": {"image_id": True}}})
        with self.assertRaisesRegex(PolicyError, "gcp_resolved_image_plan_required"): self.measure()
        self.adapter.get.assert_not_called(); self.adapter.http.request.assert_not_called(); self.assertEqual(self.http.calls, [])

    def test_exact_named_image_id_and_availability_rechecked_without_family_lookup(self):
        for mutation in ({"name": "ubuntu-2204-jammy-v20261002"}, {"id": "987654321"}, {"id": 1234567890123456789},
                         {"deprecated": {"state": "DEPRECATED"}}, {"deprecated": {"state": "ACTIVE", "obsolete": "2099-01-01T00:00:00Z"}}):
            self.adapter.http.request.return_value = image() | mutation
            with self.subTest(mutation=mutation), self.assertRaisesRegex(PolicyError, "gcp_price_ubuntu_image_required"): self.measure()
        self.assertEqual(self.http.calls, [])
        self.adapter.http.request.return_value = image()
        result = self.measure()
        self.assertEqual(result["selection"]["image_id"], image()["id"])
        self.assertEqual(result["selection"]["image_self_link"], image()["selfLink"])
        self.assertNotIn("resolved_image_plan_binding", result["not_verified"])
        self.assertTrue(all(call.args[0] == prices.IMAGE_ENDPOINT + image()["name"] for call in self.adapter.http.request.call_args_list))

    def test_changed_model_cost_network_cleanup_and_image_binding_stay_pending(self):
        result = self.measure()
        for code in ("model_execution_cost_bound", "egress_unit_price_and_volume_bound", "cleanup_deadline_enforcement"):
            self.assertIn(code, result["not_verified"])

    def test_expired_measurement_cannot_receive_receipt(self):
        clock = iter((NOW, NOW + 301))
        with self.assertRaisesRegex(PolicyError, "price_measurement_expired"): self.measure(lambda: next(clock))


class CatalogTest(unittest.TestCase):
    def catalog(self, pages=None):
        cloud = cloud_config()["providers"]["gcp"]
        http = FakeHTTP(pages)
        return prices.GCPCatalog(adapter(cloud), http, now=lambda: NOW), http

    def test_full_pagination_keeps_scope_and_checks_late_duplicates(self):
        rows = catalog_rows()
        catalog, http = self.catalog([{"skus": rows[:2], "nextPageToken": "next&?page"}, {"skus": rows[2:]}])
        rates, proof = catalog.selected("us-east1")
        self.assertEqual(rates["cpu"], prices.Decimal(".02")); self.assertEqual(proof["page_count"], 2)
        self.assertEqual(http.calls, [(PRIVATE, None), (PRIVATE, "next&?page")])
        catalog, _ = self.catalog([{"skus": rows, "nextPageToken": "more"}, {"skus": [rows[0]]}])
        with self.assertRaisesRegex(PolicyError, "gcp_price_product_duplicate_or_changed"): catalog.selected("us-east1")

    def test_other_region_spot_custom_regional_disk_and_wrong_service_do_not_substitute(self):
        changes = (lambda r: r.update(serviceRegions=["foreign-region"]), lambda r: r["category"].update(usageType="Preemptible"),
                   lambda r: r.update(description="E2 Custom Instance Core running in Americas"),
                   lambda r: r.update(name="services/OTHER/skus/" + r["skuId"]),
                   lambda r: r.update(geoTaxonomy=None))
        for change in changes:
            rows = catalog_rows(); change(rows[0]); catalog, _ = self.catalog([{"skus": rows}])
            with self.subTest(change=change), self.assertRaises(PolicyError): catalog.selected("us-east1")
        rows = catalog_rows(); rows[2]["description"] = "Regional Balanced PD Capacity"
        catalog, _ = self.catalog([{"skus": rows}])
        with self.assertRaisesRegex(PolicyError, "gcp_price_product_missing"): catalog.selected("us-east1")

    def test_wrong_units_currency_dates_conversion_and_negative_money_fail(self):
        def expression(r): return r["pricingInfo"][0]["pricingExpression"]
        changes = (lambda r: expression(r).update(usageUnit="requests"), lambda r: expression(r).update(baseUnitConversionFactor=1),
                   lambda r: expression(r)["tieredRates"][0]["unitPrice"].update(currencyCode="EUR"),
                   lambda r: expression(r)["tieredRates"][0]["unitPrice"].update(nanos=-1),
                   lambda r: r["pricingInfo"][0].update(currencyConversionRate=1.1),
                   lambda r: r["pricingInfo"][0].update(effectiveTime="2099-01-01T00:00:00Z"),
                   lambda r: r["pricingInfo"][0].update(effectiveTime="2000-01-01T00:00:00Z"))
        for change in changes:
            row = sku("cpu"); change(row)
            with self.subTest(change=change), self.assertRaises(PolicyError): prices.unit_rate(row, "cpu", NOW)

    def test_highest_tier_ignores_free_allowance_and_display_quantity(self):
        row = sku("ipv4"); row["pricingInfo"][0]["pricingExpression"]["displayQuantity"] = 1000
        rate, proof = prices.unit_rate(row, "ipv4", NOW)
        self.assertEqual(rate, prices.Decimal(".005")); self.assertEqual(proof["rate_policy"], "highest_published_tier")
        for changes in ({"units": "1", "nanos": 500000000}, {"units": "0", "nanos": 5000000}):
            self.assertGreater(prices.money({"currencyCode": "USD", **changes}), 0)

    def test_latest_price_published_during_read_is_checked_against_completion_time(self):
        rows = catalog_rows()
        rows[0]["pricingInfo"][0]["effectiveTime"] = datetime.fromtimestamp(NOW + 4, timezone.utc).isoformat()
        catalog, _ = self.catalog([{"skus": rows}]); clock = iter((NOW, NOW, NOW + 5))
        catalog.now = lambda: next(clock)
        rates, proof = catalog.selected("us-east1")
        self.assertEqual(rates["cpu"], prices.Decimal(".02"))
        self.assertEqual(proof["products"]["cpu"]["effective_time"], rows[0]["pricingInfo"][0]["effectiveTime"])

    def test_ambiguous_sku_tiers_prices_or_repeated_pages_fail(self):
        rows = catalog_rows(); extra = copy.deepcopy(rows[0]); extra.update(skuId="AAAA-BBBB-CCCC", name=prices.SERVICE + "/skus/AAAA-BBBB-CCCC")
        catalog, _ = self.catalog([{"skus": rows + [extra]}])
        with self.assertRaisesRegex(PolicyError, "gcp_price_product_ambiguous"): catalog.selected("us-east1")
        for pages in ([{"skus": [], "nextPageToken": "same"}] * 2, [{"skus": [], "nextPageToken": None}]):
            catalog, _ = self.catalog(pages)
            with self.assertRaises(PolicyError): catalog.selected("us-east1")
        row = sku("cpu"); row["pricingInfo"].append(copy.deepcopy(row["pricingInfo"][0]))
        with self.assertRaises(PolicyError): prices.unit_rate(row, "cpu", NOW)
        row = sku("cpu"); row["pricingInfo"][0]["pricingExpression"]["tieredRates"] *= 2
        with self.assertRaises(PolicyError): prices.unit_rate(row, "cpu", NOW)

    def test_page_and_total_byte_limits_and_expired_catalog_fail(self):
        catalog, _ = self.catalog([{"skus": [], "nextPageToken": str(i)} for i in range(2)])
        with patch.object(prices, "MAX_PAGES", 2), self.assertRaisesRegex(PolicyError, "gcp_price_pagination_limit"): catalog.selected("us-east1")
        catalog, _ = self.catalog()
        with patch.object(prices, "MAX_TOTAL_BYTES", 1), self.assertRaisesRegex(PolicyError, "gcp_price_response_too_large"): catalog.selected("us-east1")
        clock = iter((NOW, NOW + 301)); catalog, _ = self.catalog(); catalog.now = lambda: next(clock)
        with self.assertRaisesRegex(PolicyError, "price_measurement_expired"): catalog.selected("us-east1")

    def test_unrelated_raw_fields_are_hashed_and_not_exposed(self):
        rows = catalog_rows(); rows[0]["unrelated"] = PRIVATE; catalog, _ = self.catalog([{"skus": rows}])
        _, proof = catalog.selected("us-east1")
        self.assertNotIn(PRIVATE, json.dumps(proof)); self.assertEqual(len(proof["products"]["cpu"]["sku_sha256"]), 64)


class TransportTest(unittest.TestCase):
    def response(self, raw):
        return SimpleNamespace(status=200, read=lambda limit: raw[:limit])

    def test_fixed_get_origin_currency_pagination_bearer_and_no_proxy(self):
        http = prices.CatalogHTTP(); response = Mock()
        response.__enter__ = Mock(return_value=self.response(b'{"skus":[]}')); response.__exit__ = Mock(return_value=False)
        with patch.object(http.opener, "open", return_value=response) as opened:
            value, size, proof = http.page(PRIVATE, "next&?page")
        request = opened.call_args.args[0]; url = urlsplit(request.full_url)
        self.assertEqual(url.scheme + "://" + url.netloc + url.path, prices.ENDPOINT)
        self.assertEqual(parse_qs(url.query), {"currencyCode": ["USD"], "pageSize": ["500"], "pageToken": ["next&?page"]})
        self.assertEqual(request.get_method(), "GET"); self.assertEqual(request.headers["Authorization"], "Bearer " + PRIVATE)
        self.assertEqual(opened.call_args.kwargs, {"timeout": 20}); self.assertEqual(value, {"skus": []})
        self.assertEqual(size, 11); self.assertEqual(len(proof), 64)
        self.assertTrue(any(isinstance(handler, prices.NoRedirect) for handler in http.opener.handlers))

    def test_duplicate_nonfinite_oversized_or_invalid_json_and_http_errors_redacted(self):
        for raw in (b'{"skus":[],"skus":[]}', b'{"x":NaN}', b'[]', b'not-json', b'x' * (prices.MAX_PAGE_BYTES + 1)):
            http = prices.CatalogHTTP(); response = Mock()
            response.__enter__ = Mock(return_value=self.response(raw)); response.__exit__ = Mock(return_value=False)
            with patch.object(http.opener, "open", return_value=response), self.assertRaises(PolicyError) as caught: http.page(PRIVATE)
            self.assertNotIn(PRIVATE, str(caught.exception))
        for error in (HTTPError("https://private.invalid", 403, PRIVATE, {}, io.BytesIO(PRIVATE.encode())), URLError(PRIVATE)):
            http = prices.CatalogHTTP()
            with patch.object(http.opener, "open", side_effect=error) as opened, self.assertRaises(PolicyError) as caught: http.page(PRIVATE)
            self.assertNotIn(PRIVATE, str(caught.exception)); self.assertEqual(opened.call_count, 1)


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation.PreparationTest("runTest"); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        for name in ("showcase_prices", "showcase_gcp_prices", "showcase_terraform", "showcase_policy", "showcase_preflight"):
            path = self.fixture.repo / "scripts" / (name + ".py"); path.parent.mkdir(exist_ok=True)
            path.write_bytes((Path(terraform.__file__).parent / (name + ".py")).read_bytes())
        self.fixture.git("add", "."); self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "price sources")
        self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip()
        self.config = cloud_config(); self.run = self.fixture.batch / "attempts" / PREFIX
        prepare(self.config, "gcp", PREFIX, self.fixture.batch, self.fixture.revision, self.fixture.repo)
        session = FakeSession(self.config); session.adapter = adapter(self.config["providers"]["gcp"])
        self.client = terraform.TerraformAttempt(self.run, self.config, "gcp", session, now=lambda: NOW)
        def tf(*args, **kwargs):
            if args[0] == "version": return json.dumps({"terraform_version": "1.16.5"}).encode()
            if args[0] == "init": (self.client.checkout / ".terraform.lock.hcl").write_text("fixture-lock")
            if args[0] == "plan": path = self.run / "deployment.tfplan"; path.write_bytes(b"fixture-plan"); path.chmod(0o600)
            if args[0] == "show": return json.dumps(plan()).encode()
            return b""
        self.client.tf = Mock(side_effect=tf); self.client.plan()

    def catalog(self):
        return prices.GCPCatalog(self.client.session.adapter, FakeHTTP(), now=lambda: NOW)

    def test_private_receipt_binds_original_plan_commit_without_state_or_reservation(self):
        result = self.client.resource_unit_prices(gcp_catalog=self.catalog())
        path = self.run / "resource-prices.json"
        self.assertEqual(path.stat().st_mode & 0o077, 0); self.assertEqual(json.loads(path.read_text()), result)
        self.assertEqual(result["source_revision"], self.fixture.revision); self.assertEqual(result["plan_sha256"], self.client.record()["plan_sha256"])
        self.assertFalse(self.client.state_path.exists()); self.assertFalse((self.fixture.batch / "reservations.json").exists())

    def test_saved_plan_drift_refused_before_api_reads(self):
        (self.run / "deployment.tfplan").write_bytes(b"changed")
        with self.assertRaisesRegex(PolicyError, "saved_plan_changed"): self.client.resource_unit_prices(gcp_catalog=self.catalog())
        self.client.session.adapter.get.assert_not_called(); self.assertFalse((self.run / "resource-prices.json").exists())

    def test_public_plan_entrypoint_reports_partial_prices_and_no_apply(self):
        import showcase_live as live
        from showcase_cleanup import private_write
        self.client.record_path.unlink()
        inputs = self.fixture.root / "private-inputs.json"; private_write(inputs, self.config)
        args = ['showcase_live.py', '--phase', 'plan', '--provider', 'gcp', '--run-id', PREFIX,
                '--config', str(inputs), '--batch', str(self.fixture.batch)]
        output = io.StringIO()
        with patch.dict(live.os.environ, {"AGENT_STACK_LIVE_TESTS": "1"}), patch.object(sys, "argv", args), \
             patch.object(live.shutil, "which", return_value="/fixture/tool"), patch.object(live.signal, "signal"), \
             patch.object(live, "CloudSession", return_value=self.client.session), patch.object(live, "TerraformAttempt", return_value=self.client), \
             patch.object(prices, "GCPCatalog", return_value=self.catalog()), contextlib.redirect_stdout(output):
            code = live.main()
        result = json.loads(output.getvalue()); self.assertEqual(code, 0, result)
        self.assertFalse(result["launch_ready"]); self.assertFalse(result["spend_reserved"])
        self.assertFalse(result["resource_unit_prices"]["complete_quote"])
        self.assertFalse(self.client.state_path.exists()); self.assertFalse((self.fixture.batch / "reservations.json").exists())
