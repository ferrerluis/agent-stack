#!/usr/bin/env python3
"""Read-only ownership inventory and independent deletion verification.

Capture after a fresh-prefix preflight and before any deletion. This module
never deletes a resource. The future execution controller must preserve this
private record through crashes and use it alongside the isolated state.
"""
import argparse
import base64
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit

from showcase_policy import PROVIDERS, PolicyError, Reservations, require, validate_inputs
from showcase_preflight import (AWS, DigitalOcean, Google, Hetzner, JsonHTTP, PreflightError,
                                check, check_dependencies, field, objects, read_secret, segment, text)

AWS_TYPES = {
    "vpc": ("describe_vpcs", "Vpcs", "VpcId", "VpcIds", ("InvalidVpcID.NotFound",)),
    "subnet": ("describe_subnets", "Subnets", "SubnetId", "SubnetIds", ("InvalidSubnetID.NotFound",)),
    "route_table": ("describe_route_tables", "RouteTables", "RouteTableId", "RouteTableIds", ("InvalidRouteTableID.NotFound",)),
    "internet_gateway": ("describe_internet_gateways", "InternetGateways", "InternetGatewayId", "InternetGatewayIds", ("InvalidInternetGatewayID.NotFound",)),
    "security_group": ("describe_security_groups", "SecurityGroups", "GroupId", "GroupIds", ("InvalidGroup.NotFound", "InvalidSecurityGroupID.NotFound")),
    "key_pair": ("describe_key_pairs", "KeyPairs", "KeyPairId", "KeyPairIds", ("InvalidKeyPair.NotFound",)),
    "volume": ("describe_volumes", "Volumes", "VolumeId", "VolumeIds", ("InvalidVolume.NotFound",)),
    "instance": ("describe_instances", "Reservations", "InstanceId", "InstanceIds", ("InvalidInstanceID.NotFound",)),
    "network_interface": ("describe_network_interfaces", "NetworkInterfaces", "NetworkInterfaceId", "NetworkInterfaceIds", ("InvalidNetworkInterfaceID.NotFound", "InvalidNetworkInterface.NotFound")),
    "network_acl": ("describe_network_acls", "NetworkAcls", "NetworkAclId", "NetworkAclIds", ("InvalidNetworkAclID.NotFound",)),
}
REST_COLLECTIONS = {
    "digitalocean": (("droplet", "/droplets", "droplets"), ("volume", "/volumes", "volumes"),
                     ("vpc", "/vpcs", "vpcs"), ("firewall", "/firewalls", "firewalls"), ("ssh_key", "/account/keys", "ssh_keys")),
    "hetzner": (("server", "/servers", "servers"), ("volume", "/volumes", "volumes"), ("firewall", "/firewalls", "firewalls"),
                ("ssh_key", "/ssh_keys", "ssh_keys"), ("network", "/networks", "networks"), ("primary_ip", "/primary_ips", "primary_ips"),
                ("floating_ip", "/floating_ips", "floating_ips"), ("load_balancer", "/load_balancers", "load_balancers")),
}


def scope(config, provider):
    cloud = config["providers"][provider]
    return {"provider": provider, "account_id": cloud["account_id"], "project_id": cloud.get("project_id", ""),
            "region": cloud["region"], "zone": cloud.get("zone", "")}


def owned_name(name, prefix):
    return isinstance(name, str) and (name == prefix or name.startswith(prefix + "-"))


def identity(adapter, provider):
    if provider == "aws":
        check(adapter.call(adapter.sts, "get_caller_identity").get("Account") == adapter.cloud["account_id"], "account_identity_mismatch")
    elif provider == "digitalocean":
        account = field(adapter.get("/account"), "account")
        team = account.get("team")
        check(team is None or isinstance(team, dict), "invalid_provider_response")
        check((team.get("uuid") if team else account.get("uuid")) == adapter.cloud["account_id"], "account_identity_mismatch")
        project = field(adapter.get("/projects/" + segment(adapter.cloud["project_id"])), "project")
        check(project.get("id") == adapter.cloud["project_id"], "project_identity_mismatch")
    elif provider == "gcp":
        project = segment(adapter.cloud["project_id"])
        value = adapter.http.request("https://cloudresourcemanager.googleapis.com/v3/projects/" + project,
                                     "Bearer " + adapter.access_token())
        check(value.get("projectId") == adapter.cloud["project_id"] and value.get("state") == "ACTIVE", "project_identity_mismatch")
        adapter.base = "https://compute.googleapis.com/compute/v1/projects/" + project
    else:
        return adapter.identity()
    return "verified"


