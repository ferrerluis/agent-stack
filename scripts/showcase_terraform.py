#!/usr/bin/env python3
"""Bounded saved-plan deployment and ownership-checked Terraform destruction.

Only the future full controller may supply measured launch readiness. The public
live CLI currently exposes planning and cleanup, never a readiness override.
Plans/state/recovery records stay private. Raw Terraform output is never emitted.
"""
import base64
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import types
from urllib.request import Request
from urllib.error import HTTPError, URLError

from showcase_attempt import (HASH_PATTERN, command, isolated_environment, private_directory,
                              remaining_timeout, verify_prepared, workspace_password_hash)
from showcase_cleanup import (capture, identity, owned_name, private_read, private_write, query,
                              receipt, scope, state_empty, validate_record)
from showcase_policy import (PolicyError, require, validate_fresh_plan,
                             validate_inputs)
from showcase_preflight import (AWS, DigitalOcean, Google, Hetzner, JsonHTTP, PreflightError,
                                check_dependencies, check_tailnet, field, objects, read_secret, segment, text)

KINDS = {
    "aws": {"aws_vpc": "vpc", "aws_subnet": "subnet", "aws_route_table": "route_table", "aws_internet_gateway": "internet_gateway",
            "aws_security_group": "security_group", "aws_key_pair": "key_pair", "aws_ebs_volume": "volume", "aws_instance": "instance"},
    "digitalocean": {"digitalocean_droplet": "droplet", "digitalocean_volume": "volume", "digitalocean_vpc": "vpc",
                     "digitalocean_firewall": "firewall", "digitalocean_ssh_key": "ssh_key"},
    "gcp": {"google_compute_instance": "instance", "google_compute_disk": "disk", "google_compute_network": "network",
            "google_compute_subnetwork": "subnetwork", "google_compute_firewall": "firewall"},
    "hetzner": {"hcloud_server": "server", "hcloud_volume": "volume", "hcloud_firewall": "firewall", "hcloud_ssh_key": "ssh_key"},
}
LOCAL_TYPES = {"random_password", "terraform_data", "local_file"}
LAUNCH_CHECKS = {"local_tests", "container_tests", "permissions_and_quotas", "prices", "model_access", "tailnet_peer_and_cleanup"}


