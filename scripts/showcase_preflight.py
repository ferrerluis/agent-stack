#!/usr/bin/env python3
"""Opt-in, read-only cloud checks. No deployments or model calls.

Credentials are read once from selected 1Password fields, stay in this process,
and never enter the report. Successful read checks do not authorize a launch.
"""
import argparse
import base64
from datetime import datetime
import json
import ipaddress
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from showcase_policy import PROVIDERS, PolicyError, require, secret_ref, text, validate_inputs, digitalocean_showcase_cidr

MAX_RESPONSE = 4 * 1024 * 1024
MAX_PAGES = 50
HOSTS = {"api.digitalocean.com", "api.hetzner.cloud", "compute.googleapis.com",
         "cloudbilling.googleapis.com", "cloudresourcemanager.googleapis.com",
         "oauth2.googleapis.com", "api.tailscale.com"}
GCP_PERMISSIONS = tuple("compute." + value for value in (
    "instances.create", "instances.delete", "instances.get", "instances.list", "instances.setMetadata",
    "instances.setTags", "instances.setLabels", "disks.create", "disks.delete", "disks.get", "disks.list", "disks.use",
    "disks.setLabels", "networks.create", "networks.delete", "networks.get", "networks.list", "networks.updatePolicy",
    "subnetworks.create", "subnetworks.delete", "subnetworks.get", "subnetworks.list", "subnetworks.use",
    "subnetworks.useExternalIp", "firewalls.create", "firewalls.delete", "firewalls.get", "firewalls.list",
    "zoneOperations.get", "regionOperations.get", "globalOperations.get"))


class PreflightError(Exception):
    """Fixed error codes only; raw provider/CLI errors contain private data."""


def check(condition, code):
    if not condition:
        raise PreflightError(code)


def objects(value, key):
    check(isinstance(value, dict) and isinstance(value.get(key), list)
          and all(isinstance(row, dict) for row in value[key]), "invalid_provider_response")
    return value[key]


def field(value, key):
    check(isinstance(value, dict) and isinstance(value.get(key), dict), "invalid_provider_response")
    return value[key]


def segment(value):
    check(text(value), "invalid_api_scope")
    return quote(value, safe="")


