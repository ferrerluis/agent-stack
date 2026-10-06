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
from unittest.mock import Mock, patch, call
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_hetzner_prices as prices
import showcase_terraform as terraform
from showcase_policy import PolicyError, reserve_amount
from showcase_attempt import prepare
from test_showcase_policy import config, NOW
import test_showcase_attempt as preparation
from test_showcase_terraform import FakeSession

PREFIX = "as7-fixture01"
PRIVATE = "private-fixture-not-for-reports"
BILLING = '<p>We always round up the hourly usage of a server.</p><p>Primary IPv6s are free and are not listed in the invoice.</p>'
VOLUMES = '<p>Volumes have a monthly price cap and are billed hourly.</p>'


def cloud_config():
    value = config(); value["providers"]["hetzner"].update(size="cpx31", region="ash")
    return value


def plan():
    return json.loads((Path(__file__).parent / "fixtures/hetzner-price-shape.json").read_text())


def money(net, gross):
    return {"net": net, "gross": gross}


def api_adapter(cloud):
    size = {"id": 23, "name": "cpx31", "architecture": "x86", "memory": 8, "disk": 160,
            "locations": [{"id": 5, "name": "ash", "available": True, "deprecation": None}]}
    image = {"id": 45557056, "name": "ubuntu-22.04", "architecture": "x86", "type": "system", "status": "available",
             "os_flavor": "ubuntu", "os_version": "22.04", "deprecation": None}
    pricing = {"currency": "EUR", "vat_rate": "19.00", "volume": {"price_per_gb_month": money("0.044", "0.0524")},
        "primary_ips": [{"type": "ipv4", "prices": [{"location": "ash", "price_hourly": money("0.001", "0.0012"),
                       "price_monthly": money("0.60", "0.714")}]}],
        "server_types": [{"id": 23, "name": "cpx31", "prices": [{"location": "ash", "price_hourly": money("0.025", "0.0298"),
             "price_monthly": money("18.00", "21.42"), "included_traffic": 1000000000000, "price_per_tb_traffic": money("1.0", "1.19")}]}]}
    def pages(path, key, style):
        assert style == "hetzner"
        if path == "/server_types" and key == "server_types": return [size]
        raise AssertionError("unexpected endpoint")
    def get(path):
        if path == "/images/45557056": return {"image": image}
        if path == "/pricing": return {"pricing": pricing}
        raise AssertionError("unexpected endpoint")
    return SimpleNamespace(cloud=cloud, size=size, image=image, pricing=pricing, pages=Mock(side_effect=pages), get=Mock(side_effect=get))


class FakePublic:
    def __init__(self):
        self.documents = {"billing": BILLING, "volumes": VOLUMES}; self.calls = []

    def fetch(self, name):
        self.calls.append(name); document = self.documents[name]
        return {"source": prices.SOURCES[name], "body_sha256": hashlib.sha256(document.encode()).hexdigest(), "document": document}