def resource(kind, value, id_key="id", name_key="name", locator=None, parent=None):
    identifier = value.get(id_key)
    check(type(identifier) in (str, int) and text(str(identifier)), "invalid_resource_identity")
    row = {"kind": kind, "id": str(identifier), "name": value.get(name_key, ""), "locator": str(identifier) if locator is None else locator}
    check(isinstance(row["name"], str) and text(row["locator"]), "invalid_resource_identity")
    if parent:
        row["parent"] = parent
    return row


def tag_name(value):
    tags = value.get("Tags", [])
    check(isinstance(tags, list) and all(isinstance(item, dict) for item in tags), "invalid_provider_response")
    matches = [item.get("Value") for item in tags if item.get("Key") == "Name"]
    check(len(matches) <= 1, "invalid_provider_response")
    return matches[0] if matches else ""


def aws_rows(value, kind):
    rows = objects(value, AWS_TYPES[kind][1])
    if kind == "instance":
        rows = [instance for row in rows for instance in objects(row, "Instances")]
    return rows


def aws_lookup(adapter, row):
    kind = row["kind"]
    operation, _, id_key, parameter, absent_codes = AWS_TYPES[kind]
    try:
        result = getattr(adapter.ec2, operation)(**{parameter: [row["id"]]})
    except Exception as error:
        response = getattr(error, "response", {})
        metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
        detail = response.get("Error", {}) if isinstance(response, dict) else {}
        if isinstance(metadata, dict) and isinstance(detail, dict) and metadata.get("HTTPStatusCode") in (400, 404) and detail.get("Code") in absent_codes:
            adapter.queries += 1
            return None
        raise PreflightError("aws_cleanup_query_failed") from None
    rows = aws_rows(result, kind)
    check(len(rows) == 1 and rows[0].get(id_key) == row["id"], "invalid_resource_identity")
    adapter.queries += 1
    if kind == "instance" and field(rows[0], "State").get("Name") == "terminated":
        return None  # EC2 retains a terminated tombstone; disks are separate checks.
    return rows[0]


def aws_inventory(adapter, prefix, tracked):
    current, raw = [], {}
    filters = [{"Name": "tag:Name", "Values": [prefix + "*"]}]
    for kind in AWS_TYPES:
        if kind in ("network_interface", "network_acl"):
            continue
        operation, key, id_key, _, _ = AWS_TYPES[kind]
        query = [{"Name": "key-name", "Values": [prefix + "*"]}] if kind == "key_pair" else filters
        values = adapter.pages(operation, key, Filters=query)
        if kind == "instance":
            values = [item for row in values for item in objects(row, "Instances")]
        for value in values:
            name = value.get("KeyName", "") if kind == "key_pair" else tag_name(value)
            check(owned_name(name, prefix), "unexpected_prefix_resource")
            if kind == "instance" and field(value, "State").get("Name") == "terminated":
                continue
            row = resource(kind, {**value, "name": name}, id_key)
            current.append(row)
            raw[(kind, row["id"])] = value
    # Capture implicit VPC children and boot disks while their parent exists.
    for parent in list(current):
        if parent["kind"] == "vpc":
            for kind in ("subnet", "route_table", "security_group", "network_interface", "network_acl"):
                operation, key, id_key, _, _ = AWS_TYPES[kind]
                for value in adapter.pages(operation, key, Filters=[{"Name": "vpc-id", "Values": [parent["id"]]}]):
                    check(value.get("VpcId") == parent["id"], "foreign_resource_parent")
                    current.append(resource(kind, {**value, "name": tag_name(value)}, id_key,
                                            parent={"kind": "vpc", "id": parent["id"]}))
        elif parent["kind"] == "instance":
            value = raw[("instance", parent["id"])]
            for mapping in objects(value, "BlockDeviceMappings"):
                volume_id = field(mapping, "Ebs").get("VolumeId")
                check(text(volume_id), "invalid_resource_identity")
                probe = {"kind": "volume", "id": volume_id}
                volume = aws_lookup(adapter, probe)
                check(volume is not None and any(item.get("InstanceId") == parent["id"] for item in objects(volume, "Attachments")), "foreign_resource_parent")
                check(owned_name(tag_name(volume), prefix) or (text(value.get("RootDeviceName"))
                      and mapping.get("DeviceName") == value["RootDeviceName"]), "foreign_attached_disk")
                current.append(resource("volume", {**volume, "name": tag_name(volume)}, "VolumeId",
                                        parent={"kind": "instance", "id": parent["id"]}))
    # Exact IDs find renamed resources and children whose parent was deleted.
    present = {(row["kind"], row["id"]) for row in current}
    for row in tracked:
        if (row["kind"], row["id"]) not in present and aws_lookup(adapter, row) is not None:
            current.append(row)
    return current, []


