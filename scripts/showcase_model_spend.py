#!/usr/bin/env python3
"""Internal model-request accounting; measured terms and gateway wiring remain required.

This component exposes no CLI and never accepts client credentials or origins.
Terms supplied by the future production measurer must bound the whole supported
context (including cache writes) and generated tokens. Fixture terms are not
live pricing proof. A request's hold is durable before credentials or HTTP and
is never refunded, including after errors, process loss or successful replies.
"""
import copy
from decimal import Decimal
import hashlib
import json
import math
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from showcase_cleanup import scope
from showcase_models import ORIGINS, NoRedirect, selected_models, strict_json
from showcase_model_completion import completion
from showcase_preflight import PreflightError
from showcase_policy import PolicyError, money, require, secret_ref, validate_model_policy as validate_policy, validate_model_account as validate_account

HASH = r"[a-f0-9]{64}"
ROLES = ("openclaw", "hermes", "codex", "claude")
MAX_BODY = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
FIELDS = {
    "/v1/responses": {"model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "reasoning", "text",
        "stream", "store", "background", "previous_response_id", "service_tier", "temperature", "top_p", "max_output_tokens"},
    "/v1/chat/completions": {"model", "messages", "tools", "tool_choice", "parallel_tool_calls", "reasoning_effort", "response_format",
        "stream", "stream_options", "store", "service_tier", "temperature", "top_p", "max_completion_tokens", "max_tokens", "n"},
    "/v1/messages": {"model", "messages", "system", "tools", "tool_choice", "thinking", "output_config", "stream", "service_tier",
        "temperature", "top_p", "top_k", "max_tokens", "stop_sequences"},
}
CONTENT_TYPES = {"text", "input_text", "output_text", "message", "function_call", "function_call_output", "reasoning", "tool_use", "tool_result", "thinking", "redacted_thinking"}


def digest(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, RecursionError):
        raise PolicyError("invalid_model_spend_policy") from None


class ModelSpend:
    def __init__(self, reservations, run_id, provider, revision, policy, now=time.time):
        self.ledger, self.run_id, self.provider, self.now = reservations, run_id, provider, now
        self.policy = copy.deepcopy(policy)
        self.costs = validate_policy(self.policy)
        require(provider in ("aws", "digitalocean", "gcp", "hetzner") and isinstance(run_id, str)
                and re.fullmatch(r"as7-[a-z0-9-]{8,30}", run_id), "invalid_model_spend_policy")
        require(self.policy["source_revision"] == revision,
                "model_spend_scope_changed")
        self.binding()

    def binding(self):
        require(self.policy["scope"] == scope(self.ledger.config, self.provider), "model_spend_scope_changed")
        choices = selected_models(self.ledger.config["models"])
        require(all((row["provider"], row["model"]) == choices[role] for role, row in self.policy["roles"].items()), "model_spend_scope_changed")

    def account(self, state, initialize=False):
        require(state["provider_bindings"].get(self.provider) == {key: value for key, value in self.policy["scope"].items() if key != "provider"},
                "model_spend_scope_changed")
        rows = [row for row in state["attempts"] if row["run_id"] == self.run_id and row["provider"] == self.provider]
        require(len(rows) == 1 and rows[0]["status"] == "active", "active_model_reservation_required")
        row = rows[0]
        require("model_usd" in row and "quote_sha256" in row, "model_allocation_missing")
        require(sum(self.costs[role] * self.policy["roles"][role]["max_requests"] for role in ROLES) <= money(row["model_usd"]),
                "model_bounds_exceed_reservation")
        if "model_policy" not in row:
            require(initialize, "model_spend_account_missing")
            row.update(model_policy=copy.deepcopy(self.policy), model_requests=[])
        require(row["model_policy"] == self.policy, "model_spend_policy_changed")
        return row

    def reserve(self, role, request_sha256, timeout):
        self.binding()
        require(role in ROLES and isinstance(request_sha256, str) and re.fullmatch(HASH, request_sha256)
                and type(timeout) is int and 1 <= timeout <= 80, "invalid_model_spend_request")
        now = self.now()
        require(type(now) in (int, float) and math.isfinite(now), "invalid_model_spend_clock")
        def update(state):
            row = self.account(state, initialize=True)
            require(row["started_at"] <= now and now + timeout <= row["work_deadline"], "model_work_deadline_reached")
            holds = row["model_requests"]
            require(not any(h["role"] == role and h["request_sha256"] == request_sha256 for h in holds), "model_request_already_admitted")
            require(sum(h["role"] == role for h in holds) < self.policy["roles"][role]["max_requests"], "model_request_limit_reached")
            held = sum((money(h["held_usd"]) for h in holds), Decimal(0))
            require(held + self.costs[role] <= money(row["model_usd"]), "model_budget_exhausted")
            hold = {"role": role, "request_sha256": request_sha256, "held_usd": str(self.costs[role]), "started_at": now, "status": "uncertain"}
            holds.append(hold); validate_account(row)
            return dict(hold)
        return self.ledger.operate(update)

    def activate(self, timeout):
        """Bind the original model allocation before opening a local listener."""
        self.binding()
        require(type(timeout) is int and 1 <= timeout <= 80, "invalid_model_spend_request")
        now = self.now()
        require(type(now) in (int, float) and math.isfinite(now), "invalid_model_spend_clock")
        def update(state):
            row = self.account(state, initialize=True)
            require(row["started_at"] <= now and now + timeout <= row["work_deadline"], "model_work_deadline_reached")
        self.ledger.operate(update)

    def dispatch_allowed(self, role, request_sha256, remaining):
        self.binding()
        now = self.now()
        require(type(now) in (int, float) and math.isfinite(now) and type(remaining) in (int, float)
                and math.isfinite(remaining) and remaining > 0, "model_work_deadline_reached")
        def check(state):
            row = self.account(state)
            require(row["started_at"] <= now and now + remaining <= row["work_deadline"], "model_work_deadline_reached")
            holds = [h for h in row["model_requests"] if h["role"] == role and h["request_sha256"] == request_sha256]
            require(len(holds) == 1 and holds[0]["status"] == "uncertain", "model_request_not_pending")
        self.ledger.operate(check)

    def received(self, role, request_sha256, proof=None):
        def update(state):
            # Cleanup can close the attempt while an admitted response arrives.
            # Keep its hold unchanged; never turn this into new spending authority.
            rows = [row for row in state["attempts"] if row["run_id"] == self.run_id and row["provider"] == self.provider]
            require(len(rows) == 1 and rows[0].get("model_policy") == self.policy, "model_spend_policy_changed")
            holds = [h for h in rows[0]["model_requests"] if h["role"] == role and h["request_sha256"] == request_sha256]
            require(len(holds) == 1 and holds[0]["status"] == "uncertain", "model_request_not_pending")
            holds[0]["status"] = "response_received"
            if proof is not None:
                holds[0]["completion"] = copy.deepcopy(proof)
            validate_account(rows[0])
        self.ledger.operate(update)


