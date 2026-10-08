import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_model_prices as prices
import showcase_terraform as terraform
from showcase_policy import PolicyError, validate_model_policy
from test_showcase_policy import config, NOW
import test_showcase_hetzner_prices as driver_fixtures

PRIVATE = "private-fixture-must-not-enter-receipts"
OPENAI = "gpt-6.1-sol"
ANTHROPIC = "claude-haiku-4-5-20251001"
OPENAI_DOCUMENT = '''<h2>Model</h2><p>1,050,000 context window</p><p>128,000 max output tokens</p>
<h2>Text tokens</h2><p>Per 1M tokens</p><p>Input $2.00 Cached input $0.10 Cache writes $2.50 Output $10.00</p>
<p>Cached input tokens are priced at 5% of the uncached input token rate.</p>
<p>Cache writes are billed at 1.25x the uncached input token rate.</p>
<p>Prompts with more than 272K input tokens are priced at 2x input and cache rates and 1.5x output for the full request.</p>
<p>Fast mode prices are 2x Standard. Batch and Flex prices are 50% lower than Standard.</p>
<p>Regional processing adds a 10% premium where available.</p><h2>Modalities</h2>
<h2>Snapshots</h2><p>Use gpt-6.1-sol to select this model.</p><p>gpt-6.1-sol</p><h2>Rate limits</h2>'''
OVERVIEW = '''<table><tr><th>Feature</th><th>Claude Haiku 4.5 Example summary</th><th>Claude Sonnet 5.5 Example summary</th></tr>
<tr><td>Claude API ID</td><td>claude-haiku-4-5-20251001</td><td>claude-sonnet-5-5</td></tr>
<tr><td>Context window</td><td>200K tokens</td><td>1M tokens</td></tr>
<tr><td>Max output</td><td>64K tokens</td><td>128K tokens</td></tr></table>'''
ANTHROPIC_DOCUMENT = '''<table><tr><th>Model</th><th>Base tokens</th><th>Prompt caching</th></tr>
<tr><th>Name</th><th>Input</th><th>Output</th><th>5m writes</th><th>1h writes</th><th>Hits and refreshes</th></tr>
<tr><td>Claude Haiku 4.5 Example summary</td><td>$1 / MTok</td><td>$5 / MTok</td><td>$1.25 / MTok</td><td>$2 / MTok</td><td>$0.10 / MTok</td></tr>
<tr><td>Claude Sonnet 5.5 Example summary</td><td>$2 / MTok</td><td>$10 / MTok</td><td>$2.50 / MTok</td><td>$4 / MTok</td><td>$0.20 / MTok</td></tr></table>
<p>US-only routing incurs a 1.1x multiplier on all token pricing categories.</p>
<p>Global routing (the default) uses standard pricing.</p><p>Current versions include the full 1M token context window at standard pricing.</p>'''
VERSIONING = '''<p>Each Claude model ID identifies a pinned version of the model.</p>
<p>For the 4.6 generation and later, the dateless ID is the canonical model ID for that release.</p>'''
HAIKU_OVERVIEW = OVERVIEW.replace("Haiku 4.5", "Haiku 5.5").replace(ANTHROPIC, "claude-haiku-5-5").replace("200K tokens", "1M tokens").replace("64K tokens", "128K tokens")
HAIKU_TIERS = '''<tr><td rowspan="2">Claude Haiku 5.5 Summary</td><td>$0.10 / MTok for prompts up to 100,000 tokens</td><td>$0.50 / MTok</td><td>$0.125 / MTok</td><td>$0.20 / MTok</td><td>$0.01 / MTok</td></tr>
<tr><td>$0.50 / MTok for prompts over 100,000 tokens</td><td>$2.50 / MTok</td><td>$0.625 / MTok</td><td>$1 / MTok</td><td>$0.05 / MTok</td></tr>'''
HAIKU_PRICES = ANTHROPIC_DOCUMENT.replace(ANTHROPIC_DOCUMENT[ANTHROPIC_DOCUMENT.index('<tr><td>Claude Haiku'):ANTHROPIC_DOCUMENT.index('<tr><td>Claude Sonnet')], HAIKU_TIERS).replace("Current versions include the full 1M token context window at standard pricing.", "Claude Haiku 5.5 is priced by prompt length: a prompt of over 100,000 tokens pays higher prices.")


