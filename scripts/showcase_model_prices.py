#!/usr/bin/env python3
"""Internal selected-model terms reader; no generation or paid launch authority.

Only fixed public documentation and selected authenticated model GETs are read.
Supported text/cache categories use their highest published rate, including
documented long-context and regional uplifts. Unknown contracts fail closed.
Account adjustments, gateway routing and real execution remain separate gates.
"""
import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from showcase_cleanup import scope
from showcase_model_spend import digest
from showcase_models import ModelHTTP, NoRedirect, selected_models, validate_metadata
from showcase_policy import PolicyError, require, secret_ref, validate_model_policy
from showcase_prices import PriceHTML, dollars

MAX_DOCUMENT = 2 * 1024 * 1024
SOURCES = {
    "anthropic_models": "https://platform.claude.com/docs/en/models/overview",
    "anthropic_prices": "https://platform.claude.com/docs/en/about-claude/pricing",
    "anthropic_versions": "https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions",
}
OPENAI_DOCS = "https://developers.openai.com/api/docs/models/"


def source_url(name, model=None):
    if name in SOURCES:
        require(model is None, "model_terms_source_refused")
        return SOURCES[name]
    require(name == "openai_model" and isinstance(model, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,100}", model), "model_terms_source_refused")
    return OPENAI_DOCS + re.sub(r"-20[0-9]{2}-[0-9]{2}-[0-9]{2}$", "", model)


class PublicModelTerms:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def fetch(self, name, model=None):
        url = source_url(name, model)
        try:
            with self.opener.open(Request(url, headers={"Accept": "text/html", "User-Agent": "AgentStack-model-terms/1"}), timeout=20) as response:
                require(response.status == 200, "model_terms_request_failed")
                require(response.headers.get("Content-Type", "").split(";", 1)[0].strip() == "text/html", "model_terms_document_type_refused")
                raw = response.read(MAX_DOCUMENT + 1)
            require(0 < len(raw) <= MAX_DOCUMENT, "model_terms_document_too_large")
            document = raw.decode("utf-8")
        except HTTPError as error:
            error.close()
            raise PolicyError("model_terms_request_failed") from None
        except (URLError, OSError, UnicodeError, ValueError):
            raise PolicyError("model_terms_request_failed") from None
        return {"source": url, "body_sha256": hashlib.sha256(raw).hexdigest(), "document": document}


def visible(document):
    parsed = PriceHTML.parse(document)
    return parsed, " ".join(" ".join(parsed.text).split())


def one(values, reason):
    require(len(values) == 1, reason)
    return values[0]


def rate(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,9})?", value), "model_terms_rate_unsupported")
    result = Decimal(value)
    require(0 < result <= 1000, "model_terms_rate_unsupported")
    return result


def tokens(value):
    match = re.fullmatch(r"([0-9][0-9,]*)([KM]?) tokens", value)
    require(match is not None, "model_terms_limits_unsupported")
    amount = int(match[1].replace(",", "")) * {"": 1, "K": 1000, "M": 1000000}[match[2]]
    require(16 <= amount <= 2000000, "model_terms_limits_unsupported")
    return amount


