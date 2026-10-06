import contextlib
import copy
import importlib.util
import io
import json
import os
from urllib.error import HTTPError, URLError
from pathlib import Path
import sys
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_aws_prices as prices
import showcase_terraform as terraform
from showcase_attempt import prepare
from showcase_policy import PolicyError, RESOURCE_TYPES, reserve_amount
import test_showcase_attempt as preparation
from test_showcase_policy import config, NOW
from test_showcase_terraform import FakeSession

PREFIX = "as7-fixture01"
PRIVATE = "fixture-private-price-key"
AMI = "ami-0123456789abcdef0"


def cloud_config():
    value = config(); value["providers"]["aws"].update(region="us-east-1", size="m5.large")
    return value


def image():
    return {"Images": [{"ImageId": AMI, "OwnerId": "099720109477", "Architecture": "x86_64", "State": "available",
        "PlatformDetails": "Linux/UNIX", "RootDeviceType": "ebs", "ProductCodes": [],
        "Name": "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-20261001"}]}


def adapter(cloud):
    value = SimpleNamespace(cloud=cloud, ec2=object(), image=image(), machine={"InstanceTypes": [{
        "InstanceType": cloud["size"], "VCpuInfo": {"DefaultVCpus": 2}, "MemoryInfo": {"SizeInMiB": 8192},
        "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
        "BurstablePerformanceSupported": cloud["size"].split(".")[0] in prices.BURST_FAMILIES}]})
    value.call = Mock(side_effect=lambda client, operation, **kwargs: value.image if operation == "describe_images" else value.machine)
    return value


class FakeTerms:
    def __init__(self):
        self.calls = []
        self.documents = {
            "gp3": '<p>baseline IOPS performance of 3,000 IOPS, which is included with the price of storage.</p><p>baseline throughput performance of 125 MiB/s, which is included with the price of storage.</p>',
            "cpu": '<h2>T8i/T4g/T3/T3a/T2 Unlimited Mode Pricing</h2><p>For T8i instances in Unlimited mode, CPU Credits are charged at: $0.06 per vCPU-Hour for Linux, RHEL and SLES, and $0.1152 per vCPU-Hour for Windows</p><p>For T2 and T3 instances in Unlimited mode, CPU Credits are charged at: $0.05 per vCPU-Hour for Linux, RHEL and SLES, and $0.096 per vCPU-Hour for Windows</p><p>The CPU Credit pricing is the same for all instance sizes, for On-Demand, Spot, and Reserved Instances, and across all regions.</p>',
        }
    def fetch(self, name):
        self.calls.append(name); document = self.documents[name]
        return {"source": prices.SOURCES[name], "body_sha256": hashlib.sha256(document.encode()).hexdigest(), "document": document}


def plan(cloud):
    rows = []
    for kind in sorted(RESOURCE_TYPES["aws"]):
        values = {"tags": {"Name": PREFIX + "-" + kind.removeprefix("aws_")}}
        unknown = {}
        if kind == "aws_key_pair": values = {"key_name": PREFIX + "-key"}
        if kind == "aws_instance":
            values.update(instance_type=cloud["size"], ami=AMI, availability_zone=cloud["region"] + "a",
                instance_market_options=[], network_interface=[], associate_public_ip_address=None,
                root_block_device=[{"volume_type": "gp3", "volume_size": 20, "delete_on_termination": True,
                                    "iops": 3000, "throughput": 125}],
                credit_specification=[{"cpu_credits": "unlimited"}] if cloud["size"].split(".")[0] in prices.BURST_FAMILIES else [])
            unknown = {"associate_public_ip_address": True, "root_block_device": [{}],
                "credit_specification": [{}] if values["credit_specification"] else []}
        if kind == "aws_ebs_volume": values.update(type="gp3", size=50, availability_zone=cloud["region"] + "a", iops=3000, throughput=125)
        if kind == "aws_subnet": values.update(map_public_ip_on_launch=True)
        if kind == "aws_volume_attachment": values = {"volume_id": None, "instance_id": None}; unknown = {"volume_id": True, "instance_id": True}
        if kind == "aws_route_table_association": values = {"subnet_id": None, "route_table_id": None}; unknown = {"subnet_id": True, "route_table_id": True}
        rows.append({"mode": "managed", "address": "module.aws[0]." + kind + ".this", "type": kind,
            "change": {"actions": ["create"], "before": None, "after": values, "after_unknown": unknown}})
    return {"resource_changes": rows}


