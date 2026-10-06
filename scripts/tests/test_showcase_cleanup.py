import copy
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_cleanup as cleanup
import test_showcase_preflight as fixtures
from test_showcase_policy import config
import test_showcase_policy as policy_fixtures

PREFIX = "as7-fixture01"
SECRET = fixtures.SECRET


def tailnet_route(nodes=()):
    return {"https://api.tailscale.com/api/v2/tailnet/fixture-tailnet/devices": {
        "devices": [{"id": "runner-peer-id", "name": "runner.fixture.ts.net", "hostname": "runner", "authorized": True}, *nodes]}}


def do_http(resources=True):
    routes = fixtures.do_routes()
    routes.update(tailnet_route([{"id": "owned-node-id", "name": PREFIX + ".fixture.ts.net", "hostname": PREFIX}] if resources else []))
    if resources:
        routes[cleanup.DigitalOcean.base + "/droplets"]["droplets"] = [{"id": 1234, "name": PREFIX}]
        routes[cleanup.DigitalOcean.base + "/volumes"]["volumes"] = [{"id": "fixture-disk-id", "name": PREFIX + "-data"}]
    return fixtures.FakeHTTP(routes)


class ReceiptAndRecordTest(unittest.TestCase):
    def result(self, http, previous=None):
        value = config()
        adapter = cleanup.DigitalOcean(value["providers"]["digitalocean"], SECRET, http)
        return cleanup.query(value, "digitalocean", PREFIX, adapter, http, SECRET, previous, now=lambda: 20000)

    def test_capture_and_independent_empty_query_produce_scoped_receipt(self):
        record = cleanup.capture(self.result(do_http()), config())
        self.assertEqual(len(record["resources"]), 2)
        self.assertEqual(record["tailnet"]["devices"][0]["id"], "owned-node-id")
        deleted = self.result(do_http(False), record)
        receipt = cleanup.receipt(deleted, record, True)
        self.assertEqual(receipt["owned_ids_checked"], 2)
        self.assertEqual(receipt["tailnet_nodes_checked"], 1)
        self.assertEqual(receipt["project_id"], config()["providers"]["digitalocean"]["project_id"])
        self.assertNotIn(SECRET, json.dumps(receipt))

    def test_recapture_after_partial_deletion_never_forgets_disk_or_node(self):
        record = cleanup.capture(self.result(do_http()), config())
        recaptured = cleanup.capture(self.result(do_http(False), record), config(), record)
        self.assertEqual(recaptured["resources"], record["resources"])
        self.assertEqual(recaptured["tailnet"]["devices"], record["tailnet"]["devices"])

    def test_renamed_known_disk_still_prevents_cleanup_receipt(self):
        record = cleanup.capture(self.result(do_http()), config())
        http = do_http(False)
        http.routes[cleanup.DigitalOcean.base + "/volumes"]["volumes"] = [{"id": "fixture-disk-id", "name": "renamed-disk"}]
        with self.assertRaisesRegex(cleanup.PreflightError, "owned_or_unknown_resources_remain"):
            cleanup.receipt(self.result(http, record), record, True)

    def test_leftover_project_urn_or_tailnet_node_is_not_deleted_evidence(self):
        record = cleanup.capture(self.result(do_http()), config())
        for kind in ("urn", "node"):
            http = do_http(False)
            if kind == "urn":
                project = config()["providers"]["digitalocean"]["project_id"]
                http.routes[cleanup.DigitalOcean.base + "/projects/" + project + "/resources"]["resources"] = [{"urn": "do:volume:fixture-disk-id"}]
            else:
                http.routes.update(tailnet_route([{"id": "owned-node-id", "name": "renamed.fixture.ts.net", "hostname": "renamed"}]))
            with self.assertRaisesRegex(cleanup.PreflightError, "owned_or_unknown_resources_remain"):
                cleanup.receipt(self.result(http, record), record, True)

    def test_prefix_boundary_preserves_similarly_named_foreign_resources(self):
        http = do_http(False)
        http.routes[cleanup.DigitalOcean.base + "/droplets"]["droplets"] = [{"id": 888, "name": PREFIX + "other"}]
        http.routes.update(tailnet_route([{"id": "foreign-node-id", "name": PREFIX + "other.fixture.ts.net", "hostname": PREFIX + "other"}]))
        result = self.result(http)
        self.assertEqual(result["resources"], [])
        self.assertEqual(result["nodes"], [])

    def test_changed_region_zone_project_or_duplicate_record_fails_before_api_reads(self):
        record = cleanup.capture(self.result(do_http()), config())
        for key in ("region", "zone", "project_id"):
            changed = copy.deepcopy(record)
            changed["scope"][key] = "different-scope"
            http = do_http(False)
            with self.assertRaisesRegex(cleanup.PreflightError, "inventory_scope_mismatch"):
                self.result(http, changed)
            self.assertEqual(http.calls, [])
        changed = copy.deepcopy(record)
        changed["resources"].append(changed["resources"][0])
        with self.assertRaisesRegex(cleanup.PreflightError, "invalid_inventory"):
            cleanup.validate_record(changed, config(), "digitalocean", PREFIX)

    def test_failed_cloud_or_tailnet_query_never_produces_receipt(self):
        record = cleanup.capture(self.result(do_http()), config())
        for endpoint in (cleanup.DigitalOcean.base + "/volumes", "https://api.tailscale.com/api/v2/tailnet/fixture-tailnet/devices"):
            http = do_http(False)
            http.routes[endpoint] = cleanup.PreflightError("provider_http_403")
            with self.assertRaisesRegex(cleanup.PreflightError, "provider_http_403"):
                self.result(http, record)

    def test_registered_runner_cannot_be_captured_as_owned_node(self):
        value = config()["tailnet"]
        value["runner_dns"] = PREFIX + ".fixture.ts.net"
        http = fixtures.FakeHTTP(tailnet_route([{"id": "test-runner-id", "name": value["runner_dns"], "hostname": PREFIX}]))
        with self.assertRaisesRegex(cleanup.PreflightError, "runner_peer_must_be_preserved"):
            cleanup.tailnet_inventory(value, SECRET, http, PREFIX, [])

    def test_receipt_requires_verified_identity_successful_queries_and_fresh_time(self):
        record = cleanup.capture(self.result(do_http()), config())
        for changes in ({"identity": "declared_only"}, {"cloud_queries": 0}, {"tailnet_queries": 0}, {"verified_at": 19999}):
            with self.assertRaises(cleanup.PreflightError):
                cleanup.receipt({**self.result(do_http(False), record), **changes}, record, True)
        with self.assertRaisesRegex(cleanup.PreflightError, "terraform_state_removal_unverified"):
            cleanup.receipt(self.result(do_http(False), record), record)


