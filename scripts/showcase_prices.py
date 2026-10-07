#!/usr/bin/env python3
"""Read selected resource unit prices; never manufacture a complete quote.

The DigitalOcean component supports bundled CPU Droplets in the checked plan.
Model work, traffic quantity, account adjustments and cleanup enforcement still
need measured bounds before the production controller can pass its price gate.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
from html.parser import HTMLParser
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from showcase_policy import PolicyError, require, validate_fresh_plan, digitalocean_showcase_cidr

SOURCES = {"volumes": "https://www.digitalocean.com/pricing/volumes",
           "droplets": "https://docs.digitalocean.com/products/droplets/details/pricing/"}
MAX_RESPONSE = 2 * 1024 * 1024
DO_TYPES = {"digitalocean_ssh_key", "digitalocean_vpc", "digitalocean_volume", "digitalocean_droplet",
            "digitalocean_volume_attachment", "digitalocean_firewall", "digitalocean_project_resources"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise PolicyError("price_redirect_refused")


class PublicPricing:
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


class PriceHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.text, self.table, self.row, self.cell = [], [], None, None, None
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag == "table":
            require(self.table is None, "price_table_unsupported")
            self.table = []
        elif tag == "tr" and self.table is not None:
            require(self.row is None, "price_table_unsupported")
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            require(self.cell is None, "price_table_unsupported")
            self.cell = []

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            require(self.cell is None, "price_table_unsupported")
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            require(self.row is None, "price_table_unsupported")
            self.tables.append(self.table)
            self.table = None

    def handle_data(self, data):
        if self.ignored:
            return
        self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    @classmethod
    def parse(cls, document):
        require(isinstance(document, str) and 0 < len(document.encode()) <= MAX_RESPONSE, "invalid_price_document")
        result = cls()
        try:
            result.feed(document)
            result.close()
        except (ValueError, TypeError):
            raise PolicyError("invalid_price_document") from None
        require(not result.ignored and result.table is None and result.row is None and result.cell is None, "price_table_unsupported")
        return result


def decimal_price(value):
    require(type(value) in (str, int, float, Decimal), "invalid_unit_price")
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise PolicyError("invalid_unit_price") from None
    require(price.is_finite() and 0 < price <= 1000000, "invalid_unit_price")
    return price


def dollars(value):
    return format(value.quantize(Decimal(".000000001"), rounding=ROUND_CEILING), "f")


def volume_rate(document):
    parsed = PriceHTML.parse(document)
    candidates = [table for table in parsed.tables if table and table[0][:3] == ["Storage", "$/hour", "$/month"]]
    require(len(candidates) == 1 and len(candidates[0]) >= 2, "volume_price_table_missing_or_ambiguous")
    rates, quantities = [], set()
    for row in candidates[0][1:]:
        require(len(row) >= 3, "volume_price_table_unsupported")
        quantity = re.fullmatch(r"([0-9][0-9,]*) GiB", row[0])
        hourly = re.fullmatch(r"\$\s*([0-9]+(?:\.[0-9]+)?)", row[1])
        monthly = re.fullmatch(r"\$\s*([0-9]+(?:\.[0-9]+)?)", row[2])
        require(quantity and hourly and monthly, "volume_price_table_unsupported")
        amount = int(quantity[1].replace(",", ""))
        require(1 <= amount <= 16384 and amount not in quantities, "volume_price_table_unsupported")
        quantities.add(amount)
        # Published rounded hourly rows may be lower than monthly/672. Use the
        # highest per-unit value, round upward, and never rely on monthly caps.
        rates.extend((decimal_price(hourly[1]) / amount, decimal_price(monthly[1]) / amount / 672))
    return max(rates).quantize(Decimal(".000000001"), rounding=ROUND_CEILING)


def droplet_terms(document):
    parsed = PriceHTML.parse(document)
    value = " ".join(" ".join(parsed.text).split())
    bandwidth = re.findall(r"Additional outbound transfer is billed at \$\s*([0-9]+(?:\.[0-9]+)?) per GiB\.", value)
    require(len(bandwidth) == 1, "egress_price_missing_or_ambiguous")
    # Fail if the pricing page changes its billing contract. A VM's monthly
    # transfer pool/cap cannot be treated as this fresh attempt's free allowance.
    require("Bundled-plan CPU Droplets are billed per second" in value
            and "minimum charge of 60 seconds or $0.01, whichever is higher" in value
            and "Usage is capped at 672 hours (28 days) per month" in value,
            "droplet_billing_contract_unsupported")
    return decimal_price(bandwidth[0])


def do_plan(plan, cloud, prefix, run):
    validate_fresh_plan(plan, "digitalocean", prefix, run)
    rows = {}
    for item in plan["resource_changes"]:
        if item.get("mode") == "data" or item["type"] in ("random_password", "terraform_data", "local_file"):
            continue
        require(item["type"] not in rows, "price_plan_shape_unsupported")
        unknown = item["change"].get("after_unknown", {})
        require(isinstance(unknown, dict) and not any(unknown.get(key) for key in ("size", "region", "image", "backups", "project", "ip_range")),
                "price_plan_values_unknown")
        rows[item["type"]] = item["change"]["after"]
    require(set(rows) == DO_TYPES, "price_plan_shape_unsupported")
    vm, volume = rows["digitalocean_droplet"], rows["digitalocean_volume"]
    require(vm.get("size") == cloud["size"] and vm.get("region") == cloud["region"]
            and vm.get("image") == "ubuntu-22-04-x64" and vm.get("backups") is False,
            "price_plan_vm_scope_changed")
    require(re.fullmatch(r"(?:s|c|g|m|so)-[0-9][A-Za-z0-9-]*", cloud["size"]), "bundled_cpu_size_required")
    amount = volume.get("size")
    require(type(amount) is int and 1 <= amount <= 16384 and volume.get("region") == cloud["region"], "price_plan_disk_scope_changed")
    require(rows["digitalocean_project_resources"].get("project") == cloud["project_id"], "price_plan_project_scope_changed")
    require(rows["digitalocean_vpc"].get("region") == cloud["region"], "price_plan_vm_scope_changed")
    require(rows["digitalocean_vpc"].get("ip_range") == digitalocean_showcase_cidr(prefix), "price_plan_network_scope_changed")
    return amount


def digitalocean_rates(plan, cloud, prefix, run, adapter, source_revision, plan_sha256, public=None, now=time.time):
    amount = do_plan(plan, cloud, prefix, run)
    require(adapter.cloud == cloud and isinstance(source_revision, str) and re.fullmatch(r"[a-f0-9]{40}", source_revision)
            and isinstance(plan_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", plan_sha256), "price_measurement_scope_changed")
    began = now()
    require(type(began) in (int, float) and math.isfinite(began) and 0 <= began <= 253402286400, "invalid_price_measurement_clock")
    sizes = adapter.pages("/sizes", "sizes", "digitalocean")
    matches = [row for row in sizes if row.get("slug") == cloud["size"]]
    require(len(matches) == 1, "price_vm_size_unavailable")
    size = matches[0]
    require(size.get("available") is True and isinstance(size.get("regions"), list) and cloud["region"] in size["regions"]
            and type(size.get("memory")) is int and size["memory"] >= 8192 and type(size.get("disk")) is int and size["disk"] >= 20
            and not size.get("gpu_info"), "price_vm_size_unavailable")
    vm = max(decimal_price(size.get("price_hourly")), decimal_price(size.get("price_monthly")) / 672)
    vm = vm.quantize(Decimal(".000001"), rounding=ROUND_CEILING)
    public = PublicPricing() if public is None else public
    sources, rates = {}, {}
    for name in SOURCES:
        fetched = public.fetch(name)
        require(isinstance(fetched, dict) and fetched.get("source") == SOURCES[name]
                and re.fullmatch(r"[a-f0-9]{64}", fetched.get("body_sha256", ""))
                and isinstance(fetched.get("document"), str)
                and hashlib.sha256(fetched["document"].encode()).hexdigest() == fetched["body_sha256"], "price_source_changed")
        rates[name] = volume_rate(fetched["document"]) if name == "volumes" else droplet_terms(fetched["document"])
        sources[name] = {key: fetched[key] for key in ("source", "body_sha256")}
    finished = now()
    require(type(finished) in (int, float) and math.isfinite(finished) and began <= finished <= began + 300, "price_measurement_expired")
    return {"schema": 1, "provider": "digitalocean", "status": "resource_unit_prices_measured", "currency": "USD",
            "run_id": prefix, "source_revision": source_revision, "plan_sha256": plan_sha256,
            "verified_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
            "selection": {key: cloud[key] for key in ("account_id", "project_id", "region", "size")},
            "vm_and_boot_hourly_usd": dollars(vm), "data_volume_gib": amount,
            "data_volume_gib_hourly_usd": dollars(rates["volumes"]),
            "vm_and_disk_hourly_usd": dollars(vm + amount * rates["volumes"]),
            "outbound_gib_usd": dollars(rates["droplets"]), "vm_minimum_usd": "0.01",
            "sources": {"sizes": {"source": "https://api.digitalocean.com/v2/sizes",
                "selected_row_sha256": hashlib.sha256(json.dumps(size, sort_keys=True, allow_nan=False).encode()).hexdigest()}, **sources},
            "launch_gate": "unverified", "complete_quote": False,
            "not_verified": ["model_execution_cost_bound", "egress_volume_bound", "cleanup_deadline_enforcement",
                             "tax_and_account_adjustments", "remaining_resource_charges"]}
