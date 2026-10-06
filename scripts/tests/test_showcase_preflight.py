import base64
import copy
from datetime import datetime, timezone
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_preflight as preflight
from test_showcase_policy import config, NOW
from test_showcase_models import FakeModels

PREFIX = "as7-fixture01"
SECRET = "private-fixture-token-must-not-reach-report"


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls, self.queries = routes, [], 0

    def request(self, url, authorization=None, payload=None, form=False):
        self.calls.append((url, authorization, payload, form))
        key = url.split("?", 1)[0]
        if key not in self.routes:
            raise AssertionError("unexpected endpoint: " + key)
        value = self.routes[key]
        if callable(value):
            value = value(url, payload)
        if isinstance(value, Exception):
            raise value
        self.queries += 1
        return copy.deepcopy(value)


def do_routes():
    base = preflight.DigitalOcean.base
    value = config()["providers"]["digitalocean"]
    routes = {
        base + "/account": {"account": {"uuid": value["account_id"], "status": "active", "droplet_limit": 10}},
        base + "/projects/" + value["project_id"]: {"project": {"id": value["project_id"]}},
        base + "/projects/" + value["project_id"] + "/resources": {"resources": []},
        base + "/sizes": {"sizes": [{"slug": value["size"], "memory": 8192, "available": True, "regions": [value["region"]]}]},
        base + "/images/ubuntu-22-04-x64": {"image": {"slug": "ubuntu-22-04-x64", "status": "available", "regions": [value["region"]]}},
    }
    for path, key in (("/droplets", "droplets"), ("/volumes", "volumes"), ("/vpcs", "vpcs"), ("/firewalls", "firewalls"), ("/account/keys", "ssh_keys")):
        routes[base + path] = {key: []}
    return routes


def hcloud_routes():
    base = preflight.Hetzner.base
    routes = {base + "/" + key: {key: [], "meta": {"pagination": {"next_page": None}}}
              for key in ("servers", "volumes", "firewalls", "ssh_keys", "networks", "primary_ips", "floating_ips", "load_balancers")}
    routes[base + "/server_types"] = {"server_types": [{"name": "fixture-size", "memory": 8, "architecture": "x86", "prices": [{"location": "us-east1"}],
                                                      "locations": [{"id": 1, "name": "us-east1", "deprecation": None, "available": True}]}],
                                       "meta": {"pagination": {"next_page": None}}}
    return routes