class PriceTest(unittest.TestCase):
    def setUp(self):
        self.cloud = cloud_config()["providers"]["hetzner"]; self.plan = plan()
        self.adapter = api_adapter(self.cloud); self.public = FakePublic()

    def measure(self, clock=None):
        return prices.hetzner_rates(self.plan, self.cloud, PREFIX, "/tmp/private-fixture", self.adapter,
                                   "a" * 40, "b" * 64, self.public, (lambda: NOW) if clock is None else clock)

    def test_actual_module_plan_gross_prices_currency_and_default_ip_assumptions_remain_partial(self):
        result = self.measure()
        self.assertEqual(result["currency"], "EUR"); self.assertEqual(result["vat_percent"], "19.00")
        self.assertEqual(result["vm_and_boot_hourly_upper_bound"], "0.031875000")
        self.assertEqual(result["data_volume_gb_hourly_upper_bound"], "0.000077977")
        self.assertEqual(result["primary_ipv4_hourly_upper_bound"], "0.001200000")
        self.assertEqual(result["vm_disk_ips_hourly_upper_bound"], "0.036973810")
        self.assertEqual(result["outbound_tb_upper_bound"], "1.190000000")
        self.assertEqual(result["selection"]["data_volume_gb"], 50); self.assertEqual(result["selection"]["image_id"], 45557056)
        self.assertIn("eur_to_usd_spend_bound_and_conversion_fees", result["not_verified"])
        self.assertIn("automatic_primary_ip_creation_defaults", result["not_verified"])
        self.assertNotIn("resolved_image_plan_binding", result["not_verified"])
        self.assertFalse(result["complete_quote"]); self.assertEqual(result["launch_gate"], "unverified")
        self.assertEqual(result["server_billing_increment_seconds"], 3600)
        self.assertEqual(self.public.calls, ["billing", "volumes"])
        self.assertEqual(self.adapter.get.call_args_list, [call("/images/45557056"), call("/pricing")])
        with self.assertRaises(PolicyError): reserve_amount(result, "hetzner", 14400, NOW)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_usd_has_no_foreign_currency_conversion_assumption_and_underrounded_vat_is_raised(self):
        self.adapter.pricing["currency"] = "USD"
        self.adapter.pricing["server_types"][0]["prices"][0]["price_hourly"] = money("0.10", "0.11")
        result = self.measure()
        self.assertEqual(result["vm_and_boot_hourly_upper_bound"], "0.119000000")
        self.assertNotIn("eur_to_usd_spend_bound_and_conversion_fees", result["not_verified"])
        self.assertFalse(result["complete_quote"])

    def test_mutable_different_or_unknown_boot_image_refuses_before_any_reads(self):
        original = copy.deepcopy(self.plan)
        for image, unknown in (("ubuntu-22.04", {}), ("45557057", {}), (45557056, {}),
                               (None, {}), ("45557056", {"image": True})):
            self.plan = copy.deepcopy(original)
            vm = next(row["change"] for row in self.plan["resource_changes"] if row["type"] == "hcloud_server")
            vm["after"]["image"] = image; vm["after_unknown"].update(unknown)
            with self.subTest(image=image, unknown=unknown), self.assertRaises(PolicyError): self.measure()
        self.adapter.pages.assert_not_called(); self.adapter.get.assert_not_called(); self.assertEqual(self.public.calls, [])

    def test_missing_forged_duplicate_or_foreign_planned_image_refuses_before_reads(self):
        original = copy.deepcopy(self.plan)
        mutations = [lambda p: p.pop("prior_state"), lambda p: p.update(prior_state=None),
                     lambda p: p["prior_state"]["values"].update(root_module=None),
                     lambda p: p["prior_state"]["values"]["root_module"].update(child_modules=[]),
                     lambda p: p["prior_state"]["values"]["root_module"]["child_modules"].append(copy.deepcopy(p["prior_state"]["values"]["root_module"]["child_modules"][0])),
                     lambda p: p["prior_state"]["values"]["root_module"]["child_modules"][0].update(address="module.foreign[0]"),
                     lambda p: p["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"].append(copy.deepcopy(p["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]))]
        for mutation in mutations:
            self.plan = copy.deepcopy(original); mutation(self.plan)
            with self.assertRaisesRegex(PolicyError, "hetzner_resolved_image_plan_required"): self.measure()
        for key, value in (("id", None), ("id", True), ("id", 0), ("id", "45557056"), ("id", 2 ** 63),
                           ("name", "debian-12"), ("type", "snapshot"), ("architecture", "arm"),
                           ("os_flavor", "debian"), ("os_version", "24.04"), ("with_architecture", "arm"),
                           ("with_status", ["creating"]), ("most_recent", True), ("include_deprecated", False),
                           ("selector", "foreign=yes"), ("with_selector", "foreign=yes")):
            self.plan = copy.deepcopy(original)
            self.plan["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"][key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(PolicyError, "hetzner_resolved_image_plan_required"): self.measure()
        self.adapter.pages.assert_not_called(); self.adapter.get.assert_not_called(); self.assertEqual(self.public.calls, [])

    def test_image_data_changes_must_be_complete_unchanged_and_unique(self):
        original = copy.deepcopy(self.plan)
        value = copy.deepcopy(self.plan["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"])
        change = {"address": prices.IMAGE_ADDRESS, "mode": "data", "type": "hcloud_image", "change": {
            "actions": ["no-op"], "before": copy.deepcopy(value), "after": copy.deepcopy(value), "after_unknown": {}}}
        self.plan["resource_changes"].append(copy.deepcopy(change)); self.measure()
        for mutation in (lambda c: c["change"].update(actions=["read"]), lambda c: c["change"]["after"].update(id=45557057),
                         lambda c: c["change"].update(after_unknown={"id": True}), lambda c: c.update(type="foreign")):
            self.plan = copy.deepcopy(original); data = copy.deepcopy(change); mutation(data); self.plan["resource_changes"].append(data)
            self.adapter.pages.reset_mock(); self.adapter.get.reset_mock(); self.public.calls.clear()
            with self.assertRaisesRegex(PolicyError, "hetzner_resolved_image_plan_required"): self.measure()
            self.adapter.pages.assert_not_called(); self.adapter.get.assert_not_called(); self.assertEqual(self.public.calls, [])
        self.plan = copy.deepcopy(original); self.plan["resource_changes"].extend([copy.deepcopy(change), copy.deepcopy(change)])
        with self.assertRaisesRegex(PolicyError, "hetzner_resolved_image_plan_required"): self.measure()
        self.adapter.pages.assert_not_called(); self.adapter.get.assert_not_called()

    def test_exact_image_reread_rejects_changed_missing_or_malformed_identity_before_pricing(self):
        original = copy.deepcopy(self.adapter.image)
        replies = [{}, {"image": None}, {"image": [original]}, {"image": original | {"id": True}},
                   {"image": original | {"id": 45557057}}, {"image": original | {"name": "debian-12"}},
                   {"image": original | {"status": "unavailable"}}]
        for reply in replies:
            self.adapter.get.reset_mock(); self.adapter.get.side_effect = None; self.adapter.get.return_value = reply
            with self.assertRaisesRegex(PolicyError, "hetzner_price_ubuntu_image_required"): self.measure()
            self.adapter.get.assert_called_once_with("/images/45557056"); self.assertEqual(self.public.calls, [])

    def test_refreshed_state_image_and_matching_copies_are_supported_but_conflicts_refuse(self):
        original = copy.deepcopy(self.plan)
        self.plan["planned_values"] = self.plan.pop("prior_state")["values"]; self.measure()
        self.plan["prior_state"] = {"values": copy.deepcopy(self.plan["planned_values"])}; self.measure()
        for key, value in (("id", 45557057), ("architecture", "arm")):
            self.plan = copy.deepcopy(original)
            self.plan["planned_values"] = copy.deepcopy(self.plan["prior_state"]["values"])
            self.plan["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"][key] = value
            self.adapter.pages.reset_mock(); self.adapter.get.reset_mock(); self.public.calls.clear()
            with self.assertRaisesRegex(PolicyError, "hetzner_resolved_image_plan_required"): self.measure()
            self.adapter.pages.assert_not_called(); self.adapter.get.assert_not_called(); self.assertEqual(self.public.calls, [])
        for prior in ({}, {"values": None}, {"values": {"root_module": None}}):
            self.plan = copy.deepcopy(original); self.plan["prior_state"] = prior
            with self.assertRaisesRegex(PolicyError, "hetzner_resolved_image_plan_required"): self.measure()
            self.adapter.pages.assert_not_called(); self.adapter.get.assert_not_called()

    def test_extra_or_unknown_plan_features_and_disk_quantities_fail_before_any_reads(self):
        original = copy.deepcopy(self.plan)
        mutations = [lambda vm, vol, unknown: vm.update(backups=True), lambda vm, vol, unknown: vm.update(backups=0),
            lambda vm, vol, unknown: vm.update(keep_disk=True), lambda vm, vol, unknown: vm.update(keep_disk=0),
            lambda vm, vol, unknown: vm.update(public_net=[{"ipv4_enabled": True}]), lambda vm, vol, unknown: vm.update(iso="paid"),
            lambda vm, vol, unknown: vm.update(image="snapshot"), lambda vm, vol, unknown: vm.update(location="wrong"),
            lambda vm, vol, unknown: vm.update(placement_group_id=42), lambda vm, vol, unknown: unknown.update(backups=True),
            lambda vm, vol, unknown: unknown.update(size=True), lambda vm, vol, unknown: unknown.update(public_net=True),
            lambda vm, vol, unknown: vol.update(size=True), lambda vm, vol, unknown: vol.update(size=9),
            lambda vm, vol, unknown: vol.update(size=10241), lambda vm, vol, unknown: vol.update(server_id=42),
            lambda vm, vol, unknown: vol.update(location="wrong")]
        for mutation in mutations:
            self.plan = copy.deepcopy(original); changes = {r["type"]: r["change"] for r in self.plan["resource_changes"]}
            mutation(changes["hcloud_server"]["after"], changes["hcloud_volume"]["after"], changes["hcloud_server"]["after_unknown"])
            with self.subTest(mutation=mutation), self.assertRaises(PolicyError): self.measure()
            self.adapter.pages.assert_not_called(); self.adapter.get.assert_not_called(); self.assertEqual(self.public.calls, [])

    def test_missing_firewall_key_extra_resources_or_foreign_attachment_cannot_be_priced(self):
        original = copy.deepcopy(self.plan)
        for kind in ("hcloud_firewall", "hcloud_ssh_key"):
            self.plan = {"resource_changes": [r for r in original["resource_changes"] if r["type"] != kind]}
            with self.assertRaises(PolicyError): self.measure()
        self.plan = copy.deepcopy(original); self.plan["resource_changes"].append(copy.deepcopy(self.plan["resource_changes"][0]))
        with self.assertRaises(PolicyError): self.measure()
        self.plan = copy.deepcopy(original)
        attachment = next(r for r in self.plan["resource_changes"] if r["type"] == "hcloud_volume_attachment")
        attachment["change"]["after"]["volume_id"] = 99
        with self.assertRaises(PolicyError): self.measure()
        self.adapter.pages.assert_not_called()

    def test_type_and_image_architecture_memory_status_and_license_scope_fail_closed(self):
        size, image = copy.deepcopy(self.adapter.size), copy.deepcopy(self.adapter.image)
        for target, mutation in (("size", {"architecture": "arm"}), ("size", {"memory": 4}), ("size", {"memory": float("inf")}),
                ("size", {"disk": 19}), ("size", {"id": True}), ("image", {"status": "unavailable"}),
                ("image", {"type": "snapshot"}), ("image", {"architecture": "arm"}), ("image", {"os_version": "24.04"})):
            self.adapter.size.clear(); self.adapter.size.update(size); self.adapter.image.clear(); self.adapter.image.update(image)
            getattr(self.adapter, target).update(mutation)
            with self.subTest(target=target, mutation=mutation), self.assertRaises(PolicyError): self.measure()

    def test_location_and_image_removal_must_cover_attempt_from_read_completion(self):
        stamp = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        dep = {"announced": stamp(NOW - 1), "unavailable_after": stamp(NOW + 14401)}
        self.adapter.image["deprecation"] = dep
        self.measure()
        with self.assertRaisesRegex(PolicyError, "vm_or_image_removal_during_attempt"): self.measure(iter([NOW, NOW + 2]).__next__)
        self.adapter.image["deprecation"] = None; self.adapter.size["locations"][0]["deprecation"] = dep
        with self.assertRaisesRegex(PolicyError, "vm_or_image_removal_during_attempt"): self.measure(iter([NOW, NOW + 2]).__next__)

    def test_duplicate_missing_or_wrong_location_vm_ip_and_image_prices_are_refused(self):
        original = copy.deepcopy(self.adapter.pricing)
        for mutation in (lambda p: p["server_types"].append(copy.deepcopy(p["server_types"][0])),
                         lambda p: p["server_types"][0].update(id=24), lambda p: p.update(primary_ips=[]),
                         lambda p: p["server_types"][0]["prices"][0].update(location="fsn1"),
                         lambda p: p["primary_ips"][0]["prices"].append(copy.deepcopy(p["primary_ips"][0]["prices"][0]))):
            self.adapter.pricing.clear(); self.adapter.pricing.update(copy.deepcopy(original)); mutation(self.adapter.pricing)
            with self.subTest(mutation=mutation), self.assertRaises(PolicyError): self.measure()
        self.adapter.pages.side_effect = None; self.adapter.pages.return_value = [self.adapter.size, self.adapter.size]
        with self.assertRaises(PolicyError): self.measure()

    def test_nonfinite_negative_boolean_scientific_and_bad_currency_or_vat_never_enter_receipt(self):
        original = copy.deepcopy(self.adapter.pricing)
        for value in ("NaN", "Infinity", "-1", "1e-3", True, 0.02, "1000001"):
            self.adapter.pricing["volume"]["price_per_gb_month"]["gross"] = value
            with self.subTest(value=value), self.assertRaises(PolicyError): self.measure()
        for mutation in ({"currency": "GBP"}, {"vat_rate": "101"}, {"vat_rate": True}):
            self.adapter.pricing.clear(); self.adapter.pricing.update(copy.deepcopy(original)); self.adapter.pricing.update(mutation)
            with self.assertRaises(PolicyError): self.measure()

    def test_explicit_ipv6_prices_must_agree_with_current_free_contract(self):
        ipv6 = {"type": "ipv6", "prices": [{"location": "ash", "price_hourly": money("0", "0"), "price_monthly": money("0", "0")}]}
        self.adapter.pricing["primary_ips"].append(ipv6); self.measure()
        ipv6["prices"][0]["price_hourly"] = money("0.1", "0.12")
        with self.assertRaisesRegex(PolicyError, "hetzner_ipv6_free_contract_changed"): self.measure()

    def test_changed_contract_hash_url_or_script_only_claim_is_not_evidence(self):
        for document in (BILLING.replace("Primary IPv6s are free", "Primary IPv6s cost money"), '<script>' + BILLING + '</script>', BILLING * 2):
            self.public.documents["billing"] = document
            with self.assertRaisesRegex(PolicyError, "hetzner_billing_contract_changed"): self.measure()
        self.public = FakePublic(); original = self.public.fetch
        for changes in ({"source": "https://evil.invalid"}, {"body_sha256": "f" * 64}):
            with patch.object(self.public, "fetch", side_effect=lambda name: original(name) | changes), self.assertRaisesRegex(PolicyError, "price_source_changed"):
                self.measure()

    def test_expired_or_nonfinite_measurement_or_adapter_scope_is_refused(self):
        for clock in (iter([NOW, NOW + 301]).__next__, iter([NOW, NOW - 1]).__next__, lambda: float("nan"), lambda: float("inf")):
            with self.assertRaises(PolicyError): self.measure(clock)
        self.adapter.cloud = dict(self.cloud, project_id="foreign")
        with self.assertRaisesRegex(PolicyError, "price_measurement_scope_changed"): self.measure()


class PublicTransportTest(unittest.TestCase):
    def response(self, raw):
        response = Mock(); response.__enter__ = Mock(return_value=SimpleNamespace(status=200, read=lambda size: raw)); response.__exit__ = Mock(return_value=False)
        return response

    def test_fixed_public_get_has_no_auth_proxy_or_redirect_and_has_byte_limit(self):
        public = prices.HetznerTerms()
        with patch.object(public.opener, "open", return_value=self.response(BILLING.encode())) as opened:
            result = public.fetch("billing")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, prices.SOURCES["billing"]); self.assertEqual(request.get_method(), "GET")
        self.assertNotIn("Authorization", request.headers); self.assertEqual(opened.call_args.kwargs, {"timeout": 20})
        self.assertEqual(result["body_sha256"], hashlib.sha256(BILLING.encode()).hexdigest())
        self.assertTrue(any(isinstance(handler, prices.NoRedirect) for handler in public.opener.handlers))

    def test_unapproved_source_and_redirect_are_refused_before_followup(self):
        public = prices.HetznerTerms()
        with patch.object(public.opener, "open") as opened, self.assertRaisesRegex(PolicyError, "price_source_refused"):
            public.fetch("https://evil.invalid/" + PRIVATE)
        opened.assert_not_called()
        with self.assertRaisesRegex(PolicyError, "price_redirect_refused"):
            prices.NoRedirect().redirect_request(None, None, 302, PRIVATE, {}, "https://evil.invalid")

    def test_invalid_utf8_oversize_http_and_network_errors_are_redacted_without_retry(self):
        for raw in (b'\xff', b'x' * (prices.MAX_RESPONSE + 1)):
            public = prices.HetznerTerms()
            with patch.object(public.opener, "open", return_value=self.response(raw)), self.assertRaises(PolicyError): public.fetch("billing")
        for error in (URLError(PRIVATE), HTTPError("https://evil.invalid", 403, PRIVATE, {}, io.BytesIO(PRIVATE.encode()))):
            public = prices.HetznerTerms()
            with patch.object(public.opener, "open", side_effect=error) as opened, self.assertRaises(PolicyError) as caught: public.fetch("billing")
            self.assertNotIn(PRIVATE, str(caught.exception)); self.assertEqual(opened.call_count, 1)


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation.PreparationTest("runTest"); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        for name in ("showcase_prices", "showcase_hetzner_prices", "showcase_terraform", "showcase_policy", "showcase_preflight"):
            path = self.fixture.repo / "scripts" / (name + ".py"); path.parent.mkdir(exist_ok=True)
            path.write_bytes((Path(terraform.__file__).parent / (name + ".py")).read_bytes())
        self.fixture.git("add", "."); self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "price sources")
        self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip(); self.config = cloud_config()
        self.run = self.fixture.batch / "attempts" / PREFIX
        prepare(self.config, "hetzner", PREFIX, self.fixture.batch, self.fixture.revision, self.fixture.repo)
        session = FakeSession(self.config); session.adapter = api_adapter(self.config["providers"]["hetzner"])
        self.client = terraform.TerraformAttempt(self.run, self.config, "hetzner", session, now=lambda: NOW)
        def tf(*args, **kwargs):
            if args[0] == "version": return json.dumps({"terraform_version": "1.16.5"}).encode()
            if args[0] == "init": (self.client.checkout / ".terraform.lock.hcl").write_text("fixture-lock")
            if args[0] == "plan": path = self.run / "deployment.tfplan"; path.write_bytes(b"fixture-plan"); path.chmod(0o600)
            if args[0] == "show": return json.dumps(plan()).encode()
            return b""
        self.client.tf = Mock(side_effect=tf); self.client.plan()

    def test_private_receipt_binds_original_plan_commit_without_state_or_reservation(self):
        result = self.client.resource_unit_prices(hetzner_public=FakePublic()); path = self.run / "resource-prices.json"
        self.assertEqual(path.stat().st_mode & 0o077, 0); self.assertEqual(json.loads(path.read_text()), result)
        self.assertEqual(result["source_revision"], self.fixture.revision); self.assertEqual(result["plan_sha256"], self.client.record()["plan_sha256"])
        self.assertFalse(self.client.state_path.exists()); self.assertFalse((self.fixture.batch / "reservations.json").exists())

    def test_saved_plan_drift_refused_before_api_reads(self):
        (self.run / "deployment.tfplan").write_bytes(b"changed")
        with self.assertRaisesRegex(PolicyError, "saved_plan_changed"): self.client.resource_unit_prices(hetzner_public=FakePublic())
        self.client.session.adapter.pages.assert_not_called(); self.assertFalse((self.run / "resource-prices.json").exists())

    def test_older_prepared_hetzner_reader_refused_before_any_price_reads(self):
        target = self.fixture.repo / "scripts/showcase_hetzner_prices.py"
        target.write_bytes(target.read_bytes() + b'\n# older fixture\n')
        self.fixture.git("add", "."); self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "older price source")
        revision = self.fixture.git("rev-parse", "HEAD").strip()
        prefix = PREFIX + "x"
        prepare(self.config, "hetzner", prefix, self.fixture.batch, revision, self.fixture.repo)
        previous_tf = self.client.tf.side_effect
        self.run = self.fixture.batch / "attempts" / prefix
        self.client = terraform.TerraformAttempt(self.run, self.config, "hetzner", self.client.session, now=lambda: NOW)
        def tf(*args, **kwargs):
            if args[0] == "show":
                value = plan()
                for row in value["resource_changes"]:
                    if row["change"]["after"].get("name"):
                        row["change"]["after"]["name"] = row["change"]["after"]["name"].replace(PREFIX, prefix)
                return json.dumps(value).encode()
            return previous_tf(*args, **kwargs)
        self.client.tf = Mock(side_effect=tf); self.client.plan()
        with self.assertRaisesRegex(PolicyError, "price_controller_source_changed"): self.client.resource_unit_prices(hetzner_public=FakePublic())
        self.client.session.adapter.pages.assert_not_called()

    def test_public_plan_entrypoint_reports_partial_prices_and_no_apply(self):
        import showcase_live as live
        from showcase_cleanup import private_write
        self.client.record_path.unlink(); inputs = self.fixture.root / "private-inputs.json"; private_write(inputs, self.config)
        args = ['showcase_live.py', '--phase', 'plan', '--provider', 'hetzner', '--run-id', PREFIX, '--config', str(inputs), '--batch', str(self.fixture.batch)]
        output = io.StringIO()
        with patch.dict(live.os.environ, {"AGENT_STACK_LIVE_TESTS": "1"}), patch.object(sys, "argv", args), \
             patch.object(live.shutil, "which", return_value="/fixture/tool"), patch.object(live.signal, "signal"), \
             patch.object(live, "CloudSession", return_value=self.client.session), patch.object(live, "TerraformAttempt", return_value=self.client), \
             patch.object(prices, "HetznerTerms", return_value=FakePublic()), contextlib.redirect_stdout(output):
            code = live.main()
        result = json.loads(output.getvalue()); self.assertEqual(code, 0, result)
        self.assertFalse(result["launch_ready"]); self.assertFalse(result["spend_reserved"]); self.assertFalse(result["resource_unit_prices"]["complete_quote"])
        self.assertFalse(self.client.state_path.exists()); self.assertFalse((self.fixture.batch / "reservations.json").exists())