def rest_inventory(adapter, provider, prefix, tracked):
    all_rows, raw = [], {}
    for kind, path, key in REST_COLLECTIONS[provider]:
        for value in adapter.pages(path, key, provider):
            row = resource(kind, value)
            all_rows.append(row)
            raw[(kind, row["id"])] = value
    tracked_ids = {(row["kind"], row["id"]) for row in tracked}
    current = [row for row in all_rows if owned_name(row["name"], prefix) or (row["kind"], row["id"]) in tracked_ids]
    if provider == "hetzner":
        implicit = {}
        for parent in current:
            if parent["kind"] == "server":
                public = field(raw[("server", parent["id"])], "public_net")
                for protocol in ("ipv4", "ipv6"):
                    address = public.get(protocol)
                    if address is not None:
                        identifier = field(public, protocol).get("id")
                        check(type(identifier) is int and identifier > 0, "invalid_resource_identity")
                        implicit[str(identifier)] = {"kind": "server", "id": parent["id"]}
        for row in all_rows:
            if row["kind"] == "primary_ip" and row["id"] in implicit:
                current.append({**row, "parent": implicit[row["id"]]})
        # A dedicated initially-empty project must be empty after cleanup.
        # Unrelated rows fail verification; they do not authorize deletion.
        current_ids = {(row["kind"], row["id"]) for row in current}
        unknown = [row for row in all_rows if (row["kind"], row["id"]) not in current_ids]
    else:
        unknown = []
        resources = adapter.pages("/projects/" + segment(adapter.cloud["project_id"]) + "/resources", "resources", provider)
        # Project membership alone is not proof of resource deletion. Conversely,
        # any leftover project URN must prevent an empty-project receipt.
        unknown = [{"kind": "project_membership", "id": row.get("urn", "invalid")}
                   for row in resources if text(row.get("urn"))]
        check(all(text(row.get("urn")) for row in resources), "invalid_provider_response")
    return current, unknown


def google_locator(value, provider_project, kind):
    link = value.get("selfLink")
    check(text(link), "invalid_resource_identity")
    parsed = urlsplit(link)
    check(parsed.scheme == "https" and parsed.hostname in ("www.googleapis.com", "compute.googleapis.com")
          and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
          and not parsed.username and not parsed.password, "foreign_resource_scope")
    match = re.fullmatch(r"/compute/v1/projects/([^/]+)/(global|zones/[a-z0-9-]+|regions/[a-z0-9-]+)/([a-z]+)/([a-z0-9-]+)", parsed.path)
    check(match is not None and match[1] == provider_project and match[3] == kind, "foreign_resource_scope")
    check(value.get("name") == match[4], "invalid_resource_identity")
    return match[2] + "/" + kind + "/" + match[4]