def read_secret(reference):
    check(secret_ref(reference), "secret_reference_required")
    if reference.startswith("file://"):
        from showcase_credentials import read_file, refresh_access_token
        try:
            refresh_access_token(reference)
            return read_file(reference)
        except ValueError:
            raise PreflightError("temporary_credential_refused") from None
    try:
        result = subprocess.run(["op", "read", "--no-newline", reference],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightError("onepassword_unavailable_or_locked") from None
    check(result.returncode == 0, "onepassword_unavailable_or_locked")
    check(0 < len(result.stdout) <= 1024 * 1024, "invalid_secret_field")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError:
        raise PreflightError("invalid_secret_field") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise PreflightError("provider_redirect_refused")


class JsonHTTP:
    def __init__(self):
        # Do not route bearer keys through a user-configured proxy or redirect.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.queries = 0

    def request(self, url, authorization=None, payload=None, form=False):
        parts = urlsplit(url)
        check(parts.scheme == "https" and parts.hostname in HOSTS and parts.port in (None, 443)
              and not parts.username and not parts.password and not parts.fragment, "provider_endpoint_refused")
        headers = {"Accept": "application/json", "User-Agent": "AgentStack-showcase-preflight/1"}
        if authorization is not None:
            check(text(authorization), "invalid_secret_field")
            headers["Authorization"] = authorization
        data = None
        if payload is not None:
            data = urlencode(payload).encode() if form else json.dumps(payload).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        try:
            with self.opener.open(Request(url, data=data, headers=headers), timeout=20) as response:
                check(response.status == 200, "provider_request_failed")
                body = response.read(MAX_RESPONSE + 1)
            check(len(body) <= MAX_RESPONSE, "provider_response_too_large")
            value = json.loads(body, object_pairs_hook=unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")))
            check(isinstance(value, dict), "invalid_provider_response")
        except HTTPError as error:
            # Never include the error's body, URL, headers or credential.
            code = error.code
            error.close()
            raise PreflightError("provider_http_" + str(code)) from None
        except (URLError, OSError, ValueError):
            raise PreflightError("provider_request_failed") from None
        self.queries += 1
        return value


class RestCloud:
    def __init__(self, cloud, token, http):
        self.cloud, self.token, self.http = cloud, token, http
        check(text(token), "invalid_secret_field")

    def get(self, path):
        return self.http.request(self.base + path, "Bearer " + self.token)

    def pages(self, path, key, style):
        rows = []
        next_token = None
        seen = set()
        for number in range(1, MAX_PAGES + 1):
            query = {"page": number, "per_page": 100} if style != "gcp" else {"maxResults": 500}
            if next_token is not None:
                query["pageToken"] = next_token
            value = self.get(path + ("&" if "?" in path else "?") + urlencode(query))
            # GCP omits items for an empty collection, but never accept a
            # malformed present collection as empty.
            if style == "gcp" and "items" not in value:
                value = {**value, "items": []}
            rows.extend(objects(value, key))
            if style == "digitalocean":
                links = value.get("links", {})
                check(isinstance(links, dict) and isinstance(links.get("pages", {}), dict), "invalid_pagination")
                # Only use presence of next; never follow its potentially
                # credential-stealing URL. Reconstruct the approved endpoint.
                if not links.get("pages", {}).get("next"):
                    return rows
            elif style == "hetzner":
                meta = field(value, "meta")
                pagination = field(meta, "pagination")
                next_page = pagination.get("next_page")
                if next_page is None:
                    return rows
                check(type(next_page) is int and next_page == number + 1, "invalid_pagination")
            else:
                next_token = value.get("nextPageToken")
                if not next_token:
                    return rows
                check(text(next_token) and next_token not in seen, "invalid_pagination")
                seen.add(next_token)
        raise PreflightError("pagination_limit_exceeded")


class DigitalOcean(RestCloud):
    base = "https://api.digitalocean.com/v2"

    def check_network_range(self, prefix, vpcs):
        candidate = ipaddress.ip_network(digitalocean_showcase_cidr(prefix))
        def check_range(row):
            try:
                value = row.get("ip_range")
                check(isinstance(value, str) and "/" in value, "network_range_unverified")
                network = ipaddress.ip_network(value, strict=True)
            except (ValueError, TypeError):
                raise PreflightError("network_range_unverified") from None
            check(network.version == 4, "network_range_unverified")
            check(not candidate.overlaps(network), "network_range_already_exists")
        for row in vpcs:
            check_range(row)
            identifier = row.get("id")
            check(isinstance(identifier, str) and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identifier),
                  "network_range_unverified")
            # Additional subnets can use ranges outside the VPC default range.
            # Read every page; no API error or malformed response is empty.
            for subnet in self.pages("/vpcs/" + identifier + "/subnets", "vpc_subnet", "digitalocean"):
                check_range(subnet)
        return str(candidate)

    def preflight(self, prefix):
        account = field(self.get("/account"), "account")
        team = account.get("team")
        check(team is None or isinstance(team, dict), "invalid_provider_response")
        identity = team.get("uuid") if team else account.get("uuid")
        check(text(identity) and identity == self.cloud["account_id"], "account_identity_mismatch")
        check(account.get("status") == "active", "account_not_active")
        project = field(self.get("/projects/" + segment(self.cloud["project_id"])), "project")
        check(project.get("id") == self.cloud["project_id"], "project_identity_mismatch")
        resources = self.pages("/projects/" + segment(self.cloud["project_id"]) + "/resources", "resources", "digitalocean")
        check(not resources, "test_project_not_empty")
        sizes = self.pages("/sizes", "sizes", "digitalocean")
        matches = [row for row in sizes if row.get("slug") == self.cloud["size"]]
        check(len(matches) == 1, "vm_size_unavailable")
        size = matches[0]
        check(type(size.get("memory")) is int and size["memory"] >= 4096, "vm_memory_below_4_gib")
        check(size.get("available") is True and isinstance(size.get("regions"), list)
              and self.cloud["region"] in size["regions"], "vm_size_unavailable")
        image = field(self.get("/images/ubuntu-22-04-x64"), "image")
        check(image.get("slug") == "ubuntu-22-04-x64" and image.get("status") == "available"
              and self.cloud["region"] in image.get("regions", []), "x86_image_unavailable")
        droplets = self.pages("/droplets", "droplets", "digitalocean")
        check(type(account.get("droplet_limit")) is int and account["droplet_limit"] > len(droplets), "vm_quota_unavailable")
        # Project membership does not cover VPC/firewall/SSH keys. Ensure the
        # attempt's names cannot collide in those account-level collections.
        for path, key in (("/droplets", "droplets"), ("/volumes", "volumes"), ("/vpcs", "vpcs"),
                          ("/firewalls", "firewalls"), ("/account/keys", "ssh_keys")):
            rows = droplets if path == "/droplets" else self.pages(path, key, "digitalocean")
            check(not any(isinstance(row.get("name"), str) and row["name"].startswith(prefix) for row in rows), "resource_prefix_already_exists")
            if path == "/vpcs":
                cidr = self.check_network_range(prefix, rows)
        return {"identity": "verified", "project": "verified_empty", "vm_memory_mib": size["memory"],
                "architecture": "x86_64_image", "vm_quota": "available", "size_available_in_region": True,
                "vpc_cidr": cidr, "network_range": "verified_unused",
                "not_verified": ["create_delete_permissions", "disk_network_quotas", "live_capacity"]}


def hetzner_deprecation(value, now, horizon_seconds=14400):
    """A selected image/type must remain available for the whole attempt."""
    check(type(now) in (int, float) and 0 <= now <= 253402286400, "invalid_provider_clock")
    if value is None:
        return
    check(isinstance(value, dict), "invalid_deprecation_response")
    try:
        dates = [datetime.fromisoformat(value[key].replace("Z", "+00:00")) for key in ("announced", "unavailable_after")]
        check(all(date.tzinfo is not None for date in dates) and 0 <= dates[0].timestamp() <= now
              and dates[1].timestamp() > now + horizon_seconds, "vm_or_image_removal_during_attempt")
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        raise PreflightError("invalid_deprecation_response") from None


def hetzner_location(size, name, now=None):
    locations = objects(size, "locations")
    matches = [row for row in locations if row.get("name") == name]
    check(len(matches) == 1 and matches[0].get("available") is True
          and type(matches[0].get("id")) is int and matches[0]["id"] > 0
          and "deprecation" in matches[0], "vm_size_unavailable")
    hetzner_deprecation(matches[0]["deprecation"], time.time() if now is None else now)
    return matches[0]


class Hetzner(RestCloud):
    base = "https://api.hetzner.cloud/v1"

    def __init__(self, cloud, token, http, identity_proof=None):
        super().__init__(cloud, token, http)
        self.identity_proof = identity_proof

    def identity(self):
        if 'identity_proof_ref' not in self.cloud:
            check(self.identity_proof is None, 'hetzner_project_binding_reference_required')
            return 'declared_only'
        from showcase_hetzner_identity import verify_proof
        try:
            return verify_proof(self.identity_proof, self.cloud, self.token)['identity']
        except ValueError:
            raise PreflightError('hetzner_project_binding_invalid') from None

    def preflight(self, prefix):
        # HCloud tokens are project-scoped. Its Cloud API does not expose the
        # account/project ID: matching the declared IDs needs separate proof.
        identity = self.identity()
        for path, key in (("/servers", "servers"), ("/volumes", "volumes"), ("/firewalls", "firewalls"),
                          ("/ssh_keys", "ssh_keys"), ("/networks", "networks"), ("/primary_ips", "primary_ips"),
                          ("/floating_ips", "floating_ips"), ("/load_balancers", "load_balancers")):
            check(not self.pages(path, key, "hetzner"), "test_project_not_empty")
        sizes = self.pages("/server_types", "server_types", "hetzner")
        matches = [row for row in sizes if row.get("name") == self.cloud["size"]]
        check(len(matches) == 1, "vm_size_unavailable")
        size = matches[0]
        check(type(size.get("memory")) in (int, float) and math.isfinite(size["memory"])
              and 4 <= size["memory"] <= 1000000, "vm_memory_below_4_gib")
        check(size.get("architecture") == "x86", "vm_architecture_not_x86_64")
        prices = objects(size, "prices")
        check(any(row.get("location") == self.cloud["region"] for row in prices), "vm_size_unavailable")
        hetzner_location(size, self.cloud["region"])
        return {"identity": identity, "project": "token_scope_verified_empty", "vm_memory_mib": int(size["memory"] * 1024),
                "architecture": "x86_64", "size_available_in_region": True,
                "not_verified": (["account_project_token_mapping"] if identity != 'verified' else [])
                    + ["create_delete_permissions", "quotas", "live_capacity"]}


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def google_x86_machine(size):
    # Compute omits architecture for E2. Its documented predefined series uses
    # Intel/AMD; an explicit different architecture still takes precedence.
    return size.get("architecture") == "X86_64" or (size.get("architecture") is None
        and isinstance(size.get("name"), str)
        and re.fullmatch(r"e2-(?:(?:standard|highmem|highcpu)-(?:2|4|8|16|32)|medium)", size["name"]) is not None)


class Google(RestCloud):
    """Service account JWT exchange; no ADC discovery or credential file."""
    def __init__(self, cloud, credential, http, now=time.time):
        try:
            from cryptography.exceptions import UnsupportedAlgorithm
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        except ImportError:
            raise PreflightError("cryptography_dependency_required") from None
        try:
            value = json.loads(credential)
            check(isinstance(value, dict) and value.get("type") == "service_account"
                  and value.get("client_email") == cloud["account_id"], "account_identity_mismatch")
            check(isinstance(value.get("private_key"), str) and 0 < len(value["private_key"]) <= 65536
                  and text(value.get("private_key_id")), "invalid_service_account_field")
            key = load_pem_private_key(value["private_key"].encode(), password=None)
            check(isinstance(key, RSAPrivateKey) and key.key_size >= 2048, "invalid_service_account_field")
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise PreflightError("invalid_service_account_field") from None
        self.cloud, self.http, self.now = cloud, http, now
        self.email, self.key, self.key_id = value["client_email"], key, value["private_key_id"]
        self.token, self.expires_at = None, 0

    def access_token(self):
        now = int(self.now())
        if self.token is not None and now < self.expires_at - 60:
            return self.token
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        header = {"alg": "RS256", "typ": "JWT", "kid": self.key_id}
        claims = {"iss": self.email, "scope": "https://www.googleapis.com/auth/cloud-platform",
                  "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}
        signing = (b64(json.dumps(header, separators=(",", ":")).encode()) + "." +
                   b64(json.dumps(claims, separators=(",", ":")).encode())).encode()
        assertion = signing.decode() + "." + b64(self.key.sign(signing, padding.PKCS1v15(), hashes.SHA256()))
        result = self.http.request("https://oauth2.googleapis.com/token", payload={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}, form=True)
        check(text(result.get("access_token")) and result.get("token_type", "").lower() == "bearer"
              and type(result.get("expires_in")) is int and 60 < result["expires_in"] <= 3600, "invalid_token_response")
        self.token, self.expires_at = result["access_token"], now + result["expires_in"]
        return self.token

    def get(self, path):
        return self.http.request(self.base + path, "Bearer " + self.access_token())

    def preflight(self, prefix):
        project_id = segment(self.cloud["project_id"])
        project = self.http.request("https://cloudresourcemanager.googleapis.com/v3/projects/" + project_id,
                                    "Bearer " + self.access_token())
        check(project.get("projectId") == self.cloud["project_id"] and project.get("state") == "ACTIVE", "project_identity_mismatch")
        billing = self.http.request("https://cloudbilling.googleapis.com/v1/projects/" + project_id + "/billingInfo",
                                    "Bearer " + self.access_token())
        check(billing.get("billingEnabled") is True, "project_billing_disabled")
        permissions = self.http.request("https://cloudresourcemanager.googleapis.com/v3/projects/" + project_id + ":testIamPermissions",
                                        "Bearer " + self.access_token(), {"permissions": list(GCP_PERMISSIONS)})
        check(isinstance(permissions.get("permissions"), list) and set(GCP_PERMISSIONS) <= set(permissions["permissions"]), "deployment_cleanup_permissions_missing")
        self.base = "https://compute.googleapis.com/compute/v1/projects/" + project_id
        zone, region = segment(self.cloud["zone"]), segment(self.cloud["region"])
        size = self.get("/zones/" + zone + "/machineTypes/" + segment(self.cloud["size"]))
        check(size.get("name") == self.cloud["size"] and type(size.get("memoryMb")) is int
              and size["memoryMb"] >= 4096, "vm_memory_below_4_gib")
        check(google_x86_machine(size), "vm_architecture_not_x86_64")
        region_info = self.get("/regions/" + region)
        quota = objects(region_info, "quotas")
        cpu = [row for row in quota if row.get("metric") == "CPUS"]
        check(type(size.get("guestCpus")) is int and size["guestCpus"] > 0 and len(cpu) == 1
              and all(type(cpu[0].get(key)) in (int, float) for key in ("limit", "usage"))
              and cpu[0]["limit"] - cpu[0]["usage"] >= size["guestCpus"], "vm_quota_unavailable")
        # An automatically-created default network is harmless. VMs and disks
        # anywhere in the dedicated test project make it an occupied project.
        for kind in ("instances", "disks"):
            groups = self.pages("/aggregated/" + kind, "items", "gcp_aggregated")
            check(not groups, "test_project_not_empty")
        for path in ("/global/networks", "/global/firewalls", "/regions/" + region + "/subnetworks"):
            rows = self.pages(path, "items", "gcp")
            check(not any(isinstance(row.get("name"), str) and row["name"].startswith(prefix) for row in rows), "resource_prefix_already_exists")
        return {"identity": "verified", "project": "verified_no_vms_or_disks", "billing": "enabled", "compute_api": "read_succeeded",
                "vm_memory_mib": size["memoryMb"], "architecture": "x86_64", "regional_cpu_quota": "available",
                "project_permissions": "required_permissions_returned", "not_verified": ["conditional_resource_permissions", "machine_family_disk_ip_quotas", "live_capacity"]}

    def pages(self, path, key, style):
        if style != "gcp_aggregated":
            return super().pages(path, key, style)
        rows, token, seen = [], None, set()
        for _ in range(MAX_PAGES):
            params = {"maxResults": 500}
            if token:
                params["pageToken"] = token
            value = self.get(path + "?" + urlencode(params))
            check(value.get("unreachables", []) == [], "incomplete_project_inventory")
            groups = value.get("items", {})
            check(isinstance(groups, dict), "invalid_provider_response")
            kind = path.rsplit("/", 1)[-1]
            for group in groups.values():
                check(isinstance(group, dict), "invalid_provider_response")
                if kind in group:
                    rows.extend(objects(group, kind))
                elif "warning" in group:
                    check(isinstance(group["warning"], dict) and group["warning"].get("code") == "NO_RESULTS_ON_PAGE", "incomplete_project_inventory")
                else:
                    check(not group, "invalid_provider_response")
            token = value.get("nextPageToken")
            if not token:
                return rows
            check(text(token) and token not in seen, "invalid_pagination")
            seen.add(token)
        raise PreflightError("pagination_limit_exceeded")


class AWS:
    def __init__(self, cloud, credentials, factory=None):
        self.cloud, self.queries = cloud, 0
        check(bool(re.fullmatch(r"(?:us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-[0-9]", cloud["region"])), "aws_region_unsupported")
        check(all(text(credentials.get(key)) for key in ("access_key", "secret_key")), "invalid_secret_field")
        if factory is None:
            try:
                import botocore.session
                from botocore.config import Config
                from botocore.configprovider import ConstantProvider
            except ImportError:
                raise PreflightError("botocore_dependency_required") from None
            session = botocore.session.Session()
            session.set_config_variable("config_file", "/dev/null")
            session.set_config_variable("credentials_file", "/dev/null")
            # set_config_variable(profile, None) falls back to AWS_PROFILE.
            # Override the provider itself to prohibit personal profile lookup.
            session.get_component("config_store").set_config_provider("profile", ConstantProvider(None))
            def factory(service):
                return session.create_client(service, region_name=cloud["region"],
                    endpoint_url="https://" + service + "." + cloud["region"] + ".amazonaws.com",
                    verify=True,
                    aws_access_key_id=credentials["access_key"], aws_secret_access_key=credentials["secret_key"],
                    aws_session_token=credentials.get("session_token"),
                    config=Config(connect_timeout=10, read_timeout=20, retries={"total_max_attempts": 1}, proxies={}))
        try:
            self.sts, self.ec2 = factory("sts"), factory("ec2")
        except Exception:
            raise PreflightError("aws_client_initialization_failed") from None

    def call(self, client, operation, **kwargs):
        try:
            result = getattr(client, operation)(**kwargs)
        except Exception:
            raise PreflightError("aws_request_failed") from None
        check(isinstance(result, dict), "invalid_provider_response")
        self.queries += 1
        return result

    def pages(self, operation, key, **kwargs):
        rows, seen = [], set()
        for _ in range(MAX_PAGES):
            value = self.call(self.ec2, operation, **kwargs)
            rows.extend(objects(value, key))
            token = value.get("NextToken")
            if not token:
                return rows
            check(text(token) and token not in seen, "invalid_pagination")
            seen.add(token)
            kwargs["NextToken"] = token
        raise PreflightError("pagination_limit_exceeded")

    def preflight(self, prefix):
        identity = self.call(self.sts, "get_caller_identity")
        check(identity.get("Account") == self.cloud["account_id"], "account_identity_mismatch")
        sizes = objects(self.call(self.ec2, "describe_instance_types", InstanceTypes=[self.cloud["size"]]), "InstanceTypes")
        check(len(sizes) == 1 and sizes[0].get("InstanceType") == self.cloud["size"], "vm_size_unavailable")
        size = sizes[0]
        memory, processor = field(size, "MemoryInfo"), field(size, "ProcessorInfo")
        check(type(memory.get("SizeInMiB")) is int and memory["SizeInMiB"] >= 4096, "vm_memory_below_4_gib")
        check("x86_64" in processor.get("SupportedArchitectures", []), "vm_architecture_not_x86_64")
        zones = objects(self.call(self.ec2, "describe_availability_zones", Filters=[{"Name": "state", "Values": ["available"]}]), "AvailabilityZones")
        check(zones and text(zones[0].get("ZoneName")), "availability_zone_unavailable")
        offerings = self.pages("describe_instance_type_offerings", "InstanceTypeOfferings", LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [self.cloud["size"]]}, {"Name": "location", "Values": [zones[0]["ZoneName"]]}])
        check(any(row.get("InstanceType") == self.cloud["size"] and row.get("Location") == zones[0]["ZoneName"] for row in offerings), "vm_size_unavailable")
        filters = [{"Name": "tag:Name", "Values": [prefix + "*"]}]
        for operation, key in (("describe_vpcs", "Vpcs"), ("describe_subnets", "Subnets"), ("describe_route_tables", "RouteTables"),
                               ("describe_internet_gateways", "InternetGateways"), ("describe_security_groups", "SecurityGroups"), ("describe_volumes", "Volumes")):
            check(not self.pages(operation, key, Filters=filters), "resource_prefix_already_exists")
        reservations = self.pages("describe_instances", "Reservations", Filters=filters)
        instances = [instance for row in reservations for instance in objects(row, "Instances")]
        check(not any(field(row, "State").get("Name") != "terminated" for row in instances), "resource_prefix_already_exists")
        check(not self.pages("describe_key_pairs", "KeyPairs", Filters=[{"Name": "key-name", "Values": [prefix + "*"]}]), "resource_prefix_already_exists")
        return {"identity": "verified", "new_vpc_prefix": "unused", "vm_memory_mib": memory["SizeInMiB"],
                "architecture": "x86_64", "size_offered_in_first_api_zone": True,
                "not_verified": ["terraform_selected_zone", "create_delete_permissions", "quotas", "live_capacity"]}


