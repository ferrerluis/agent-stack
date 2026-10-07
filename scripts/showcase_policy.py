#!/usr/bin/env python3
"""Credential-free input, plan and reservation rules for the showcase runner.

These checks authorize no cloud operation and establish no live matrix result.
The runner must additionally verify identities, prices, permissions and deletion
through provider APIs before using these rules to perform an attempt.
"""
import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.parse import urlparse
from showcase_model_completion import validate_proof

PROVIDERS = ("aws", "digitalocean", "gcp", "hetzner")
RESOURCE_TYPES = {
    "aws": {"aws_vpc", "aws_subnet", "aws_internet_gateway", "aws_route_table", "aws_route_table_association", "aws_security_group", "aws_key_pair", "aws_ebs_volume", "aws_instance", "aws_volume_attachment"},
    "digitalocean": {"digitalocean_ssh_key", "digitalocean_vpc", "digitalocean_volume", "digitalocean_droplet", "digitalocean_volume_attachment", "digitalocean_firewall", "digitalocean_project_resources"},
    "gcp": {"google_compute_network", "google_compute_subnetwork", "google_compute_firewall", "google_compute_disk", "google_compute_instance"},
    "hetzner": {"hcloud_ssh_key", "hcloud_volume", "hcloud_firewall", "hcloud_server", "hcloud_volume_attachment"},
}
ESSENTIAL_TYPES = {
    "aws": {"aws_vpc", "aws_instance", "aws_ebs_volume"},
    "digitalocean": {"digitalocean_droplet", "digitalocean_volume", "digitalocean_project_resources"},
    "gcp": {"google_compute_network", "google_compute_disk", "google_compute_instance"},
    "hetzner": {"hcloud_server", "hcloud_volume"},
}
PRICE_HOSTS = {"aws": ("aws.amazon.com",), "digitalocean": ("www.digitalocean.com", "api.digitalocean.com"),
               "gcp": ("cloud.google.com", "cloudbilling.googleapis.com"), "hetzner": ("www.hetzner.com", "api.hetzner.cloud")}


class PolicyError(Exception):
    """Only stable error codes may reach CLI output."""


def require(condition, code):
    if not condition:
        raise PolicyError(code)


def object_fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys() and not (value.keys() - set(required) - set(optional)), "invalid_fields")


def money(value):
    require(isinstance(value, (str, int)) and not isinstance(value, bool), "invalid_money")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PolicyError("invalid_money") from None
    require(result.is_finite() and result >= 0, "invalid_money")
    return result


def text(value):
    return isinstance(value, str) and bool(value.strip()) and not re.search(r"[\x00-\x1f\x7f]", value)


def secret_ref(value):
    # Require field-level references; never accept values, CLI commands or
    # item exports that could include unrelated secrets.
    from showcase_credentials import file_path
    return isinstance(value, str) and (bool(re.fullmatch(r"op://[^/\s]+/[^/\s]+/(?:[^/\s]+/)?[^/\s]+", value))
                                     or file_path(value) is not None)