class HetznerInventoryTest(unittest.TestCase):
    def result(self, resources=True, previous=None):
        routes = fixtures.hcloud_routes()
        routes.update(tailnet_route())
        if resources:
            routes[cleanup.Hetzner.base + "/servers"]["servers"] = [{"id": 1, "name": PREFIX,
                "public_net": {"ipv4": {"id": 2}, "ipv6": {"id": 3}}}]
            routes[cleanup.Hetzner.base + "/primary_ips"]["primary_ips"] = [{"id": 2, "name": "auto-ipv4"}, {"id": 3, "name": "auto-ipv6"}]
        http = fixtures.FakeHTTP(routes)
        adapter = cleanup.Hetzner(config()["providers"]["hetzner"], SECRET, http)
        return cleanup.query(config(), "hetzner", PREFIX, adapter, http, SECRET, previous, now=lambda: 20000)

    def test_implicit_ipv4_and_ipv6_ids_are_recorded_with_server_parent(self):
        result = self.result()
        record = cleanup.capture(result, config())
        ips = [row for row in record["resources"] if row["kind"] == "primary_ip"]
        self.assertEqual({row["id"] for row in ips}, {"2", "3"})
        self.assertTrue(all(row["parent"] == {"kind": "server", "id": "1"} for row in ips))

    def test_empty_token_scope_does_not_prove_declared_account_project_mapping(self):
        record = cleanup.capture(self.result(), config())
        with self.assertRaisesRegex(cleanup.PreflightError, "account_project_mapping_unverified"):
            cleanup.receipt(self.result(False, record), record, True)

    def test_unmapped_primary_ip_is_reported_without_claiming_ownership(self):
        routes = fixtures.hcloud_routes()
        routes.update(tailnet_route())
        routes[cleanup.Hetzner.base + "/primary_ips"]["primary_ips"] = [{"id": 99, "name": "operator-ip"}]
        http = fixtures.FakeHTTP(routes)
        adapter = cleanup.Hetzner(config()["providers"]["hetzner"], SECRET, http)
        result = cleanup.query(config(), "hetzner", PREFIX, adapter, http, SECRET)
        self.assertEqual(result["resources"], [])
        self.assertEqual(result["unknown_resources"][0]["id"], "99")