def check_tailnet(config, key, http, prefix=None):
    check(text(key), "invalid_secret_field")
    authorization = "Basic " + base64.b64encode((key + ":").encode()).decode()
    value = http.request("https://api.tailscale.com/api/v2/tailnet/" + segment(config["name"]) + "/devices", authorization)
    devices = objects(value, "devices")
    if prefix is not None:
        for row in devices:
            check(text(row.get("name")), "invalid_provider_response")
            label = row["name"].split(".", 1)[0]
            hostname = row.get("hostname", "")
            check(not any(isinstance(name, str) and (name == prefix or name.startswith(prefix + "-"))
                          for name in (label, hostname)), "tailnet_prefix_already_exists")
    matches = [row for row in devices if row.get("name", "").rstrip(".") == config["runner_dns"]]
    check(len(matches) == 1 and matches[0].get("authorized") is True, "authorized_second_peer_not_found")
    return {"second_peer": "registered_authorized", "not_verified": ["enrollment_key", "node_delete_permission", "peer_connectivity", "serve_https_browser_auth"]}


def check_dependencies(provider, aws_factory=None):
    # Check local prerequisites before prompting for any secret.
    if provider == "aws" and aws_factory is None:
        try:
            import botocore.session  # noqa: F401
        except ImportError:
            raise PreflightError("botocore_dependency_required") from None
    if provider == "gcp":
        try:
            import cryptography  # noqa: F401
        except ImportError:
            raise PreflightError("cryptography_dependency_required") from None