def inputs():
    value = config()
    value["models"] = {"openclaw": "openai/" + OPENAI, "hermes": "anthropic/" + ANTHROPIC, "codex": OPENAI, "claude": ANTHROPIC}
    return value


class FakePublic:
    def __init__(self):
        self.documents = {"openai_model": OPENAI_DOCUMENT, "anthropic_models": OVERVIEW,
                          "anthropic_prices": ANTHROPIC_DOCUMENT, "anthropic_versions": VERSIONING}
        self.calls = []

    def fetch(self, name, model=None):
        self.calls.append((name, model)); document = self.documents[name]
        return {"source": prices.source_url(name, model), "document": document, "body_sha256": hashlib.sha256(document.encode()).hexdigest()}


class FakeModels:
    def __init__(self): self.calls = []; self.responses = {}

    def retrieve(self, provider, model, key):
        self.calls.append((provider, model, key))
        return copy.deepcopy(self.responses.get(model, {"id": model, "object" if provider == "openai" else "type": "model",
            "max_input_tokens": 200000, "max_tokens": 64000, "owned_by": PRIVATE, "capabilities": {"private": PRIVATE}}))


class TermsTest(unittest.TestCase):
    def setUp(self):
        self.config = inputs(); self.reader = Mock(return_value=PRIVATE); self.http = FakeModels(); self.public = FakePublic()

    def measure(self, clock=None):
        return prices.measure_model_terms(self.config, "aws", "a" * 40, "b" * 64, self.reader, self.http, self.public,
                                          (lambda: NOW) if clock is None else clock)

    def limits(self): return {role: {"max_requests": 2, "output_tokens_upper_bound": 128} for role in self.config["models"]}

    def test_residency_availability_note_accepts_fast_without_changing_price_bounds(self):
        baseline = prices.openai_terms(OPENAI_DOCUMENT, OPENAI)
        for tiers in ("Standard, Flex, and Batch", "Standard, Fast, Flex, and Batch"):
            note = "EU data residency is available with " + tiers + " processing."
            document = OPENAI_DOCUMENT.replace("<h2>Modalities</h2>", "<p>" + note + "</p><h2>Modalities</h2>")
            self.assertEqual(prices.openai_terms(document, OPENAI), baseline)
            with self.assertRaisesRegex(PolicyError, "model_terms_price_unsupported"):
                prices.openai_terms(document.replace(note, note + " Regional rates are doubled."), OPENAI)

    def test_highest_cache_long_context_regional_rates_exact_roles_sources_and_no_secret_or_launch_claim(self):
        result = self.measure()
        self.assertEqual(result["roles"]["codex"]["input_usd_per_million_upper_bound"], "5.500000000")
        self.assertEqual(result["roles"]["codex"]["output_usd_per_million_upper_bound"], "16.500000000")
        self.assertEqual(result["roles"]["claude"]["input_usd_per_million_upper_bound"], "2.200000000")
        self.assertEqual(result["roles"]["codex"]["context_tokens_upper_bound"], 1050000)
        self.assertEqual(result["roles"]["claude"]["context_tokens_upper_bound"], 200000)
        self.assertEqual(len(self.http.calls), 2); self.assertEqual(len(self.public.calls), 4); self.reader.assert_called_once()
        self.assertNotIn(PRIVATE, json.dumps(result)); self.assertNotIn("document", json.dumps(result)); self.assertFalse(result["complete_quote"])
        self.assertEqual(result["launch_gate"], "unverified")
        policy = prices.policy_from_terms(result, self.config, "aws", "a" * 40, "b" * 64, self.limits(), lambda: NOW)
        costs = validate_model_policy(policy); self.assertEqual(str(costs["claude"]), "0.440704000")
        self.assertEqual(set(policy["roles"]["claude"]), {"provider", "model", "context_tokens_upper_bound", "output_tokens_upper_bound",
            "max_requests", "input_usd_per_million_upper_bound", "output_usd_per_million_upper_bound", "terms_sha256"})

    def test_current_dateless_claude_id_uses_published_api_limits_and_full_context_prices(self):
        self.config["models"]["claude"] = "claude-sonnet-5-5"
        self.http.responses["claude-sonnet-5-5"] = {"type": "model", "id": "claude-sonnet-5-5", "max_input_tokens": 1000000, "max_tokens": 128000}
        result = self.measure(); self.assertEqual(len(self.http.calls), 3)
        self.assertEqual(result["roles"]["claude"]["input_usd_per_million_upper_bound"], "4.400000000")
        self.assertEqual(result["roles"]["claude"]["model"], "claude-sonnet-5-5")

    def tiered_haiku(self):
        self.config["models"].update(hermes="anthropic/claude-haiku-5-5", claude="claude-haiku-5-5")
        self.http.responses["claude-haiku-5-5"] = {"type": "model", "id": "claude-haiku-5-5", "max_input_tokens": 1000000, "max_tokens": 128000,
            "capabilities": {"thinking": {"types": {"disabled": {"supported": True}}}, "effort": {"low": {"supported": True}}}}
        self.public.documents.update(anthropic_models=HAIKU_OVERVIEW, anthropic_prices=HAIKU_PRICES)

    def test_haiku_prompt_length_tiers_bound_highest_input_output_and_cache_rates(self):
        self.tiered_haiku(); result = self.measure()
        self.assertEqual(result["roles"]["claude"]["input_usd_per_million_upper_bound"], "1.100000000")
        self.assertEqual(result["roles"]["claude"]["output_usd_per_million_upper_bound"], "2.750000000")
        self.assertEqual(result["roles"]["claude"]["context_tokens_upper_bound"], 1000000)
        self.assertEqual(result["roles"]["hermes"]["model"], "claude-haiku-5-5")

    def test_missing_mismatched_extra_or_unbounded_prompt_tier_refuses(self):
        for doc in (HAIKU_PRICES.replace(HAIKU_TIERS.split('\n')[1], ''),
                    HAIKU_PRICES.replace('prompts over 100,000', 'prompts over 200,000'),
                    HAIKU_PRICES.replace('prompts up to 100,000', 'prompts up to 1,000,000'),
                    HAIKU_PRICES.replace('$0.625 / MTok', '$NaN / MTok'),
                    HAIKU_PRICES.replace(HAIKU_TIERS, HAIKU_TIERS + '\n' + HAIKU_TIERS.split('\n')[1]),
                    HAIKU_PRICES.replace('tokens pays higher prices.', 'tokens has unknown prices.')):
            self.tiered_haiku(); self.public.documents['anthropic_prices'] = doc
            with self.subTest(document=doc[:80]), self.assertRaises(PolicyError): self.measure()

    def test_simple_price_row_with_unbound_continuation_refuses(self):
        self.public.documents['anthropic_prices'] = ANTHROPIC_DOCUMENT.replace('<tr><td>Claude Sonnet', HAIKU_TIERS.split('\n')[1] + '<tr><td>Claude Sonnet')
        with self.assertRaisesRegex(PolicyError, 'model_terms_price_unsupported'): self.measure()

    def test_haiku_cannot_use_unverified_thinking_and_effort_settings(self):
        for capabilities in (None, {}, {'thinking': {'types': {'disabled': {'supported': False}}}, 'effort': {'low': {'supported': True}}},
                             {'thinking': {'types': {'disabled': {'supported': True}}}, 'effort': {'low': {'supported': False}}},
                             {'thinking': {'types': {'disabled': {'supported': 1}}}, 'effort': {'low': {'supported': True}}}):
            self.tiered_haiku(); self.http.responses['claude-haiku-5-5']['capabilities'] = capabilities
            with self.subTest(capabilities=capabilities), self.assertRaisesRegex(PolicyError, 'model_terms_capabilities_changed'): self.measure()

    def test_openai_dated_snapshot_is_bound_to_page_without_changing_requested_id(self):
        model = "gpt-6.1-sol-2026-09-24"
        self.config["models"].update(openclaw="openai/" + model, codex=model)
        self.public.documents["openai_model"] = OPENAI_DOCUMENT.replace("Use gpt-6.1-sol to select this model.", "Available aliases and snapshots: " + model)
        result = self.measure(); self.assertEqual(result["roles"]["codex"]["model"], model)
        self.assertEqual(self.http.calls[0][1], model)
        self.assertEqual(self.public.calls[0], ("openai_model", model))

    def test_old_moving_alias_or_unknown_snapshot_is_refused_before_key(self):
        self.public.documents["openai_model"] = OPENAI_DOCUMENT.replace("Use gpt-6.1-sol to select this model.", "gpt-6.1-sol-2026-09-24")
        with self.assertRaisesRegex(PolicyError, "model_snapshot_required"): self.measure()
        self.reader.assert_not_called(); self.assertEqual(self.http.calls, [])
        self.config["models"]["openclaw"] = "openai/gpt-6.1-sol-2026-09-25"
        with self.assertRaisesRegex(PolicyError, "model_terms_identity_unsupported"): self.measure()

    def test_missing_or_extra_price_modifiers_limits_rates_comparison_and_script_only_claims_refuse(self):
        for doc in (OPENAI_DOCUMENT.replace("Regional processing adds a 10% premium where available.", ""),
                    OPENAI_DOCUMENT.replace("$2.50", "$NaN"), OPENAI_DOCUMENT.replace("$2.50", "$0"),
                    OPENAI_DOCUMENT.replace("1,050,000", "3,000,000"), OPENAI_DOCUMENT + '<p>1,000 context window</p>',
                    OPENAI_DOCUMENT.replace("<h2>Modalities", "<p>Additional routing costs $10 per request.</p><h2>Modalities"),
                    OPENAI_DOCUMENT.replace("<h2>Modalities", "<p>Quick comparison</p><h2>Modalities"),
                    '<script>' + OPENAI_DOCUMENT + '</script>'):
            self.public.documents["openai_model"] = doc
            with self.subTest(document=doc[:50]), self.assertRaises(PolicyError): self.measure()
        self.reader.assert_not_called(); self.assertEqual(self.http.calls, [])

    def test_anthropic_alias_changed_metadata_missing_limits_or_boolean_limits_refuse(self):
        for mutation in ({"id": "claude-haiku-4-5"}, {"max_input_tokens": 1000000}, {"max_tokens": True}, {"max_tokens": None}):
            self.http.responses[ANTHROPIC] = {"id": ANTHROPIC, "type": "model", "max_input_tokens": 200000, "max_tokens": 64000} | mutation
            with self.subTest(mutation=mutation), self.assertRaises(PolicyError): self.measure()

    def test_anthropic_missing_cache_categories_duplicate_tables_foreign_row_or_changed_versioning_refuse(self):
        for name, doc in (("anthropic_prices", ANTHROPIC_DOCUMENT.replace("$2 / MTok", "$NaN / MTok")),
                          ("anthropic_prices", ANTHROPIC_DOCUMENT * 2),
                          ("anthropic_models", OVERVIEW.replace(ANTHROPIC, "claude-haiku-4-5")),
                          ("anthropic_models", OVERVIEW.replace("200K tokens", "2M tokens")),
                          ("anthropic_versions", VERSIONING.replace("pinned version", "moving version")),
                          ("anthropic_prices", ANTHROPIC_DOCUMENT.replace("1.1x multiplier", "1.4x multiplier"))):
            self.public = FakePublic(); self.public.documents[name] = doc
            with self.subTest(name=name), self.assertRaises(PolicyError): self.measure()

    def test_hash_url_or_expired_clock_and_full_attempt_shutdown_refuse(self):
        original = self.public.fetch
        for change in ({"source": "https://evil.invalid"}, {"body_sha256": "f" * 64}):
            with patch.object(self.public, "fetch", side_effect=lambda *args: original(*args) | change), self.assertRaisesRegex(PolicyError, "model_terms_source_changed"):
                self.measure()
        for clock in (lambda: float("nan"), iter([NOW, NOW + 301]).__next__, iter([NOW, NOW - 1]).__next__):
            with self.assertRaises(PolicyError): self.measure(clock)
        self.http.responses[OPENAI] = {"id": OPENAI, "object": "model", "shutdown_date": "2020-01-01"}
        with self.assertRaisesRegex(PolicyError, "model_shuts_down_before_attempt_ends"): self.measure()

    def test_scope_model_and_reference_mutations_during_read_cannot_rebind_receipt(self):
        for kind in ("scope", "models", "ref", "seconds", "refs_replaced"):
            self.setUp()
            def read(reference):
                if kind == "scope": self.config["providers"]["aws"]["region"] = "foreign"
                elif kind == "models": self.config["models"]["codex"] = "different-model"
                elif kind == "ref": self.config["secret_refs"]["openai_api_key"] = "op://vault/foreign/key"
                elif kind == "refs_replaced": self.config["secret_refs"] = self.config["secret_refs"] | {"openai_api_key": "op://vault/foreign/key"}
                else: self.config["attempt_seconds"] -= 1
                return PRIVATE
            self.reader.side_effect = read
            with self.subTest(kind=kind), self.assertRaisesRegex(PolicyError, "model_terms_scope_changed"): self.measure()

    def test_bad_secret_or_bad_route_or_scope_refuse_without_private_error(self):
        for key in ("", "bad key", "é", "x" * 65537):
            self.reader.return_value = key
            with self.assertRaisesRegex(PolicyError, "invalid_model_key_field"): self.measure()
        self.assertEqual(self.http.calls, [])
        for model in ("../key", "ft:private-model", "id?token", "a/b"):
            with self.assertRaises(PolicyError): prices.source_url("openai_model", model)
        self.config["secret_refs"]["openai_api_key"] = PRIVATE
        with self.assertRaisesRegex(PolicyError, "secret_reference_required"): self.measure()

    def test_policy_expired_wrong_scope_plan_source_or_changed_terms_are_refused(self):
        result = self.measure()
        for mutate in (lambda row: row.update(plan_sha256="c" * 64), lambda row: row.update(source_revision="c" * 40),
                       lambda row: row["scope"].update(account_id="foreign"), lambda row: row.update(verified_at="invalid"),
                       lambda row: row["roles"]["codex"].update(input_usd_per_million_upper_bound="0.01")):
            bad = copy.deepcopy(result); mutate(bad)
            with self.assertRaises(PolicyError): prices.policy_from_terms(bad, self.config, "aws", "a" * 40, "b" * 64, self.limits(), lambda: NOW)
        with self.assertRaisesRegex(PolicyError, "model_terms_measurement_expired"):
            prices.policy_from_terms(result, self.config, "aws", "a" * 40, "b" * 64, self.limits(), lambda: NOW + 301)

    def test_policy_cannot_shrink_whole_context_or_exceed_output_or_request_limits(self):
        result = self.measure()
        for bound in ({"max_requests": 33, "output_tokens_upper_bound": 128}, {"max_requests": 1, "output_tokens_upper_bound": 4097},
                      {"max_requests": 1, "output_tokens_upper_bound": True}, {"max_requests": 1, "output_tokens_upper_bound": 128, "context_tokens_upper_bound": 1}):
            limits = self.limits(); limits["codex"] = bound
            with self.assertRaisesRegex(PolicyError, "invalid_model_spend_policy"):
                prices.policy_from_terms(result, self.config, "aws", "a" * 40, "b" * 64, limits, lambda: NOW)