class FakeAWSError(Exception):
    def __init__(self, code, status=400):
        super().__init__(SECRET)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeAWS:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            value = self.routes[name]
            if callable(value):
                value = value(kwargs)
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        return call


class AWSInventoryTest(unittest.TestCase):
    def adapter(self, populated=True):
        value = config()["providers"]["aws"]
        value["region"] = "us-east-1"
        routes = {operation: {key: []} for operation, key, *_ in cleanup.AWS_TYPES.values()}
        if populated:
            routes["describe_instances"] = {"Reservations": [{"Instances": [{"InstanceId": "i-1234", "State": {"Name": "running"},
                "Tags": [{"Key": "Name", "Value": PREFIX}], "RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-1234"}}]}]}]}
            routes["describe_vpcs"] = {"Vpcs": [{"VpcId": "vpc-1234", "Tags": [{"Key": "Name", "Value": PREFIX + "-vpc"}]}]}
            def volumes(kwargs):
                if "VolumeIds" in kwargs:
                    return {"Volumes": [{"VolumeId": "vol-1234", "Attachments": [{"InstanceId": "i-1234"}]}]}
                return {"Volumes": []}
            routes["describe_volumes"] = volumes
            def groups(kwargs):
                if kwargs.get("Filters", [{}])[0].get("Name") == "vpc-id":
                    return {"SecurityGroups": [{"GroupId": "sg-1234", "VpcId": "vpc-1234"}]}
                return {"SecurityGroups": []}
            routes["describe_security_groups"] = groups
        self.ec2 = FakeAWS(routes)
        sts = FakeAWS({"get_caller_identity": {"Account": "123456789012"}})
        return cleanup.AWS(value, {"access_key": SECRET, "secret_key": SECRET}, lambda name: sts if name == "sts" else self.ec2)

    def test_untagged_boot_volume_and_default_vpc_child_are_captured(self):
        rows, unknown = cleanup.aws_inventory(self.adapter(), PREFIX, [])
        records = cleanup.normalized(rows)
        self.assertEqual(unknown, [])
        boot = next(row for row in records if row["kind"] == "volume")
        self.assertEqual(boot["parent"], {"kind": "instance", "id": "i-1234"})
        group = next(row for row in records if row["kind"] == "security_group")
        self.assertEqual(group["parent"], {"kind": "vpc", "id": "vpc-1234"})

    def test_detached_boot_disk_is_found_by_exact_id_after_vm_and_tags_disappear(self):
        original = cleanup.normalized(cleanup.aws_inventory(self.adapter(), PREFIX, [])[0])
        adapter = self.adapter(False)
        for row in original:
            operation, _, _, _, codes = cleanup.AWS_TYPES[row["kind"]]
            def lookup(kwargs, kind=row["kind"], codes=codes):
                if "Filters" in kwargs:
                    return {cleanup.AWS_TYPES[kind][1]: []}
                if kind == "volume":
                    return {"Volumes": [{"VolumeId": "vol-1234", "Attachments": []}]}
                raise FakeAWSError(codes[0])
            self.ec2.routes[operation] = lookup
        rows, _ = cleanup.aws_inventory(adapter, PREFIX, original)
        self.assertEqual([(row["kind"], row["id"]) for row in rows], [("volume", "vol-1234")])

    def test_permission_error_or_empty_malformed_response_is_not_absence(self):
        adapter = self.adapter(False)
        for value in (FakeAWSError("InvalidVolume.NotFound", 403), TimeoutError(SECRET), {"Volumes": []}):
            self.ec2.routes["describe_volumes"] = value
            with self.assertRaises(cleanup.PreflightError) as caught:
                cleanup.aws_lookup(adapter, {"kind": "volume", "id": "vol-1234"})
            self.assertNotIn(SECRET, str(caught.exception))

    def test_terminated_instance_tombstone_is_absent_but_disk_remains_separate(self):
        adapter = self.adapter(False)
        self.ec2.routes["describe_instances"] = {"Reservations": [{"Instances": [{"InstanceId": "i-1234", "State": {"Name": "terminated"}}]}]}
        self.assertIsNone(cleanup.aws_lookup(adapter, {"kind": "instance", "id": "i-1234"}))

    def test_foreign_nonroot_disk_is_not_adopted_as_an_owned_child(self):
        for malformed in (False, True):
            adapter = self.adapter()
            instance = self.ec2.routes["describe_instances"]["Reservations"][0]["Instances"][0]
            if malformed:
                instance.pop("RootDeviceName")
                instance["BlockDeviceMappings"][0].pop("DeviceName")
            else:
                instance["BlockDeviceMappings"][0]["DeviceName"] = "/dev/sdf"
            with self.assertRaisesRegex(cleanup.PreflightError, "foreign_attached_disk"):
                cleanup.aws_inventory(adapter, PREFIX, [])

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "optional live botocore dependency unavailable")
    def test_actual_sdk_all_exact_id_shapes_and_notfound_codes(self):
        from botocore.stub import Stubber
        value = config()["providers"]["aws"]
        value["region"] = "us-east-1"
        adapter = cleanup.AWS(value, {"access_key": "fixture-access-key", "secret_key": SECRET})
        with Stubber(adapter.ec2) as stub:
            for kind, (operation, _, _, parameter, codes) in cleanup.AWS_TYPES.items():
                stub.add_client_error(operation, service_error_code=codes[0], service_message=SECRET, http_status_code=400,
                                      expected_params={parameter: ["fixture-exact-id"]})
                self.assertIsNone(cleanup.aws_lookup(adapter, {"kind": kind, "id": "fixture-exact-id"}))
            stub.assert_no_pending_responses()


class GoogleInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.GoogleTest.setUpClass()

    def result(self, populated=True, previous=None, mutate=None):
        routes = fixtures.GoogleTest().routes()
        routes.update(tailnet_route())
        base = "https://compute.googleapis.com/compute/v1/projects/agent-stack-fixture"
        def row(name, kind, identifier):
            return {"id": identifier, "name": name, "selfLink": base + "/zones/us-east1-b/" + kind + "/" + name}
        routes[base + "/global/networks"] = {}
        routes[base + "/global/firewalls"] = {}
        routes[base + "/aggregated/subnetworks"] = {}
        if populated:
            disk = row("provider-boot-disk", "disks", "2")
            instance = row(PREFIX, "instances", "1")
            instance["disks"] = [{"boot": True, "source": disk["selfLink"]}]
            routes[base + "/aggregated/instances"] = {"items": {"zones/us-east1-b": {"instances": [instance]}}}
            routes[base + "/aggregated/disks"] = {"items": {"zones/us-east1-b": {"disks": [disk]}}}
        else:
            routes[base + "/aggregated/instances"] = {}
            routes[base + "/aggregated/disks"] = {}
        if mutate:
            mutate(routes, base)
        http = fixtures.FakeHTTP(routes)
        adapter = cleanup.Google(config()["providers"]["gcp"], fixtures.GoogleTest.credential, http, now=lambda: 20000)
        return cleanup.query(config(), "gcp", PREFIX, adapter, http, SECRET, previous, now=lambda: 20000)

    def test_boot_disk_without_prefix_is_captured_and_checked_after_vm_deletion(self):
        record = cleanup.capture(self.result(), config())
        disk = next(row for row in record["resources"] if row["kind"] == "disk")
        self.assertEqual(disk["parent"], {"kind": "instance", "id": "1"})
        def orphan(routes, base):
            routes[base + "/aggregated/disks"] = {"items": {"zones/us-east1-b": {"disks": [{"id": "2", "name": "renamed-boot-disk",
                "selfLink": base + "/zones/us-east1-b/disks/renamed-boot-disk"}]}}}
        with self.assertRaisesRegex(cleanup.PreflightError, "owned_or_unknown_resources_remain"):
            cleanup.receipt(self.result(False, record, orphan), record, True)
        self.assertEqual(cleanup.receipt(self.result(False, record), record, True)["owned_ids_checked"], 2)

    def test_foreign_disk_link_or_foreign_nonboot_attachment_fails(self):
        for kind in ("project", "nonboot"):
            def mutate(routes, base):
                instance = routes[base + "/aggregated/instances"]["items"]["zones/us-east1-b"]["instances"][0]
                if kind == "project":
                    instance["disks"][0]["source"] = instance["disks"][0]["source"].replace("agent-stack-fixture", "operator-project")
                else:
                    instance["disks"][0]["boot"] = False
            with self.assertRaises(cleanup.PreflightError):
                self.result(mutate=mutate)

    def test_project_identity_and_unreachable_zone_fail_verification(self):
        record = cleanup.capture(self.result(), config())
        def unreachable(routes, base):
            routes[base + "/aggregated/disks"] = {"unreachables": ["zones/other-zone"]}
        with self.assertRaisesRegex(cleanup.PreflightError, "incomplete_project_inventory"):
            self.result(False, record, unreachable)


