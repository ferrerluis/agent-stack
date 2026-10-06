#!/usr/bin/env python3
"""Authenticated model metadata only; no generation, pricing or launch proof.

Use the selected 1Password fields once per session. Requests have fixed origins,
no proxy/redirect/retry and never return raw provider bodies or private headers.
"""
from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from showcase_policy import PolicyError, require as check, secret_ref, text

MODEL_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}"
ORIGINS = {"openai": "https://api.openai.com", "anthropic": "https://api.anthropic.com"}
MAX_RESPONSE = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise PolicyError("model_redirect_refused")


def selected_models(value):
    check(isinstance(value, dict) and set(value) == {"openclaw", "hermes", "codex", "claude"}, "model_ids_required")
    result = {}
    for role in ("openclaw", "hermes", "codex", "claude"):
        model = value[role]
        check(isinstance(model, str), "model_ids_required")
        if role in ("openclaw", "hermes"):
            provider, separator, model = model.partition("/")
            check(separator == "/" and provider in ORIGINS, "model_provider_unsupported")
        else:
            provider = "openai" if role == "codex" else "anthropic"
        check(re.fullmatch(MODEL_ID, model) is not None, "model_id_unsupported")
        result[role] = (provider, model)
    return result


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite value")))


class ModelHTTP:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def retrieve(self, provider, model, key):
        check(provider in ORIGINS and isinstance(model, str) and re.fullmatch(MODEL_ID, model), "model_id_unsupported")
        check(text(key) and len(key) <= 65536 and key == key.strip() and key.isascii(), "invalid_model_key_field")
        url = ORIGINS[provider] + "/v1/models/" + quote(model, safe="")
        headers = {"Accept": "application/json", "User-Agent": "AgentStack-showcase-model-metadata/1"}
        if provider == "openai":
            headers["Authorization"] = "Bearer " + key
        else:
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            with self.opener.open(Request(url, method="GET", headers=headers), timeout=20) as response:
                check(response.status == 200, "model_metadata_request_failed")
                raw = response.read(MAX_RESPONSE + 1)
            check(len(raw) <= MAX_RESPONSE, "model_metadata_response_too_large")
            value = strict_json(raw)
            check(isinstance(value, dict), "invalid_model_metadata")
            return value
        except HTTPError as error:
            code = error.code
            error.close()
            reason = {401: "model_key_rejected", 403: "model_metadata_access_denied", 404: "model_not_found",
                      429: "model_metadata_rate_limited"}.get(code, "model_metadata_request_failed")
            raise PolicyError(reason) from None
        except (URLError, OSError, ValueError, TypeError, UnicodeError):
            raise PolicyError("model_metadata_request_failed") from None


def validate_metadata(value, provider, model, horizon):
    check(isinstance(value, dict) and value.get("id") == model, "model_alias_or_identity_mismatch")
    check(value.get("object" if provider == "openai" else "type") == "model", "invalid_model_metadata")
    shutdown = value.get("shutdown_date")
    if shutdown is not None:
        check(isinstance(shutdown, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", shutdown), "invalid_model_metadata")
        try:
            deadline = datetime.strptime(shutdown, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            raise PolicyError("invalid_model_metadata") from None
        check(deadline > horizon, "model_shuts_down_before_attempt_ends")
    # Do not publish owner, display name, capabilities, limits or provider data.
    # Model visibility does not prove the selected runtime/API can execute it.
    return {"provider": provider, "model": model, "status": "metadata_read_passed"}


def check_models(config, secret_reader, http=None, now=time.time):
    choices = selected_models(config.get("models"))
    refs = config.get("secret_refs")
    check(isinstance(refs, dict) and all(secret_ref(refs.get(name)) for name in ("openai_api_key", "anthropic_api_key")), "secret_reference_required")
    seconds = config.get("attempt_seconds")
    measured = now()
    check(type(seconds) is int and 900 < seconds <= 14400 and type(measured) in (int, float)
          and math.isfinite(measured) and measured >= 0, "invalid_model_metadata_scope")
    http = ModelHTTP() if http is None else http
    secrets, metadata, roles = {}, {}, {}
    for role, (provider, model) in choices.items():
        reference = refs[provider + "_api_key"]
        if reference not in secrets:
            secrets[reference] = secret_reader(reference)
            key = secrets[reference]
            check(text(key) and len(key) <= 65536 and key == key.strip() and key.isascii(), "invalid_model_key_field")
        pair = (provider, model)
        if pair not in metadata:
            metadata[pair] = http.retrieve(provider, model, secrets[reference])
            validate_metadata(metadata[pair], provider, model, measured + seconds)
    finished = now()
    check(type(finished) in (int, float) and math.isfinite(finished) and measured <= finished <= measured + 300,
          "model_metadata_measurement_expired")
    for role, (provider, model) in choices.items():
        roles[role] = validate_metadata(metadata[(provider, model)], provider, model, finished + seconds)
    return {"status": "metadata_read_checks_passed", "verified_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
            "roles": roles, "successful_read_queries": len(metadata), "generation": "not_tested", "launch_gate": "unverified",
            "not_verified": ["generation_permissions", "billing_and_rate_limits", "runtime_and_cli_compatibility", "model_prices"]}