def google_inventory(adapter, prefix, tracked):
    current, all_rows, raw = [], [], {}
    project = adapter.cloud["project_id"]
    for kind, plural, path in (("instance", "instances", "/aggregated/instances"), ("disk", "disks", "/aggregated/disks"),
                               ("subnetwork", "subnetworks", "/aggregated/subnetworks"), ("network", "networks", "/global/networks"),
                               ("firewall", "firewalls", "/global/firewalls")):
        style = "gcp_aggregated" if path.startswith("/aggregated/") else "gcp"
        for value in adapter.pages(path, "items", style):
            locator = google_locator(value, project, plural)
            row = resource(kind, value, locator=locator)
            all_rows.append(row)
            raw[(kind, row["id"])] = value
    tracked_ids = {(row["kind"], row["id"]) for row in tracked}
    current = [row for row in all_rows if owned_name(row["name"], prefix) or (row["kind"], row["id"]) in tracked_ids]
    allowed = ("global/", "zones/" + adapter.cloud["zone"] + "/", "regions/" + adapter.cloud["region"] + "/")
    check(all(row["locator"].startswith(allowed) for row in current), "foreign_resource_scope")
    # Boot disks are included even if the provider picks a non-prefixed name.
    for parent in list(current):
        if parent["kind"] != "instance":
            continue
        for disk in objects(raw[("instance", parent["id"])], "disks"):
            source = disk.get("source")
            if source is None:
                continue  # a local scratch disk is part of the VM, not a disk resource
            name = urlsplit(source).path.rsplit("/", 1)[-1] if text(source) else ""
            locator = google_locator({"selfLink": source, "name": name}, project, "disks")
            matches = [row for row in all_rows if row["kind"] == "disk" and row["locator"] == locator]
            check(len(matches) == 1, "implicit_disk_inventory_incomplete")
            check(owned_name(matches[0]["name"], prefix) or disk.get("boot") is True, "foreign_attached_disk")
            current.append({**matches[0], "parent": {"kind": "instance", "id": parent["id"]}})
    return current, []


def tailnet_inventory(config, key, http, prefix, tracked):
    auth = "Basic " + base64.b64encode((key + ":").encode()).decode()
    value = http.request("https://api.tailscale.com/api/v2/tailnet/" + segment(config["name"]) + "/devices", auth)
    devices = objects(value, "devices")
    known = {row["id"] for row in tracked}
    result = []
    for value in devices:
        check(text(value.get("name")) and text(value.get("id")), "invalid_provider_response")
        dns = value["name"].rstrip(".")
        first_label = dns.split(".", 1)[0]
        if value["id"] in known or owned_name(first_label, prefix) or owned_name(value.get("hostname"), prefix):
            check(dns != config["runner_dns"], "runner_peer_must_be_preserved")
            node = {"id": value["id"], "name": dns, "hostname": value.get("hostname", "")}
            if config.get("required_tag"):
                check(config["required_tag"] in value.get("tags", []), "tailnet_ownership_tag_missing")
                node["tags"] = value["tags"]
            result.append(node)
    return result


def normalized(rows):
    merged = {}
    for row in rows:
        key = (row["kind"], row["id"])
        old = merged.get(key)
        check(old is None or old["locator"] == row["locator"], "resource_identity_changed")
        if old and "parent" in old and "parent" not in row:
            row = {**row, "parent": old["parent"]}
        merged[key] = row
    check(len(merged) <= 512, "inventory_limit_exceeded")
    return sorted(merged.values(), key=lambda row: (row["kind"], row["id"]))


def validate_record(record, config, provider, prefix):
    check(isinstance(record, dict) and record.get("schema") == 1 and record.get("scope") == scope(config, provider)
          and record.get("run_id") == prefix and isinstance(record.get("resources"), list)
          and isinstance(record.get("tailnet"), dict), "inventory_scope_mismatch")
    check(type(record.get("captured_at")) in (int, float) and math.isfinite(record["captured_at"])
          and record["captured_at"] > 0, "invalid_inventory")
    kinds = set(AWS_TYPES) if provider == "aws" else ({row[0] for row in REST_COLLECTIONS[provider]} if provider != "gcp"
                                                    else {"instance", "disk", "network", "subnetwork", "firewall"})
    for row in record["resources"]:
        check(isinstance(row, dict) and row.get("kind") in kinds and all(text(row.get(key)) for key in ("id", "locator"))
              and isinstance(row.get("name"), str), "invalid_inventory")
        if provider == "aws":
            check(re.fullmatch(r"[a-z]+-[a-f0-9]+", row["id"]) is not None, "invalid_inventory")
    tailnet = record["tailnet"]
    check(tailnet.get("name") == config["tailnet"]["name"] and tailnet.get("runner_dns") == config["tailnet"]["runner_dns"]
          and isinstance(tailnet.get("devices"), list), "inventory_tailnet_mismatch")
    check(all(isinstance(row, dict) and text(row.get("id")) and text(row.get("name"))
              and row["name"] != tailnet["runner_dns"] for row in tailnet["devices"]), "invalid_inventory")
    check(len(normalized(record["resources"])) == len(record["resources"])
          and len({row["id"] for row in tailnet["devices"]}) == len(tailnet["devices"]) <= 512, "invalid_inventory")