def exclusive(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        private_directory(self.run)
        fd = os.open(self.run / "controller.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            metadata = os.fstat(lock.fileno())
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid() and metadata.st_mode & 0o077 == 0, "private_lock_required")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PolicyError("attempt_busy") from None
            return method(self, *args, **kwargs)
    return invoke


class CloudSession:
    """Selected provider credentials read once and held only in memory."""
    def __init__(self, config, provider, secret_reader=read_secret, http=None, aws_factory=None):
        validate_inputs(config, provider)
        check_dependencies(provider, aws_factory)
        self.config, self.provider, self.reader = config, provider, secret_reader
        self.http = JsonHTTP() if http is None else http
        self.secrets = {}
        cloud = config["providers"][provider]
        self.credentials = {key: self.secret(value) for key, value in cloud["credential_refs"].items()}
        if provider == "aws":
            self.adapter = AWS(cloud, self.credentials, aws_factory)
        elif provider == "gcp":
            self.adapter = Google(cloud, self.credentials["service_account_json"], self.http)
        else:
            self.adapter = {"digitalocean": DigitalOcean, "hetzner": Hetzner}[provider](cloud, self.credentials["token"], self.http)

    def secret(self, reference):
        if reference not in self.secrets:
            self.secrets[reference] = self.reader(reference)
        return self.secrets[reference]

    def preflight(self, prefix):
        cloud = self.adapter.preflight(prefix)
        tailnet = check_tailnet(self.config["tailnet"], self.secret(self.config["tailnet"]["cleanup_key_ref"]), self.http, prefix)
        return {"schema": 1, "provider": self.provider, "status": "read_checks_passed", "cloud": cloud, "tailnet": tailnet,
                "launch_ready": False, "live_matrix": "not_tested"}

    def model_metadata(self, http=None):
        from showcase_models import check_models
        return check_models(self.config, self.secret, http=http)

    def environment(self, run, min_lifetime=0):
        env = isolated_environment(run)
        if self.provider == "aws":
            env.update(AWS_ACCESS_KEY_ID=self.credentials["access_key"], AWS_SECRET_ACCESS_KEY=self.credentials["secret_key"],
                       AWS_REGION=self.adapter.cloud["region"], AWS_DEFAULT_REGION=self.adapter.cloud["region"])
            if "session_token" in self.credentials:
                env["AWS_SESSION_TOKEN"] = self.credentials["session_token"]
        elif self.provider == "gcp":
            if self.adapter.expires_at - self.adapter.now() < min_lifetime:
                self.adapter.token = None
            env["GOOGLE_OAUTH_ACCESS_TOKEN"] = self.adapter.access_token()
            require(self.adapter.expires_at - self.adapter.now() >= min_lifetime, "google_token_lifetime_insufficient")
        else:
            env["DIGITALOCEAN_TOKEN" if self.provider == "digitalocean" else "HCLOUD_TOKEN"] = self.credentials["token"]
        return env

    def inventory(self, prefix, previous=None):
        return query(self.config, self.provider, prefix, self.adapter, self.http,
                     self.secret(self.config["tailnet"]["cleanup_key_ref"]), previous)

    def delete_nodes(self, record):
        validate_record(record, self.config, self.provider, record["run_id"])
        # The original node IDs were captured while the VM existed. Never
        # discover a new deletion target after destroy or delete the runner.
        current = self.inventory(record["run_id"], record)
        by_id = {row["id"]: row for row in current["nodes"]}
        for node in record["tailnet"]["devices"]:
            if node["id"] not in by_id:
                continue
            require(node["name"] != self.config["tailnet"]["runner_dns"] and
                    node["name"].split(".", 1)[0] == record["run_id"], "unexpected_tailnet_deletion_target")
            tailnet_delete(self.http, self.secret(self.config["tailnet"]["cleanup_key_ref"]), node["id"])


def tailnet_delete(http, key, identifier):
    """Only the exact captured device ID; independent absence checks follow."""
    require(text(key) and isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier), "invalid_tailnet_deletion_target")
    url = "https://api.tailscale.com/api/v2/device/" + segment(identifier)
    authorization = "Basic " + base64.b64encode((key + ":").encode()).decode()
    request = Request(url, method="DELETE", headers={"Authorization": authorization, "Accept": "application/json"})
    try:
        with http.opener.open(request, timeout=20) as response:
            require(response.status in (200, 204) and len(response.read(4097)) <= 4096, "tailnet_delete_failed")
    except HTTPError as error:
        error.close()
        raise PreflightError("tailnet_delete_failed") from None
    except (URLError, TimeoutError, OSError):
        raise PreflightError("tailnet_delete_failed") from None
    http.queries += 1


def state_resources(state):
    require(isinstance(state, dict) and type(state.get("version")) is int and state["version"] == 4
            and isinstance(state.get("resources"), list), "invalid_terraform_state")
    rows = {}
    for resource in state["resources"]:
        require(isinstance(resource, dict) and resource.get("mode") in ("managed", "data")
                and isinstance(resource.get("instances"), list), "invalid_terraform_state")
        if resource["mode"] == "data":
            continue
        require(text(resource.get("type")) and text(resource.get("name")), "invalid_terraform_state")
        address = (resource["module"] + "." if resource.get("module") else "") + resource["type"] + "." + resource["name"]
        for instance in resource["instances"]:
            require(isinstance(instance, dict) and isinstance(instance.get("attributes"), dict) and not instance.get("deposed"), "invalid_terraform_state")
            suffix = ""
            if "index_key" in instance:
                require(type(instance["index_key"]) in (str, int), "invalid_terraform_state")
                suffix = "[" + json.dumps(instance["index_key"]) + "]"
            full = address + suffix
            require(full not in rows and type(instance["attributes"].get("id")) in (str, int)
                    and text(str(instance["attributes"]["id"])), "invalid_terraform_state")
            rows[full] = {"type": resource["type"], "values": instance["attributes"]}
    return rows