class PrivateFileAndCLITest(unittest.TestCase):
    def test_cli_capture_then_verify_preserves_record_and_updates_same_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = config()
            inputs = root / "inputs.json"
            cleanup.private_write(inputs, value)
            ledger = cleanup.Reservations(root / "ledger", value)
            ledger.reserve("digitalocean", PREFIX, policy_fixtures.quote("digitalocean"), now=policy_fixtures.NOW)
            args = [cleanup.__file__, "--config", str(inputs), "--provider", "digitalocean", "--run-id", PREFIX,
                    "--inventory", str(root / "inventory.json"), "--ledger", str(root / "ledger"), "--phase", "capture"]
            original_query = cleanup.query
            def bounded_clock(*args, **kwargs):
                return original_query(*args, **kwargs, now=lambda: policy_fixtures.NOW + 200)
            with patch.dict(cleanup.os.environ, {"AGENT_STACK_LIVE_TESTS": "1"}), patch.object(cleanup, "read_secret", return_value=SECRET) as secret:
                with patch.object(sys, "argv", args), patch.object(cleanup, "JsonHTTP", return_value=do_http()), patch.object(cleanup, "query", side_effect=bounded_clock):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cleanup.main(), 0)
                    self.assertEqual(json.loads(output.getvalue())["status"], "inventory_captured")
                    self.assertEqual(secret.call_count, 1)
                    self.assertNotIn("owned-node-id", output.getvalue())
                record = cleanup.private_read(root / "inventory.json")
                self.assertEqual(len(record["resources"]), 2)
                cleanup.private_write(root / "terraform.tfstate", {"version": 4, "resources": []})
                verify = [*args[:-1], "verify", "--state", str(root / "terraform.tfstate")]
                with patch.object(sys, "argv", verify), patch.object(cleanup, "JsonHTTP", return_value=do_http(False)), patch.object(cleanup, "query", side_effect=bounded_clock):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cleanup.main(), 0)
                    self.assertEqual(json.loads(output.getvalue())["status"], "cleanup_verified")
                    self.assertNotIn(SECRET, output.getvalue())
                self.assertEqual(cleanup.private_read(root / "inventory.json"), record)
                saved = cleanup.private_read(root / "ledger/reservations.json")
                self.assertEqual(saved["attempts"][0]["status"], "cleaned")
                self.assertEqual(saved["attempts"][0]["reserved_usd"], "2.00")

    def test_state_empty_requires_private_valid_state_and_no_managed_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terraform.tfstate"
            for state in ({"version": 4, "resources": []}, {"version": 4, "resources": [{"mode": "data", "instances": [{"attributes": {"private": SECRET}}]}]}):
                cleanup.private_write(path, state)
                self.assertTrue(cleanup.state_empty(path))
            for state in ({"version": 4, "resources": [{"mode": "managed", "instances": [{"attributes": {"private": SECRET}}]}]},
                          {"version": 4, "resources": [{}]}, {"version": 5, "resources": []}):
                cleanup.private_write(path, state)
                with self.assertRaises(cleanup.PreflightError) as caught:
                    cleanup.state_empty(path)
                self.assertNotIn(SECRET, str(caught.exception))

    def test_private_atomic_record_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            cleanup.private_write(path, {"owned_ids": []})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cleanup.private_read(path), {"owned_ids": []})
            target = Path(directory) / "operator-file"
            target.write_text("preserve")
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(cleanup.PreflightError):
                cleanup.private_write(path, {"must": "not-write"})
            self.assertEqual(target.read_text(), "preserve")

    def test_cli_requires_opt_in_and_active_attempt_before_any_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs.json"
            inputs.write_text(json.dumps(config()))
            inputs.chmod(0o600)
            args = [sys.executable, cleanup.__file__, "--config", str(inputs), "--provider", "digitalocean", "--run-id", PREFIX,
                    "--inventory", str(root / "inventory.json"), "--ledger", str(root / "ledger"), "--phase", "capture"]
            env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(args, capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(result.stdout)["reason"], "live_opt_in_required")
            env["AGENT_STACK_LIVE_TESTS"] = "1"
            result = subprocess.run(args, capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(result.stdout)["reason"], "active_attempt_scope_required")
            self.assertEqual(result.stderr, "")
            self.assertFalse((root / "inventory.json").exists())


if __name__ == "__main__":
    unittest.main()