def query(config, provider, prefix, adapter, http, tailnet_key, previous=None, now=time.time):
    validate_inputs(config, provider)
    check(isinstance(prefix, str) and re.fullmatch(r"as7-[a-z0-9-]{8,30}", prefix) is not None, "invalid_resource_scope")
    if previous is not None:
        validate_record(previous, config, provider, prefix)
    tracked = [] if previous is None else previous["resources"]
    prior_nodes = [] if previous is None else previous["tailnet"]["devices"]
    cloud_start = http.queries + getattr(adapter, "queries", 0)
    identity_status = identity(adapter, provider)
    if provider == "aws":
        current, unknown = aws_inventory(adapter, prefix, tracked)
    elif provider == "gcp":
        current, unknown = google_inventory(adapter, prefix, tracked)
    else:
        current, unknown = rest_inventory(adapter, provider, prefix, tracked)
    cloud_queries = http.queries + getattr(adapter, "queries", 0) - cloud_start
    tailnet_start = http.queries
    nodes = tailnet_inventory(config["tailnet"], tailnet_key, http, prefix, prior_nodes)
    return {"scope": scope(config, provider), "run_id": prefix, "identity": identity_status,
            "resources": normalized(current), "unknown_resources": unknown, "nodes": nodes,
            "cloud_queries": cloud_queries, "tailnet_queries": http.queries - tailnet_start, "verified_at": now()}


def capture(result, config, previous=None):
    provider, prefix = result["scope"]["provider"], result["run_id"]
    if previous is not None:
        validate_record(previous, config, provider, prefix)
    rows = ([] if previous is None else previous["resources"]) + result["resources"]
    nodes = {row["id"]: row for row in ([] if previous is None else previous["tailnet"]["devices"]) + result["nodes"]}
    # Recapture after partial deletion preserves every observed ID.
    return {"schema": 1, "scope": result["scope"], "run_id": prefix, "captured_at": result["verified_at"],
            "identity": result["identity"], "resources": normalized(rows), "tailnet": {
                "name": config["tailnet"]["name"], "runner_dns": config["tailnet"]["runner_dns"], "devices": sorted(nodes.values(), key=lambda row: row["id"])}}


def receipt(result, record, state_verified_empty=False):
    check(result["scope"] == record["scope"] and result["run_id"] == record["run_id"], "inventory_scope_mismatch")
    check(result["identity"] == "verified", "account_project_mapping_unverified")
    check(state_verified_empty is True, "terraform_state_removal_unverified")
    check(result["cloud_queries"] > 0 and result["tailnet_queries"] > 0 and result["verified_at"] >= record["captured_at"], "cleanup_unverified")
    check(not result["resources"] and not result["unknown_resources"] and not result["nodes"], "owned_or_unknown_resources_remain")
    return {**result["scope"], "terraform_state_verified_empty": True, "cloud_queries_succeeded": result["cloud_queries"], "owned_ids_checked": len(record["resources"]),
            "remaining_ids": [], "tailnet_queries_succeeded": result["tailnet_queries"], "tailnet_nodes_checked": len(record["tailnet"]["devices"]),
            "remaining_tailnet_nodes": [], "verified_at": result["verified_at"]}


def private_read(path):
    path = Path(path)
    check(not path.is_symlink() and path.is_file() and path.stat().st_uid == os.geteuid()
          and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "private_file_required")
    return json.loads(path.read_text())