def product(service, filters, rate="0.10", sku="A" * 16, unit="Hrs"):
    item = {"sku": sku, "attributes": {"servicecode": service, "locationType": "AWS Region"}}
    item["attributes"].update({key: value for key, value in filters.items() if key != "productFamily"})
    if "productFamily" in filters: item["productFamily"] = filters["productFamily"]
    if service == "AmazonVPC": item["attributes"]["usagetype"] = "USE1-PublicIPv4:InUseAddress"
    term_key = sku + ".JRTCKXETXF"; code = term_key + ".6YS6EN2CT7"
    return {"serviceCode": service, "product": item, "publicationDate": "2026-09-01T00:00:00Z",
        "terms": {"OnDemand": {term_key: {"sku": sku, "offerTermCode": "JRTCKXETXF", "termAttributes": {},
            "effectiveDate": "2026-09-01T00:00:00Z", "priceDimensions": {code: {"rateCode": code, "unit": unit,
                "beginRange": "0", "endRange": "Inf", "appliesTo": [], "pricePerUnit": {"USD": rate}}}}}}}


class FakeQuery:
    def __init__(self): self.calls = []; self.mutate = lambda row: None
    def products(self, service, filters):
        self.calls.append((service, filters))
        if service == "AmazonVPC": row = product(service, filters, ".005", "C" * 16)
        elif filters["productFamily"] == "Storage": row = product(service, filters, ".08", "B" * 16, "GB-Mo")
        else: row = product(service, filters)
        self.mutate(row)
        return [row]