def validate_inputs(config, provider):
    require(provider in PROVIDERS, "invalid_provider")
    object_fields(config, ("schema", "profile", "owner", "budget_usd", "attempt_seconds", "max_attempts", "runner", "pins", "models", "secret_refs", "tailnet", "providers"), ("funding",))
    require(type(config["schema"]) is int and config["schema"] == 1 and config["profile"] == "showcase", "invalid_profile")
    require(text(config["owner"]), "owner_required")
    require(0 < money(config["budget_usd"]) <= 25, "budget_exceeds_approval")
    if "funding" in config:
        require(config["funding"] == {"purchased_usd": "10.30", "peer_reserved_usd": "1.00",
                "prepaid_caps_usd": {"openai": "1.00", "anthropic": "1.00"}}, "funding_exceeds_approval")
        require(models_approved(config.get("models")), "approved_models_required")
    require(type(config["attempt_seconds"]) is int and 900 < config["attempt_seconds"] <= 14400, "lifetime_exceeds_approval")
    require(type(config["max_attempts"]) is int and 1 <= config["max_attempts"] <= 2, "retries_exceed_approval")
    object_fields(config["runner"], ("ssh_cidr",), ("peer_ssh",))
    require(text(config["runner"]["ssh_cidr"]), "runner_ipv4_32_required")
    try:
        cidr = ipaddress.ip_network(config["runner"]["ssh_cidr"], strict=True)
    except (ValueError, TypeError):
        raise PolicyError("runner_ipv4_32_required") from None
    require(cidr.version == 4 and cidr.prefixlen == 32 and not cidr.is_unspecified, "runner_ipv4_32_required")
    if "peer_ssh" in config["runner"]:
        peer = config["runner"]["peer_ssh"]
        object_fields(peer, ("host", "port", "username", "host_public_key", "private_key_ref"))
        try:
            address = ipaddress.ip_address(peer["host"])
            require(not address.is_loopback and not address.is_unspecified and not address.is_multicast, "peer_ssh_input_invalid")
        except (ValueError, TypeError):
            raise PolicyError("peer_ssh_input_invalid") from None
        require(type(peer["port"]) is int and 1 <= peer["port"] <= 65535
                and isinstance(peer["username"], str) and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", peer["username"])
                and isinstance(peer["host_public_key"], str) and re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?", peer["host_public_key"])
                and secret_ref(peer["private_key_ref"]), "peer_ssh_input_invalid")
    pins = config["pins"]
    object_fields(pins, ("openclaw_version", "openclaw_digest", "hermes_image", "codex_release", "claude_release"))
    require(isinstance(pins["openclaw_version"], str) and bool(re.fullmatch(r"20[0-9]{2}\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", pins["openclaw_version"])), "immutable_pins_required")
    require(isinstance(pins["openclaw_digest"], str) and bool(re.fullmatch(r"sha256:[a-f0-9]{64}", pins["openclaw_digest"])), "immutable_pins_required")
    require(isinstance(pins["hermes_image"], str) and bool(re.fullmatch(r"(?:ghcr.io/)?nousresearch/hermes-agent@sha256:[a-f0-9]{64}", pins["hermes_image"])), "immutable_pins_required")
    require(all(isinstance(pins[key], str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pins[key]) for key in ("codex_release", "claude_release")), "immutable_pins_required")
    models = config["models"]
    object_fields(models, ("openclaw", "hermes", "codex", "claude"))
    require(all(text(value) for value in models.values()) and all(re.fullmatch(r"(?:openai|anthropic)/[A-Za-z0-9_.:/-]+", models[key]) for key in ("openclaw", "hermes")), "model_ids_required")
    refs = config["secret_refs"]
    object_fields(refs, ("openai_api_key", "anthropic_api_key", "workspace_password"))
    require(all(secret_ref(value) for value in refs.values()), "secret_reference_required")
    tailnet = config["tailnet"]
    object_fields(tailnet, ("name", "runner_dns", "auth_key_ref", "cleanup_key_ref"), ("required_tag",))
    if "required_tag" in tailnet:
        require(tailnet["required_tag"] == "tag:agentstack-todo7-3bc66e", "tailnet_ownership_tag_invalid")
    require(text(tailnet["name"]) and isinstance(tailnet["runner_dns"], str) and bool(re.fullmatch(r"[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net", tailnet["runner_dns"])), "second_peer_required")
    require(all(secret_ref(tailnet[key]) for key in ("auth_key_ref", "cleanup_key_ref")), "tailnet_credentials_required")
    require(isinstance(config["providers"], dict) and not config["providers"].keys() - set(PROVIDERS), "invalid_provider")
    cloud = config["providers"].get(provider)
    required = {"account_id", "region", "size", "credential_refs"}
    if provider != "aws":
        required.update(("project_id", "isolated_project"))
    if provider == "gcp":
        required.add("zone")
    object_fields(cloud, required, ("ami_id",) if provider == "aws" else ())
    require(all(text(cloud[key]) for key in ("account_id", "region", "size")), "cloud_identity_required")
    if provider == "aws":
        require(bool(re.fullmatch(r"[0-9]{12}", cloud["account_id"])), "cloud_identity_required")
        if "ami_id" in cloud:
            require(cloud["region"] == "us-east-1" and cloud["ami_id"] == "ami-062944a84867f2386", "approved_image_required")
    else:
        require(cloud["isolated_project"] is True and text(cloud["project_id"]), "isolated_project_required")
        expression = {"digitalocean": r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", "gcp": r"[a-z][a-z0-9-]{4,28}[a-z0-9]", "hetzner": r"[0-9]+"}[provider]
        require(bool(re.fullmatch(expression, cloud["project_id"])), "isolated_project_required")
    if provider == "gcp":
        require(text(cloud["zone"]) and cloud["zone"].startswith(cloud["region"] + "-"), "zone_region_mismatch")
    credential_fields = {"aws": ("access_key", "secret_key"), "digitalocean": ("token",), "gcp": ("service_account_json",), "hetzner": ("token",)}[provider]
    object_fields(cloud["credential_refs"], credential_fields, ("session_token",) if provider == "aws" else ())
    require(all(secret_ref(value) for value in cloud["credential_refs"].values()), "secret_reference_required")
    return {"provider": provider, "status": "inputs_complete", "identity_permissions_prices_models_tailnet": "not_verified", "live_matrix": "not_tested"}


def models_approved(models):
    return models == {"openclaw": "openai/gpt-6-luna", "hermes": "anthropic/claude-haiku-4-5-20251001",
                      "codex": "gpt-6-luna", "claude": "claude-haiku-4-5-20251001"}


def validate_fresh_plan(plan, provider, resource_prefix, run_dir):
    require(provider in PROVIDERS and bool(re.fullmatch(r"as7-[a-z0-9-]{8,30}", resource_prefix)), "invalid_resource_scope")
    require(isinstance(plan, dict) and isinstance(plan.get("resource_changes"), list), "invalid_plan")
    types = set()
    addresses = set()
    for resource in plan["resource_changes"]:
        require(isinstance(resource, dict) and isinstance(resource.get("change"), dict), "invalid_plan")
        if resource.get("mode") == "data":
            require(resource["change"].get("actions") in (["read"], ["no-op"]), "invalid_data_action")
            continue
        change = resource["change"]
        require(change.get("actions") == ["create"] and change.get("before") is None and not change.get("importing"), "fresh_creates_only")
        address, kind, values = resource.get("address"), resource.get("type"), change.get("after")
        require(isinstance(address, str) and address not in addresses and isinstance(values, dict), "invalid_plan")
        addresses.add(address)
        if kind in ("random_password", "terraform_data", "local_file"):
            require(not address.startswith("module."), "unexpected_resource")
            if kind == "local_file":
                filename = values.get("filename")
                require(text(filename), "private_output_required")
                path = Path(filename)
                path = path if path.is_absolute() else Path(run_dir) / path
                require(path.resolve().is_relative_to(Path(run_dir).resolve()), "private_output_required")
            continue
        require(kind in RESOURCE_TYPES[provider] and address.startswith("module." + provider + "[0]."), "unexpected_resource")
        # Every named resource must use this attempt's prefix. Unnamed
        # associations may only refer to new IDs unknown until apply.
        tags = values.get("tags")
        name = values.get("name") or values.get("key_name") or (tags.get("Name") if isinstance(tags, dict) else None)
        unnamed = {"aws_volume_attachment", "aws_route_table_association", "digitalocean_volume_attachment", "digitalocean_project_resources", "hcloud_volume_attachment"}
        if kind not in unnamed:
            require(isinstance(name, str) and (name == resource_prefix or name.startswith(resource_prefix + "-")), "foreign_resource_name")
        relationships = {"aws_volume_attachment": ("volume_id", "instance_id"), "aws_route_table_association": ("subnet_id", "route_table_id"),
                         "digitalocean_volume_attachment": ("volume_id", "droplet_id"), "hcloud_volume_attachment": ("volume_id", "server_id")}.get(kind, ())
        if relationships:
            unknown = change.get("after_unknown")
            require(isinstance(unknown, dict) and all(unknown.get(key) is True and values.get(key) is None for key in relationships), "existing_resource_reference")
        types.add(kind)
    require(ESSENTIAL_TYPES[provider] <= types, "fresh_vm_and_disk_required")
    return {"status": "fresh_plan_checked", "resource_count": len(addresses), "identity": "requires_live_preflight"}


def resolved_plan_data(plan, module, kind, address, error):
    """Completed data reads may be stored only in a saved plan's refreshed state."""
    copies = []
    states = []
    if "planned_values" in plan:
        states.append(plan["planned_values"])
    if "prior_state" in plan:
        prior = plan["prior_state"]
        require(isinstance(prior, dict) and isinstance(prior.get("values"), dict), error)
        states.append(prior["values"])
    for state in states:
        require(isinstance(state, dict) and isinstance(state.get("root_module"), dict), error)
        modules = state["root_module"].get("child_modules", [])
        require(isinstance(modules, list) and all(isinstance(item, dict) for item in modules), error)
        selected = [item for item in modules if item.get("address") == module]
        require(len(selected) <= 1, error)
        if not selected:
            continue
        rows = selected[0].get("resources", [])
        require(isinstance(rows, list) and all(isinstance(item, dict) for item in rows), error)
        images = [item for item in rows if item.get("type") == kind or item.get("address") == address]
        require(len(images) <= 1, error)
        if images:
            image = images[0]
            require(image.get("address") == address and image.get("type") == kind and image.get("mode") == "data"
                    and isinstance(image.get("values"), dict), error)
            copies.append(image["values"])
    require(copies and all(value == copies[0] for value in copies), error)
    value = copies[0]
    changes = [item for item in plan["resource_changes"] if item.get("address") == address]
    require(len(changes) <= 1 and all(item.get("mode") == "data" and item.get("type") == kind
            and item["change"].get("actions") == ["no-op"] and item["change"].get("after") == value
            and item["change"].get("after_unknown", {}) == {} for item in changes), error)
    return value


def reserve_amount(quote, provider, attempt_seconds, now):
    require(provider in PROVIDERS and type(attempt_seconds) is int and 900 < attempt_seconds <= 14400, "invalid_price_scope")
    object_fields(quote, ("provider", "currency", "verified_at", "source", "hourly_usd", "fixed_usd", "model_usd", "cleanup_usd"), ("model_prepaid",))
    require("model_prepaid" not in quote or quote["model_prepaid"] is True, "invalid_prepaid_quote")
    require(quote["provider"] == provider and quote["currency"] == "USD", "price_scope_mismatch")
    source = urlparse(quote["source"]) if isinstance(quote["source"], str) else None
    require(source is not None and source.scheme == "https" and source.hostname in PRICE_HOSTS[provider] and not source.username and not source.password, "official_price_source_required")
    try:
        quoted = datetime.fromisoformat(quote["verified_at"].replace("Z", "+00:00"))
        require(quoted.tzinfo is not None and 0 <= now - quoted.timestamp() <= 86400, "stale_price")
    except (ValueError, TypeError, AttributeError):
        raise PolicyError("stale_price") from None
    hourly, fixed, model, cleanup = (money(quote[key]) for key in ("hourly_usd", "fixed_usd", "model_usd", "cleanup_usd"))
    require(hourly > 0 and model > 0 and cleanup > 0, "complete_price_required")
    return (hourly * Decimal(attempt_seconds) / 3600 + fixed + (0 if quote.get("model_prepaid") else model) + cleanup).quantize(Decimal(".01"), rounding=ROUND_CEILING)


MODEL_ROLES = ("openclaw", "hermes", "codex", "claude")
MODEL_HASH = r"[a-f0-9]{64}"


def validate_model_policy(value):
    object_fields(value, ("source_revision", "scope", "roles"))
    require(isinstance(value["source_revision"], str) and re.fullmatch(r"[a-f0-9]{40}", value["source_revision"])
            and isinstance(value["scope"], dict) and value["scope"].get("provider") in ("aws", "digitalocean", "gcp", "hetzner"), "invalid_model_spend_policy")
    object_fields(value["scope"], ("provider", "account_id", "project_id", "region", "zone"))
    require(all(isinstance(item, str) for item in value["scope"].values()), "invalid_model_spend_policy")
    require(isinstance(value["roles"], dict) and set(value["roles"]) == set(MODEL_ROLES), "invalid_model_spend_policy")
    costs = {}
    for role, row in value["roles"].items():
        object_fields(row, ("provider", "model", "context_tokens_upper_bound", "output_tokens_upper_bound", "max_requests",
                            "input_usd_per_million_upper_bound", "output_usd_per_million_upper_bound", "terms_sha256"))
        require(row["provider"] in ("openai", "anthropic") and isinstance(row["model"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", row["model"])
                and isinstance(row["terms_sha256"], str) and re.fullmatch(MODEL_HASH, row["terms_sha256"]), "invalid_model_spend_policy")
        require(type(row["context_tokens_upper_bound"]) is int and 1 <= row["context_tokens_upper_bound"] <= 2000000
                and type(row["output_tokens_upper_bound"]) is int and 16 <= row["output_tokens_upper_bound"] <= 4096
                and type(row["max_requests"]) is int and 1 <= row["max_requests"] <= 32, "invalid_model_spend_policy")
        names = ("input_usd_per_million_upper_bound", "output_usd_per_million_upper_bound")
        require(all(isinstance(row[key], str) and re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,9})?", row[key]) for key in names),
                "invalid_model_spend_policy")
        rates = [money(row[key]) for key in names]
        require(all(0 < rate <= 1000 for rate in rates), "invalid_model_spend_policy")
        costs[role] = ((row["context_tokens_upper_bound"] * rates[0] + row["output_tokens_upper_bound"] * rates[1]) / 1000000).quantize(
            Decimal(".000000001"), rounding=ROUND_CEILING)
    return costs


def validate_model_account(row):
    """Validate optional accounting on old/new reservation rows without migration."""
    allocated = any(key in row for key in ("model_usd", "quote_sha256", "price_quote"))
    require(not allocated or all(key in row for key in ("model_usd", "quote_sha256", "price_quote")), "invalid_model_spend_account")
    if allocated:
        require(0 < money(row["model_usd"]) and (row["price_quote"].get("model_prepaid") is True or money(row["model_usd"]) <= money(row["reserved_usd"]))
                and isinstance(row["quote_sha256"], str) and re.fullmatch(MODEL_HASH, row["quote_sha256"]), "invalid_model_spend_account")
        require(all(type(row.get(key)) in (int, float) and math.isfinite(row[key]) for key in ("started_at", "deadline", "work_deadline")),
                "invalid_model_spend_account")
        seconds = row["deadline"] - row["started_at"]
        require(seconds == int(seconds) and row["work_deadline"] == row["deadline"] - 900, "invalid_model_spend_account")
        require(money(row["reserved_usd"]) == reserve_amount(row["price_quote"], row["provider"], int(seconds), row["started_at"])
                and money(row["model_usd"]) == money(row["price_quote"]["model_usd"])
                and hashlib.sha256(json.dumps(row["price_quote"], sort_keys=True, separators=(",", ":")).encode()).hexdigest() == row["quote_sha256"],
                "model_quote_allocation_changed")
    initialized = "model_policy" in row or "model_requests" in row
    require(not initialized or allocated and "model_policy" in row and "model_requests" in row, "invalid_model_spend_account")
    if not initialized:
        return
    costs = validate_model_policy(row["model_policy"])
    require(row["model_policy"]["scope"].get("provider") == row["provider"], "invalid_model_spend_account")
    require(all(type(row.get(key)) in (int, float) and math.isfinite(row[key]) for key in ("started_at", "work_deadline")),
            "invalid_model_spend_account")
    holds = row["model_requests"]
    require(isinstance(holds, list) and len(holds) <= 128, "invalid_model_spend_account")
    seen, completions, counts, total = set(), set(), dict.fromkeys(MODEL_ROLES, 0), Decimal(0)
    for hold in holds:
        object_fields(hold, ("role", "request_sha256", "held_usd", "started_at", "status"), ("completion", "input_tokens_upper_bound"))
        require(hold["role"] in MODEL_ROLES and isinstance(hold["request_sha256"], str) and re.fullmatch(MODEL_HASH, hold["request_sha256"]), "invalid_model_spend_account")
        key = (hold["role"], hold["request_sha256"])
        expected = costs[hold["role"]]
        if "input_tokens_upper_bound" in hold:
            terms = row["model_policy"]["roles"][hold["role"]]
            require(type(hold["input_tokens_upper_bound"]) is int and 1 <= hold["input_tokens_upper_bound"] <= terms["context_tokens_upper_bound"], "invalid_model_spend_account")
            expected = request_cost(terms, hold["input_tokens_upper_bound"])
        require(key not in seen and money(hold["held_usd"]) == expected
                and type(hold["started_at"]) in (int, float) and math.isfinite(hold["started_at"])
                and row["started_at"] <= hold["started_at"] < row["work_deadline"]
                and hold["status"] in ("uncertain", "response_received"), "invalid_model_spend_account")
        if "completion" in hold:
            require(hold["status"] == "response_received", "invalid_model_spend_account")
            try:
                validate_proof(hold["completion"], row["model_policy"]["roles"][hold["role"]])
            except (ValueError, TypeError, KeyError):
                raise PolicyError("invalid_model_completion_account") from None
            proof_key = (hold["completion"]["provider"], hold["completion"]["response_id_sha256"])
            require(proof_key not in completions, "model_completion_already_recorded")
            completions.add(proof_key)
            if "input_tokens_upper_bound" in hold:
                require(hold["completion"]["usage"]["context_tokens"] <= hold["input_tokens_upper_bound"],
                        "model_usage_exceeds_admitted_bound")
        seen.add(key); counts[hold["role"]] += 1; total += expected
    require(all(counts[role] <= row["model_policy"]["roles"][role]["max_requests"] for role in MODEL_ROLES)
            and total <= money(row["model_usd"]), "invalid_model_spend_account")


def request_cost(terms, input_tokens):
    return ((input_tokens * money(terms["input_usd_per_million_upper_bound"])
             + terms["output_tokens_upper_bound"] * money(terms["output_usd_per_million_upper_bound"]))
            / 1000000).quantize(Decimal(".000000001"), rounding=ROUND_CEILING)


def prepaid_charge(hold, terms):
    # Keep the original hold intact. A verified complete provider usage record
    # supplies a tighter upper bound; HTTP success alone supplies no discount.
    if hold.get("status") != "response_received" or "completion" not in hold:
        return money(hold["held_usd"])
    try:
        validate_proof(hold["completion"], terms)
    except (ValueError, TypeError, KeyError):
        raise PolicyError("invalid_model_completion_account") from None
    usage = hold["completion"]["usage"]
    cost = ((usage["context_tokens"] * money(terms["input_usd_per_million_upper_bound"])
            + usage["output_tokens"] * money(terms["output_usd_per_million_upper_bound"])) / 1000000).quantize(
                Decimal(".000000001"), rounding=ROUND_CEILING)
    require(cost <= money(hold["held_usd"]), "model_usage_exceeds_admitted_bound")
    return cost


class Reservations:
    """One private batch ledger, exclusive per process and conservative on costs.

    A completed attempt still consumes its full reservation. Never infer a
    refund from elapsed time or delayed provider billing. A missing cleanup
    receipt blocks every new launch but never blocks recovery/deletion.
    """
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.config = config
        # Other providers may become ready later, and repairs may change pins
        # or credentials. Neither may reset costs or past attempts.
        self.fingerprint = hashlib.sha256(config["owner"].encode()).hexdigest()

    def operate(self, update):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not self.directory.is_symlink() and self.directory.stat().st_uid == os.geteuid() and stat.S_IMODE(self.directory.stat().st_mode) & 0o077 == 0, "private_ledger_required")
        lock_fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.directory / "reservations.json"
            require(not path.is_symlink(), "private_ledger_required")
            if path.exists():
                require(not path.is_symlink() and path.stat().st_uid == os.geteuid() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "private_ledger_required")
                try:
                    state = json.loads(path.read_text())
                except (ValueError, OSError):
                    raise PolicyError("ledger_unreadable") from None
            else:
                state = {"schema": 3, "owner_fingerprint": self.fingerprint, "limits": {
                    "budget_usd": self.config["budget_usd"], "max_attempts": self.config["max_attempts"], "attempt_seconds": self.config["attempt_seconds"]
                }, "provider_bindings": {}, "attempts": []}
                if "funding" in self.config:
                    state["funding"] = self.config["funding"]
            require(isinstance(state, dict) and state.get("schema") == 3 and state.get("owner_fingerprint") == self.fingerprint
                    and isinstance(state.get("attempts"), list) and isinstance(state.get("provider_bindings"), dict), "ledger_scope_mismatch")
            object_fields(state.get("limits"), ("budget_usd", "max_attempts", "attempt_seconds"))
            require(state.get("funding") == self.config.get("funding"), "ledger_funding_changed")
            require(money(self.config["budget_usd"]) <= money(state["limits"]["budget_usd"])
                    and self.config["max_attempts"] <= state["limits"]["max_attempts"]
                    and self.config["attempt_seconds"] <= state["limits"]["attempt_seconds"], "ledger_scope_mismatch")
            for attempt in state["attempts"]:
                object_fields(attempt, ("run_id", "provider", "reserved_usd", "started_at", "deadline", "work_deadline", "status"),
                              ("cleanup_receipt", "model_usd", "quote_sha256", "price_quote", "model_policy", "model_requests"))
                require(attempt["status"] in ("active", "cleaned") and attempt["provider"] in PROVIDERS, "ledger_unreadable")
                money(attempt["reserved_usd"])
                validate_model_account(attempt)
            result = update(state)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as file:
                temporary = Path(file.name)
                try:
                    json.dump(state, file, sort_keys=True)
                    file.flush()
                    os.fsync(file.fileno())
                    temporary.chmod(0o600)
                    temporary.replace(path)
                    directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    temporary.unlink(missing_ok=True)
            return result

    def reserve(self, provider, run_id, quote, now=None):
        now = time.time() if now is None else now
        validate_inputs(self.config, provider)
        require(bool(re.fullmatch(r"as7-[a-z0-9-]{8,30}", run_id)), "invalid_run_id")
        amount = reserve_amount(quote, provider, self.config["attempt_seconds"], now)
        require(bool(quote.get("model_prepaid")) == ("funding" in self.config), "prepaid_funding_required")
        def update(state):
            attempts = state["attempts"]
            require(not any(item["status"] == "active" for item in attempts), "cleanup_required_before_launch")
            require(not any(item["run_id"] == run_id for item in attempts), "run_id_already_used")
            require(sum(item["provider"] == provider for item in attempts) < self.config["max_attempts"], "attempt_limit_reached")
            funding = self.config.get("funding", {})
            committed = money(funding.get("purchased_usd", "0")) + money(funding.get("peer_reserved_usd", "0"))
            require(committed + sum((money(item["reserved_usd"]) for item in attempts), Decimal(0)) + amount <= money(self.config["budget_usd"]), "insufficient_budget")
            cloud = self.config["providers"][provider]
            binding = {"account_id": cloud["account_id"], "project_id": cloud.get("project_id", ""),
                       "region": cloud["region"], "zone": cloud.get("zone", "")}
            require(provider not in state["provider_bindings"] or state["provider_bindings"][provider] == binding, "provider_scope_changed")
            state["provider_bindings"][provider] = binding
            attempt = {"run_id": run_id, "provider": provider, "reserved_usd": str(amount), "started_at": now,
                       "deadline": now + self.config["attempt_seconds"], "work_deadline": now + self.config["attempt_seconds"] - 900, "status": "active",
                       "model_usd": str(money(quote["model_usd"])),
                       "price_quote": json.loads(json.dumps(quote)),
                       "quote_sha256": hashlib.sha256(json.dumps(quote, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            attempts.append(attempt)
            return dict(attempt)
        return self.operate(update)

    def confirm_cleanup(self, run_id, receipt):
        object_fields(receipt, ("provider", "account_id", "project_id", "region", "zone", "terraform_state_verified_empty", "cloud_queries_succeeded", "owned_ids_checked", "remaining_ids", "tailnet_queries_succeeded", "tailnet_nodes_checked", "remaining_tailnet_nodes", "verified_at"), ("terraform_state_proof",))
        require(receipt.get("terraform_state_proof", "destroyed_empty_local_state") in ("destroyed_empty_local_state", "never_applied_verified_absent"), "invalid_state_proof")
        require(receipt["terraform_state_verified_empty"] is True, "terraform_state_removal_unverified")
        require(type(receipt["owned_ids_checked"]) is int and receipt["owned_ids_checked"] >= 0 and receipt["remaining_ids"] == []
                and type(receipt["tailnet_nodes_checked"]) is int and receipt["tailnet_nodes_checked"] >= 0 and receipt["remaining_tailnet_nodes"] == []
                and all(type(receipt[key]) is int and receipt[key] > 0 for key in ("cloud_queries_succeeded", "tailnet_queries_succeeded")), "cleanup_unverified")
        def update(state):
            matches = [item for item in state["attempts"] if item["run_id"] == run_id]
            require(len(matches) == 1 and matches[0]["status"] == "active", "active_attempt_required")
            attempt = matches[0]
            binding = state["provider_bindings"].get(attempt["provider"])
            require(receipt["provider"] == attempt["provider"] and isinstance(binding, dict)
                    and all(receipt[key] == binding.get(key) for key in ("account_id", "project_id", "region", "zone")), "cleanup_identity_mismatch")
            require(type(receipt["verified_at"]) in (int, float) and receipt["verified_at"] >= attempt["started_at"], "cleanup_unverified")
            attempt.update(status="cleaned", cleanup_receipt=receipt)
            return {"run_id": run_id, "cleanup": "verified", "reserved_usd": attempt["reserved_usd"]}
        return self.operate(update)


def main():
    parser = argparse.ArgumentParser(description="Check showcase input structure without retrieving secrets or contacting providers.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    args = parser.parse_args()
    try:
        path = Path(args.config)
        require(not path.is_symlink() and path.stat().st_uid == os.geteuid() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "private_config_required")
        report = validate_inputs(json.loads(path.read_text()), args.provider)
    except PolicyError as error:
        report = {"status": "fail", "reason": str(error), "live_matrix": "not_tested"}
    except (OSError, ValueError, TypeError):
        report = {"status": "fail", "reason": "config_unreadable", "live_matrix": "not_tested"}
    print(json.dumps(report))
    return 0 if report["status"] == "inputs_complete" else 1


if __name__ == "__main__":
    sys.exit(main())