def state_empty(path):
    state = private_read(path)
    check(isinstance(state, dict) and type(state.get("version")) is int and state["version"] == 4
          and isinstance(state.get("resources"), list), "invalid_terraform_state")
    for row in state["resources"]:
        check(isinstance(row, dict) and row.get("mode") in ("managed", "data")
              and isinstance(row.get("instances"), list), "invalid_terraform_state")
        check(row["mode"] != "managed" or not row["instances"], "terraform_resources_remain")
    return True


def private_write(path, value):
    path = Path(path)
    check(path.parent.is_dir() and not path.parent.is_symlink() and path.parent.stat().st_uid == os.geteuid()
          and stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0 and not path.is_symlink(), "private_directory_required")
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--state", help="Private isolated local .tfstate after destroy; mandatory for verification")
    parser.add_argument("--phase", choices=("capture", "verify"), required=True)
    args = parser.parse_args()
    try:
        require(os.environ.get("AGENT_STACK_LIVE_TESTS") == "1", "live_opt_in_required")
        config = private_read(args.config)
        validate_inputs(config, args.provider)
        check_dependencies(args.provider)
        inventory_path = Path(args.inventory)
        check(inventory_path.parent.is_dir() and not inventory_path.parent.is_symlink()
              and inventory_path.parent.stat().st_uid == os.geteuid()
              and stat.S_IMODE(inventory_path.parent.stat().st_mode) & 0o077 == 0, "private_directory_required")
        fd = os.open(inventory_path.parent / (inventory_path.name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = private_read(inventory_path) if inventory_path.exists() or inventory_path.is_symlink() else None
            check(args.phase != "verify" or previous is not None, "capture_record_required")
            if args.phase == "verify":
                check(args.state is not None, "private_terraform_state_required")
                state_empty(args.state)
            if previous is not None:
                validate_record(previous, config, args.provider, args.run_id)
            def active(state):
                matches = [row for row in state["attempts"] if row["run_id"] == args.run_id and row["provider"] == args.provider and row["status"] == "active"]
                require(len(matches) == 1 and state["provider_bindings"].get(args.provider) == {key: value for key, value in scope(config, args.provider).items() if key != "provider"}, "active_attempt_scope_required")
            ledger = Reservations(args.ledger, config)
            ledger.operate(active)
            http, secrets = JsonHTTP(), {}
            def secret(reference):
                if reference not in secrets:
                    secrets[reference] = read_secret(reference)
                return secrets[reference]
            cloud = config["providers"][args.provider]
            credentials = {key: secret(value) for key, value in cloud["credential_refs"].items()}
            if args.provider == "aws":
                adapter = AWS(cloud, credentials)
            elif args.provider == "gcp":
                adapter = Google(cloud, credentials["service_account_json"], http)
            else:
                extra = {'identity_proof':secret(cloud['identity_proof_ref'])} if args.provider == 'hetzner' and 'identity_proof_ref' in cloud else {}
                adapter = {"digitalocean": DigitalOcean, "hetzner": Hetzner}[args.provider](cloud, credentials["token"], http, **extra)
            result = query(config, args.provider, args.run_id, adapter, http, secret(config["tailnet"]["cleanup_key_ref"]), previous)
            if args.phase == "capture":
                record = capture(result, config, previous)
                private_write(inventory_path, record)
                report = {"status": "inventory_captured", "owned_ids": len(record["resources"]), "tailnet_nodes": len(record["tailnet"]["devices"]),
                          "unknown_resources": len(result["unknown_resources"]), "identity": result["identity"], "deletion_authorized": False}
            else:
                # Recheck after the API reads; a concurrently changed state is
                # not the empty state observed at the start of verification.
                proof = receipt(result, previous, state_empty(args.state))
                private_write(inventory_path.parent / (inventory_path.name + ".cleanup-receipt.json"), proof)
                report = ledger.confirm_cleanup(args.run_id, proof)
                report["status"] = "cleanup_verified"
    except (PolicyError, PreflightError) as error:
        report = {"status": "fail", "reason": str(error), "cleanup": "unverified"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        report = {"status": "fail", "reason": "invalid_input_or_provider_response", "cleanup": "unverified"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in ("inventory_captured", "cleanup_verified") else 1


if __name__ == "__main__":
    sys.exit(main())