class TransportTest(unittest.TestCase):
    def response(self, raw, content_type="text/html"):
        response = SimpleNamespace(status=200, headers={"Content-Type": content_type}, read=Mock(return_value=raw))
        return response, Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))

    def test_fixed_public_get_no_auth_proxy_redirect_or_retry_and_bounded_bytes(self):
        public = prices.PublicModelTerms(); response, context = self.response(OPENAI_DOCUMENT.encode())
        with patch.object(public.opener, "open", return_value=context) as opened: result = public.fetch("openai_model", OPENAI)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, prices.OPENAI_DOCS + OPENAI); self.assertEqual(request.get_method(), "GET")
        self.assertNotIn("Authorization", request.headers); self.assertIsNone(request.data)
        self.assertEqual(opened.call_args.kwargs, {"timeout": 20}); response.read.assert_called_once_with(prices.MAX_DOCUMENT + 1)
        self.assertEqual(result["body_sha256"], hashlib.sha256(OPENAI_DOCUMENT.encode()).hexdigest())
        self.assertTrue(any(isinstance(handler, prices.NoRedirect) for handler in public.opener.handlers))
        self.assertFalse(any(getattr(handler, "proxies", None) for handler in public.opener.handlers))

    def test_unapproved_source_and_redirect_refuse_before_followup(self):
        public = prices.PublicModelTerms()
        with patch.object(public.opener, "open") as opened, self.assertRaises(PolicyError): public.fetch("https://evil.invalid")
        opened.assert_not_called()
        with self.assertRaisesRegex(PolicyError, "model_redirect_refused"):
            prices.NoRedirect().redirect_request(None, None, 302, PRIVATE, {}, "https://evil.invalid")

    def test_http_network_invalid_utf8_oversize_empty_or_json_response_is_redacted(self):
        for raw, kind in ((b'\xff', "text/html"), (b'', "text/html"), (b'x' * (prices.MAX_DOCUMENT + 1), "text/html"), (b'{}', "application/json")):
            public = prices.PublicModelTerms(); _, response = self.response(raw, kind)
            with patch.object(public.opener, "open", return_value=response), self.assertRaises(PolicyError): public.fetch("openai_model", OPENAI)
        for error in (URLError(PRIVATE), HTTPError("https://evil.invalid", 403, PRIVATE, {}, io.BytesIO(PRIVATE.encode()))):
            public = prices.PublicModelTerms()
            with patch.object(public.opener, "open", side_effect=error) as opened, self.assertRaises(PolicyError) as caught: public.fetch("openai_model", OPENAI)
            self.assertNotIn(PRIVATE, str(caught.exception)); self.assertEqual(opened.call_count, 1)


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.fixture = driver_fixtures.DriverTest("runTest"); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client; self.source_fixture = self.fixture.fixture
        for name in ("showcase_model_prices", "showcase_model_spend", "showcase_models", "showcase_cleanup", "showcase_attempt"):
            (self.source_fixture.repo / "scripts" / (name + ".py")).write_bytes((Path(terraform.__file__).parent / (name + ".py")).read_bytes())
        self.source_fixture.git("add", "."); self.source_fixture.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "model terms sources")
        revision = self.source_fixture.git("rev-parse", "HEAD").strip()
        value = inputs(); value["providers"]["hetzner"] = self.fixture.config["providers"]["hetzner"]
        run = self.source_fixture.batch / "attempts" / (driver_fixtures.PREFIX + "m")
        from showcase_attempt import prepare
        prepare(value, "hetzner", driver_fixtures.PREFIX + "m", self.source_fixture.batch, revision, self.source_fixture.repo)
        session = terraform.CloudSession.__new__(terraform.CloudSession)
        session.config, session.provider, session.secrets, session.reader = value, "hetzner", {}, Mock(return_value=PRIVATE)
        session.preflight = self.client.session.preflight; session.environment = self.client.session.environment
        self.client = terraform.TerraformAttempt(run, value, "hetzner", session, now=lambda: NOW)
        def tf(*args, **kwargs):
            if args[0] == "version": return json.dumps({"terraform_version": "1.16.5"}).encode()
            if args[0] == "init": (self.client.checkout / ".terraform.lock.hcl").write_text("fixture-lock")
            if args[0] == "plan": path = run / "deployment.tfplan"; path.write_bytes(b"fixture-plan"); path.chmod(0o600)
            if args[0] == "show":
                raw = json.dumps(driver_fixtures.plan()).replace(driver_fixtures.PREFIX, driver_fixtures.PREFIX + "m")
                return raw.encode()
            return b""
        self.client.tf = Mock(side_effect=tf); self.client.plan()

    def test_private_receipt_binds_original_saved_plan_source_and_reuses_keys_only_in_memory(self):
        public, http = FakePublic(), FakeModels()
        result = self.client.model_unit_prices(http, public); self.client.model_unit_prices(http, public)
        path = self.client.run / "model-prices.json"
        self.assertEqual(path.stat().st_mode & 0o077, 0); self.assertEqual(json.loads(path.read_text()), result)
        self.assertEqual(result["source_revision"], self.client.prepared["source_revision"])
        self.assertEqual(result["plan_sha256"], self.client.record()["plan_sha256"]); self.client.session.reader.assert_called_once()
        self.assertFalse(self.client.state_path.exists()); self.assertFalse((self.source_fixture.batch / "reservations.json").exists())
        self.assertNotIn(PRIVATE, path.read_text())

    def test_saved_plan_or_unplanned_stage_refuses_before_price_key_or_http_reads(self):
        public, http = FakePublic(), FakeModels()
        (self.client.run / "deployment.tfplan").write_bytes(b"changed")
        with self.assertRaisesRegex(PolicyError, "saved_plan_changed"): self.client.model_unit_prices(http, public)
        self.assertEqual(public.calls, []); self.assertEqual(http.calls, []); self.client.session.reader.assert_not_called()

    def test_prepared_reader_with_different_loaded_source_refuses_before_model_reads(self):
        public, http = FakePublic(), FakeModels()
        # Altering the prepared export is caught by its independent manifest.
        target = self.client.checkout / "scripts/showcase_model_prices.py"
        target.write_bytes(target.read_bytes() + b'\n# changed\n')
        with self.assertRaises(PolicyError): self.client.model_unit_prices(http, public)
        self.assertEqual(public.calls, []); self.assertEqual(http.calls, [])
