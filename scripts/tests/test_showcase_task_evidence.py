import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_task_evidence as task
import showcase_model_completion as completion
import showcase_model_completion_fixtures as fixtures
import test_showcase_model_spend as spending

CHALLENGE = {"lhs": 17, "rhs": 26, "nonce": "a1" * 16}


class TaskEvidenceTest(unittest.TestCase):
    def held(self, role="codex", index=0, challenge=None, tool_call=False, api=None):
        terms = spending.policy(spending.config())["roles"][role]
        api = api or ("messages" if terms["provider"] == "anthropic" else "responses")
        challenge = CHALLENGE if challenge is None else challenge
        answer = {"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]}
        value = fixtures.response(api, json.dumps(answer), "resp_fixture_" + str(index), tool_call)
        path = next(path for path, name in completion.APIS.items() if name == api)
        tools = [{"name": "write_file"}] if api != "chat" else [{"function": {"name": "write_file"}}]
        request = {"stream": False, {"responses": "max_output_tokens", "chat": "max_completion_tokens", "messages": "max_tokens"}[api]: 128, "tools": tools}
        proof = completion.completion(terms["provider"], terms["model"], path, "application/json", fixtures.encoded(value), request, terms)
        self.assertIsNotNone(proof)
        return {"role": role, "status": "response_received", "completion": proof}, terms

    def counts(self, role="codex", multiplier=1):
        terms = spending.policy(spending.config())["roles"][role]
        return {"input_tokens": (30 if role == "codex" or terms["provider"] == "anthropic" else 20) * multiplier,
                "output_tokens": 20 * multiplier, "cache_read_tokens": 10 * multiplier,
                "cache_write_tokens": (5 if terms["provider"] == "anthropic" else 0) * multiplier}

    def test_all_roles_match_actual_answer_and_their_fixed_cache_counting_contract(self):
        for role in task.ROLES:
            hold, terms = self.held(role)
            proof = task.correlate(role, CHALLENGE, self.counts(role), [hold], terms)
            self.assertEqual(proof["status"], "task_provider_correlated")
            self.assertEqual(proof["answer_sha256"], task.expected_answer(CHALLENGE))
            self.assertEqual(proof["requests_sha256"], task.hashed([hold]))
            self.assertNotIn(CHALLENGE["nonce"], json.dumps(proof))

    def test_chat_completion_uses_the_same_runtime_uncached_contract(self):
        role = "openclaw"
        terms = spending.policy(spending.config())["roles"][role]
        if terms["provider"] != "openai": self.fail("fixture requires OpenAI runtime")
        hold, terms = self.held(role, api="chat")
        self.assertEqual(task.correlate(role, CHALLENGE, self.counts(role), [hold], terms)["model_usage"]["input_tokens"], 20)

    def test_coding_tool_intermediate_calls_and_final_answer_all_count_once(self):
        for role in ("codex", "claude"):
            first, terms = self.held(role, tool_call=True)
            last, _ = self.held(role, index=1)
            self.assertIsNone(first["completion"]["answer_sha256"])
            self.assertEqual(task.correlate(role, CHALLENGE, self.counts(role, 2), [first, last], terms)["model_usage"], self.counts(role, 2))
            with self.assertRaises(ValueError): task.correlate(role, CHALLENGE, self.counts(role), [first, last], terms)

    def test_final_tool_call_or_runtime_tool_activity_cannot_be_an_answer(self):
        for role in task.ROLES:
            hold, terms = self.held(role, tool_call=True)
            last, _ = self.held(role, index=1)
            with self.assertRaises(ValueError): task.correlate(role, CHALLENGE, self.counts(role), [hold], terms)
            if role in ("openclaw", "hermes"):
                with self.assertRaises(ValueError): task.correlate(role, CHALLENGE, self.counts(role, 2), [hold, last], terms)

    def test_correct_shape_with_stale_nonce_or_wrong_sum_cannot_satisfy_current_task(self):
        for changed in ({**CHALLENGE, "nonce": "b2" * 16}, {**CHALLENGE, "lhs": 18}):
            hold, terms = self.held(challenge=changed)
            with self.assertRaises(ValueError): task.correlate("codex", CHALLENGE, self.counts(), [hold], terms)

    def test_empty_uncertain_missing_legacy_or_duplicate_provider_holds_fail(self):
        original, terms = self.held()
        legacy = copy.deepcopy(original); legacy["completion"].pop("answer_sha256")
        missing = copy.deepcopy(original); missing.pop("completion")
        for holds in ([], [original | {"status": "uncertain"}], [missing], [legacy], [original, original]):
            with self.assertRaises(ValueError): task.correlate("codex", CHALLENGE, self.counts(), holds, terms)

    def test_changed_provider_model_role_or_answer_hash_fails(self):
        original, terms = self.held()
        for key, value in (("provider", "anthropic"), ("model", "other"), ("answer_sha256", "0" * 64)):
            hold = copy.deepcopy(original); hold["completion"][key] = value
            with self.assertRaises(ValueError): task.correlate("codex", CHALLENGE, self.counts(), [hold], terms)
        with self.assertRaises(ValueError): task.correlate("codex", CHALLENGE, self.counts(), [original | {"role": "claude"}], terms)

    def test_every_native_token_bucket_must_match_without_client_basis_override(self):
        hold, terms = self.held()
        for key in task.COUNTS:
            for value in (None, True, -1, 1.5, 1000000001, self.counts()[key] + 1):
                counts = self.counts(); counts[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    task.correlate("codex", CHALLENGE, counts, [hold], terms)
        for counts in ({}, self.counts() | {"input_basis": "uncached"}):
            with self.assertRaises(ValueError): task.correlate("codex", CHALLENGE, counts, [hold], terms)

    def test_fully_cached_runtime_input_can_be_zero_but_counts_still_match(self):
        hold, terms = self.held("openclaw")
        counts = hold["completion"]["usage"]; counts["cache_read_input_tokens"] = counts["input_tokens"]
        native = self.counts("openclaw") | {"input_tokens": 0, "cache_read_tokens": 30}
        self.assertEqual(task.correlate("openclaw", CHALLENGE, native, [hold], terms)["model_usage"], native)

    def test_overlapping_openai_runtime_cache_buckets_cannot_be_guessed_or_clamped(self):
        hold, terms = self.held("openclaw")
        hold["completion"]["usage"].update(cache_read_input_tokens=25, cache_creation_input_tokens=10)
        with self.assertRaises(ValueError):
            task.correlate("openclaw", CHALLENGE, self.counts("openclaw") | {"input_tokens": 0, "cache_read_tokens": 25, "cache_write_tokens": 10}, [hold], terms)

    def test_challenge_requires_exact_bounded_integer_and_nonce_fields(self):
        for change in ({"lhs": True}, {"rhs": -1}, {"lhs": 1000000}, {"nonce": "old"}, {"extra": 1}):
            with self.assertRaises(ValueError): task.input_hash(CHALLENGE | change)
        raw = json.dumps(CHALLENGE, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(task.input_hash(CHALLENGE), hashlib.sha256(raw).hexdigest())

    def test_request_hash_changes_when_order_or_original_hold_details_change(self):
        first, terms = self.held(); last, _ = self.held(index=1)
        original = task.correlate("codex", CHALLENGE, self.counts(multiplier=2), [first, last], terms)
        self.assertNotEqual(original["requests_sha256"], task.hashed([last, first]))
        self.assertNotEqual(original["requests_sha256"], task.hashed([first | {"worst_usd": "0.50"}, last]))


if __name__ == "__main__": unittest.main()
