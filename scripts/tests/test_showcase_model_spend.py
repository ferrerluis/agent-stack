import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_model_spend as spend
from showcase_cleanup import scope
from showcase_policy import PolicyError, Reservations
from test_showcase_policy import config, quote, NOW
import test_showcase_policy as policy_fixtures

RUN = "as7-fixture01"
PRIVATE = "private-fixture-token-must-not-reach-ledger-or-errors"


def policy(config, provider="aws"):
    routes = spend.selected_models(config["models"])
    return {"source_revision": "a" * 40, "scope": scope(config, provider), "roles": {
        role: {"provider": pair[0], "model": pair[1], "context_tokens_upper_bound": 20000, "output_tokens_upper_bound": 128,
               "max_requests": 2, "input_usd_per_million_upper_bound": "1.00", "output_usd_per_million_upper_bound": "5.00",
               "terms_sha256": "b" * 64} for role, pair in routes.items()}}


def body(path="/v1/responses", text="fixture-request", **extra):
    if path == "/v1/responses": value = {"model": "fixture-model", "input": text, "max_output_tokens": 1000, "stream": True}
    else: value = {"model": "fixture-model", "messages": [{"role": "user", "content": text}], "max_tokens": 1000}
    return json.dumps(value | extra).encode()


class SpendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = config(); self.ledger = Reservations(Path(self.temp.name) / "batch", self.config)
        self.reservation = self.ledger.reserve("aws", RUN, quote(), now=NOW)
        self.policy = policy(self.config); self.clock = [NOW]
        self.spend = spend.ModelSpend(self.ledger, RUN, "aws", "a" * 40, self.policy, now=lambda: self.clock[0])
        self.reader = Mock(return_value=PRIVATE); self.opener = SimpleNamespace(open=Mock(side_effect=self.response))
        self.gate = spend.RequestGate(self.spend, self.reader, self.opener, now=lambda: self.clock[0])

    def response(self, request, timeout):
        # Observe the actual locked/fsynced ledger while the request is being
        # dispatched; no fixture provider request precedes durable accounting.
        row = self.account(); self.assertEqual(row["model_requests"][-1]["status"], "uncertain")
        return io_response(b'data: {"fixture":true}\n\n', "text/event-stream")

    def account(self):
        return json.loads((self.ledger.directory / "reservations.json").read_text())["attempts"][0]

    def rewrite(self, mutate):
        path = self.ledger.directory / "reservations.json"; value = json.loads(path.read_text()); mutate(value["attempts"][0]); path.write_text(json.dumps(value))

    def test_bound_hold_is_durable_before_secret_and_http_and_success_never_refunds(self):
        def read(ref):
            self.assertEqual(self.account()["model_requests"][0]["held_usd"], "0.020640000")
            return PRIVATE
        self.reader.side_effect = read
        kind, data = self.gate.forward("codex", "/v1/responses", body())
        self.assertEqual(kind, "text/event-stream"); self.assertIn(b'fixture', data)
        row = self.account(); self.assertEqual(row["reserved_usd"], "2.00"); self.assertEqual(row["model_usd"], "1.00")
        self.assertEqual(row["model_requests"][0]["held_usd"], "0.020640000"); self.assertEqual(row["model_requests"][0]["status"], "response_received")
        self.assertNotIn(PRIVATE, json.dumps(row)); self.assertNotIn("fixture-request", json.dumps(row))
        self.assertEqual((self.ledger.directory / "reservations.json").stat().st_mode & 0o077, 0)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses"); self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers["Authorization"], "Bearer " + PRIVATE)
        forwarded = json.loads(request.data); self.assertEqual(forwarded["max_output_tokens"], 128)
        self.assertIs(forwarded["store"], False); self.assertEqual(forwarded["service_tier"], "default")

    def test_all_supported_routes_fix_origins_auth_and_generated_token_ceiling(self):
        for role, path in (("openclaw", "/v1/chat/completions"), ("hermes", "/v1/messages"), ("claude", "/v1/messages")):
            self.gate.forward(role, path, body(path, role))
            request = self.opener.open.call_args.args[0]; value = json.loads(request.data)
            if path == "/v1/messages":
                self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
                self.assertEqual(request.headers["X-api-key"], PRIVATE); self.assertNotIn("Authorization", request.headers)
                self.assertEqual(value["max_tokens"], 128); self.assertEqual(value["service_tier"], "standard_only")
            else:
                self.assertEqual(value["max_completion_tokens"], 128); self.assertNotIn("max_tokens", value)

    def test_restart_duplicate_or_native_retry_cannot_replay_admitted_body(self):
        self.gate.forward("codex", "/v1/responses", body())
        restarted = spend.ModelSpend(Reservations(self.ledger.directory, self.config), RUN, "aws", "a" * 40, self.policy, now=lambda: NOW)
        gate = spend.RequestGate(restarted, self.reader, self.opener, now=lambda: NOW)
        with self.assertRaisesRegex(PolicyError, "model_request_already_admitted"): gate.forward("codex", "/v1/responses", body())
        self.assertEqual(self.reader.call_count, 1); self.assertEqual(self.opener.open.call_count, 1)

    def test_atomic_concurrent_request_limit_keeps_exact_total_and_all_other_roles_available(self):
        def reserve(index):
            try:
                self.spend.reserve("codex", hashlib.sha256(str(index).encode()).hexdigest(), 60)
                return True
            except PolicyError as error:
                self.assertEqual(str(error), "model_request_limit_reached"); return False
        with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(results), 2)
        self.spend.reserve("claude", "d" * 64, 60)
        self.assertEqual(len(self.account()["model_requests"]), 3)

    def test_network_http_invalid_key_secret_failure_and_interrupt_hold_uncertain_without_retry(self):
        for index, error in enumerate((URLError(PRIVATE), HTTPError("https://evil.invalid", 429, PRIVATE, {}, io.BytesIO(PRIVATE.encode())), KeyboardInterrupt())):
            role = spend.ROLES[index]; path = "/v1/messages" if self.policy["roles"][role]["provider"] == "anthropic" else "/v1/responses"
            self.opener.open.side_effect = error
            with self.assertRaises(PolicyError) as caught: self.gate.forward(role, path, body(path, str(index)))
            self.assertNotIn(PRIVATE, str(caught.exception)); self.assertEqual(self.account()["model_requests"][-1]["status"], "uncertain")
        self.opener.open.reset_mock(); self.reader.side_effect = RuntimeError(PRIVATE)
        with self.assertRaises(PolicyError) as caught: self.gate.forward("claude", "/v1/messages", body("/v1/messages", "secret-fails"))
        self.assertNotIn(PRIVATE, str(caught.exception)); self.opener.open.assert_not_called()
        self.assertEqual(len(self.account()["model_requests"]), 4)
        self.reader.side_effect = spend.PreflightError("onepassword_unavailable_or_locked")
        with self.assertRaisesRegex(PolicyError, "onepassword_unavailable_or_locked"):
            self.gate.forward("claude", "/v1/messages", body("/v1/messages", "locked-secret"))
        self.opener.open.assert_not_called(); self.assertEqual(self.account()["model_requests"][-1]["status"], "uncertain")

    def test_full_role_bounds_must_fit_original_model_slice_before_any_key_or_request(self):
        self.policy["roles"]["codex"]["input_usd_per_million_upper_bound"] = "100"
        gate = spend.RequestGate(spend.ModelSpend(self.ledger, RUN, "aws", "a" * 40, self.policy, now=lambda: NOW), self.reader, self.opener)
        with self.assertRaisesRegex(PolicyError, "model_bounds_exceed_reservation"): gate.forward("codex", "/v1/responses", body())
        self.reader.assert_not_called(); self.opener.open.assert_not_called(); self.assertNotIn("model_requests", self.account())

    def test_legacy_ledger_still_allows_cleanup_but_cannot_invent_a_model_allocation(self):
        self.rewrite(lambda row: [row.pop(key) for key in ("model_usd", "quote_sha256", "price_quote")])
        self.ledger.operate(lambda state: None)
        with self.assertRaisesRegex(PolicyError, "model_allocation_missing"): self.gate.forward("codex", "/v1/responses", body())
        self.reader.assert_not_called(); self.opener.open.assert_not_called()
        receipt = policy_fixtures.ReservationTest("runTest").receipt()
        self.ledger.confirm_cleanup(RUN, receipt)
        self.assertEqual(self.account()["reserved_usd"], "2.00")

    def test_edited_quote_allocation_or_request_cost_status_or_policy_is_refused(self):
        self.gate.forward("codex", "/v1/responses", body())
        original = self.account()
        for mutate in (lambda r: r.update(model_usd="1.50"), lambda r: r["price_quote"].update(model_usd="1.50"),
                       lambda r: r["model_requests"][0].update(held_usd="0"), lambda r: r["model_requests"][0].update(status="refunded"),
                       lambda r: r["model_requests"].append(copy.deepcopy(r["model_requests"][0])),
                       lambda r: r.update(work_deadline=NOW + 20000)):
            self.rewrite(lambda r: (r.clear(), r.update(copy.deepcopy(original)), mutate(r)))
            with self.subTest(mutate=mutate), self.assertRaises(PolicyError): self.ledger.operate(lambda state: None)
        self.rewrite(lambda r: (r.clear(), r.update(copy.deepcopy(original))))
        changed = copy.deepcopy(self.policy); changed["roles"]["codex"]["max_requests"] = 3
        gate = spend.RequestGate(spend.ModelSpend(self.ledger, RUN, "aws", "a" * 40, changed, now=lambda: NOW), self.reader, self.opener)
        with self.assertRaisesRegex(PolicyError, "model_spend_policy_changed"): gate.forward("codex", "/v1/responses", body(text="new-body"))

    def test_nonfinite_rate_limits_or_changed_model_scope_are_refused_before_ledger_mutation(self):
        for changes in ({"context_tokens_upper_bound": True}, {"max_requests": 0}, {"output_tokens_upper_bound": 4097},
                        {"input_usd_per_million_upper_bound": "NaN"}, {"input_usd_per_million_upper_bound": "1e-999999"},
                        {"output_usd_per_million_upper_bound": True}, {"model": "fallback-model"}, {"terms_sha256": "bad"}):
            value = copy.deepcopy(self.policy); value["roles"]["codex"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(PolicyError): spend.ModelSpend(self.ledger, RUN, "aws", "a" * 40, value)
        value = copy.deepcopy(self.policy); value["scope"]["region"] = "foreign-region"
        with self.assertRaisesRegex(PolicyError, "model_spend_scope_changed"): spend.ModelSpend(self.ledger, RUN, "aws", "a" * 40, value)
        self.assertNotIn("model_requests", self.account())

    def test_bad_codex_tracking_fails_before_admission_secret_or_http(self):
        for extra in ({"client_metadata": {"session_id": "x" * 257}}, {"include": ["web_search_call.action.sources"]},
                      {"prompt_cache_key": "foreign-identity"}):
            with self.assertRaises(PolicyError): self.gate.forward("codex", "/v1/responses", body(**extra))
        self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.assertNotIn("model_requests", self.account())

    def test_closed_attempt_expired_clock_or_insufficient_full_request_time_denies_dispatch(self):
        for now in (NOW - 1, self.reservation["work_deadline"] - 59, float("nan"), float("inf")):
            self.clock[0] = now
            with self.assertRaises(PolicyError): self.gate.forward("codex", "/v1/responses", body())
        self.clock[0] = NOW; self.ledger.operate(lambda state: state["attempts"][0].update(status="cleaned"))
        with self.assertRaisesRegex(PolicyError, "active_model_reservation_required"): self.gate.forward("codex", "/v1/responses", body())
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_slow_or_locked_secret_and_cleanup_between_admission_and_http_keep_hold_but_send_nothing(self):
        def slow(ref): self.clock[0] += 61; return PRIVATE
        self.reader.side_effect = slow
        with self.assertRaises(PolicyError): self.gate.forward("codex", "/v1/responses", body())
        self.assertEqual(self.account()["model_requests"][0]["status"], "uncertain"); self.opener.open.assert_not_called()
        def close(ref): self.ledger.operate(lambda state: state["attempts"][0].update(status="cleaned")); return PRIVATE
        self.clock[0] = NOW; self.reader.side_effect = close
        with self.assertRaises(PolicyError): self.gate.forward("claude", "/v1/messages", body("/v1/messages"))
        self.opener.open.assert_not_called(); self.assertEqual(len(self.account()["model_requests"]), 2)

    def test_failed_durable_commit_cannot_retrieve_a_key_or_contact_provider(self):
        with patch.object(self.ledger, "operate", side_effect=OSError(PRIVATE)), self.assertRaises(OSError):
            self.gate.forward("codex", "/v1/responses", body())
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_config_or_policy_model_change_between_setup_and_request_denies_spending(self):
        self.spend.policy["roles"]["codex"]["model"] = "fallback-model"
        with self.assertRaisesRegex(PolicyError, "model_spend_scope_changed"):
            self.gate.forward("codex", "/v1/responses", body(model="fallback-model"))
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_new_model_policy_cannot_move_an_original_reservation_to_another_cloud_account(self):
        changed = copy.deepcopy(self.config); changed["providers"]["aws"]["account_id"] = "999999999999"
        ledger = Reservations(self.ledger.directory, changed)
        other = spend.ModelSpend(ledger, RUN, "aws", "a" * 40, policy(changed), now=lambda: NOW)
        gate = spend.RequestGate(other, self.reader, self.opener, now=lambda: NOW)
        with self.assertRaisesRegex(PolicyError, "model_spend_scope_changed"): gate.forward("codex", "/v1/responses", body())
        self.reader.assert_not_called(); self.opener.open.assert_not_called(); self.assertNotIn("model_requests", self.account())

    def test_oversized_wrong_type_and_late_success_leave_full_hold_uncertain(self):
        for role, response in (("codex", io_response(b'x' * (spend.MAX_RESPONSE + 1))),
                              ("openclaw", io_response(b'html', "text/html"))):
            self.opener.open.side_effect = None; self.opener.open.return_value = response
            with self.assertRaises(PolicyError): self.gate.forward(role, "/v1/responses", body(text=role))
            self.assertEqual(self.account()["model_requests"][-1]["status"], "uncertain")
        self.opener.open.return_value = io_response(b'ok'); clock = iter([0, 0, 61]); self.gate.now = lambda: next(clock)
        with self.assertRaises(PolicyError): self.gate.forward("claude", "/v1/messages", body("/v1/messages"))
        self.assertEqual(self.account()["model_requests"][-1]["status"], "uncertain")


def io_response(raw, kind="application/json"):
    response = Mock(); stream = io.BytesIO(raw)
    response.__enter__ = Mock(return_value=SimpleNamespace(status=200, headers={"Content-Type": kind}, read=stream.read))
    response.__exit__ = Mock(return_value=False)
    return response


class RequestValidationTest(unittest.TestCase):
    def setUp(self): self.policy = policy(config())

    def test_aliases_premium_routes_builtin_tools_multimodal_background_and_unknown_fields_fail(self):
        for changes in ({"model": "alias"}, {"service_tier": "priority"}, {"background": True}, {"previous_response_id": "resp_old"},
                        {"store": True}, {"tools": [{"type": "web_search"}]}, {"tools": [{"type": "mcp"}]},
                        {"input": [{"type": "input_image", "image_url": "https://evil.invalid"}]}, {"conversation": "conv_old"},
                        {"max_output_tokens": True}, {"stream": 1}, {"audio": {}}, {"prompt_cache_retention": "24h"}):
            with self.subTest(changes=changes), self.assertRaises(PolicyError): spend.prepare_request(self.policy, "codex", "/v1/responses", body(**changes))
        for path in ("https://evil.invalid/v1/responses", "/v1/responses?api-key=private", "/v1/responses/compact", "/v1/embeddings"):
            with self.assertRaises(PolicyError): spend.prepare_request(self.policy, "codex", path, body())
        with self.assertRaises(PolicyError): spend.prepare_request(self.policy, "claude", "/v1/responses", body())

    def test_codex_native_tracking_is_bounded_and_discarded_before_forwarding(self):
        tracking = {"client_metadata": {"session_id": "fixture-session", "x-codex-window-id": "{fixture-window}",
                    "x-codex-turn-metadata": '{"sandbox":"workspace-write"}'},
                    "prompt_cache_key": "019c1234-abcd-7000-8000-123456789abc", "include": ["reasoning.encrypted_content"]}
        _, prepared = spend.prepare_request(self.policy, "codex", "/v1/responses", body(**tracking))
        _, plain = spend.prepare_request(self.policy, "codex", "/v1/responses", body())
        self.assertEqual(prepared, plain)
        for include in ([], ["reasoning.encrypted_content"]):
            spend.prepare_request(self.policy, "codex", "/v1/responses", body(include=include))

    def test_codex_tracking_refuses_unknown_unbounded_or_ambiguous_shapes(self):
        changes = [{"client_metadata": item} for item in (None, [], {}, {"session_id": ""}, {"session_id": True},
                   {"session_id": "x" * 257}, {"session_id": "é" * 129}, {"x-codex-turn-metadata": "x" * 16385},
                   {"session_id": "fixture", "service_tier": "priority"})]
        changes += [{"prompt_cache_key": item} for item in (None, True, "", "foreign-identity", "x" * 1024)]
        changes += [{"include": item} for item in (None, "reasoning.encrypted_content", ["web_search_call.action.sources"],
                    ["reasoning.encrypted_content", "reasoning.encrypted_content"], [True])]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(PolicyError):
                spend.prepare_request(self.policy, "codex", "/v1/responses", body(**change))
        raw = body(client_metadata={"session_id": "fixture"})
        raw = raw[:-1] + b',"client_metadata":{"session_id":"other"}}'
        with self.assertRaises(PolicyError): spend.prepare_request(self.policy, "codex", "/v1/responses", raw)

    def test_codex_tracking_cannot_relax_paid_features_or_other_roles_and_routes(self):
        for role, path in (("openclaw", "/v1/responses"), ("hermes", "/v1/messages"),
                           ("claude", "/v1/messages"), ("codex", "/v1/chat/completions")):
            for extra in ({"client_metadata": {"session_id": "fixture"}},
                          {"prompt_cache_key": "019c1234-abcd-7000-8000-123456789abc"}, {"include": []}):
                with self.subTest(role=role, path=path, extra=extra), self.assertRaises(PolicyError):
                    spend.prepare_request(self.policy, role, path, body(path, **extra))
        for change in ({"model": "alias"}, {"store": True}, {"background": True}, {"service_tier": "priority"},
                       {"tools": [{"type": "namespace", "name": "multi_agent_v1", "tools": []}]}):
            with self.assertRaises(PolicyError):
                spend.prepare_request(self.policy, "codex", "/v1/responses",
                                      body(client_metadata={"session_id": "fixture"}, **change))

    def test_claude_tracking_is_bounded_discarded_and_not_available_to_other_roles(self):
        _, prepared = spend.prepare_request(self.policy, "claude", "/v1/messages",
                                           body("/v1/messages", metadata={"user_id": "fixture-session"}))
        self.assertNotIn("metadata", json.loads(prepared))
        for metadata in ({}, {"user_id": ""}, {"user_id": "x" * 1025}, {"user_id": 1},
                         {"user_id": "fixture", "service_tier": "priority"}, "fixture", None):
            with self.subTest(metadata=metadata), self.assertRaises(PolicyError):
                spend.prepare_request(self.policy, "claude", "/v1/messages", body("/v1/messages", metadata=metadata))
        for role, path in (("hermes", "/v1/messages"), ("codex", "/v1/responses")):
            with self.assertRaises(PolicyError):
                spend.prepare_request(self.policy, role, path, body(path, metadata={"user_id": "fixture"}))

    def test_function_tools_text_history_cache_blocks_and_clamped_output_are_supported(self):
        value = body(input=[{"type": "function_call_output", "call_id": "fixture-id", "output": "fixture-result"}],
                     tools=[{"type": "function", "name": "write_file", "parameters": {"type": "object"}}])
        _, raw = spend.prepare_request(self.policy, "codex", "/v1/responses", value)
        self.assertEqual(json.loads(raw)["max_output_tokens"], 128)
        value = body("/v1/messages", tools=[{"name": "Read", "input_schema": {"type": "object"}}],
                     system=[{"type": "text", "text": "fixture", "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
        spend.prepare_request(self.policy, "claude", "/v1/messages", value)
        with self.assertRaises(PolicyError): spend.prepare_request(self.policy, "claude", "/v1/messages", body("/v1/messages", tools=[{"type": "web_search_20250305"}]))

    def test_duplicate_nonfinite_invalid_oversized_json_and_multiple_chat_choices_are_refused(self):
        deep = '[' * 1000 + '0' + ']' * 1000
        for raw in (b'{"model":"fixture-model","model":"alias"}', b'{"x":NaN}', b'{"model":"fixture-model","temperature":1e999}',
                    deep.encode(), b'not-json', b'[]', b'x' * (spend.MAX_BODY + 1)):
            with self.assertRaises(PolicyError): spend.prepare_request(self.policy, "codex", "/v1/responses", raw)
        for changes in ({"n": 2}, {"n": True}, {"max_completion_tokens": 10}, {"max_completion_tokens": 100}):
            with self.assertRaises(PolicyError): spend.prepare_request(self.policy, "codex", "/v1/chat/completions", body("/v1/chat/completions", **changes))

    def test_default_transport_cannot_follow_redirects_or_inherit_proxy(self):
        with patch.object(spend, "build_opener") as build:
            spend.RequestGate(None, None)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {}); self.assertIsInstance(handlers[1], spend.NoRedirect)
        with self.assertRaisesRegex(PolicyError, "model_redirect_refused"):
            handlers[1].redirect_request(None, None, 302, PRIVATE, {}, "https://evil.invalid")