def openai_terms(document, model):
    _, text = visible(document)
    base = source_url("openai_model", model).removeprefix(OPENAI_DOCS)
    snapshots = one(re.findall(r"Snapshots (.*?) Rate limits", text), "model_terms_identity_unsupported")
    if model != base:
        require(re.fullmatch(re.escape(base) + r"-20[0-9]{2}-[0-9]{2}-[0-9]{2}", model)
                and re.search(r"(?<![A-Za-z0-9.-])" + re.escape(model) + r"(?![A-Za-z0-9.-])", snapshots), "model_terms_identity_unsupported")
    else:
        # Current release IDs can be canonical without a date. Earlier pages
        # listing both snapshots and a convenience alias must use a snapshot.
        require("Use " + model + " to select this model." in snapshots
                and not re.search(re.escape(base) + r"-20[0-9]{2}-[0-9]{2}-[0-9]{2}", snapshots), "model_snapshot_required")
    context = int(one(re.findall(r"([0-9][0-9,]*) context window", text), "model_terms_limits_unsupported").replace(",", ""))
    output = int(one(re.findall(r"([0-9][0-9,]*) max output tokens", text), "model_terms_limits_unsupported").replace(",", ""))
    require(16 <= output <= context <= 2000000, "model_terms_limits_unsupported")
    pricing = one(re.findall(r"Text tokens Per 1M tokens (.*?) Modalities", text), "model_terms_price_unsupported")
    # Ignore the UI's Batch toggle, never the selected default prices.
    pricing = re.sub(r"^∙ Batch API price ", "", pricing)
    match = re.match(r"Input \$([0-9.]+) Cached input \$([0-9.]+) (?:Cache writes \$([0-9.]+) )?Output \$([0-9.]+)(?: |$)", pricing)
    require(match is not None, "model_terms_price_unsupported")
    input_rate = max(rate(value) for value in match.groups()[:3] if value is not None)
    output_rate = rate(match[4])
    tail = pricing[match.end():]
    # Old model cards include comparison prices. They are not price modifiers,
    # but this version deliberately refuses such cards until fully supported.
    require("Quick comparison" not in tail, "model_terms_price_unsupported")
    tail = re.sub(r"Cached input tokens are priced at [0-9]+% of the uncached input token rate\. ?", "", tail)
    tail = re.sub(r"Cache writes are billed at 1\.25x the uncached input token rate\. ?", "", tail)
    long_context = re.findall(r"Prompts with more than ([0-9]+)K input tokens are priced at ([0-9.]+)x input and cache rates and ([0-9.]+)x output for the full request\.", tail)
    require(len(long_context) <= 1, "model_terms_price_unsupported")
    if long_context:
        threshold, input_factor, output_factor = long_context[0]
        require(16 <= int(threshold) * 1000 < context, "model_terms_price_unsupported")
        input_rate *= max(Decimal(1), rate(input_factor)); output_rate *= max(Decimal(1), rate(output_factor))
        tail = re.sub(r"Prompts with more than [0-9]+K input tokens are priced at [0-9.]+x input and cache rates and [0-9.]+x output for the full request\. ?", "", tail)
    tail = tail.replace("Fast mode prices are 2x Standard. Batch and Flex prices are 50% lower than Standard.", "")
    require("Regional processing adds a 10% premium where available." in tail, "model_terms_price_unsupported")
    tail = tail.replace("Regional processing adds a 10% premium where available.", "")
    require(not tail.strip(), "model_terms_price_unsupported")
    # Budget even the documented regional premium; the gateway still fixes the
    # global origin and standard tier. Never rely on discounts or cache hits.
    return {"context_tokens_upper_bound": context, "published_max_output_tokens": output,
            "input_usd_per_million_upper_bound": dollars(input_rate * Decimal("1.1")),
            "output_usd_per_million_upper_bound": dollars(output_rate * Decimal("1.1"))}