class HTTPAndSecretTest(unittest.TestCase):
    def test_op_is_one_direct_field_read_and_error_never_leaks(self):
        with patch.object(preflight.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, SECRET.encode(), b"")
            reference = "op://fixture-vault/fixture-item/fixture-field"
            self.assertEqual(preflight.read_secret(reference), SECRET)
            self.assertEqual(run.call_args.args[0], ["op", "read", "--no-newline", reference])
            run.return_value = subprocess.CompletedProcess([], 1, SECRET.encode(), SECRET.encode())
            with self.assertRaisesRegex(preflight.PreflightError, "onepassword_unavailable_or_locked") as caught:
                preflight.read_secret(reference)
            self.assertNotIn(SECRET, str(caught.exception))
            run.side_effect = subprocess.TimeoutExpired(["op"], 60, SECRET)
            with self.assertRaisesRegex(preflight.PreflightError, "onepassword_unavailable_or_locked"):
                preflight.read_secret(reference)

    def test_nonprovider_url_and_redirect_cannot_receive_bearer(self):
        http = preflight.JsonHTTP()
        with patch.object(http.opener, "open") as opened:
            for url in ("http://api.digitalocean.com/v2/account", "https://api.digitalocean.com.evil.test/v2/account",
                        "https://api.digitalocean.com:444/v2/account", "https://user@api.digitalocean.com/v2/account"):
                with self.assertRaisesRegex(preflight.PreflightError, "provider_endpoint_refused"):
                    http.request(url, "Bearer " + SECRET)
            opened.assert_not_called()
        with self.assertRaisesRegex(preflight.PreflightError, "provider_redirect_refused"):
            preflight.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test")

    def test_http_auth_failure_discards_provider_error_body(self):
        http = preflight.JsonHTTP()
        error = HTTPError("https://api.digitalocean.com/v2/account", 403, SECRET, {}, io.BytesIO(SECRET.encode()))
        with patch.object(http.opener, "open", side_effect=error):
            with self.assertRaisesRegex(preflight.PreflightError, "provider_http_403") as caught:
                http.request("https://api.digitalocean.com/v2/account", "Bearer " + SECRET)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(http.queries, 0)

    def test_duplicate_or_nonfinite_provider_json_is_refused_without_leaking_or_retry(self):
        for raw in (b'{"pricing":{},"pricing":{}}', b'{"quota":NaN}', b'{"value":Infinity}'):
            http = preflight.JsonHTTP()
            response = Mock(); response.__enter__ = Mock(return_value=SimpleNamespace(status=200, read=lambda size: raw)); response.__exit__ = Mock(return_value=False)
            with patch.object(http.opener, "open", return_value=response) as opened, self.assertRaisesRegex(preflight.PreflightError, "provider_request_failed") as caught:
                http.request("https://api.hetzner.cloud/v1/pricing", "Bearer " + SECRET)
            self.assertNotIn(SECRET, str(caught.exception)); self.assertEqual(http.queries, 0); self.assertEqual(opened.call_count, 1)

    def test_cli_requires_opt_in_before_reading_input_or_secret(self):
        script = Path(preflight.__file__)
        result = subprocess.run([sys.executable, str(script), "--config", "/missing/private.json", "--provider", "aws", "--run-id", PREFIX],
                                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(json.loads(result.stdout)["reason"], "live_opt_in_required")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")


class DigitalOceanTest(unittest.TestCase):
    def adapter(self, routes=None):
        return preflight.DigitalOcean(config()["providers"]["digitalocean"], SECRET, FakeHTTP(do_routes() if routes is None else routes))

    def test_user_and_team_identity_and_new_project(self):
        self.assertEqual(self.adapter().preflight(PREFIX)["identity"], "verified")
        routes = do_routes()
        routes[preflight.DigitalOcean.base + "/account"]["account"].update(uuid="personal-user", team={"uuid": "fixture-account"})
        self.assertEqual(self.adapter(routes).preflight(PREFIX)["project"], "verified_empty")

    def test_wrong_team_occupied_project_low_ram_quota_or_unavailable_region_fail(self):
        value = config()["providers"]["digitalocean"]
        base = preflight.DigitalOcean.base
        mutations = (
            lambda r: r[base + "/account"]["account"].update(team={"uuid": "wrong-team"}),
            lambda r: r[base + "/projects/" + value["project_id"] + "/resources"].update(resources=[{"urn": "do:droplet:existing"}]),
            lambda r: r[base + "/sizes"]["sizes"][0].update(memory=4096),
            lambda r: r[base + "/account"]["account"].update(droplet_limit=0),
            lambda r: r[base + "/sizes"]["sizes"][0].update(regions=["wrong-region"]),
            lambda r: r[base + "/firewalls"].update(firewalls=[{"name": PREFIX + "-firewall"}]),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                routes = do_routes()
                mutate(routes)
                with self.assertRaises(preflight.PreflightError):
                    self.adapter(routes).preflight(PREFIX)

    def test_project_and_global_inventory_pages_are_complete_without_following_urls(self):
        routes = do_routes()
        base = preflight.DigitalOcean.base
        def page(url, _):
            if "page=1&" in url:
                return {"firewalls": [], "links": {"pages": {"next": "https://evil.test/?steal=key"}}}
            return {"firewalls": [{"name": PREFIX + "-collision"}]}
        routes[base + "/firewalls"] = page
        adapter = self.adapter(routes)
        with self.assertRaisesRegex(preflight.PreflightError, "resource_prefix_already_exists"):
            adapter.preflight(PREFIX)
        self.assertFalse(any("evil.test" in call[0] for call in adapter.http.calls))


class HetznerTest(unittest.TestCase):
    def adapter(self, routes=None):
        return preflight.Hetzner(config()["providers"]["hetzner"], SECRET, FakeHTTP(hcloud_routes() if routes is None else routes))

    def test_project_scope_read_does_not_claim_account_identity_or_capacity(self):
        report = self.adapter().preflight(PREFIX)
        self.assertEqual(report["identity"], "declared_only")
        self.assertIn("account_project_token_mapping", report["not_verified"])
        self.assertIn("live_capacity", report["not_verified"])

    def test_existing_primary_ip_arm_low_ram_wrong_location_fail(self):
        base = preflight.Hetzner.base
        for changes in ("primary_ip", "arm", "ram", "location", "capacity"):
            routes = hcloud_routes()
            if changes == "primary_ip":
                routes[base + "/primary_ips"]["primary_ips"] = [{"id": 777}]
            else:
                row = routes[base + "/server_types"]["server_types"][0]
                row.update({"arm": {"architecture": "arm"}, "ram": {"memory": 4}, "location": {"prices": []},
                            "capacity": {"locations": [{"location": {"name": "us-east1"}, "available": False}]}}[changes])
            with self.assertRaises(preflight.PreflightError):
                self.adapter(routes).preflight(PREFIX)

    def test_malformed_pagination_cannot_hide_resources(self):
        routes = hcloud_routes()
        routes[preflight.Hetzner.base + "/servers"]["meta"]["pagination"]["next_page"] = 1
        with self.assertRaisesRegex(preflight.PreflightError, "invalid_pagination"):
            self.adapter(routes).preflight(PREFIX)

    def test_current_flat_location_is_required_and_duplicates_or_legacy_shape_refused(self):
        row = hcloud_routes()[preflight.Hetzner.base + "/server_types"]["server_types"][0]
        self.assertEqual(preflight.hetzner_location(row, "us-east1", NOW)["id"], 1)
        original = copy.deepcopy(row["locations"][0])
        for locations in ([original, original], [{"location": {"name": "us-east1"}, "available": True}],
                          [dict(original, available=False)], [dict(original, id=True)],
                          [{k: v for k, v in original.items() if k != "deprecation"}]):
            with self.subTest(locations=locations), self.assertRaises(preflight.PreflightError):
                preflight.hetzner_location(dict(row, locations=locations), "us-east1", NOW)

    def test_nonfinite_or_boolean_memory_is_refused_before_integer_conversion(self):
        for memory in (True, float("inf"), float("nan"), 1000001):
            routes = hcloud_routes(); routes[preflight.Hetzner.base + "/server_types"]["server_types"][0]["memory"] = memory
            with self.subTest(memory=memory), self.assertRaisesRegex(preflight.PreflightError, "vm_memory_below_8_gib"):
                self.adapter(routes).preflight(PREFIX)

    def test_deprecation_must_cover_whole_four_hour_attempt(self):
        stamp = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        preflight.hetzner_deprecation({"announced": stamp(NOW - 1), "unavailable_after": stamp(NOW + 14401)}, NOW)
        for value in ({"announced": stamp(NOW - 1), "unavailable_after": stamp(NOW + 14400)},
                      {"announced": stamp(NOW + 1), "unavailable_after": stamp(NOW + 20000)},
                      {"announced": "2026-01-01", "unavailable_after": stamp(NOW + 20000)}, {}, "legacy-date"):
            with self.subTest(value=value), self.assertRaises(preflight.PreflightError):
                preflight.hetzner_deprecation(value, NOW)
        for now in (float("nan"), float("inf"), True):
            with self.assertRaises(preflight.PreflightError): preflight.hetzner_deprecation(None, now)

    def test_fixed_image_filters_survive_each_pagination_query(self):
        base = preflight.Hetzner.base
        routes = {base + "/images": lambda url, _: {"images": [], "meta": {"pagination": {
            "next_page": 2 if "page=1&" in url else None}}}}
        adapter = self.adapter(routes)
        adapter.pages("/images?type=system&name=ubuntu-22.04", "images", "hetzner")
        self.assertEqual([call[0] for call in adapter.http.calls], [base + "/images?type=system&name=ubuntu-22.04&page=1&per_page=100",
            base + "/images?type=system&name=ubuntu-22.04&page=2&per_page=100"])


class GoogleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
        except ImportError:
            raise unittest.SkipTest("optional live cryptography dependency unavailable") from None
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.credential = json.dumps({"type": "service_account", "client_email": "fixture-account", "private_key_id": "fixture-key",
                                    "private_key": cls.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
                                    "token_uri": "https://evil.test/token", "project_id": "central-identity-project"})

    def routes(self):
        project = "agent-stack-fixture"
        base = "https://compute.googleapis.com/compute/v1/projects/" + project
        return {
            "https://oauth2.googleapis.com/token": {"access_token": SECRET, "token_type": "Bearer", "expires_in": 3600},
            "https://cloudresourcemanager.googleapis.com/v3/projects/" + project: {"projectId": project, "state": "ACTIVE"},
            "https://cloudresourcemanager.googleapis.com/v3/projects/" + project + ":testIamPermissions": {"permissions": list(preflight.GCP_PERMISSIONS)},
            "https://cloudbilling.googleapis.com/v1/projects/" + project + "/billingInfo": {"billingEnabled": True},
            base + "/zones/us-east1-b/machineTypes/fixture-size": {"name": "fixture-size", "memoryMb": 8192, "guestCpus": 2, "architecture": "X86_64"},
            base + "/regions/us-east1": {"quotas": [{"metric": "CPUS", "usage": 0, "limit": 8}]},
            base + "/aggregated/instances": {"items": {"zones/us-east1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}}}},
            base + "/aggregated/disks": {},
            base + "/global/networks": {"items": [{"name": "default"}]},
            base + "/global/firewalls": {}, base + "/regions/us-east1/subnetworks": {},
        }

    def adapter(self, routes=None, clock=None):
        return preflight.Google(config()["providers"]["gcp"], self.credential, FakeHTTP(self.routes() if routes is None else routes),
                                (lambda: 10000) if clock is None else clock)

    def test_signed_exchange_fixed_endpoint_and_cached_token_refresh(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        now = [10000]
        adapter = self.adapter(clock=lambda: now[0])
        self.assertEqual(adapter.access_token(), SECRET)
        assertion = adapter.http.calls[0][2]["assertion"]
        header, payload, signature = assertion.split(".")
        decode = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        self.key.public_key().verify(decode(signature), (header + "." + payload).encode(), padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual(json.loads(decode(payload))["aud"], "https://oauth2.googleapis.com/token")
        self.assertEqual(adapter.access_token(), SECRET)
        self.assertEqual(len(adapter.http.calls), 1)
        now[0] += 3541
        adapter.access_token()
        self.assertEqual(len(adapter.http.calls), 2)
        self.assertFalse(any("evil.test" in call[0] for call in adapter.http.calls))

    def test_new_project_billing_permissions_cpu_and_x86_size_checks(self):
        report = self.adapter().preflight(PREFIX)
        self.assertEqual(report["identity"], "verified")
        self.assertEqual(report["project_permissions"], "required_permissions_returned")
        self.assertIn("machine_family_disk_ip_quotas", report["not_verified"])

    def test_cross_zone_resources_permissions_billing_or_arm_fail(self):
        project = "agent-stack-fixture"
        base = "https://compute.googleapis.com/compute/v1/projects/" + project
        mutations = (
            lambda r: r[base + "/aggregated/disks"].update(items={"zones/other-zone": {"disks": [{"name": "operator-disk"}]}}),
            lambda r: r[base + "/aggregated/instances"].update(items={"zones/other-zone": {"warning": {"code": "UNREACHABLE"}}}),
            lambda r: r[base + "/aggregated/instances"].update(unreachables=["zones/unreachable-zone"]),
            lambda r: r["https://cloudbilling.googleapis.com/v1/projects/" + project + "/billingInfo"].update(billingEnabled=False),
            lambda r: r["https://cloudresourcemanager.googleapis.com/v3/projects/" + project + ":testIamPermissions"].update(permissions=["compute.instances.create"]),
            lambda r: r[base + "/zones/us-east1-b/machineTypes/fixture-size"].update(architecture="ARM64"),
            lambda r: r[base + "/regions/us-east1"]["quotas"][0].update(usage=8),
        )
        for mutate in mutations:
            routes = self.routes()
            mutate(routes)
            with self.assertRaises(preflight.PreflightError):
                self.adapter(routes).preflight(PREFIX)

    def test_service_account_identity_is_not_inferred_from_source_project(self):
        self.adapter().access_token()  # central service account may deploy to another project
        value = json.loads(self.credential)
        value["client_email"] = "wrong-account"
        http = FakeHTTP(self.routes())
        with self.assertRaisesRegex(preflight.PreflightError, "account_identity_mismatch"):
            preflight.Google(config()["providers"]["gcp"], json.dumps(value), http)
        self.assertEqual(http.calls, [])


class FakeAWSClient:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __getattr__(self, operation):
        def call(**kwargs):
            self.calls.append((operation, kwargs))
            result = self.routes[operation]
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        return call


class AWSTest(unittest.TestCase):
    def adapter(self, account="123456789012"):
        value = config()["providers"]["aws"]
        value["region"] = "us-east-1"
        self.sts = FakeAWSClient({"get_caller_identity": {"Account": account}})
        self.ec2 = FakeAWSClient({
            "describe_instance_types": {"InstanceTypes": [{"InstanceType": "fixture-size", "MemoryInfo": {"SizeInMiB": 8192}, "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]}}]},
            "describe_availability_zones": {"AvailabilityZones": [{"ZoneName": "us-east-1a"}]},
            "describe_instance_type_offerings": {"InstanceTypeOfferings": [{"Location": "us-east-1a", "InstanceType": "fixture-size"}]},
            **{name: {key: []} for name, key in (("describe_vpcs", "Vpcs"), ("describe_subnets", "Subnets"), ("describe_route_tables", "RouteTables"),
                ("describe_internet_gateways", "InternetGateways"), ("describe_security_groups", "SecurityGroups"), ("describe_volumes", "Volumes"),
                ("describe_instances", "Reservations"), ("describe_key_pairs", "KeyPairs"))},
        })
        return preflight.AWS(value, {"access_key": SECRET, "secret_key": SECRET}, lambda name: {"sts": self.sts, "ec2": self.ec2}[name])

    def test_exact_account_size_offering_and_unused_vpc_prefix(self):
        self.assertEqual(self.adapter().preflight(PREFIX)["identity"], "verified")
        self.assertTrue(all(operation.startswith("describe_") for operation, _ in self.ec2.calls))

    def test_wrong_account_fails_before_any_ec2_read(self):
        with self.assertRaisesRegex(preflight.PreflightError, "account_identity_mismatch"):
            self.adapter("987654321098").preflight(PREFIX)
        self.assertEqual(self.ec2.calls, [])

    def test_terminated_tombstone_allowed_but_live_instance_or_orphan_disk_fail(self):
        for kind in ("terminated", "running", "orphan_disk"):
            adapter = self.adapter()
            if kind == "orphan_disk":
                self.ec2.routes["describe_volumes"] = {"Volumes": [{"VolumeId": "vol-owned-orphan"}]}
            else:
                self.ec2.routes["describe_instances"] = {"Reservations": [{"Instances": [{"State": {"Name": kind}}]}]}
            if kind == "terminated":
                adapter.preflight(PREFIX)
            else:
                with self.assertRaisesRegex(preflight.PreflightError, "resource_prefix_already_exists"):
                    adapter.preflight(PREFIX)

    def test_provider_error_text_and_sdk_exception_are_discarded(self):
        adapter = self.adapter()
        self.ec2.routes["describe_instance_types"] = RuntimeError(SECRET)
        with self.assertRaisesRegex(preflight.PreflightError, "aws_request_failed") as caught:
            adapter.preflight(PREFIX)
        self.assertNotIn(SECRET, str(caught.exception))

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "optional live botocore dependency unavailable")
    def test_actual_sdk_models_explicit_credentials_and_fixed_endpoints(self):
        from botocore.stub import Stubber
        value = config()["providers"]["aws"]
        value["region"] = "us-east-1"
        with patch.dict(preflight.os.environ, {"AWS_PROFILE": "nonexistent-personal-profile", "AWS_CONFIG_FILE": "/missing/operator-config",
                                               "AWS_SHARED_CREDENTIALS_FILE": "/missing/operator-credentials",
                                               "AWS_ENDPOINT_URL": "https://evil.test"}):
            adapter = preflight.AWS(value, {"access_key": "fixture-access-key", "secret_key": SECRET})
        self.assertEqual(adapter.sts.meta.endpoint_url, "https://sts.us-east-1.amazonaws.com")
        self.assertEqual(adapter.ec2.meta.endpoint_url, "https://ec2.us-east-1.amazonaws.com")
        self.assertEqual(adapter.ec2._request_signer._credentials.access_key, "fixture-access-key")
        self.assertEqual(adapter.ec2.meta.config.retries["total_max_attempts"], 1)
        self.assertEqual(adapter.ec2.meta.config.proxies, {})
        # Validate actual SDK request shapes, without a socket or live key.
        with Stubber(adapter.sts) as sts, Stubber(adapter.ec2) as ec2:
            sts.add_response("get_caller_identity", {"Account": "123456789012"})
            ec2.add_response("describe_instance_types", {"InstanceTypes": [{"InstanceType": "t3.large", "MemoryInfo": {"SizeInMiB": 8192},
                             "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]}}]}, {"InstanceTypes": ["t3.large"]})
            ec2.add_response("describe_availability_zones", {"AvailabilityZones": [{"ZoneName": "us-east-1a"}]}, {"Filters": [{"Name": "state", "Values": ["available"]}]})
            ec2.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [{"Location": "us-east-1a", "InstanceType": "t3.large"}]},
                             {"LocationType": "availability-zone", "Filters": [{"Name": "instance-type", "Values": ["t3.large"]}, {"Name": "location", "Values": ["us-east-1a"]}]})
            filters = [{"Name": "tag:Name", "Values": [PREFIX + "*"]}]
            for operation, key in (("describe_vpcs", "Vpcs"), ("describe_subnets", "Subnets"), ("describe_route_tables", "RouteTables"),
                                   ("describe_internet_gateways", "InternetGateways"), ("describe_security_groups", "SecurityGroups"),
                                   ("describe_volumes", "Volumes"), ("describe_instances", "Reservations")):
                ec2.add_response(operation, {key: []}, {"Filters": filters})
            ec2.add_response("describe_key_pairs", {"KeyPairs": []}, {"Filters": [{"Name": "key-name", "Values": [PREFIX + "*"]}]})
            adapter.cloud["size"] = "t3.large"
            self.assertEqual(adapter.preflight(PREFIX)["identity"], "verified")
            sts.assert_no_pending_responses()
            ec2.assert_no_pending_responses()


class OrchestrationTest(unittest.TestCase):
    def test_invalid_model_route_rejected_before_cloud_secret_or_request(self):
        value = config(); value["models"]["openclaw"] = "openai/../../private"
        reader, http = Mock(), FakeHTTP({})
        with self.assertRaisesRegex(preflight.PolicyError, "model_id_unsupported"):
            preflight.preflight(value, "digitalocean", PREFIX, reader, http)
        reader.assert_not_called(); self.assertEqual(http.calls, [])

    def test_only_selected_cloud_and_second_peer_fields_are_read_once(self):
        value = config()
        routes = do_routes()
        routes["https://api.tailscale.com/api/v2/tailnet/fixture-tailnet/devices"] = {"devices": [{"name": "runner.fixture.ts.net.", "authorized": True}]}
        reads = []
        def reader(reference):
            reads.append(reference)
            return SECRET
        model_http = FakeModels()
        result = preflight.preflight(value, "digitalocean", PREFIX, reader, FakeHTTP(routes), model_http=model_http)
        self.assertEqual(len(reads), 1)  # fixture references are identical
        self.assertEqual(result["status"], "read_checks_passed")
        self.assertFalse(result["launch_ready"])
        self.assertEqual(result["live_matrix"], "not_tested")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn("node_delete_permission", result["tailnet"]["not_verified"])
        self.assertEqual(len(model_http.calls), 2)
        self.assertEqual(result["models"]["launch_gate"], "unverified")

    def test_unauthorized_or_wrong_peer_is_not_accepted(self):
        value = config()["tailnet"]
        url = "https://api.tailscale.com/api/v2/tailnet/fixture-tailnet/devices"
        for row in ({"name": "runner.fixture.ts.net", "authorized": False}, {"name": "other.fixture.ts.net", "authorized": True}):
            with self.assertRaisesRegex(preflight.PreflightError, "authorized_second_peer_not_found"):
                preflight.check_tailnet(value, SECRET, FakeHTTP({url: {"devices": [row]}}))

    def test_old_node_with_attempt_prefix_blocks_capture_of_existing_peer(self):
        value = config()["tailnet"]
        url = "https://api.tailscale.com/api/v2/tailnet/fixture-tailnet/devices"
        for row in ({"name": PREFIX + ".fixture.ts.net", "hostname": PREFIX},
                    {"name": "renamed.fixture.ts.net", "hostname": PREFIX + "-old"}):
            http = FakeHTTP({url: {"devices": [{"name": value["runner_dns"], "authorized": True}, row]}})
            with self.assertRaisesRegex(preflight.PreflightError, "tailnet_prefix_already_exists"):
                preflight.check_tailnet(value, SECRET, http, PREFIX)


if __name__ == "__main__":
    unittest.main()