def guard_state(state, plan, config, provider, prefix, run, inventory, adapter=None):
    validate_fresh_plan(plan, provider, prefix, run)
    validate_record(inventory, config, provider, prefix)
    planned = {row["address"]: row for row in plan["resource_changes"] if row.get("mode") != "data"}
    rows = state_resources(state)
    owned = {(row["kind"], row["id"]) for row in inventory["resources"]}
    for address, row in rows.items():
        require(address in planned and planned[address]["type"] == row["type"], "unplanned_state_resource")
        values, kind = row["values"], row["type"]
        if kind in LOCAL_TYPES:
            if kind == "local_file":
                require(values.get("filename") == planned[address]["change"]["after"].get("filename"), "foreign_local_file")
            continue
        if kind in KINDS[provider]:
            name = values.get("name") or values.get("key_name") or (values.get("tags") or {}).get("Name")
            require(owned_name(name, prefix), "foreign_state_resource")
            planned_values = planned[address]["change"]["after"]
            planned_name = planned_values.get("name") or planned_values.get("key_name") or (planned_values.get("tags") or {}).get("Name")
            require(name == planned_name, "state_resource_name_changed")
            api_kind = KINDS[provider][kind]
            if provider == "gcp":
                identifier = values.get("id")
                start = "projects/" + config["providers"][provider]["project_id"] + "/"
                require(isinstance(identifier, str) and identifier.startswith(start), "foreign_state_resource")
                require(any(item["kind"] == api_kind and item["locator"] == identifier[len(start):]
                            and item["name"] == name for item in inventory["resources"]), "state_resource_not_in_inventory")
            elif kind == "aws_key_pair":
                require(any(item["kind"] == api_kind and item["name"] == name
                            and (not values.get("key_pair_id") or item["id"] == values["key_pair_id"]) for item in inventory["resources"]), "state_resource_not_in_inventory")
            else:
                require((api_kind, str(values["id"])) in owned, "state_resource_not_in_inventory")
            continue
        relationships = {
            "aws_volume_attachment": (("volume_id", "volume"), ("instance_id", "instance")),
            "aws_route_table_association": (("subnet_id", "subnet"), ("route_table_id", "route_table")),
            "digitalocean_volume_attachment": (("volume_id", "volume"), ("droplet_id", "droplet")),
            "hcloud_volume_attachment": (("volume_id", "volume"), ("server_id", "server")),
        }.get(kind)
        if relationships:
            require(all((api_kind, str(values.get(key))) in owned for key, api_kind in relationships), "foreign_state_relationship")
            if kind == "aws_route_table_association":
                require(adapter is not None, "route_association_query_required")
                tables = objects(adapter.call(adapter.ec2, "describe_route_tables", RouteTableIds=[values["route_table_id"]]), "RouteTables")
                require(len(tables) == 1 and tables[0].get("RouteTableId") == values["route_table_id"] and
                        any(item.get("RouteTableAssociationId") == values["id"] and item.get("SubnetId") == values["subnet_id"]
                            for item in objects(tables[0], "Associations")), "foreign_state_relationship")
            continue
        if kind == "digitalocean_project_resources":
            require(values.get("project") == config["providers"][provider]["project_id"] and isinstance(values.get("resources"), list), "foreign_project_assignment")
            for urn in values["resources"]:
                match = re.fullmatch(r"do:(droplet|volume):([A-Za-z0-9-]+)", urn) if isinstance(urn, str) else None
                require(match is not None and (match[1], match[2]) in owned, "foreign_project_assignment")
            continue
        raise PolicyError("unexpected_state_type")
    return rows