def anthropic_terms(documents, model, metadata):
    overview, _ = visible(documents["anthropic_models"])
    tables = [table for table in overview.tables if table and table[0][0] == "Feature"]
    table = one(tables, "model_terms_identity_unsupported")
    rows = {}
    for row in table[1:]:
        require(row[0] not in rows, "model_terms_identity_unsupported")
        rows[row[0]] = row[1:]
    for name in ("Claude API ID", "Context window", "Max output"):
        require(name in rows and len(rows[name]) == len(table[0]) - 1, "model_terms_limits_unsupported")
    index = one([index for index, item in enumerate(rows["Claude API ID"]) if item == model], "model_terms_identity_unsupported")
    label = re.match(r"(Claude [A-Za-z]+ [0-9]+(?:\.[0-9]+)?)(?: |$)", table[0][index + 1])
    require(label is not None, "model_terms_identity_unsupported")
    _, versioning = visible(documents["anthropic_versions"])
    require("Each Claude model ID identifies a pinned version of the model." in versioning
            and "For the 4.6 generation and later, the dateless ID is the canonical model ID for that release." in versioning,
            "model_terms_versioning_unsupported")
    context, output = tokens(rows["Context window"][index]), tokens(rows["Max output"][index])
    require(type(metadata.get("max_input_tokens")) is int and type(metadata.get("max_tokens")) is int
            and metadata["max_input_tokens"] == context and metadata["max_tokens"] == output,
            "model_terms_metadata_limits_changed")
    parsed, pricing = visible(documents["anthropic_prices"])
    prices = one([table for table in parsed.tables if len(table) >= 2 and table[1] ==
                  ["Name", "Input", "Output", "5m writes", "1h writes", "Hits and refreshes"]], "model_terms_price_unsupported")
    matches = [row for row in prices[2:] if row and re.match(re.escape(label[1]) + r"(?: |$)", row[0])]
    row = one(matches, "model_terms_price_unsupported")
    require(len(row) == 6, "model_terms_price_unsupported")
    values = []
    for cell in row[1:]:
        match = re.fullmatch(r"\$([0-9.]+) / MTok", cell)
        require(match is not None, "model_terms_rate_unsupported")
        values.append(rate(match[1]))
    require("incurs a 1.1x multiplier on all token pricing categories" in pricing
            and "Global routing (the default) uses standard pricing." in pricing, "model_terms_price_unsupported")
    if context > 200000:
        version = tuple(int(part) for part in label[1].rsplit(" ", 1)[1].split("."))
        require("include the full 1M token context window at standard pricing." in pricing
                and version >= (4, 6) and not re.search(r"-20[0-9]{6}$", model), "model_terms_price_unsupported")
    return {"context_tokens_upper_bound": context, "published_max_output_tokens": output,
            "input_usd_per_million_upper_bound": dollars(max(values[0], *values[2:]) * Decimal("1.1")),
            "output_usd_per_million_upper_bound": dollars(values[1] * Decimal("1.1"))}