def finite_shape(value):
    pending = [(value, 0)]
    while pending:
        row, depth = pending.pop()
        require(depth <= 64, "invalid_model_request")
        if isinstance(row, dict):
            pending.extend((item, depth + 1) for item in row.values())
        elif isinstance(row, list):
            pending.extend((item, depth + 1) for item in row)
        elif isinstance(row, float):
            require(math.isfinite(row), "invalid_model_request")


def content(value):
    if isinstance(value, list):
        for row in value:
            content(row)
    elif isinstance(value, dict):
        require(value.get("type", "message") in CONTENT_TYPES, "model_paid_feature_refused")
        require(not any(key in value for key in ("image_url", "file_id", "file_url", "file_data", "audio", "source")), "model_paid_feature_refused")
        if "content" in value:
            content(value["content"])
        if isinstance(value.get("output"), (list, dict)):
            content(value["output"])
    else:
        require(isinstance(value, str) or value is None, "invalid_model_request")


def prepare_request(policy, role, path, raw):
    require(role in ROLES and path in FIELDS and isinstance(raw, bytes) and 0 < len(raw) <= MAX_BODY, "model_endpoint_or_body_refused")
    terms = policy["roles"][role]; provider = terms["provider"]
    require(path == "/v1/messages" if provider == "anthropic" else path in ("/v1/responses", "/v1/chat/completions"), "model_endpoint_or_body_refused")
    try:
        value = strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise PolicyError("invalid_model_request") from None
    finite_shape(value)
    require(isinstance(value, dict) and not value.keys() - FIELDS[path] and value.get("model") == terms["model"], "model_request_scope_changed")
    require(value.get("stream", False) in (True, False) and type(value.get("stream", False)) is bool
            and (value.get("background") is None or value.get("background") is False)
            and value.get("previous_response_id") is None and (value.get("store") is None or value.get("store") is False), "model_paid_feature_refused")
    if path == "/v1/chat/completions":
        require(type(value.get("n", 1)) is int and value.get("n", 1) == 1, "model_paid_feature_refused")
        require(not ("max_tokens" in value and "max_completion_tokens" in value), "invalid_model_request")
        if value.get("stream", False):
            options = value.get("stream_options", {})
            require(isinstance(options, dict) and not options.keys() - {"include_usage", "include_obfuscation"}
                    and all(type(item) is bool for item in options.values()), "invalid_model_request")
            value["stream_options"] = options | {"include_usage": True}
    require(value.get("service_tier") in (None, "auto", "default", "standard_only"), "model_paid_feature_refused")
    value["service_tier"] = "standard_only" if provider == "anthropic" else "default"
    for key in ("input", "messages", "system"):
        if key in value:
            content(value[key])
    tools = value.get("tools", [])
    require(isinstance(tools, list) and len(tools) <= 128 and all(isinstance(tool, dict) for tool in tools), "invalid_model_request")
    require(all(tool.get("type") == "function" if provider == "openai" else "type" not in tool and isinstance(tool.get("input_schema"), dict)
                for tool in tools), "model_paid_feature_refused")
    limit_key = {"/v1/responses": "max_output_tokens", "/v1/chat/completions": "max_completion_tokens", "/v1/messages": "max_tokens"}[path]
    original = value.pop("max_tokens", None) if path == "/v1/chat/completions" else None
    limit = value.get(limit_key, original if original is not None else terms["output_tokens_upper_bound"])
    require(type(limit) is int and limit >= 16, "invalid_model_request")
    value[limit_key] = min(limit, terms["output_tokens_upper_bound"])
    if provider == "openai":
        value["store"] = False
    return ORIGINS[provider] + path, json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class RequestGate:
    """One fixed-origin HTTP POST per durable hold, no redirects/proxies/retries."""
    def __init__(self, spend, secret_reader, opener=None, now=time.monotonic, permit=None):
        self.spend, self.reader, self.now = spend, secret_reader, now
        self.permit = (lambda: True) if permit is None else permit
        self.opener = build_opener(ProxyHandler({}), NoRedirect()) if opener is None else opener

    def forward(self, role, path, raw, timeout=60, sink=None):
        began = self.now()
        url, body = prepare_request(self.spend.policy, role, path, raw)
        request_hash = digest({"path": path, "body_sha256": hashlib.sha256(body).hexdigest()})
        self.spend.reserve(role, request_hash, timeout)
        provider = self.spend.policy["roles"][role]["provider"]
        reference = self.spend.ledger.config["secret_refs"][provider + "_api_key"]
        require(secret_ref(reference), "secret_reference_required")
        try:
            require(self.permit() is True, "model_gateway_closed")
            key = self.reader(reference)
            require(isinstance(key, str) and 0 < len(key) <= 65536 and not any(c.isspace() for c in key), "invalid_secret_field")
            headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json", "User-Agent": "AgentStack-model-gate/1"}
            headers.update({"Authorization": "Bearer " + key} if provider == "openai" else {"x-api-key": key, "anthropic-version": "2023-06-01"})
            remaining = timeout - (self.now() - began)
            require(0 < remaining <= timeout, "model_request_timeout")
            self.spend.dispatch_allowed(role, request_hash, remaining)
            require(self.permit() is True, "model_gateway_closed")
            with self.opener.open(Request(url, data=body, method="POST", headers=headers), timeout=min(remaining, 20)) as response:
                require(response.status == 200, "model_request_failed")
                response_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                require(response_type in ("application/json", "text/event-stream"), "model_response_type_refused")
                if sink is not None:
                    sink.start(response_type)
                result = bytearray()
                received_bytes = 0
                while True:
                    require(0 <= self.now() - began < timeout, "model_request_timeout")
                    require(self.permit() is True, "model_gateway_closed")
                    read = response.read if sink is None else response.read1
                    chunk = read(min(65536, MAX_RESPONSE + 1 - received_bytes))
                    require(isinstance(chunk, bytes), "model_request_failed")
                    if not chunk:
                        break
                    received_bytes += len(chunk)
                    require(received_bytes <= MAX_RESPONSE, "model_response_too_large")
                    # Keep the existing bounded wire delivery incremental.
                    # Observe the same bytes privately for completion proof.
                    result.extend(chunk)
                    if sink is not None:
                        sink.write(chunk)
            require(0 <= self.now() - began < timeout, "model_request_timeout")
        except HTTPError as error:
            error.close()
            raise PolicyError("model_request_failed") from None
        except PreflightError as error:
            reason = "onepassword_unavailable_or_locked" if str(error) == "onepassword_unavailable_or_locked" else "model_request_failed"
            raise PolicyError(reason) from None
        except BaseException:
            # The admission remains uncertain and charged at its whole bound.
            raise PolicyError("model_request_failed") from None
        terms = self.spend.policy["roles"][role]
        proof = completion(provider, terms["model"], path, response_type, bytes(result), strict_json(body), terms)
        self.spend.received(role, request_hash, proof)
        if sink is not None:
            sink.complete()
        return response_type, bytes(result) if sink is None else b""
