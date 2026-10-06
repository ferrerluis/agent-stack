#!/usr/bin/env python3
"""Bounded AWS On-Demand unit-price reads, never a complete launch quote."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from showcase_policy import PolicyError, RESOURCE_TYPES, require, text, validate_fresh_plan
from showcase_prices import Decimal, MAX_RESPONSE, NoRedirect, PriceHTML, decimal_price, dollars

ENDPOINT = "https://api.pricing.us-east-1.amazonaws.com"
MAX_PAGES = 10
MAX_BYTES = 4 * 1024 * 1024
SOURCES = {"gp3": "https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html",
           "cpu": "https://aws.amazon.com/ec2/pricing/on-demand/"}
BURST_FAMILIES = {"t2", "t3", "t3a", "t8i"}


class AWSTerms:
    """Anonymous fixed-source reads; no account credentials or redirects."""
    def __init__(self): self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def fetch(self, name):
        require(name in SOURCES, "price_source_refused")
        try:
            request = Request(SOURCES[name], method="GET", headers={"Accept": "text/html", "User-Agent": "AgentStack-public-pricing-check/1"})
            with self.opener.open(request, timeout=20) as response:
                require(response.status == 200, "price_request_failed")
                raw = response.read(MAX_RESPONSE + 1)
            require(0 < len(raw) <= MAX_RESPONSE, "price_response_too_large")
            document = raw.decode("utf-8")
        except HTTPError as error:
            error.close(); raise PolicyError("price_request_failed") from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise PolicyError("price_request_failed") from None
        return {"source": SOURCES[name], "body_sha256": hashlib.sha256(raw).hexdigest(), "document": document}


def public_terms(public, name):
    value = public.fetch(name)
    require(isinstance(value, dict) and value.get("source") == SOURCES[name] and isinstance(value.get("document"), str)
            and value.get("body_sha256") == hashlib.sha256(value["document"].encode()).hexdigest(), "price_source_changed")
    parsed = PriceHTML.parse(value["document"])
    return " ".join(" ".join(parsed.text).split()), {key: value[key] for key in ("source", "body_sha256")}


def included_gp3(document):
    iops = re.findall(r"baseline IOPS performance of ([0-9,]+) IOPS, which is included with the price of storage\.", document)
    throughput = re.findall(r"baseline throughput performance of ([0-9]+) MiB/s, which is included with the price of storage\.", document)
    require(iops == ["3,000"] and throughput == ["125"], "aws_gp3_included_performance_changed")


def cpu_unit_rate(document, family):
    require(family in BURST_FAMILIES, "aws_cpu_credit_family_unsupported")
    # AWS's pricing section groups T3a with T3; require that scope explicitly.
    if family == "t3a":
        require(document.count("T8i/T4g/T3/T3a/T2 Unlimited Mode Pricing") == 1, "aws_cpu_credit_terms_changed")
    label = "T8i" if family == "t8i" else "T2 and T3"
    matches = re.findall(r"For " + label + r" instances in Unlimited mode, CPU Credits are charged at: \$([0-9]+(?:\.[0-9]+)?) per vCPU-Hour for Linux, RHEL and SLES, and", document)
    require(len(matches) == 1 and document.count("The CPU Credit pricing is the same for all instance sizes, for On-Demand, Spot, and Reserved Instances, and across all regions.") == 1,
            "aws_cpu_credit_terms_changed")
    return decimal_price(matches[0])


def machine_selection(adapter, cloud, credit_mode):
    value = adapter.call(adapter.ec2, "describe_instance_types", InstanceTypes=[cloud["size"]])
    rows = value.get("InstanceTypes") if isinstance(value, dict) else None
    require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict), "aws_price_machine_required")
    machine = rows[0]
    require(isinstance(machine.get("VCpuInfo"), dict), "aws_price_machine_required")
    cpu = machine["VCpuInfo"].get("DefaultVCpus")
    require(machine.get("InstanceType") == cloud["size"] and type(cpu) is int and 1 <= cpu <= 256
            and isinstance(machine.get("MemoryInfo"), dict) and type(machine["MemoryInfo"].get("SizeInMiB")) is int
            and machine["MemoryInfo"]["SizeInMiB"] >= 8192 and isinstance(machine.get("ProcessorInfo"), dict)
            and machine["ProcessorInfo"].get("SupportedArchitectures") == ["x86_64"]
            and type(machine.get("BurstablePerformanceSupported")) is bool, "aws_price_machine_required")
    burst = machine["BurstablePerformanceSupported"]; family = cloud["size"].split(".", 1)[0]
    require((burst and family in BURST_FAMILIES and credit_mode in ("standard", "unlimited"))
            or (not burst and family not in BURST_FAMILIES and credit_mode == "not_applicable"), "aws_price_cpu_scope_changed")
    return cpu, machine


def price_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite value")))
    except (ValueError, TypeError, UnicodeError):
        raise PolicyError("invalid_aws_price_response") from None
    require(isinstance(value, dict), "invalid_aws_price_response")
    return value


class AWSPriceQuery:
    def __init__(self, credentials, client=None):
        require(isinstance(credentials, dict) and all(text(credentials.get(key)) for key in ("access_key", "secret_key")),
                "invalid_aws_price_credentials")
        if client is None:
            try:
                import botocore.session
                from botocore.config import Config
                from botocore.configprovider import ConstantProvider
                session = botocore.session.Session()
                session.set_config_variable("config_file", "/dev/null")
                session.set_config_variable("credentials_file", "/dev/null")
                session.get_component("config_store").set_config_provider("profile", ConstantProvider(None))
                client = session.create_client("pricing", region_name="us-east-1", endpoint_url=ENDPOINT, verify=True,
                    aws_access_key_id=credentials["access_key"], aws_secret_access_key=credentials["secret_key"],
                    aws_session_token=credentials.get("session_token"), config=Config(connect_timeout=10, read_timeout=20,
                        retries={"total_max_attempts": 1}, proxies={}))
            except ImportError:
                raise PolicyError("botocore_dependency_required") from None
            except Exception:
                raise PolicyError("aws_price_client_initialization_failed") from None
        require(client.meta.endpoint_url == ENDPOINT, "aws_price_endpoint_refused")
        self.client, self.queries = client, 0

    def products(self, service, filters):
        require(service in ("AmazonEC2", "AmazonVPC") and isinstance(filters, dict) and 0 < len(filters) <= 20
                and all(text(key) and text(value) for key, value in filters.items()), "aws_price_scope_invalid")
        request = {"ServiceCode": service, "FormatVersion": "aws_v1", "MaxResults": 100,
                   "Filters": [{"Type": "TERM_MATCH", "Field": key, "Value": value} for key, value in sorted(filters.items())]}
        rows, seen, size = [], set(), 0
        for _ in range(MAX_PAGES):
            try:
                result = self.client.get_products(**request)
            except Exception:
                raise PolicyError("aws_price_request_failed") from None
            require(isinstance(result, dict) and result.get("FormatVersion") == "aws_v1"
                    and isinstance(result.get("PriceList"), list) and len(result["PriceList"]) <= 100, "invalid_aws_price_response")
            self.queries += 1
            for raw in result["PriceList"]:
                require(isinstance(raw, str) and 0 < len(raw.encode()) <= 1024 * 1024, "aws_price_response_too_large")
                size += len(raw.encode())
                require(size <= MAX_BYTES, "aws_price_response_too_large")
                rows.append(price_json(raw))
            token = result.get("NextToken")
            if token is None:
                return rows
            require(text(token) and len(token) <= 8192 and token not in seen, "invalid_aws_price_pagination")
            seen.add(token)
            request["NextToken"] = token
        raise PolicyError("aws_price_pagination_limit")


def past_date(value, now):
    require(isinstance(value, str), "invalid_aws_price_date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None and 0 <= parsed.timestamp() <= now, "invalid_aws_price_date")
    except (ValueError, OverflowError):
        raise PolicyError("invalid_aws_price_date") from None
    return value


def flat_price(rows, service, filters, unit, now, suffix=None):
    require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict), "aws_price_product_missing_or_ambiguous")
    row = rows[0]
    product = row.get("product")
    require(row.get("serviceCode") == service and isinstance(product, dict)
            and isinstance(product.get("attributes"), dict), "aws_price_product_scope_changed")
    sku, attrs = product.get("sku"), product["attributes"]
    require(isinstance(sku, str) and re.fullmatch(r"[A-Z0-9]{16}", sku), "invalid_aws_price_product")
    for key, value in filters.items():
        actual = product.get(key) if key == "productFamily" else attrs.get(key)
        require(actual == value, "aws_price_product_scope_changed")
    require(attrs.get("servicecode") == service and attrs.get("locationType") == "AWS Region", "aws_price_product_scope_changed")
    if suffix is not None:
        require(isinstance(attrs.get("usagetype"), str) and attrs["usagetype"].endswith(suffix), "aws_price_product_scope_changed")
    publication = past_date(row.get("publicationDate"), now)
    terms = row.get("terms")
    require(isinstance(terms, dict) and isinstance(terms.get("OnDemand"), dict) and len(terms["OnDemand"]) == 1,
            "aws_flat_ondemand_price_required")
    term_key, term = next(iter(terms["OnDemand"].items()))
    require(isinstance(term, dict) and term.get("sku") == sku and term.get("offerTermCode") == "JRTCKXETXF"
            and term_key == sku + ".JRTCKXETXF" and term.get("termAttributes") == {}, "aws_flat_ondemand_price_required")
    effective = past_date(term.get("effectiveDate"), now)
    dimensions = term.get("priceDimensions")
    require(isinstance(dimensions, dict) and len(dimensions) == 1, "aws_flat_ondemand_price_required")
    code, dimension = next(iter(dimensions.items()))
    require(isinstance(dimension, dict) and isinstance(code, str) and code.startswith(term_key + ".")
            and dimension.get("rateCode") == code and dimension.get("unit") == unit and dimension.get("beginRange") == "0"
            and dimension.get("endRange") == "Inf" and dimension.get("appliesTo") == []
            and isinstance(dimension.get("pricePerUnit"), dict) and set(dimension["pricePerUnit"]) == {"USD"},
            "aws_flat_ondemand_price_required")
    rate = decimal_price(dimension["pricePerUnit"]["USD"])
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return rate, {"service": service, "sku": sku, "rate_code": code, "unit": unit,
                  "publication_date": publication, "effective_date": effective, "product_sha256": digest}


def aws_plan(plan, cloud, prefix, run):
    validate_fresh_plan(plan, "aws", prefix, run)
    rows, unknowns = {}, {}
    for item in plan["resource_changes"]:
        if item.get("mode") == "data" or item["type"] in ("random_password", "terraform_data", "local_file"):
            continue
        require(item["type"] not in rows, "aws_price_plan_shape_unsupported")
        rows[item["type"]] = item["change"]["after"]
        unknown = item["change"].get("after_unknown", {})
        require(isinstance(unknown, dict) and not any(unknown.get(key) for key in ("instance_type", "ami", "availability_zone", "size", "type")),
                "aws_price_plan_values_unknown")
        unknowns[item["type"]] = unknown
    require(set(rows) == RESOURCE_TYPES["aws"], "aws_price_plan_shape_unsupported")
    vm, volume, subnet = rows["aws_instance"], rows["aws_ebs_volume"], rows["aws_subnet"]
    zone, ami = vm.get("availability_zone"), vm.get("ami")
    require(vm.get("instance_type") == cloud["size"] and isinstance(zone, str)
            and re.fullmatch(re.escape(cloud["region"]) + r"[a-z]", zone) and isinstance(ami, str)
            and re.fullmatch(r"ami-[a-f0-9]{8,17}", ami), "aws_price_plan_vm_scope_changed")
    require(vm.get("instance_market_options") == [] and vm.get("network_interface") == []
            and subnet.get("map_public_ip_on_launch") is True
            and (vm.get("associate_public_ip_address") is None or vm.get("associate_public_ip_address") is True),
            "aws_price_plan_network_or_market_unsupported")
    roots = vm.get("root_block_device")
    require(isinstance(roots, list) and len(roots) == 1 and isinstance(roots[0], dict), "aws_price_plan_disk_scope_changed")
    root = roots[0]
    require(root.get("volume_type") == "gp3" and type(root.get("volume_size")) is int and 20 <= root["volume_size"] <= 16384
            and root.get("delete_on_termination") is True and volume.get("type") == "gp3"
            and type(volume.get("size")) is int and 1 <= volume["size"] <= 16384 and volume.get("availability_zone") == zone,
            "aws_price_plan_disk_scope_changed")
    for disk in (root, volume):
        require(type(disk.get("iops")) is int and disk["iops"] == 3000
                and type(disk.get("throughput")) is int and disk["throughput"] == 125, "aws_price_extra_disk_performance_unpriced")
    root_unknown = unknowns["aws_instance"].get("root_block_device", [])
    require(root_unknown == [] or isinstance(root_unknown, list) and len(root_unknown) == 1 and isinstance(root_unknown[0], dict)
            and not any(root_unknown[0].get(key) for key in ("volume_type", "volume_size", "delete_on_termination", "iops", "throughput")),
            "aws_price_plan_values_unknown")
    require(not any(unknowns["aws_ebs_volume"].get(key) for key in ("iops", "throughput")), "aws_price_plan_values_unknown")
    credits = vm.get("credit_specification")
    credit_unknown = unknowns["aws_instance"].get("credit_specification", [])
    known = isinstance(credit_unknown, list) and (credit_unknown == [] or len(credit_unknown) == 1
        and isinstance(credit_unknown[0], dict) and set(credit_unknown[0]) <= {"cpu_credits"}
        and all(type(value) is bool and value is False for value in credit_unknown[0].values()))
    require(isinstance(credits, list) and known, "aws_price_cpu_mode_required")
    if cloud["size"].split(".", 1)[0] in BURST_FAMILIES:
        require(len(credits) == 1 and isinstance(credits[0], dict) and set(credits[0]) == {"cpu_credits"}
                and credits[0]["cpu_credits"] in ("standard", "unlimited"), "aws_price_cpu_mode_required")
        mode = credits[0]["cpu_credits"]
    else:
        require(credits == [], "aws_price_cpu_mode_required"); mode = "not_applicable"
    return {"ami": ami, "availability_zone": zone, "root_volume_gib": root["volume_size"], "data_volume_gib": volume["size"],
            "cpu_credit_mode": mode, "gp3_iops": 3000, "gp3_throughput_mib_s": 125}


def aws_rates(plan, cloud, prefix, run, adapter, credentials, source_revision, plan_sha256, query=None, now=time.time, public=None):
    selection = aws_plan(plan, cloud, prefix, run)
    require(adapter.cloud == cloud and isinstance(source_revision, str) and re.fullmatch(r"[a-f0-9]{40}", source_revision)
            and isinstance(plan_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", plan_sha256), "price_measurement_scope_changed")
    began = now()
    require(type(began) in (int, float) and math.isfinite(began) and 0 <= began <= 253402286400, "invalid_price_measurement_clock")
    image_result = adapter.call(adapter.ec2, "describe_images", ImageIds=[selection["ami"]])
    images = image_result.get("Images")
    require(isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict), "aws_price_ubuntu_image_required")
    image = images[0]
    require(image.get("ImageId") == selection["ami"] and image.get("OwnerId") == "099720109477"
            and image.get("Architecture") == "x86_64" and image.get("PlatformDetails") == "Linux/UNIX"
            and image.get("State") == "available" and image.get("RootDeviceType") == "ebs" and image.get("ProductCodes", []) == []
            and isinstance(image.get("Name"), str)
            and re.fullmatch(r"ubuntu/images/hvm-ssd/ubuntu-jammy-22\.04-amd64-server-[0-9.]+", image["Name"]),
            "aws_price_ubuntu_image_required")
    vcpus, machine = machine_selection(adapter, cloud, selection["cpu_credit_mode"])
    public = AWSTerms() if public is None else public
    performance, performance_source = public_terms(public, "gp3"); included_gp3(performance)
    cpu_rate, cpu_source = Decimal(0), None
    if selection["cpu_credit_mode"] == "unlimited":
        terms, cpu_source = public_terms(public, "cpu")
        cpu_rate = cpu_unit_rate(terms, cloud["size"].split(".", 1)[0])
    cpu_hourly = cpu_rate * vcpus
    query = AWSPriceQuery(credentials) if query is None else query
    region = cloud["region"]
    requested = {
        "vm": ("AmazonEC2", {"regionCode": region, "productFamily": "Compute Instance", "instanceType": cloud["size"],
            "operatingSystem": "Linux", "tenancy": "Shared", "preInstalledSw": "NA", "capacitystatus": "Used", "licenseModel": "No License required"}, "Hrs", None),
        "gp3": ("AmazonEC2", {"regionCode": region, "productFamily": "Storage", "volumeApiName": "gp3"}, "GB-Mo", None),
        "ipv4": ("AmazonVPC", {"regionCode": region, "group": "VPCPublicIPv4Address",
            "groupDescription": "Hourly charge for In-use Public IPv4 Addresses"}, "Hrs", ":InUseAddress")}
    rates, sources = {}, {}
    for name, (service, filters, unit, suffix) in requested.items():
        rates[name], sources[name] = flat_price(query.products(service, filters), service, filters, unit, began, suffix)
    finished = now()
    require(type(finished) in (int, float) and math.isfinite(finished) and began <= finished <= began + 300, "price_measurement_expired")
    disk_hourly = rates["gp3"] / 672
    pending = ["model_execution_cost_bound", "egress_unit_price_and_volume_bound", "cleanup_deadline_enforcement",
               "tax_and_account_adjustments", "remaining_resource_charges"]
    return {"schema": 1, "provider": "aws", "status": "resource_unit_prices_measured", "currency": "USD", "run_id": prefix,
        "source_revision": source_revision, "plan_sha256": plan_sha256,
        "verified_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
        "selection": {"account_id": cloud["account_id"], "region": region, "size": cloud["size"], "vcpus": vcpus, **selection},
        "vm_hourly_usd": dollars(rates["vm"]), "gp3_gib_monthly_usd": dollars(rates["gp3"]),
        "gp3_gib_hourly_usd_upper_bound": dollars(disk_hourly), "public_ipv4_count": 1,
        "public_ipv4_hourly_usd": dollars(rates["ipv4"]),
        "cpu_credit_vcpu_hourly_usd": dollars(cpu_rate), "cpu_credit_hourly_usd_upper_bound": dollars(cpu_hourly),
        "cpu_credit_bound_policy": "full_vcpu_utilization_no_earned_credit_allowance",
        "vm_cpu_disks_ipv4_hourly_usd_upper_bound": dollars(rates["vm"] + cpu_hourly + (selection["root_volume_gib"] + selection["data_volume_gib"]) * disk_hourly + rates["ipv4"]),
        "sources": {"endpoint": ENDPOINT, "products": sources, "machine_sha256": hashlib.sha256(json.dumps(machine, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                    "gp3_terms": performance_source, **({"cpu_terms": cpu_source} if cpu_source else {})},
        "launch_gate": "unverified", "complete_quote": False, "not_verified": pending}