class PriceTest(unittest.TestCase):
    def setUp(self):
        self.cloud = cloud_config()["providers"]["aws"]
        self.plan = plan(self.cloud)
        self.adapter = adapter(self.cloud)
        self.query = FakeQuery()
        self.public = FakeTerms()

    def measure(self, clock=None):
        return prices.aws_rates(self.plan, self.cloud, PREFIX, "/tmp/fixture-repo", self.adapter,
            {"access_key": PRIVATE, "secret_key": PRIVATE}, "a" * 40, "b" * 64, self.query, (lambda: NOW) if clock is None else clock, self.public)

    def test_on_demand_vm_both_disks_and_one_public_ipv4_bound_without_complete_quote(self):
        result = self.measure()
        self.assertEqual(result["vm_hourly_usd"], "0.100000000")
        self.assertEqual(result["gp3_gib_monthly_usd"], "0.080000000")
        self.assertEqual(result["public_ipv4_hourly_usd"], "0.005000000")
        self.assertEqual(result["vm_cpu_disks_ipv4_hourly_usd_upper_bound"], "0.113333334")
        self.assertEqual(result["cpu_credit_hourly_usd_upper_bound"], "0.000000000")
        self.assertEqual(result["selection"]["root_volume_gib"], 20); self.assertEqual(result["selection"]["data_volume_gib"], 50)
        self.assertEqual(result["public_ipv4_count"], 1); self.assertEqual(result["plan_sha256"], "b" * 64)
        self.assertFalse(result["complete_quote"]); self.assertEqual(result["launch_gate"], "unverified")
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual([row[0] for row in self.query.calls], ["AmazonEC2", "AmazonEC2", "AmazonVPC"])
        self.assertEqual(self.adapter.call.call_args_list, [call(self.adapter.ec2, "describe_images", ImageIds=[AMI]),
            call(self.adapter.ec2, "describe_instance_types", InstanceTypes=[self.cloud["size"]])])
        with self.assertRaises(PolicyError): reserve_amount(result, "aws", 14400, NOW)

    def test_unknown_scope_spot_network_disk_and_extra_performance_refused_before_reads(self):
        changes = (
            lambda r: r["aws_instance"]["after"].update(availability_zone="foreign-zone"),
            lambda r: r["aws_instance"]["after_unknown"].update(ami=True),
            lambda r: r["aws_instance"]["after"].update(instance_market_options=[{"market_type": "spot"}]),
            lambda r: r["aws_instance"]["after"].update(network_interface=[{"network_interface_id": "existing"}]),
            lambda r: r["aws_subnet"]["after"].update(map_public_ip_on_launch=False),
            lambda r: r["aws_instance"]["after"].update(associate_public_ip_address=False),
            lambda r: r["aws_instance"]["after"]["root_block_device"][0].update(volume_type="io2"),
            lambda r: r["aws_instance"]["after"]["root_block_device"][0].update(volume_size=True),
            lambda r: r["aws_instance"]["after_unknown"]["root_block_device"][0].update(volume_size=True),
            lambda r: r["aws_ebs_volume"]["after"].update(size=0),
            lambda r: r["aws_ebs_volume"]["after"].update(availability_zone="us-east-1b"),
            lambda r: r["aws_ebs_volume"]["after"].update(iops=6000),
            lambda r: r["aws_instance"]["after"]["root_block_device"][0].update(throughput=250),
        )
        original = copy.deepcopy(self.plan)
        for change in changes:
            self.plan = copy.deepcopy(original); change({row["type"]: row["change"] for row in self.plan["resource_changes"]})
            with self.subTest(change=change), self.assertRaises(PolicyError): self.measure()
            self.adapter.call.assert_not_called(); self.assertEqual(self.query.calls, [])

    def test_missing_duplicate_or_unexpected_resources_refused_before_price_read(self):
        original = copy.deepcopy(self.plan)
        for kind in ("aws_security_group", "aws_key_pair"):
            self.plan = {"resource_changes": [row for row in original["resource_changes"] if row["type"] != kind]}
            with self.subTest(kind=kind), self.assertRaises(PolicyError): self.measure()
        self.plan = copy.deepcopy(original); self.plan["resource_changes"].append(copy.deepcopy(self.plan["resource_changes"][0]))
        with self.assertRaises(PolicyError): self.measure()
        self.adapter.call.assert_not_called()

    def test_paid_pro_foreign_owner_arm_or_marketplace_ami_refused_before_price_reads(self):
        for change in ({"OwnerId": "foreign"}, {"Architecture": "arm64"}, {"PlatformDetails": "Windows"},
            {"ProductCodes": [{"ProductCodeId": PRIVATE}]}, {"Name": "ubuntu-pro-paid"}, {"ImageId": "ami-foreign"}):
            data = image(); data["Images"][0].update(change); self.adapter.image = data
            with self.subTest(change=change), self.assertRaisesRegex(PolicyError, "aws_price_ubuntu_image_required"): self.measure()
            self.assertEqual(self.query.calls, [])

    def test_burstable_full_cpu_bound_includes_every_vcpu_without_earned_credit_allowances(self):
        for family, rate in (("t2", ".05"), ("t3", ".05"), ("t3a", ".05"), ("t8i", ".06")):
            self.cloud["size"] = family + ".large"; self.plan = plan(self.cloud); self.adapter = adapter(self.cloud)
            result = self.measure()
            with self.subTest(family=family):
                self.assertEqual(result["cpu_credit_hourly_usd_upper_bound"], prices.dollars(prices.Decimal(rate) * 2))
                self.assertEqual(result["vm_cpu_disks_ipv4_hourly_usd_upper_bound"], prices.dollars(prices.Decimal(".1133333333333333333333333333") + prices.Decimal(rate) * 2))
                self.assertNotIn("cpu_credit_cost_bound", result["not_verified"])
                self.assertNotIn("disk_performance_defaults", result["not_verified"])
                self.assertEqual(result["selection"]["cpu_credit_mode"], "unlimited")
                self.assertIn("cpu_terms", result["sources"])

    def test_missing_unknown_or_paid_cpu_disk_plan_controls_fail_before_reads(self):
        self.cloud["size"] = "t3.large"; original = plan(self.cloud)
        changes = (
            lambda r: r["aws_instance"]["after"].update(credit_specification=[]),
            lambda r: r["aws_instance"]["after"].update(credit_specification=[{"cpu_credits": "unknown"}]),
            lambda r: r["aws_instance"]["after_unknown"].update(credit_specification=True),
            lambda r: r["aws_instance"]["after_unknown"].update(credit_specification=[{"cpu_credits": True}]),
            lambda r: r["aws_instance"]["after"]["root_block_device"][0].update(iops=None),
            lambda r: r["aws_instance"]["after_unknown"]["root_block_device"][0].update(iops=True),
            lambda r: r["aws_ebs_volume"]["after_unknown"].update(throughput=True),
            lambda r: r["aws_ebs_volume"]["after"].update(throughput=None),
        )
        for change in changes:
            self.plan = copy.deepcopy(original); change({row["type"]: row["change"] for row in self.plan["resource_changes"]})
            with self.subTest(change=change), self.assertRaises(PolicyError): self.measure()
        self.adapter.call.assert_not_called(); self.assertEqual(self.public.calls, []); self.assertEqual(self.query.calls, [])

    def test_machine_identity_cpu_architecture_memory_and_burst_mode_cannot_be_assumed(self):
        original = copy.deepcopy(self.adapter.machine)
        for mutation in ({"InstanceType": "foreign"}, {"VCpuInfo": None}, {"VCpuInfo": {"DefaultVCpus": True}},
                         {"MemoryInfo": {"SizeInMiB": 4096}}, {"ProcessorInfo": {"SupportedArchitectures": ["arm64"]}},
                         {"BurstablePerformanceSupported": None}, {"BurstablePerformanceSupported": True}):
            self.adapter.machine = copy.deepcopy(original); self.adapter.machine["InstanceTypes"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(PolicyError): self.measure()
        self.assertEqual(self.public.calls, []); self.assertEqual(self.query.calls, [])

    def test_changed_ambiguous_or_missing_public_cost_contracts_fail_before_prices(self):
        self.cloud["size"] = "t3.large"; self.plan = plan(self.cloud); self.adapter = adapter(self.cloud)
        original = FakeTerms().documents
        for name, changed in (("gp3", original["gp3"].replace("3,000", "2,000")),
                              ("gp3", original["gp3"] * 2), ("cpu", original["cpu"] * 2),
                              ("cpu", original["cpu"].replace("and across all regions", "in one region")),
                              ("cpu", original["cpu"].replace("$0.05", "$0"))):
            self.public.documents = dict(original); self.public.documents[name] = changed
            with self.subTest(name=name, changed=changed[:60]), self.assertRaises(PolicyError): self.measure()
        self.assertEqual(self.query.calls, [])

    def test_measured_changed_cpu_rate_and_standard_mode_are_not_hardcoded(self):
        self.cloud["size"] = "t3.large"; self.plan = plan(self.cloud); self.adapter = adapter(self.cloud)
        self.public.documents["cpu"] = self.public.documents["cpu"].replace("$0.05", "$0.07")
        self.assertEqual(self.measure()["cpu_credit_hourly_usd_upper_bound"], "0.140000000")
        vm = next(row["change"]["after"] for row in self.plan["resource_changes"] if row["type"] == "aws_instance")
        vm["credit_specification"] = [{"cpu_credits": "standard"}]; self.public.calls.clear()
        self.assertEqual(self.measure()["cpu_credit_hourly_usd_upper_bound"], "0.000000000")
        self.assertEqual(self.public.calls, ["gp3"])

    def test_wrong_region_currency_unit_zero_tier_or_future_terms_fail(self):
        def dimensions(row): return next(iter(next(iter(row["terms"]["OnDemand"].values()))["priceDimensions"].values()))
        changes = (
            lambda r: r["product"]["attributes"].update(regionCode="foreign-region"),
            lambda r: dimensions(r).update(unit="Requests"),
            lambda r: dimensions(r).update(pricePerUnit={"EUR": ".10"}),
            lambda r: dimensions(r).update(pricePerUnit={"USD": "0"}),
            lambda r: dimensions(r).update(beginRange="100"),
            lambda r: dimensions(r).update(endRange="1000"),
            lambda r: dimensions(r).update(appliesTo=["other-sku"]),
            lambda r: r.update(publicationDate="2099-01-01T00:00:00Z"),
            lambda r: next(iter(r["terms"]["OnDemand"].values())).update(effectiveDate="2099-01-01T00:00:00Z"),
        )
        for change in changes:
            self.query.mutate = change
            with self.subTest(change=change), self.assertRaises(PolicyError): self.measure()

    def test_duplicate_products_or_terms_dimensions_and_missing_ondemand_fail(self):
        filters = {"regionCode": "us-east-1"}; row = product("AmazonEC2", filters)
        with self.assertRaises(PolicyError): prices.flat_price([row, row], "AmazonEC2", filters, "Hrs", NOW)
        for change in (lambda r: r["terms"].pop("OnDemand"), lambda r: r["terms"]["OnDemand"].update(extra={}),
            lambda r: next(iter(r["terms"]["OnDemand"].values()))["priceDimensions"].update(extra={})):
            edited = copy.deepcopy(row); change(edited)
            with self.subTest(change=change), self.assertRaises(PolicyError): prices.flat_price([edited], "AmazonEC2", filters, "Hrs", NOW)

    def test_unrelated_response_fields_are_hashed_and_never_published(self):
        self.query.mutate = lambda row: row["product"]["attributes"].update(unrelated=PRIVATE)
        self.assertNotIn(PRIVATE, json.dumps(self.measure()))

    def test_expired_or_backwards_measurement_cannot_receive_receipt(self):
        for end in (NOW + 301, NOW - 1, float("nan")):
            clock = iter((NOW, end))
            with self.subTest(end=end), self.assertRaisesRegex(PolicyError, "price_measurement_expired"): self.measure(lambda: next(clock))


class QueryTest(unittest.TestCase):
    def query(self, responses):
        client = SimpleNamespace(meta=SimpleNamespace(endpoint_url=prices.ENDPOINT), get_products=Mock(side_effect=responses))
        return prices.AWSPriceQuery({"access_key": PRIVATE, "secret_key": PRIVATE}, client), client

    def test_pagination_uses_explicit_filters_and_original_service(self):
        query, client = self.query([{"FormatVersion": "aws_v1", "PriceList": [], "NextToken": "page2"}, {"FormatVersion": "aws_v1", "PriceList": []}])
        self.assertEqual(query.products("AmazonEC2", {"regionCode": "us-west-2"}), [])
        self.assertEqual(query.queries, 2)
        first, second = [call.kwargs for call in client.get_products.call_args_list]
        self.assertEqual(first, {"ServiceCode": "AmazonEC2", "FormatVersion": "aws_v1", "MaxResults": 100,
            "Filters": [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-west-2"}]})
        self.assertEqual(second, {**first, "NextToken": "page2"})

    def test_sdk_failure_and_malformed_payload_redacted_without_retry(self):
        for responses in ([RuntimeError(PRIVATE)], [{"FormatVersion": "other", "PriceList": []}],
            [{"FormatVersion": "aws_v1", "PriceList": ['{"id":NaN}']}], [{"FormatVersion": "aws_v1", "PriceList": ['{"id":1,"id":2}']}],
            [{"FormatVersion": "aws_v1", "PriceList": [PRIVATE]}]):
            query, client = self.query(responses)
            with self.subTest(responses_type=type(responses[0]).__name__), self.assertRaises(PolicyError) as caught: query.products("AmazonEC2", {"regionCode": "us-east-1"})
            self.assertNotIn(PRIVATE, str(caught.exception)); self.assertEqual(client.get_products.call_count, 1)

    def test_repeated_missing_or_excessive_pages_and_oversized_payload_fail(self):
        for rows in ([{"FormatVersion": "aws_v1", "PriceList": [], "NextToken": "same"}] * 2,
                     [{"FormatVersion": "aws_v1", "PriceList": [], "NextToken": str(i)} for i in range(10)],
                     [{"FormatVersion": "aws_v1", "PriceList": ["x" * (1024 * 1024 + 1)]}]):
            query, _ = self.query(rows)
            with self.subTest(count=len(rows)), self.assertRaises(PolicyError): query.products("AmazonEC2", {"regionCode": "us-east-1"})

    def test_endpoint_and_service_refused_before_request(self):
        client = SimpleNamespace(meta=SimpleNamespace(endpoint_url="https://evil.test"))
        with self.assertRaisesRegex(PolicyError, "aws_price_endpoint_refused"): prices.AWSPriceQuery({"access_key": PRIVATE, "secret_key": PRIVATE}, client)
        query, client = self.query([])
        with self.assertRaisesRegex(PolicyError, "aws_price_scope_invalid"): query.products("OtherService", {"regionCode": "us-east-1"})
        client.get_products.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "optional live SDK unavailable")
    def test_actual_sdk_isolates_profile_endpoints_credentials_and_request_shapes(self):
        from botocore.stub import Stubber
        with patch.dict(os.environ,
            {"AWS_PROFILE": "missing-personal-profile", "AWS_CONFIG_FILE": "/missing", "AWS_SHARED_CREDENTIALS_FILE": "/missing",
             "AWS_ENDPOINT_URL": "https://evil.test", "AWS_ENDPOINT_URL_PRICING": "https://evil.test"}):
            query = prices.AWSPriceQuery({"access_key": "fixture-access", "secret_key": PRIVATE})
        client = query.client
        self.assertEqual(client.meta.endpoint_url, prices.ENDPOINT); self.assertEqual(client.meta.region_name, "us-east-1")
        self.assertEqual(client.meta.config.proxies, {}); self.assertEqual(client.meta.config.retries["total_max_attempts"], 1)
        self.assertEqual(client._request_signer._credentials.access_key, "fixture-access")
        request = {"ServiceCode": "AmazonEC2", "FormatVersion": "aws_v1", "MaxResults": 100,
            "Filters": [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-west-2"}]}
        with Stubber(client) as stub:
            stub.add_response("get_products", {"FormatVersion": "aws_v1", "PriceList": []}, request)
            self.assertEqual(query.products("AmazonEC2", {"regionCode": "us-west-2"}), [])
            stub.assert_no_pending_responses()


class PublicTermsTest(unittest.TestCase):
    def response(self, raw):
        reply = Mock()
        reply.__enter__ = Mock(return_value=SimpleNamespace(status=200, read=lambda limit: raw[:limit]))
        reply.__exit__ = Mock(return_value=False)
        return reply

    def test_fixed_anonymous_gets_no_redirects_proxy_or_credentials(self):
        public = prices.AWSTerms()
        with patch.object(public.opener, "open", return_value=self.response(b'<p>fixture</p>')) as opened:
            result = public.fetch("gp3")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, prices.SOURCES["gp3"]); self.assertEqual(request.get_method(), "GET")
        self.assertEqual(opened.call_args.kwargs, {"timeout": 20}); self.assertNotIn("Authorization", request.headers)
        self.assertEqual(result["body_sha256"], hashlib.sha256(b'<p>fixture</p>').hexdigest())
        self.assertTrue(any(isinstance(h, prices.NoRedirect) for h in public.opener.handlers))
        with patch.object(public.opener, "open") as opened, self.assertRaisesRegex(PolicyError, "price_source_refused"):
            public.fetch("https://foreign.invalid")
        opened.assert_not_called()

    def test_changed_source_hash_and_missing_or_oversized_document_refused(self):
        for mutation in ({"source": "https://foreign.invalid"}, {"body_sha256": "a" * 64}, {"document": None}):
            public = FakeTerms(); original = public.fetch("gp3")
            public.fetch = Mock(return_value=original | mutation)
            with self.subTest(mutation=mutation), self.assertRaises(PolicyError): prices.public_terms(public, "gp3")
        for raw in (b'', b'x' * (prices.MAX_RESPONSE + 1), b'\xff'):
            public = prices.AWSTerms()
            with patch.object(public.opener, "open", return_value=self.response(raw)), self.assertRaises(PolicyError): public.fetch("cpu")

    def test_failed_http_is_bounded_and_redacted_without_retry(self):
        for error in (HTTPError("https://foreign.invalid", 403, PRIVATE, {}, io.BytesIO(PRIVATE.encode())), URLError(PRIVATE)):
            public = prices.AWSTerms()
            with patch.object(public.opener, "open", side_effect=error) as opened, self.assertRaises(PolicyError) as caught:
                public.fetch("cpu")
            self.assertEqual(opened.call_count, 1); self.assertNotIn(PRIVATE, str(caught.exception))


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation.PreparationTest("runTest"); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        for name in ("showcase_prices", "showcase_aws_prices", "showcase_terraform", "showcase_policy", "showcase_preflight"):
            path = self.fixture.repo / "scripts" / (name + ".py"); path.parent.mkdir(exist_ok=True)
            path.write_bytes((Path(terraform.__file__).parent / (name + ".py")).read_bytes())
        self.fixture.git("add", "."); self.fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "price sources")
        self.fixture.revision = self.fixture.git("rev-parse", "HEAD").strip()
        self.config = cloud_config(); self.run = self.fixture.batch / "attempts" / PREFIX
        prepare(self.config, "aws", PREFIX, self.fixture.batch, self.fixture.revision, self.fixture.repo)
        session = FakeSession(self.config); session.credentials = {"access_key": PRIVATE, "secret_key": PRIVATE}
        session.adapter = adapter(self.config["providers"]["aws"])
        self.client = terraform.TerraformAttempt(self.run, self.config, "aws", session, now=lambda: NOW)
        def tf(*args, **kwargs):
            if args[0] == "version": return json.dumps({"terraform_version": "1.16.5"}).encode()
            if args[0] == "init": (self.client.checkout / ".terraform.lock.hcl").write_text("fixture-lock")
            if args[0] == "plan": path = self.run / "deployment.tfplan"; path.write_bytes(b"fixture-plan"); path.chmod(0o600)
            if args[0] == "show": return json.dumps(plan(self.config["providers"]["aws"])).encode()
            return b""
        self.client.tf = Mock(side_effect=tf); self.client.plan()

    def test_private_receipt_binds_plan_commit_and_no_reservation_or_state(self):
        result = self.client.resource_unit_prices(aws_query=FakeQuery(), aws_public=FakeTerms())
        path = self.run / "resource-prices.json"
        self.assertEqual(json.loads(path.read_text()), result); self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(result["source_revision"], self.fixture.revision); self.assertEqual(result["plan_sha256"], self.client.record()["plan_sha256"])
        self.assertFalse((self.fixture.batch / "reservations.json").exists()); self.assertFalse(self.client.state_path.exists())

    def test_drift_before_read_does_not_leave_a_price_receipt(self):
        (self.run / "deployment.tfplan").write_bytes(b"changed")
        query = FakeQuery()
        with self.assertRaisesRegex(PolicyError, "saved_plan_changed"): self.client.resource_unit_prices(aws_query=query, aws_public=FakeTerms())
        self.client.session.adapter.call.assert_not_called(); self.assertEqual(query.calls, [])
        self.assertFalse((self.run / "resource-prices.json").exists())

    def test_public_plan_entrypoint_measures_partial_prices_without_apply_or_reservation(self):
        import showcase_live as live
        from showcase_cleanup import private_write
        self.client.record_path.unlink()
        inputs = self.fixture.root / "private-inputs.json"; private_write(inputs, self.config)
        argv = ['showcase_live.py', '--phase', 'plan', '--provider', 'aws', '--run-id', PREFIX,
                '--config', str(inputs), '--batch', str(self.fixture.batch)]
        output = io.StringIO(); query = FakeQuery()
        with patch.dict(live.os.environ, {"AGENT_STACK_LIVE_TESTS": "1"}), patch.object(sys, "argv", argv), \
             patch.object(live.shutil, "which", return_value="/fixture/tool"), patch.object(live.signal, "signal"), \
             patch.object(live, "CloudSession", return_value=self.client.session), patch.object(live, "TerraformAttempt", return_value=self.client), \
             patch.object(prices, "AWSPriceQuery", return_value=query), patch.object(prices, "AWSTerms", return_value=FakeTerms()), contextlib.redirect_stdout(output):
            code = live.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result); self.assertEqual(result["status"], "planned")
        self.assertFalse(result["launch_ready"]); self.assertFalse(result["spend_reserved"])
        self.assertFalse(result["resource_unit_prices"]["complete_quote"])
        self.assertFalse(self.client.state_path.exists()); self.assertFalse((self.fixture.batch / "reservations.json").exists())