def guard_destroy(plan, before):
    require(isinstance(plan, dict) and isinstance(plan.get("resource_changes", []), list), "invalid_destroy_plan")
    seen = set()
    for row in plan.get("resource_changes", []):
        require(isinstance(row, dict) and isinstance(row.get("change"), dict), "invalid_destroy_plan")
        change = row["change"]
        if row.get("mode") == "data":
            require(change.get("actions") in (["read"], ["no-op"], ["delete"]), "invalid_destroy_data_action")
            continue
        address = row.get("address")
        require(address in before and address not in seen and row.get("type") == before[address]["type"], "unowned_destroy_resource")
        require(change.get("actions") == ["delete"] and change.get("after") is None and not change.get("importing")
                and isinstance(change.get("before"), dict) and change["before"].get("id") == before[address]["values"]["id"], "unsafe_destroy_action")
        seen.add(address)
    return len(seen)


class TerraformAttempt:
    def __init__(self, run, config, provider, session, terraform="terraform", execute=command, now=time.time):
        self.run = private_directory(run)
        self.config, self.provider, self.session = config, provider, session
        self.terraform, self.execute, self.now = terraform, execute, now
        self.prepared = verify_prepared(self.run, config, provider)
        self.prefix = self.prepared["run_id"]
        self.checkout = self.run / "repo"
        self.record_path = self.run / "terraform.json"
        self.state_path = self.checkout / "terraform.tfstate"

    def tf(self, *args, timeout=60, runtime=False, cleanup=False, active=None, expected_plan=None):
        verify_prepared(self.run, self.config, self.provider)
        env = self.session.environment(self.run, min_lifetime=timeout + 60)
        if runtime:
            if (self.run / "workspace-hash.json").exists():
                hashed = private_read(self.run / "workspace-hash.json")["hash"]
            else:
                require(not cleanup, "workspace_hash_recovery_required")
                hashed = workspace_password_hash(self.config["secret_refs"]["workspace_password"], secret_reader=self.session.reader)
                private_write(self.run / "workspace-hash.json", {"hash": hashed})
            require(isinstance(hashed, str) and re.fullmatch(HASH_PATTERN, hashed), "invalid_workspace_hash")
            env["TF_VAR_workspace_password_hash"] = hashed
            if not cleanup:
                for name in ("openai_api_key", "anthropic_api_key"):
                    key = self.session.secret(self.config["secret_refs"][name])
                    require(text(key) and not any(char.isspace() for char in key) and len(key.encode()) <= 65536, "invalid_secret_field")
                    env["TF_VAR_" + name] = key
                auth_key = self.session.secret(self.config["tailnet"]["auth_key_ref"])
                require(text(auth_key) and not any(char.isspace() for char in auth_key), "invalid_secret_field")
                env["TF_VAR_tailscale_auth_key"] = auth_key
        if active is not None:
            timeout = remaining_timeout(active, timeout, cleanup=cleanup, now=self.now())
        if args[0] == "apply":
            path = self.run / ("destroy.tfplan" if cleanup else "deployment.tfplan")
            require(args[-1] == str(path) and path.is_file() and not path.is_symlink()
                    and path.stat().st_mode & 0o077 == 0 and isinstance(expected_plan, str)
                    and hashlib.sha256(path.read_bytes()).hexdigest() == expected_plan, "saved_plan_changed")
        return self.execute([self.terraform, "-chdir=" + str(self.checkout), *args], env=env, timeout=timeout)

    def check_tool(self):
        result = json.loads(self.tf("version", "-json"))
        match = re.fullmatch(r"1\.(\d+)\.(\d+)", result.get("terraform_version", ""))
        require(match is not None and int(match[1]) >= 9, "terraform_1_9_required")

    @exclusive
    def plan(self):
        require(not self.record_path.exists() and not self.state_path.exists(), "fresh_attempt_required")
        self.check_tool()
        self.tf("init", "-input=false", "-backend=false", "-no-color", timeout=600)
        preflight = self.session.preflight(self.prefix)
        require(preflight.get("status") == "read_checks_passed" and preflight.get("launch_ready") is False, "read_preflight_required")
        private_write(self.run / "preflight.json", preflight)
        path = self.run / "deployment.tfplan"
        self.tf("plan", "-input=false", "-no-color", "-lock-timeout=30s", "-out=" + str(path), timeout=600, runtime=True)
        require(path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0, "private_saved_plan_required")
        plan = json.loads(self.tf("show", "-json", str(path), timeout=120))
        result = validate_fresh_plan(plan, self.provider, self.prefix, self.checkout)
        private_write(self.run / "deployment-plan.json", plan)
        lock = self.checkout / ".terraform.lock.hcl"
        require(lock.is_file() and not lock.is_symlink(), "provider_lock_required")
        record = {"schema": 1, "stage": "planned", "run_id": self.prefix, "scope": scope(self.config, self.provider),
                  "source_revision": self.prepared["source_revision"], "plan_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "plan_json_sha256": hashlib.sha256((self.run / "deployment-plan.json").read_bytes()).hexdigest(),
                  "provider_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "planned_at": self.now(), "apply_invocations": 0}
        private_write(self.record_path, record)
        return {**result, "launch_ready": False, "live_matrix": "not_tested"}

    def record(self):
        record = private_read(self.record_path)
        require(record.get("schema") == 1 and record.get("run_id") == self.prefix and record.get("scope") == scope(self.config, self.provider)
                and record.get("source_revision") == self.prepared["source_revision"], "attempt_record_scope_mismatch")
        for filename, key in (("deployment.tfplan", "plan_sha256"), ("deployment-plan.json", "plan_json_sha256")):
            path = self.run / filename
            require(path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0
                    and hashlib.sha256(path.read_bytes()).hexdigest() == record.get(key), "saved_plan_changed")
        lock = self.checkout / ".terraform.lock.hcl"
        require(lock.is_file() and not lock.is_symlink() and hashlib.sha256(lock.read_bytes()).hexdigest() == record.get("provider_lock_sha256"), "provider_lock_changed")
        return record

    def active(self, ledger):
        def select(state):
            matches = [row for row in state["attempts"] if row["run_id"] == self.prefix and row["provider"] == self.provider and row["status"] == "active"]
            require(len(matches) == 1 and state["provider_bindings"].get(self.provider) == {key: value for key, value in scope(self.config, self.provider).items() if key != "provider"}, "active_attempt_scope_required")
            return dict(matches[0])
        return ledger.operate(select)

    def container_module(self):
        path = self.checkout / "scripts/showcase_container_probe.py"
        require(path.is_file() and not path.is_symlink(), "checked_container_probe_required")
        module = types.ModuleType("checked_container_probe")
        module.__file__ = str(path)
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        return module

    @exclusive
    def resource_unit_prices(self, public=None, aws_query=None):
        from showcase_prices import digitalocean_rates
        verify_prepared(self.run, self.config, self.provider)
        record = self.record()
        require(self.provider in ("aws", "digitalocean") and record.get("stage") == "planned", "planned_price_attempt_required")
        root = Path(__file__).resolve().parents[1]
        names = ("showcase_prices", "showcase_terraform", "showcase_policy", "showcase_preflight")
        if self.provider == "aws":
            names += ("showcase_aws_prices",)
        for name in names:
            relative = "scripts/" + name + ".py"
            require((self.checkout / relative).is_file() and (self.checkout / relative).read_bytes() == (root / relative).read_bytes(),
                    "price_controller_source_changed")
        common = (private_read(self.run / "deployment-plan.json"), self.config["providers"][self.provider], self.prefix, self.checkout, self.session.adapter)
        if self.provider == "aws":
            from showcase_aws_prices import aws_rates
            result = aws_rates(*common, self.session.credentials, self.prepared["source_revision"], record["plan_sha256"], query=aws_query, now=self.now)
        else:
            result = digitalocean_rates(*common, self.prepared["source_revision"], record["plan_sha256"], public=public, now=self.now)
        private_write(self.run / "resource-prices.json", result)
        return result

    def launch_gates(self, readiness):
        require(isinstance(readiness, dict) and readiness.get("source_revision") == self.prepared["source_revision"]
                and readiness.get("scope") == scope(self.config, self.provider), "measured_launch_gates_required")
        checks = readiness.get("checks")
        if checks == {key: "pass" for key in LAUNCH_CHECKS}:
            require("container_deferral" not in readiness, "container_deferral_invalid")
            return "pass"
        require(checks == {key: "deferred_owned_vm" if key == "container_tests" else "pass" for key in LAUNCH_CHECKS}
                and set(readiness) == {"source_revision", "scope", "checks", "container_deferral"}, "measured_launch_gates_required")
        proof = readiness["container_deferral"]
        require(isinstance(proof, dict) and set(proof) == {"mode", "run_id", "source_revision", "bundle_sha256", "local_docker"}
                and proof["mode"] == "first_owned_vm" and proof["run_id"] == self.prefix
                and proof["source_revision"] == self.prepared["source_revision"] and proof["local_docker"] == "absent"
                and shutil.which("docker") is None, "container_deferral_invalid")
        bundle = private_read(self.run / "container-bundle.json")
        module = self.container_module()
        try:
            sha = module.validate_bundle(bundle)
        except Exception:
            raise PolicyError("container_deferral_invalid") from None
        require(sha == proof["bundle_sha256"] and bundle["source_revision"] == self.prepared["source_revision"]
                and bundle["script"] == (self.checkout / "scripts/test_containers.py").read_text()
                and all(bundle["pins"][name] == self.config["pins"][name] for name in ("codex_release", "claude_release")),
                "container_deferral_invalid")
        return "deferred_owned_vm"

    @exclusive
    def apply(self, ledger, readiness):
        # Not a user override or a file loaded by the public CLI. The complete
        # production controller must generate these measured gate results.
        container_gate = self.launch_gates(readiness)
        active = self.active(ledger)
        record = self.record()
        require(record.get("stage") == "planned" and record.get("apply_invocations") == 0 and not self.state_path.exists(), "one_saved_plan_apply_required")
        require(identity(self.session.adapter, self.provider) == "verified", "account_project_mapping_unverified")
        require(self.session.preflight(self.prefix).get("status") == "read_checks_passed", "fresh_prefix_preflight_required")
        remaining_timeout(active, 1800, now=self.now())
        record.update(stage="apply_started", apply_invocations=1, apply_started_at=self.now(), container_tests_gate=container_gate)
        if container_gate == "deferred_owned_vm":
            record["container_bundle_sha256"] = readiness["container_deferral"]["bundle_sha256"]
        private_write(self.record_path, record)
        try:
            self.tf("apply", "-input=false", "-no-color", "-lock-timeout=30s", str(self.run / "deployment.tfplan"),
                    timeout=1800, active=active, expected_plan=record["plan_sha256"])
        except BaseException:
            record.update(stage="apply_uncertain")
            private_write(self.record_path, record)
            raise
        record.update(stage="applied")
        private_write(self.record_path, record)
        return {"deployment": "applied", "acceptance": "pending", "live_matrix": "not_tested"}

    @exclusive
    def cleanup(self, ledger):
        active = self.active(ledger)
        record = self.record()
        require(record.get("stage") in ("planned", "apply_started", "apply_uncertain", "applied", "destroy_started", "cleanup_failed"), "applied_attempt_required")
        require(identity(self.session.adapter, self.provider) == "verified", "account_project_mapping_unverified")
        inventory_path = self.run / "inventory.json"
        previous = private_read(inventory_path) if inventory_path.exists() else None
        current = self.session.inventory(self.prefix, previous)
        inventory = capture(current, self.config, previous)
        private_write(inventory_path, inventory)
        if record["stage"] == "planned":
            require(record.get("apply_invocations") == 0 and not self.state_path.exists() and not self.state_path.is_symlink(), "never_applied_proof_required")
            # No fake state file: the controller never invoked apply, and the
            # isolated source/plan/state path plus two independent inventories
            # prove no resources were created. Reservation history is retained.
            result = self.session.inventory(self.prefix, inventory)
            require(not self.state_path.exists() and not self.state_path.is_symlink(), "state_changed_during_cleanup")
            proof = receipt(result, inventory, True)
            proof["terraform_state_proof"] = "never_applied_verified_absent"
            private_write(self.run / "cleanup-receipt.json", proof)
            ledger.confirm_cleanup(self.prefix, proof)
            record.update(stage="cleaned")
            private_write(self.record_path, record)
            return {"cleanup": "verified", "owned_ids_checked": proof["owned_ids_checked"], "tailnet_nodes_checked": proof["tailnet_nodes_checked"]}
        # A lost state after apply is uncertain, never proof of absence. Keep
        # the captured orphan IDs and original ledger available for recovery.
        require(self.state_path.is_file(), "missing_state_after_apply")
        initial = private_read(self.state_path)
        before = guard_state(initial, private_read(self.run / "deployment-plan.json"), self.config, self.provider, self.prefix,
                             self.checkout, inventory, self.session.adapter)
        destroy_path = self.run / "destroy.tfplan"
        self.tf("plan", "-destroy", "-input=false", "-no-color", "-lock-timeout=30s", "-out=" + str(destroy_path),
                runtime=True, cleanup=True, timeout=remaining_timeout(active, 600, cleanup=True, now=self.now()))
        require(destroy_path.is_file() and not destroy_path.is_symlink() and destroy_path.stat().st_mode & 0o077 == 0, "private_saved_plan_required")
        destroy_hash = hashlib.sha256(destroy_path.read_bytes()).hexdigest()
        destroy = json.loads(self.tf("show", "-json", str(destroy_path), timeout=120))
        require(hashlib.sha256(destroy_path.read_bytes()).hexdigest() == destroy_hash, "saved_plan_changed")
        guard_destroy(destroy, before)
        # No file or state drift between inspection and execution. Terraform
        # also rejects a saved plan if its state serial/lineage has changed.
        require(private_read(self.state_path) == initial, "state_changed_during_cleanup")
        record.update(stage="destroy_started", destroy_plan_sha256=destroy_hash)
        private_write(self.record_path, record)
        try:
            self.tf("apply", "-input=false", "-no-color", "-lock-timeout=30s", str(destroy_path),
                    timeout=remaining_timeout(active, 1800, cleanup=True, now=self.now()), cleanup=True, expected_plan=destroy_hash)
            state_empty(self.state_path)
            self.session.delete_nodes(inventory)
            result = self.session.inventory(self.prefix, inventory)
            proof = receipt(result, inventory, state_empty(self.state_path))
            proof["terraform_state_proof"] = "destroyed_empty_local_state"
            private_write(self.run / "cleanup-receipt.json", proof)
            ledger.confirm_cleanup(self.prefix, proof)
        except BaseException:
            record.update(stage="cleanup_failed")
            private_write(self.record_path, record)
            raise
        record.update(stage="cleaned")
        private_write(self.record_path, record)
        return {"cleanup": "verified", "owned_ids_checked": proof["owned_ids_checked"], "tailnet_nodes_checked": proof["tailnet_nodes_checked"]}