def preflight(config, provider, prefix, secret_reader=read_secret, http=None, aws_factory=None, model_http=None):
    validate_inputs(config, provider)
    from showcase_models import check_models, selected_models
    selected_models(config["models"])
    require(isinstance(prefix, str) and bool(re.fullmatch(r"as7-[a-z0-9-]{8,30}", prefix)), "invalid_resource_scope")
    check_dependencies(provider, aws_factory)
    http = JsonHTTP() if http is None else http
    cloud = config["providers"][provider]
    # Same field referenced more than once is read once, then reused in memory.
    cache = {}
    def secret(reference):
        if reference not in cache:
            cache[reference] = secret_reader(reference)
        return cache[reference]
    credentials = {name: secret(reference) for name, reference in cloud["credential_refs"].items()}
    if provider == "aws":
        adapter = AWS(cloud, credentials, aws_factory)
    elif provider == "gcp":
        adapter = Google(cloud, credentials["service_account_json"], http)
    else:
        extra = {'identity_proof':secret(cloud['identity_proof_ref'])} if provider == 'hetzner' and 'identity_proof_ref' in cloud else {}
        adapter = {"digitalocean": DigitalOcean, "hetzner": Hetzner}[provider](cloud, credentials["token"], http, **extra)
    report = adapter.preflight(prefix)
    tailnet = check_tailnet(config["tailnet"], secret(config["tailnet"]["cleanup_key_ref"]), http, prefix)
    models = check_models(config, secret, http=model_http)
    return {"schema": 1, "provider": provider, "status": "read_checks_passed", "cloud": report, "tailnet": tailnet, "models": models,
            "successful_read_queries": http.queries + getattr(adapter, "queries", 0) + models["successful_read_queries"], "launch_ready": False,
            "live_matrix": "not_tested", "prices_and_model_access": "metadata_only_remaining_checks_unverified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        require(os.environ.get("AGENT_STACK_LIVE_TESTS") == "1", "live_opt_in_required")
        path = Path(args.config)
        require(not path.is_symlink() and path.is_file() and path.stat().st_uid == os.geteuid()
                and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "private_config_required")
        report = preflight(json.loads(path.read_text()), args.provider, args.run_id)
    except (PolicyError, PreflightError) as error:
        report = {"provider": args.provider, "status": "fail", "reason": str(error), "launch_ready": False, "live_matrix": "not_tested"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        report = {"provider": args.provider, "status": "fail", "reason": "invalid_input_or_provider_response", "launch_ready": False, "live_matrix": "not_tested"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "read_checks_passed" else 1


if __name__ == "__main__":
    sys.exit(main())