def measure_model_terms(config, provider, revision, plan_sha256, secret_reader, http=None, public=None, now=time.time):
    choices = selected_models(config.get("models"))
    require(provider in config.get("providers", {}) and isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
            and isinstance(plan_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", plan_sha256), "model_terms_scope_changed")
    seconds = config.get("attempt_seconds")
    require(type(seconds) is int and 900 < seconds <= 14400, "model_terms_scope_changed")
    refs = config.get("secret_refs", {})
    require(all(secret_ref(refs.get(name)) for name in ("openai_api_key", "anthropic_api_key")), "secret_reference_required")
    binding = {"scope": scope(config, provider), "models": copy.deepcopy(config["models"]),
               "refs": {name: refs[name] for name in ("openai_api_key", "anthropic_api_key")}, "seconds": seconds}
    started = now()
    require(type(started) in (int, float) and math.isfinite(started) and 0 <= started <= 253402286400, "invalid_model_terms_clock")
    http, public = (ModelHTTP() if http is None else http), (PublicModelTerms() if public is None else public)
    documents, sources, keys, metadata, terms = {}, {}, {}, {}, {}
    def document(name, model=None):
        url = source_url(name, model)
        if url not in documents:
            fetched = public.fetch(name, model)
            require(isinstance(fetched, dict) and fetched.get("source") == url and isinstance(fetched.get("document"), str)
                    and 0 < len(fetched["document"].encode()) <= MAX_DOCUMENT
                    and hashlib.sha256(fetched["document"].encode()).hexdigest() == fetched.get("body_sha256"), "model_terms_source_changed")
            documents[url] = fetched["document"]; sources[url] = fetched["body_sha256"]
        return documents[url]
    for model_provider, model in dict.fromkeys(choices.values()):
        if model_provider == "openai":
            measured = openai_terms(document("openai_model", model), model)
        else:
            measured = None
            for name in SOURCES: document(name)
        ref = refs[model_provider + "_api_key"]
        if ref not in keys: keys[ref] = secret_reader(ref)
        require(isinstance(keys[ref], str) and 0 < len(keys[ref]) <= 65536 and keys[ref].isascii()
                and not any(char.isspace() for char in keys[ref]), "invalid_model_key_field")
        raw = http.retrieve(model_provider, model, keys[ref])
        validate_metadata(raw, model_provider, model, started + seconds)
        if measured is None:
            measured = anthropic_terms({name: documents[url] for name, url in SOURCES.items()}, model, raw)
        metadata[(model_provider, model)] = raw
        terms[(model_provider, model)] = {"provider": model_provider, "model": model, **measured,
            "metadata_sha256": digest({key: raw[key] for key in ("id", "max_input_tokens", "max_tokens", "shutdown_date") if key in raw})}
    finished = now()
    require(binding == {"scope": scope(config, provider), "models": config["models"],
                       "refs": {name: config["secret_refs"][name] for name in ("openai_api_key", "anthropic_api_key")},
                       "seconds": config["attempt_seconds"]}, "model_terms_scope_changed")
    require(type(finished) in (int, float) and math.isfinite(finished) and started <= finished <= started + 300, "model_terms_measurement_expired")
    for pair, raw in metadata.items(): validate_metadata(raw, *pair, finished + seconds)
    roles = {}
    for role, pair in choices.items():
        row = copy.deepcopy(terms[pair]); row["terms_sha256"] = digest({"terms": row, "sources": sources})
        roles[role] = row
    return {"schema": 1, "status": "model_unit_prices_measured", "source_revision": revision, "scope": scope(config, provider),
            "plan_sha256": plan_sha256, "verified_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
            "roles": roles, "sources": sources, "launch_gate": "unverified", "complete_quote": False,
            "not_verified": ["gateway_routing_and_protocol", "generation_permissions_and_billing", "account_tax_and_adjustments", "execution_cost_bound"]}


def policy_from_terms(receipt, config, provider, revision, plan_sha256, limits, now=time.time):
    """Internal conversion; production must obtain receipts from its own reader."""
    require(isinstance(receipt, dict) and receipt.get("schema") == 1 and receipt.get("status") == "model_unit_prices_measured"
            and receipt.get("source_revision") == revision and receipt.get("scope") == scope(config, provider)
            and receipt.get("plan_sha256") == plan_sha256 and receipt.get("complete_quote") is False
            and receipt.get("launch_gate") == "unverified", "model_terms_scope_changed")
    try: verified = datetime.fromisoformat(receipt["verified_at"])
    except (KeyError, ValueError, TypeError): raise PolicyError("model_terms_measurement_expired") from None
    current = now()
    require(verified.utcoffset() == timezone.utc.utcoffset(None) and type(current) in (int, float) and math.isfinite(current)
            and verified.timestamp() <= current <= verified.timestamp() + 300, "model_terms_measurement_expired")
    choices = selected_models(config["models"])
    require(isinstance(limits, dict) and set(limits) == set(choices) and isinstance(receipt.get("roles"), dict)
            and set(receipt["roles"]) == set(choices), "invalid_model_spend_policy")
    policy = {"source_revision": revision, "scope": scope(config, provider), "roles": {}}
    for role, pair in choices.items():
        row = copy.deepcopy(receipt["roles"][role]); sha = row.pop("terms_sha256", None)
        require((row.get("provider"), row.get("model")) == pair and sha == digest({"terms": row, "sources": receipt.get("sources")}), "model_terms_source_changed")
        require(type(row.get("published_max_output_tokens")) is int and 16 <= row["published_max_output_tokens"] <= 2000000
                and isinstance(row.get("metadata_sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", row["metadata_sha256"]), "model_terms_source_changed")
        bound = limits[role]
        require(isinstance(bound, dict) and set(bound) == {"max_requests", "output_tokens_upper_bound"}
                and type(bound["output_tokens_upper_bound"]) is int
                and bound["output_tokens_upper_bound"] <= row["published_max_output_tokens"], "invalid_model_spend_policy")
        row.pop("published_max_output_tokens"); row.pop("metadata_sha256")
        policy["roles"][role] = row | bound | {"terms_sha256": sha}
    validate_model_policy(policy)
    return policy
