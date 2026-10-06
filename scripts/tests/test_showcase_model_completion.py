import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_model_completion as completion
import showcase_model_spend as spend
from showcase_policy import PolicyError, validate_model_account
import showcase_model_completion_fixtures as fixtures
import test_showcase_model_spend as spending


class CompletionTest(unittest.TestCase):
    def proof(self, api="responses", value=None, streaming=False, raw=None, request_changes=None, tool_call=False):
        path = next(path for path, name in completion.APIS.items() if name == api)
        role = "claude" if api == "messages" else "codex"
        policy = spending.policy(spending.config()); terms = policy["roles"][role]
        tools = [{"name": "write_file", "input_schema": {"type": "object"}}] if api == "messages" else [
            {"type": "function", **({"name": "write_file", "parameters": {"type": "object"}} if api == "responses" else
                                    {"function": {"name": "write_file", "parameters": {"type": "object"}}})}]
        _, body = spend.prepare_request(policy, role, path, spending.body(path, stream=streaming, tools=tools, **(request_changes or {})))
        value = fixtures.response(api, tool_call=tool_call) if value is None else value
        raw = fixtures.encoded(value, streaming) if raw is None else raw
        return completion.completion(terms["provider"], terms["model"], path,
                                     "text/event-stream" if streaming else "application/json", raw, json.loads(body), terms)

    def test_all_three_json_and_stream_protocols_select_only_hashes_model_and_usage(self):
        for api in completion.APIS.values():
            for streaming in (False, True):
                with self.subTest(api=api, streaming=streaming):
                    proof = self.proof(api, streaming=streaming)
                    self.assertIsNotNone(proof); self.assertEqual(proof["kind"], "text")
                    self.assertEqual(proof["text_sha256"], hashlib.sha256(b"fixture final answer").hexdigest())
                    self.assertEqual(proof["usage"]["context_tokens"], 45 if api == "messages" else 30)
                    self.assertEqual(proof["usage"]["output_tokens"], 20)
                    self.assertNotIn("fixture final answer", json.dumps(proof)); self.assertNotIn("resp_fixture", json.dumps(proof))

    def test_complete_client_tool_calls_are_intermediate_not_final_answer_proofs(self):
        for api in completion.APIS.values():
            for streaming in (False, True):
                with self.subTest(api=api, streaming=streaming):
                    proof = self.proof(api, streaming=streaming, tool_call=True)
                    self.assertIsNotNone(proof); self.assertEqual(proof["kind"], "tool_call")

    def test_http_ok_arbitrary_empty_duplicate_nonfinite_deep_or_bad_utf8_is_unverified(self):
        for raw in (b'{"ok":true}', b'{}', b'[]', b'null', b'', b'{"model":"fixture-model","model":"fixture-model"}',
                    b'{"usage":NaN}', b'{"usage":1e1000}', b'\xff', b'[' * 70 + b'0' + b']' * 70, b'x' * (completion.LIMIT + 1)):
            with self.subTest(size=len(raw)): self.assertIsNone(self.proof(raw=raw))

    def test_wrong_model_missing_id_failed_incomplete_or_nonstandard_tier_never_pass(self):
        for api in completion.APIS.values():
            original = fixtures.response(api)
            changes = [{"model": "another-model"}, {"id": None}, {"id": "raw private id"}, {"error": {"message": "failure"}}]
            if api == "responses": changes += [{"status": status} for status in ("failed", "incomplete", "in_progress", "cancelled", "queued")]
            if api != "messages": changes += [{"service_tier": "priority"}]
            for change in changes:
                with self.subTest(api=api, change=change): self.assertIsNone(self.proof(api, original | change))

    def test_zero_missing_boolean_negative_fractional_or_over_bound_usage_is_unverified(self):
        for api in completion.APIS.values():
            for key in (("input_tokens", "output_tokens") if api != "chat" else ("prompt_tokens", "completion_tokens")):
                for count in (None, True, -1, 1.5, 2000001):
                    value = fixtures.response(api); value["usage"][key] = count
                    with self.subTest(api=api, key=key, count=count): self.assertIsNone(self.proof(api, value))
            value = fixtures.response(api); output = "completion_tokens" if api == "chat" else "output_tokens"
            for count in (0, 129):
                value["usage"][output] = count
                self.assertIsNone(self.proof(api, value))
            value = fixtures.response(api)
            self.assertIsNone(self.proof(api, value, request_changes={"max_tokens": 16} if api != "responses" else {"max_output_tokens": 16}))

    def test_cache_accounting_total_context_and_paid_server_usage_are_checked(self):
        for api in ("responses", "chat"):
            value = fixtures.response(api); value["usage"]["total_tokens"] = 49; self.assertIsNone(self.proof(api, value))
            value = fixtures.response(api); value["usage"]["input_tokens_details" if api == "responses" else "prompt_tokens_details"] = {"cached_tokens": 31}
            self.assertIsNone(self.proof(api, value))
        for change in ({"cache_read_input_tokens": 20000}, {"cache_creation_input_tokens": True}, {"service_tier": "priority"},
                       {"server_tool_use": {"web_search_requests": 1}}, {"cache_creation": {"ephemeral_5m_input_tokens": 4}}):
            value = fixtures.response("messages"); value["usage"].update(change)
            self.assertIsNone(self.proof("messages", value))
        value = fixtures.response("messages"); value["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 5}
        self.assertIsNotNone(self.proof("messages", value))

    def test_refusals_length_content_filters_server_tools_and_empty_answers_are_not_success(self):
        for reason in ("length", "content_filter", "function_call", None):
            value = fixtures.response("chat"); value["choices"][0]["finish_reason"] = reason; self.assertIsNone(self.proof("chat", value))
        for reason in ("max_tokens", "refusal", "pause_turn", None):
            value = fixtures.response("messages"); value["stop_reason"] = reason; self.assertIsNone(self.proof("messages", value))
        value = fixtures.response(); value["output"][0]["content"] = [{"type": "refusal", "refusal": "no"}]; self.assertIsNone(self.proof(value=value))
        value = fixtures.response(); value["output"][0]["type"] = "web_search_call"; self.assertIsNone(self.proof(value=value))
        value = fixtures.response("messages"); value["content"][0]["type"] = "server_tool_use"; self.assertIsNone(self.proof("messages", value))
        value = fixtures.response("chat"); value["choices"][0]["message"]["refusal"] = "no"; self.assertIsNone(self.proof("chat", value))
        for api in completion.APIS.values(): self.assertIsNone(self.proof(api, fixtures.response(api, text="")))

    def test_unrequested_incomplete_duplicate_or_wrong_finished_tools_are_unverified(self):
        for api in completion.APIS.values():
            value = fixtures.response(api, tool_call=True)
            row = value["output"][0] if api == "responses" else value["content"][0] if api == "messages" else value["choices"][0]["message"]["tool_calls"][0]["function"]
            row["name"] = "unrequested_tool"; self.assertIsNone(self.proof(api, value))
            value = fixtures.response(api, tool_call=True)
            if api == "responses": value["output"][0]["arguments"] = '{"path":'
            elif api == "chat": value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"path":'
            else: value["content"][0]["input"] = []
            self.assertIsNone(self.proof(api, value))

    def test_sse_truncation_missing_terminals_duplicate_terminals_and_error_events_never_pass(self):
        for api in completion.APIS.values():
            value = fixtures.response(api); raw = fixtures.encoded(value, True)
            rows = fixtures.stream_rows(value); named = api != "chat"
            for changed in (raw[:-1], raw[:-10], b"".join(fixtures.frame(row, named) for row in rows[:-1]),
                            raw + fixtures.frame(rows[-1], named), fixtures.frame({"type": "error", "error": {"message": "raw private failure"}}) + raw):
                with self.subTest(api=api, size=len(changed)): self.assertIsNone(self.proof(api, streaming=True, raw=changed))

    def test_responses_sse_enforces_sequence_identity_and_actual_streamed_text(self):
        mutations = [lambda rows: rows[1].update(sequence_number=9),
                     lambda rows: rows[-1]["response"].update(id="different"),
                     lambda rows: rows[1].update(delta="different"), lambda rows: rows[1].update(item_id="different"),
                     lambda rows: rows[1].update(content_index=True), lambda rows: rows[-1].update(type="response.incomplete"),
                     lambda rows: rows[2]["item"]["content"][0].update(text="different")]
        for mutate in mutations:
            rows = copy.deepcopy(fixtures.stream_rows(fixtures.response())); mutate(rows)
            self.assertIsNone(self.proof(streaming=True, raw=b"".join(fixtures.frame(row) for row in rows)))

    def test_chat_sse_requires_role_single_finish_final_usage_same_model_and_done(self):
        mutations = [lambda rows: rows.pop(2), lambda rows: rows[0]["choices"][0]["delta"].pop("role"),
                     lambda rows: rows[1].update(model="different"), lambda rows: rows[1]["choices"][0].update(index=1),
                     lambda rows: rows.insert(2, copy.deepcopy(rows[1])), lambda rows: rows[2].update(usage=None)]
        for mutate in mutations:
            rows = fixtures.stream_rows(fixtures.response("chat")); mutate(rows)
            self.assertIsNone(self.proof("chat", streaming=True, raw=b"".join(fixtures.frame(row, False) for row in rows)))

    def test_anthropic_sse_requires_complete_ordered_blocks_cumulative_usage_and_stop(self):
        mutations = [lambda rows: rows.pop(4), lambda rows: rows[3].update(index=1),
                     lambda rows: rows[5]["usage"].update(output_tokens=0), lambda rows: rows[5]["delta"].update(stop_reason="max_tokens"),
                     lambda rows: rows.insert(3, copy.deepcopy(rows[2])), lambda rows: rows[5].pop("usage"),
                     lambda rows: rows[2]["content_block"].update(type="fallback")]
        for mutate in mutations:
            rows = fixtures.stream_rows(fixtures.response("messages")); mutate(rows)
            self.assertIsNone(self.proof("messages", streaming=True, raw=b"".join(fixtures.frame(row) for row in rows)))

    def test_crlf_comments_and_multiline_data_are_parsed_without_altering_wire_hash(self):
        raw = fixtures.encoded(fixtures.response(), True).replace(b"\n", b"\r\n")
        raw = b": keepalive\r\n\r\n" + raw
        proof = self.proof(streaming=True, raw=raw)
        self.assertIsNotNone(proof); self.assertEqual(proof["body_sha256"], hashlib.sha256(raw).hexdigest())
        event = fixtures.frame(fixtures.stream_rows(fixtures.response())[0])
        raw = fixtures.encoded(fixtures.response(), True).replace(event, event.replace(b'"response":', b'\ndata: "response":'))
        self.assertIsNotNone(self.proof(streaming=True, raw=raw))

    def test_response_format_mismatch_and_injected_event_names_are_not_completion_proof(self):
        raw = fixtures.encoded(fixtures.response(), True).replace(b"event: response.created", b"event: response.completed", 1)
        self.assertIsNone(self.proof(streaming=True, raw=raw))
        self.assertIsNone(self.proof(streaming=True, raw=fixtures.encoded(fixtures.response())))
        self.assertIsNone(self.proof(raw=fixtures.encoded(fixtures.response(), True)))

    def test_responses_done_text_parts_and_added_items_must_match_completed_output(self):
        value = fixtures.response(); item = value["output"][0]
        rows = fixtures.stream_rows(value)
        extra = [{"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                 {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "item_id": item["id"], "text": item["content"][0]["text"]},
                 {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "item_id": item["id"], "part": item["content"][0]}]
        rows = rows[:1] + extra[:1] + rows[1:2] + extra[1:] + rows[2:]
        def encode(items): return b"".join(fixtures.frame({**row, "sequence_number": i}) for i, row in enumerate(items))
        self.assertIsNotNone(self.proof(streaming=True, raw=encode(rows)))
        for mutate in (lambda items: items[1]["item"].update(type="web_search_call"),
                       lambda items: items[3].update(text="different"), lambda items: items[4]["part"].update(text="different"),
                       lambda items: items[3].update(item_id="different")):
            changed = copy.deepcopy(rows); mutate(changed)
            self.assertIsNone(self.proof(streaming=True, raw=encode(changed)))

    def test_responses_content_cannot_change_after_its_done_event_even_when_final_text_matches(self):
        rows = fixtures.stream_rows(fixtures.response()); text = rows[1]["delta"]
        done = {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "item_id": "item_fixture", "text": text}
        for changed in (rows[:1] + [done] + rows[1:], rows[:1] + rows[2:3] + rows[1:2] + rows[3:]):
            raw = b"".join(fixtures.frame({**row, "sequence_number": i}) for i, row in enumerate(changed))
            self.assertIsNone(self.proof(streaming=True, raw=raw))

    def test_anthropic_stop_sequence_requires_the_actual_requested_sequence(self):
        value = fixtures.response("messages"); value.update(stop_reason="stop_sequence", stop_sequence="END")
        self.assertIsNone(self.proof("messages", value))
        self.assertIsNone(self.proof("messages", value, request_changes={"stop_sequences": ["OTHER"]}))
        self.assertIsNotNone(self.proof("messages", value, request_changes={"stop_sequences": ["END"]}))
        self.assertIsNotNone(self.proof("messages", value, streaming=True, request_changes={"stop_sequences": ["END"]}))

    def test_streaming_chat_requests_require_usage_in_the_original_admitted_body(self):
        policy = spending.policy(spending.config())
        _, raw = spend.prepare_request(policy, "codex", "/v1/chat/completions",
                                       spending.body("/v1/chat/completions", stream=True, stream_options={"include_usage": False}))
        self.assertEqual(json.loads(raw)["stream_options"], {"include_usage": True})
        for options in (None, [], {"include_usage": 1}, {"foreign_option": True}):
            with self.assertRaises(PolicyError):
                spend.prepare_request(policy, "codex", "/v1/chat/completions",
                                      spending.body("/v1/chat/completions", stream=True, stream_options=options))


class CompletionLedgerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = spending.SpendTest("runTest"); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)

    def dispatch(self, api="responses", response_id="resp_fixture", tool_call=False):
        path = next(path for path, name in completion.APIS.items() if name == api)
        value = fixtures.response(api, response_id=response_id, tool_call=tool_call)
        self.fixture.opener.open.side_effect = lambda *args, **kwargs: spending.io_response(fixtures.encoded(value), "application/json")
        self.fixture.gate.forward("codex", path, spending.body(path, response_id, stream=False,
                                 tools=[{"type": "function", "name": "write_file", "parameters": {"type": "object"}}] if api == "responses" else []))
        return self.fixture.account()

    def test_actual_locked_ledger_adds_selected_completion_without_refunding_original_hold(self):
        account = self.dispatch(); hold = account["model_requests"][0]
        self.assertEqual(hold["held_usd"], "0.020640000"); self.assertEqual(hold["status"], "response_received")
        self.assertEqual(hold["completion"]["model"], fixtures.MODEL); validate_model_account(account)
        self.assertNotIn("fixture final answer", json.dumps(account)); self.assertNotIn(spending.PRIVATE, json.dumps(account))

    def test_legacy_http_delivery_remains_readable_without_inferred_completion(self):
        self.fixture.gate.forward("codex", "/v1/responses", spending.body())
        account = self.fixture.account(); validate_model_account(account)
        self.assertEqual(account["model_requests"][0]["status"], "response_received")
        self.assertNotIn("completion", account["model_requests"][0])

    def test_same_provider_response_id_on_another_request_keeps_second_full_hold_uncertain(self):
        self.dispatch()
        self.fixture.opener.open.side_effect = lambda *args, **kwargs: spending.io_response(fixtures.encoded(fixtures.response()), "application/json")
        with self.assertRaisesRegex(PolicyError, "model_completion_already_recorded"):
            self.fixture.gate.forward("codex", "/v1/responses", spending.body(text="different request", stream=False))
        account = self.fixture.account(); self.assertEqual(len(account["model_requests"]), 2)
        self.assertEqual(account["model_requests"][1]["status"], "uncertain")
        self.assertEqual(account["model_requests"][1]["held_usd"], "0.020640000")

    def test_saved_completion_schema_model_counts_status_and_extra_private_fields_are_revalidated(self):
        account = self.dispatch()
        changes = [lambda hold: hold.update(status="uncertain"), lambda hold: hold["completion"].update(model="another"),
                   lambda hold: hold["completion"].update(schema=True), lambda hold: hold["completion"].update(raw="private answer"),
                   lambda hold: hold["completion"]["usage"].update(context_tokens=31),
                   lambda hold: hold["completion"]["usage"].update(output_tokens=True)]
        for mutate in changes:
            changed = copy.deepcopy(account); mutate(changed["model_requests"][0])
            with self.assertRaises(PolicyError): validate_model_account(changed)


if __name__ == "__main__": unittest.main()
