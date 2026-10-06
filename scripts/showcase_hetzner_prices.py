#!/usr/bin/env python3
"""Read plan-bound Hetzner unit prices in their actual account currency."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from showcase_policy import PolicyError, RESOURCE_TYPES, require, validate_fresh_plan, resolved_plan_data
from showcase_preflight import PreflightError, hetzner_deprecation, hetzner_location
from showcase_prices import Decimal, MAX_RESPONSE, NoRedirect, PriceHTML, dollars

SOURCES = {"billing": "https://docs.hetzner.com/cloud/billing/faq/",
           "volumes": "https://docs.hetzner.com/cloud/volumes/overview/"}
IMAGE_ADDRESS = "module.hetzner[0].data.hcloud_image.boot[0]"
BASE = "https://api.hetzner.cloud/v1"


def digest(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        raise PolicyError("invalid_hetzner_price_response") from None


class HetznerTerms:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def fetch(self, name):
        require(name in SOURCES, "price_source_refused")
        try:
            request = Request(SOURCES[name], method="GET", headers={"Accept": "text/html", "User-Agent": "AgentStack-public-pricing-check/1"})
            with self.opener.open(request, timeout=20) as response:
                require(response.status == 200, "price_request_failed")
                raw = response.read(MAX_RESPONSE + 1)
            require(len(raw) <= MAX_RESPONSE, "price_response_too_large")
            document = raw.decode("utf-8")
        except HTTPError as error:
            error.close()
            raise PolicyError("price_request_failed") from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise PolicyError("price_request_failed") from None
        return {"source": SOURCES[name], "body_sha256": hashlib.sha256(raw).hexdigest(), "document": document}


def terms(public):
    proofs = {}
    for name, url in SOURCES.items():
        fetched = public.fetch(name)
        require(isinstance(fetched, dict) and fetched.get("source") == url and isinstance(fetched.get("document"), str)
                and fetched.get("body_sha256") == hashlib.sha256(fetched["document"].encode()).hexdigest(), "price_source_changed")
        parsed = PriceHTML.parse(fetched["document"])
        text = " ".join(" ".join(parsed.text).split())
        contract = ("We always round up the hourly usage of a server.", "Primary IPv6s are free") if name == "billing" else (
            "Volumes have a monthly price cap and are billed hourly.",)
        require(all(text.count(phrase) == 1 for phrase in contract), "hetzner_billing_contract_changed")
        proofs[name] = {key: fetched[key] for key in ("source", "body_sha256")}
    return proofs


def number(value, upper=1000000):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,12})?", value), "invalid_hetzner_unit_price")
    result = Decimal(value)
    require(0 <= result <= upper, "invalid_hetzner_unit_price")
    return result


def gross(value, vat):
    require(isinstance(value, dict), "invalid_hetzner_unit_price")
    net, published = number(value.get("net")), number(value.get("gross"))
    require(published >= net, "invalid_hetzner_unit_price")
    # Published gross prices may be rounded. Never lower the tax-inclusive bound.
    return max(published, net * (1 + vat / 100))


def rows(value, key):
    require(isinstance(value, dict) and isinstance(value.get(key), list) and all(isinstance(row, dict) for row in value[key]),
            "invalid_hetzner_price_response")
    return value[key]


def selected(values, key, wanted):
    require(isinstance(values, list) and all(isinstance(row, dict) for row in values), "invalid_hetzner_price_response")
    matches = [row for row in values if row.get(key) == wanted]
    require(len(matches) == 1, "hetzner_price_selection_missing_or_ambiguous")
    return matches[0]


def hourly(row, vat):
    result = max(gross(row.get("price_hourly"), vat), gross(row.get("price_monthly"), vat) / 672)
    require(result > 0, "invalid_hetzner_unit_price")
    return result


def planned_image(plan):
    """Require the actual resolved data value, never a caller-supplied image hint."""
    image = resolved_plan_data(plan, "module.hetzner[0]", "hcloud_image", IMAGE_ADDRESS, "hetzner_resolved_image_plan_required")
    identifier = image.get("id")
    require(type(identifier) is int and 0 < identifier < 2 ** 63 and image.get("name") == "ubuntu-22.04"
            and image.get("type") == "system" and image.get("architecture") == "x86"
            and image.get("os_flavor") == "ubuntu" and image.get("os_version") == "22.04"
            and image.get("with_architecture") == "x86" and image.get("with_status") == ["available"]
            and image.get("most_recent") is False and image.get("include_deprecated") is True
            and all(image.get(key) is None for key in ("selector", "with_selector")), "hetzner_resolved_image_plan_required")
    return identifier


def plan_selection(plan, cloud, prefix, run):
    validate_fresh_plan(plan, "hetzner", prefix, run)
    image_id = planned_image(plan)
    changes = {}
    for item in plan["resource_changes"]:
        if item.get("mode") == "data" or item["type"] in ("random_password", "terraform_data", "local_file"):
            continue
        require(item["type"] not in changes, "hetzner_price_plan_shape_unsupported")
        changes[item["type"]] = item["change"]
    require(set(changes) == RESOURCE_TYPES["hetzner"], "hetzner_price_plan_shape_unsupported")
    for change in changes.values():
        unknown = change.get("after_unknown", {})
        require(isinstance(unknown, dict) and not any(unknown.get(key) for key in (
            "size", "location", "server_type", "image", "backups", "iso", "rescue", "placement_group_id", "keep_disk")),
            "hetzner_price_plan_values_unknown")
    vm, volume = changes["hcloud_server"]["after"], changes["hcloud_volume"]["after"]
    require(vm.get("server_type") == cloud["size"] and vm.get("location") == cloud["region"] and vm.get("image") == str(image_id)
            and volume.get("location") == cloud["region"], "hetzner_price_plan_scope_changed")
    require((vm.get("backups") is None or vm.get("backups") is False)
            and (vm.get("keep_disk") is None or vm.get("keep_disk") is False)
            and vm.get("public_net") == [] and vm.get("network") == []
            and all(vm.get(key) is None for key in ("iso", "rescue", "placement_group_id", "datacenter")),
            "hetzner_price_extra_vm_features_unpriced")
    # The actual provider plan leaves automatic Primary IP creation implicit.
    # Price one of each, but keep that default a pending launch requirement.
    unknown = changes["hcloud_server"].get("after_unknown", {})
    require(unknown.get("public_net", []) == [] and unknown.get("network", []) == [], "hetzner_price_plan_values_unknown")
    amount = volume.get("size")
    require(type(amount) is int and 10 <= amount <= 10240 and volume.get("server_id") is None, "hetzner_price_plan_disk_changed")
    return amount, image_id


def hetzner_rates(plan, cloud, prefix, run, adapter, source_revision, plan_sha256, public=None, now=time.time):
    amount, image_id = plan_selection(plan, cloud, prefix, run)
    require(adapter.cloud == cloud and isinstance(source_revision, str) and re.fullmatch(r"[a-f0-9]{40}", source_revision)
            and isinstance(plan_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", plan_sha256), "price_measurement_scope_changed")
    began = now()
    require(type(began) in (int, float) and math.isfinite(began) and 0 <= began <= 253402286400, "invalid_price_measurement_clock")
    size = selected(adapter.pages("/server_types", "server_types", "hetzner"), "name", cloud["size"])
    require(type(size.get("id")) is int and size["id"] > 0 and size.get("architecture") == "x86"
            and type(size.get("memory")) in (int, float) and math.isfinite(size["memory"]) and 8 <= size["memory"] <= 1000000
            and type(size.get("disk")) in (int, float) and math.isfinite(size["disk"]) and 20 <= size["disk"] <= 1000000,
            "hetzner_price_vm_size_unavailable")
    try:
        hetzner_location(size, cloud["region"], began)
    except PreflightError as error:
        raise PolicyError(str(error)) from None
    reply = adapter.get("/images/" + str(image_id))
    image = reply.get("image") if isinstance(reply, dict) else None
    require(isinstance(image, dict) and type(image.get("id")) is int and image["id"] == image_id
            and image.get("name") == "ubuntu-22.04" and image.get("type") == "system" and image.get("status") == "available"
            and image.get("architecture") == "x86" and image.get("os_flavor") == "ubuntu" and image.get("os_version") == "22.04"
            and "deprecation" in image, "hetzner_price_ubuntu_image_required")
    value = adapter.get("/pricing")
    require(isinstance(value, dict) and isinstance(value.get("pricing"), dict), "invalid_hetzner_price_response")
    pricing = value["pricing"]
    require(pricing.get("currency") in ("EUR", "USD"), "hetzner_price_currency_unsupported")
    vat = number(pricing.get("vat_rate"), 100)
    vm_prices = selected(rows(pricing, "server_types"), "name", cloud["size"])
    require(type(vm_prices.get("id")) is int and vm_prices["id"] == size["id"], "hetzner_price_server_type_changed")
    vm = selected(rows(vm_prices, "prices"), "location", cloud["region"])
    vm_rate = hourly(vm, vat)
    traffic = gross(vm.get("price_per_tb_traffic"), vat)
    require(traffic > 0 and type(vm.get("included_traffic")) is int and vm["included_traffic"] >= 0, "invalid_hetzner_unit_price")
    ips = rows(pricing, "primary_ips")
    require(all(row.get("type") in ("ipv4", "ipv6") for row in ips), "hetzner_price_ip_type_changed")
    ipv4 = selected(rows(selected(ips, "type", "ipv4"), "prices"), "location", cloud["region"])
    ip_rate = hourly(ipv4, vat)
    ipv6s = [row for row in ips if row.get("type") == "ipv6"]
    require(len(ipv6s) <= 1, "hetzner_price_selection_missing_or_ambiguous")
    if ipv6s:
        ip6 = selected(rows(ipv6s[0], "prices"), "location", cloud["region"])
        require(gross(ip6.get("price_hourly"), vat) == 0 and gross(ip6.get("price_monthly"), vat) == 0, "hetzner_ipv6_free_contract_changed")
    volume = pricing.get("volume")
    volume_rate = gross(volume.get("price_per_gb_month") if isinstance(volume, dict) else None, vat) / 672
    require(volume_rate > 0, "invalid_hetzner_unit_price")
    proofs = terms(HetznerTerms() if public is None else public)
    finished = now()
    require(type(finished) in (int, float) and math.isfinite(finished) and began <= finished <= began + 300, "price_measurement_expired")
    try:
        location = hetzner_location(size, cloud["region"], finished)
        hetzner_deprecation(image["deprecation"], finished)
    except PreflightError as error:
        raise PolicyError(str(error)) from None
    pending = ["model_execution_cost_bound", "egress_quantity_and_tb_billing_unit_bound", "cleanup_deadline_enforcement",
               "account_project_token_mapping", "tax_and_account_adjustments",
               "automatic_primary_ip_creation_defaults", "remaining_resource_charges"]
    if pricing["currency"] == "EUR":
        pending.append("eur_to_usd_spend_bound_and_conversion_fees")
    return {"schema": 1, "provider": "hetzner", "status": "resource_unit_prices_measured", "currency": pricing["currency"], "run_id": prefix,
        "source_revision": source_revision, "plan_sha256": plan_sha256, "verified_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
        "selection": {key: cloud[key] for key in ("account_id", "project_id", "region", "size")} | {"image_id": image["id"], "data_volume_gb": amount},
        "vat_percent": format(vat, "f"), "price_policy": "gross_or_net_plus_vat_whichever_higher_no_allowances",
        "vm_and_boot_hourly_upper_bound": dollars(vm_rate), "data_volume_gb_hourly_upper_bound": dollars(volume_rate),
        "primary_ipv4_count_priced": 1, "primary_ipv6_count_priced": 1, "primary_ipv4_hourly_upper_bound": dollars(ip_rate),
        "primary_ipv6_hourly_upper_bound": "0.000000000", "server_billing_increment_seconds": 3600,
        "vm_disk_ips_hourly_upper_bound": dollars(vm_rate + amount * volume_rate + ip_rate), "outbound_tb_upper_bound": dollars(traffic),
        "sources": proofs | {"api": {"source": BASE + "/pricing", "pricing_sha256": digest(pricing),
            "server_type_sha256": digest(size), "selected_location_sha256": digest(location), "image_sha256": digest(image)}},
        "launch_gate": "unverified", "complete_quote": False, "not_verified": pending}
