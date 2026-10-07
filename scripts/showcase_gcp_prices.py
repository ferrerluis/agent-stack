#!/usr/bin/env python3
"""Read plan-bound GCP E2 unit rates; never approve a complete launch quote."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, ProxyHandler

from showcase_policy import PolicyError, RESOURCE_TYPES, require, text, validate_fresh_plan, resolved_plan_data
from showcase_prices import Decimal, NoRedirect, dollars

SERVICE = "services/6F81-5844-456A"
ENDPOINT = "https://cloudbilling.googleapis.com/v1/" + SERVICE + "/skus"
LATEST_ENDPOINT = "https://cloudbilling.googleapis.com/v2beta/skus/"
DEFAULT_CONSUMPTION_MODEL = "consumptionModels/7754-699E-0EBF"
IMAGE_ENDPOINT = "https://compute.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/"
IMAGE_LINK = "https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/"
IMAGE_ADDRESS = "module.gcp[0].data.google_compute_image.boot[0]"
LICENSE = "https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/licenses/ubuntu-2204-lts"
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 200
MAX_TOTAL_BYTES = 128 * 1024 * 1024
PAGE_SIZE = 500


def digest(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        raise PolicyError("invalid_gcp_price_response") from None


class CatalogHTTP:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def page(self, token, page_token=None):
        require(text(token) and (page_token is None or text(page_token) and len(page_token) <= 8192), "invalid_gcp_price_request")
        params = {"currencyCode": "USD", "pageSize": PAGE_SIZE}
        if page_token is not None:
            params["pageToken"] = page_token
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value
        try:
            request = Request(ENDPOINT + "?" + urlencode(params), method="GET", headers={
                "Accept": "application/json", "Authorization": "Bearer " + token,
                "User-Agent": "AgentStack-showcase-pricing/1"})
            with self.opener.open(request, timeout=20) as response:
                require(response.status == 200, "gcp_price_request_failed")
                raw = response.read(MAX_PAGE_BYTES + 1)
            require(len(raw) <= MAX_PAGE_BYTES, "gcp_price_response_too_large")
            value = json.loads(raw, object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")))
            require(isinstance(value, dict), "invalid_gcp_price_response")
        except HTTPError as error:
            error.close()
            raise PolicyError("gcp_price_request_failed") from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise PolicyError("gcp_price_request_failed") from None
        return value, len(raw), hashlib.sha256(raw).hexdigest()

    def latest(self, token, sku):
        require(text(token) and isinstance(sku, str)
                and re.fullmatch(r"[A-F0-9]{4}(?:-[A-F0-9]{4}){2}", sku), "invalid_gcp_price_request")
        endpoint = LATEST_ENDPOINT + sku + "/price?currencyCode=USD"
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value
        try:
            request = Request(endpoint, method="GET", headers={"Accept": "application/json",
                "Authorization": "Bearer " + token, "Cache-Control": "no-cache",
                "User-Agent": "AgentStack-showcase-pricing/1"})
            with self.opener.open(request, timeout=20) as response:
                require(response.status == 200, "gcp_price_request_failed")
                raw = response.read(MAX_PAGE_BYTES + 1)
                date = response.headers.get("Date")
            require(len(raw) <= MAX_PAGE_BYTES, "gcp_price_response_too_large")
            value = json.loads(raw, object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")))
            require(isinstance(value, dict) and isinstance(date, str), "invalid_gcp_latest_price_response")
        except HTTPError as error:
            error.close()
            raise PolicyError("gcp_price_request_failed") from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise PolicyError("gcp_price_request_failed") from None
        return {"value": value, "date": date, "body_sha256": hashlib.sha256(raw).hexdigest()}


def catalog_kind(row, region):
    description, category, regions = row.get("description"), row.get("category"), row.get("serviceRegions")
    require(isinstance(description, str) and len(description) <= 256 and isinstance(category, dict)
            and isinstance(regions, list) and all(text(item) for item in regions), "invalid_gcp_price_response")
    patterns = {"cpu": r"E2 Instance Core running in .+", "ram": r"E2 Instance Ram running in .+",
                "disk": r"Balanced PD Capacity(?: in .+)?", "ipv4": r"External IP Charge on a Standard VM"}
    matches = [kind for kind, pattern in patterns.items() if re.fullmatch(pattern, description)]
    if not matches:
        return None
    kind = matches[0]
    geo = row.get("geoTaxonomy", {})
    require(isinstance(geo, dict) and isinstance(geo.get("regions", []), list)
            and all(text(item) for item in geo.get("regions", [])), "invalid_gcp_price_response")
    if kind == "ipv4":
        eligible = regions == ["global"] and geo.get("type") == "GLOBAL" and geo.get("regions", []) == []
    else:
        eligible = region in regions and geo.get("type") in ("REGIONAL", "MULTI_REGIONAL") and region in geo.get("regions", [])
    if not eligible:
        return None
    require(category.get("serviceDisplayName") == "Compute Engine" and category.get("usageType") == "OnDemand"
            and category.get("resourceFamily") == {"cpu": "Compute", "ram": "Compute", "disk": "Storage", "ipv4": "Network"}[kind]
            and text(category.get("resourceGroup")), "gcp_price_product_scope_changed")
    return kind


def money(value):
    require(isinstance(value, dict) and value.get("currencyCode") == "USD", "gcp_usd_price_required")
    units, nanos = value.get("units", "0"), value.get("nanos", 0)
    require(isinstance(units, str) and re.fullmatch(r"[0-9]{1,7}", units)
            and type(nanos) is int and 0 <= nanos < 1000000000, "invalid_gcp_unit_price")
    price = Decimal(units) + Decimal(nanos) / 1000000000
    require(0 <= price <= 1000000, "invalid_gcp_unit_price")
    return price


def latest_rate(response, sku, unit, now):
    require(isinstance(response, dict) and isinstance(response.get("body_sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", response["body_sha256"]), "invalid_gcp_latest_price_response")
    try:
        date = parsedate_to_datetime(response["date"])
        require(date.tzinfo is not None and now - 300 <= date.timestamp() <= now + 60,
                "gcp_latest_price_required")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PolicyError("gcp_latest_price_required") from None
    value = response.get("value")
    require(isinstance(value, dict) and value.get("name") == "skus/" + sku + "/price"
            and value.get("currencyCode") == "USD", "gcp_price_product_scope_changed")
    models = value.get("skuPrices")
    require(isinstance(models, list) and 0 < len(models) <= 20 and all(isinstance(item, dict) for item in models),
            "invalid_gcp_latest_price_response")
    selected = [item for item in models if item.get("consumptionModel") == DEFAULT_CONSUMPTION_MODEL
                or item.get("consumptionModelDescription") == "Default"]
    require(len(selected) == 1 and selected[0].get("consumptionModel") == DEFAULT_CONSUMPTION_MODEL
            and selected[0].get("consumptionModelDescription") == "Default" and selected[0].get("valueType") == "rate",
            "gcp_default_consumption_price_required")
    rate = selected[0].get("rate")
    require(isinstance(rate, dict) and isinstance(rate.get("unitInfo"), dict)
            and rate["unitInfo"].get("unit") == unit, "gcp_price_unit_changed")
    def decimal_value(item):
        require(isinstance(item, dict) and isinstance(item.get("value"), str)
                and re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,18})?", item["value"]), "gcp_price_unit_changed")
        return Decimal(item["value"])
    require(decimal_value(rate["unitInfo"].get("unitQuantity")) == 1, "gcp_price_unit_changed")
    tiers = rate.get("tiers")
    require(isinstance(tiers, list) and 0 < len(tiers) <= 20 and all(isinstance(item, dict) for item in tiers),
            "gcp_price_tiers_unsupported")
    starts = [decimal_value(item.get("startAmount")) for item in tiers]
    rates = [money(item.get("listPrice")) for item in tiers]
    require(starts[0] == 0 and all(start <= 10 ** 15 for start in starts)
            and all(a < b for a, b in zip(starts, starts[1:])) and max(rates) > 0, "gcp_price_tiers_unsupported")
    return max(rates), {"endpoint": LATEST_ENDPOINT + sku + "/price", "response_date": response["date"],
        "body_sha256": response["body_sha256"], "consumption_model": DEFAULT_CONSUMPTION_MODEL,
        "rate_policy": "highest_default_consumption_tier"}


def unit_rate(row, kind, now, latest=None):
    sku = row.get("skuId")
    require(isinstance(sku, str) and re.fullmatch(r"[A-F0-9]{4}(?:-[A-F0-9]{4}){2}", sku)
            and row.get("name") == SERVICE + "/skus/" + sku and row.get("serviceProviderName") == "Google", "gcp_price_product_scope_changed")
    timeline = row.get("pricingInfo")
    require(isinstance(timeline, list) and len(timeline) == 1 and isinstance(timeline[0], dict), "gcp_latest_price_required")
    info = timeline[0]
    try:
        effective = datetime.fromisoformat(info["effectiveTime"].replace("Z", "+00:00"))
        require(effective.tzinfo is not None and effective.timestamp() <= now
                and (latest is not None or now - 43200 <= effective.timestamp()), "gcp_latest_price_required")
    except (KeyError, AttributeError, ValueError, OverflowError):
        raise PolicyError("gcp_latest_price_required") from None
    require(type(info.get("currencyConversionRate")) in (int, float) and info["currencyConversionRate"] == 1,
            "gcp_usd_price_required")
    expression = info.get("pricingExpression")
    unit, base, factor = {"cpu": ("h", "s", 3600), "ram": ("GiBy.h", "By.s", 2 ** 30 * 3600),
                         "disk": ("GiBy.mo", "By.s", None), "ipv4": ("h", "s", 3600)}[kind]
    require(isinstance(expression, dict) and expression.get("usageUnit") == unit and expression.get("baseUnit") == base,
            "gcp_price_unit_changed")
    conversion = expression.get("baseUnitConversionFactor")
    require(type(conversion) in (int, float) and math.isfinite(conversion)
            and (conversion == factor if factor is not None else 2 ** 30 * 672 * 3600 <= conversion <= 2 ** 30 * 744 * 3600),
            "gcp_price_unit_changed")
    tiers = expression.get("tieredRates")
    require(isinstance(tiers, list) and 0 < len(tiers) <= 20, "gcp_price_tiers_unsupported")
    starts, rates = [], []
    for tier in tiers:
        require(isinstance(tier, dict), "gcp_price_tiers_unsupported")
        start = tier.get("startUsageAmount", 0)
        require(type(start) in (int, float) and math.isfinite(start) and 0 <= start <= 10 ** 15, "gcp_price_tiers_unsupported")
        starts.append(start); rates.append(money(tier.get("unitPrice")))
    require(starts[0] == 0 and all(a < b for a, b in zip(starts, starts[1:])) and max(rates) > 0, "gcp_price_tiers_unsupported")
    # Charge every unit at the highest published tier. Never assume access to
    # the billing account's shared free allowance or discounts.
    upper = max(rates)
    proof = {"sku_id": sku, "unit": unit, "effective_time": info["effectiveTime"],
             "sku_sha256": digest(row), "rate_policy": "highest_published_tier"}
    if latest is not None:
        current, proof["latest_price"] = latest_rate(latest, sku, unit, now)
        upper = max(upper, current)
        proof["rate_policy"] = "highest_catalog_or_latest_default_tier"
    return upper, proof


class GCPCatalog:
    def __init__(self, adapter, http=None, now=time.time):
        self.adapter, self.http, self.now = adapter, CatalogHTTP() if http is None else http, now

    def selected(self, region):
        began = self.now()
        require(type(began) in (int, float) and math.isfinite(began), "invalid_price_measurement_clock")
        token, seen_tokens, seen_skus, candidates, hashes, total = None, set(), set(), {}, [], 0
        for _ in range(MAX_PAGES):
            require(began <= self.now() <= began + 300, "price_measurement_expired")
            value, size, body_hash = self.http.page(self.adapter.access_token(), token)
            require(type(size) is int and 0 < size <= MAX_PAGE_BYTES and isinstance(body_hash, str)
                    and re.fullmatch(r"[a-f0-9]{64}", body_hash), "invalid_gcp_price_response")
            total += size
            require(total <= MAX_TOTAL_BYTES, "gcp_price_response_too_large")
            hashes.append(body_hash)
            rows = value.get("skus") if isinstance(value, dict) else None
            require(isinstance(rows, list) and len(rows) <= PAGE_SIZE and all(isinstance(row, dict) for row in rows), "invalid_gcp_price_response")
            for row in rows:
                sku = row.get("skuId")
                require(isinstance(sku, str) and re.fullmatch(r"[A-F0-9]{4}(?:-[A-F0-9]{4}){2}", sku)
                        and row.get("name") == SERVICE + "/skus/" + sku and sku not in seen_skus, "gcp_price_product_duplicate_or_changed")
                seen_skus.add(sku)
                kind = catalog_kind(row, region)
                if kind is not None:
                    require(kind not in candidates, "gcp_price_product_ambiguous")
                    candidates[kind] = row
            token = value.get("nextPageToken", "")
            if token == "":
                require(set(candidates) == {"cpu", "ram", "disk", "ipv4"}, "gcp_price_product_missing")
                finished = self.now()
                require(type(finished) in (int, float) and math.isfinite(finished) and began <= finished <= began + 300, "price_measurement_expired")
                rates, proofs = {}, {}
                for kind, row in candidates.items():
                    latest = self.http.latest(self.adapter.access_token(), row["skuId"])
                    rates[kind], proofs[kind] = unit_rate(row, kind, finished, latest)
                require(began <= self.now() <= began + 300, "price_measurement_expired")
                return rates, {"endpoint": ENDPOINT, "page_count": len(hashes), "page_hashes_sha256": digest(hashes), "products": proofs}
            require(text(token) and len(token) <= 8192 and token not in seen_tokens, "invalid_gcp_price_pagination")
            seen_tokens.add(token)
        raise PolicyError("gcp_price_pagination_limit")


def planned_image(plan):
    """Require the actual resolved data value, never a caller-supplied image hint."""
    value = resolved_plan_data(plan, "module.gcp[0]", "google_compute_image", IMAGE_ADDRESS, "gcp_resolved_image_plan_required")
    name, identifier = value.get("name"), value.get("image_id")
    require(isinstance(name, str) and re.fullmatch(r"ubuntu-2204-jammy-v[0-9]+", name)
            and isinstance(identifier, str) and re.fullmatch(r"[1-9][0-9]{0,19}", identifier) and int(identifier) < 2 ** 64
            and value.get("project") == "ubuntu-os-cloud" and value.get("family") == "ubuntu-2204-lts"
            and value.get("status") == "READY" and value.get("licenses") == [LICENSE]
            and value.get("self_link") == IMAGE_LINK + name, "gcp_resolved_image_plan_required")
    return {"image": name, "image_id": identifier, "image_self_link": value["self_link"]}


def known_shape(value):
    """A Terraform unknown mask may contain only false leaves here."""
    if value is False:
        return True
    if isinstance(value, dict):
        return all(known_shape(item) for item in value.values())
    if isinstance(value, list):
        return all(known_shape(item) for item in value)
    return False


def standard_vm(vm, unknown):
    """Price only explicit standard VM settings, never unknown provider defaults."""
    code = "gcp_price_plan_extra_vm_features_unpriced"
    blocks = {
        "scheduling": ({"provisioning_model": "STANDARD", "preemptible": False,
                        "automatic_restart": True, "on_host_maintenance": "MIGRATE"},
                       {"availability_domain", "host_error_timeout_seconds", "instance_termination_action",
                        "min_node_cpus", "termination_time", "local_ssd_recovery_timeout", "max_run_duration",
                        "node_affinities", "on_instance_stop_action"}),
        "reservation_affinity": ({"type": "NO_RESERVATION"}, {"specific_reservation"}),
        "confidential_instance_config": ({"enable_confidential_compute": False}, {"confidential_instance_type"}),
    }
    for key, (required, empty) in blocks.items():
        value = vm.get(key)
        require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict), code)
        value = value[0]
        require(set(value) <= set(required) | empty
                and all(type(value.get(field)) is type(expected) and value[field] == expected for field, expected in required.items())
                and all(value.get(field) is None or value.get(field) == [] for field in empty)
                and known_shape(unknown.get(key, {})), code)
    accelerators = vm.get("guest_accelerator")
    require(accelerators == [] or isinstance(accelerators, list) and len(accelerators) == 1
            and isinstance(accelerators[0], dict) and set(accelerators[0]) == {"count", "type"}
            and type(accelerators[0]["count"]) is int and accelerators[0]["count"] == 0
            and accelerators[0]["type"] == "", code)
    for key in ("guest_accelerator", "scratch_disk", "advanced_machine_features"):
        require((key == "guest_accelerator" or vm.get(key) == []) and known_shape(unknown.get(key, {})), code)


def plan_selection(plan, cloud, prefix, run):
    validate_fresh_plan(plan, "gcp", prefix, run)
    image = planned_image(plan)
    rows = {}
    for resource in plan["resource_changes"]:
        if resource.get("mode") == "data" or resource["type"] in ("random_password", "terraform_data", "local_file"):
            continue
        rows.setdefault(resource["type"], []).append(resource["change"])
    require(set(rows) == RESOURCE_TYPES["gcp"] and all(len(items) == (2 if kind == "google_compute_firewall" else 1)
            for kind, items in rows.items()), "gcp_price_plan_shape_unsupported")
    for items in rows.values():
        for item in items:
            unknown = item.get("after_unknown", {})
            require(isinstance(unknown, dict) and not any(unknown.get(key) for key in ("project", "zone", "region", "machine_type", "size", "type")),
                    "gcp_price_plan_values_unknown")
            require(item["after"].get("project") == cloud["project_id"], "gcp_price_plan_project_changed")
    vm, disk = rows["google_compute_instance"][0]["after"], rows["google_compute_disk"][0]["after"]
    family = re.fullmatch(r"e2-(standard|highmem|highcpu)-([0-9]+)", cloud["size"])
    require(family is not None and vm.get("machine_type") == cloud["size"] and vm.get("zone") == cloud["zone"]
            and disk.get("zone") == cloud["zone"] and re.fullmatch(re.escape(cloud["region"]) + r"-[a-z]", cloud["zone"])
            and rows["google_compute_subnetwork"][0]["after"].get("region") == cloud["region"], "gcp_e2_plan_scope_required")
    vm_unknown = rows["google_compute_instance"][0].get("after_unknown", {})
    standard_vm(vm, vm_unknown)
    roots, networks = vm.get("boot_disk"), vm.get("network_interface")
    require(isinstance(roots, list) and len(roots) == 1 and isinstance(roots[0], dict)
            and isinstance(networks, list) and len(networks) == 1 and isinstance(networks[0], dict), "gcp_price_plan_shape_unsupported")
    root = roots[0]; params = root.get("initialize_params")
    require(isinstance(params, list) and len(params) == 1 and isinstance(params[0], dict), "gcp_price_plan_disk_changed")
    boot = params[0]
    require(boot.get("image") == image["image_self_link"] and boot.get("type") == "pd-balanced"
            and type(boot.get("size")) is int and 20 <= boot["size"] <= 65536
            and disk.get("type") == "pd-balanced" and type(disk.get("size")) is int and 10 <= disk["size"] <= 65536,
            "gcp_price_plan_disk_changed")
    require((root.get("auto_delete") is None or root.get("auto_delete") is True) and root.get("source") is None and boot.get("snapshot") is None
            and not boot.get("replica_zones") and not boot.get("storage_pool") and not boot.get("resource_policies")
            and not disk.get("snapshot") and not disk.get("image") and not disk.get("source_disk") and not disk.get("source_instant_snapshot")
            and not disk.get("resource_policies"),
            "gcp_price_plan_disk_changed")
    attached = vm.get("attached_disk")
    require(isinstance(attached, list) and len(attached) == 1 and isinstance(attached[0], dict)
            and attached[0].get("source") is None and attached[0].get("mode") == "READ_WRITE"
            and attached[0].get("device_name") == "agent-stack-data", "gcp_price_plan_disk_changed")
    network = networks[0]
    access = network.get("access_config")
    require(isinstance(access, list) and len(access) == 1 and isinstance(access[0], dict)
            and access[0].get("nat_ip") is None and access[0].get("network_tier") == "PREMIUM"
            and network.get("stack_type") == "IPV4_ONLY" and network.get("subnetwork") is None
            and network.get("ipv6_access_config") == [], "gcp_price_plan_network_changed")
    network_unknown = vm_unknown.get("network_interface", [{}])
    require(isinstance(network_unknown, list) and len(network_unknown) == 1 and isinstance(network_unknown[0], dict)
            and known_shape(network_unknown[0].get("stack_type", {})), "gcp_price_plan_values_unknown")
    access_unknown = network_unknown[0].get("access_config", [{}])
    require(isinstance(access_unknown, list) and len(access_unknown) == 1 and isinstance(access_unknown[0], dict)
            and known_shape(access_unknown[0].get("network_tier", {})), "gcp_price_plan_values_unknown")
    root_unknown = vm_unknown.get("boot_disk", [{}])
    require(isinstance(root_unknown, list) and len(root_unknown) == 1 and isinstance(root_unknown[0], dict), "gcp_price_plan_values_unknown")
    boot_unknown = root_unknown[0].get("initialize_params", [{}])
    require(isinstance(boot_unknown, list) and len(boot_unknown) == 1 and isinstance(boot_unknown[0], dict)
            and not any(boot_unknown[0].get(key) for key in ("image", "size", "type")), "gcp_price_plan_values_unknown")
    return {"boot_volume_gib": boot["size"], "data_volume_gib": disk["size"], "vcpus": int(family[2]),
            "ram_gib_per_vcpu": {"standard": 4, "highmem": 8, "highcpu": 1}[family[1]], **image}


def gcp_rates(plan, cloud, prefix, run, adapter, source_revision, plan_sha256, catalog=None, now=time.time):
    selection = plan_selection(plan, cloud, prefix, run)
    require(adapter.cloud == cloud and isinstance(source_revision, str) and re.fullmatch(r"[a-f0-9]{40}", source_revision)
            and isinstance(plan_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", plan_sha256), "price_measurement_scope_changed")
    began = now()
    require(type(began) in (int, float) and math.isfinite(began) and 0 <= began <= 253402286400, "invalid_price_measurement_clock")
    machine = adapter.get("/zones/" + quote(cloud["zone"], safe="") + "/machineTypes/" + quote(cloud["size"], safe=""))
    ram = selection.pop("ram_gib_per_vcpu") * selection["vcpus"]
    from showcase_preflight import google_x86_machine
    require(machine.get("name") == cloud["size"] and google_x86_machine(machine)
            and type(machine.get("guestCpus")) is int and machine["guestCpus"] == selection["vcpus"] and 2 <= selection["vcpus"] <= 32
            and type(machine.get("memoryMb")) is int and machine["memoryMb"] == ram * 1024 and ram >= 4,
            "gcp_price_vm_size_unavailable")
    image = adapter.http.request(IMAGE_ENDPOINT + selection["image"], "Bearer " + adapter.access_token())
    require(isinstance(image, dict) and image.get("family") == "ubuntu-2204-lts" and image.get("architecture") == "X86_64"
            and image.get("status") == "READY" and image.get("licenses") == [LICENSE]
            and image.get("name") == selection["image"] and image.get("id") == selection["image_id"]
            and image.get("selfLink") == selection["image_self_link"]
            and image.get("deprecated") in (None, {}),
            "gcp_price_ubuntu_image_required")
    catalog = GCPCatalog(adapter, now=now) if catalog is None else catalog
    rates, sources = catalog.selected(cloud["region"])
    finished = now()
    require(type(finished) in (int, float) and math.isfinite(finished) and began <= finished <= began + 300, "price_measurement_expired")
    vm_rate = rates["cpu"] * selection["vcpus"] + rates["ram"] * ram
    disk_rate = rates["disk"] / 672
    return {"schema": 1, "provider": "gcp", "status": "resource_unit_prices_measured", "currency": "USD", "run_id": prefix,
        "source_revision": source_revision, "plan_sha256": plan_sha256, "verified_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
        "selection": {key: cloud[key] for key in ("account_id", "project_id", "region", "zone", "size")} | selection | {"ram_gib": ram},
        "vcpu_hourly_usd_upper_bound": dollars(rates["cpu"]), "ram_gib_hourly_usd_upper_bound": dollars(rates["ram"]),
        "vm_hourly_usd_upper_bound": dollars(vm_rate), "balanced_disk_gib_monthly_usd_upper_bound": dollars(rates["disk"]),
        "balanced_disk_gib_hourly_usd_upper_bound": dollars(disk_rate), "external_ipv4_count": 1,
        "external_ipv4_hourly_usd_upper_bound": dollars(rates["ipv4"]),
        "vm_disks_ipv4_hourly_usd_upper_bound": dollars(vm_rate + (selection["boot_volume_gib"] + selection["data_volume_gib"]) * disk_rate + rates["ipv4"]),
        "sources": sources | {"machine_sha256": digest(machine), "image_sha256": digest(image)},
        "launch_gate": "unverified", "complete_quote": False,
        "not_verified": ["model_execution_cost_bound", "egress_unit_price_and_volume_bound", "cleanup_deadline_enforcement",
                         "tax_and_account_adjustments", "disk_network_and_scheduling_defaults", "remaining_resource_charges"]}
