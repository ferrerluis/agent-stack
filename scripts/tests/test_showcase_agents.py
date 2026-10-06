import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_agents as agents

ID = "12345678-1234-4234-8234-123456789abc"
RUN = "run_fixture01"
CHALLENGE = {"lhs": 719821, "rhs": 106543, "nonce": "a" * 32}
MODEL = "openai/fixture-model"
CONTAINER = "b" * 64
SECRET = "fake-secret-not-for-output"


def response(runtime):
    answer = json.dumps({"sum": CHALLENGE["lhs"] + CHALLENGE["rhs"], "nonce": CHALLENGE["nonce"]})
    if runtime == "openclaw":
        return {"runId": ID, "status": "ok", "summary": "completed", "result": {
            "payloads": [{"text": answer}], "meta": {"aborted": False, "stopReason": "completed", "agentMeta": {
                "sessionId": ID, "provider": "openai", "model": "fixture-model", "usage": {
                    "input": 30, "output": 25, "cacheRead": 10, "cacheWrite": 2}}}}}
    return {"object": "hermes.run", "run_id": RUN, "status": "completed", "model": "echo-only",
        "output": answer, "runtime": {"provider": "custom", "model": "fixture-model", "route_source": "raw_request"},
        "usage": {"input_tokens": 30, "output_tokens": 25, "cache_read_tokens": 10, "cache_write_tokens": 2}}


class AgentProofTest(unittest.TestCase):
    def test_zero_or_omitted_uncached_input_requires_positive_cache_and_output(self):
        for runtime in ("openclaw", "hermes"):
            for omitted in (False, True):
                raw = response(runtime)
                usage = raw["result"]["meta"]["agentMeta"]["usage"] if runtime == "openclaw" else raw["usage"]
                names = ("input", "cacheRead", "cacheWrite") if runtime == "openclaw" else ("input_tokens", "cache_read_tokens", "cache_write_tokens")
                usage.update(dict(zip(names, (0, 30, 0))))
                if omitted: usage.pop(names[0])
                self.assertEqual(self.proof(runtime, raw)["model_usage"]["input_tokens"], 0)
                usage[names[1]] = 0
                with self.assertRaisesRegex(agents.ProbeError, "usage_not_verified"): self.proof(runtime, raw)

    def proof(self, runtime, raw=None):
        return agents.parse_turn(runtime, raw or response(runtime), MODEL, CHALLENGE, ID, RUN)

    def test_actual_served_pair_fresh_answer_and_cache_counts(self):
        for runtime in ("openclaw", "hermes"):
            with self.subTest(runtime=runtime):
                proof = self.proof(runtime)
                self.assertEqual(proof["served_model"], MODEL)
                self.assertEqual(proof["model_usage"], {"input_tokens": 30, "output_tokens": 25,
                    "cache_read_tokens": 10, "cache_write_tokens": 2})
                proof.update(watchdog="disarmed", duration_seconds=1.25)
                checked = agents.validate_proof(proof, runtime, MODEL, CHALLENGE, ID)
                self.assertEqual(len(checked["answer_sha256"]), 64)
                self.assertNotIn("answer", checked)

    def test_hermes_echoed_model_is_not_serving_evidence(self):
        raw = response("hermes")
        raw["model"] = "fixture-model"
        for changes in ({"provider": "anthropic"}, {"model": "different"}, {"route_source": "global"}):
            bad = copy.deepcopy(raw)
            bad["runtime"].update(changes)
            with self.assertRaisesRegex(agents.ProbeError, "served_model_mismatch"):
                self.proof("hermes", bad)
        raw.pop("runtime")
        with self.assertRaises(agents.ProbeError):
            self.proof("hermes", raw)

    def test_echo_stale_nonce_wrong_sum_extra_keys_and_fenced_text_fail(self):
        for answer in (json.dumps(CHALLENGE), json.dumps({"sum": 0, "nonce": "a" * 32}),
                       json.dumps({"sum": 826364, "nonce": "c" * 32}),
                       json.dumps({"sum": 826364, "nonce": "a" * 32, "extra": SECRET}),
                       '```json\n{"sum":826364,"nonce":"' + "a" * 32 + '"}\n```'):
            for runtime in ("openclaw", "hermes"):
                raw = response(runtime)
                if runtime == "openclaw":
                    raw["result"]["payloads"][0]["text"] = answer
                else:
                    raw["output"] = answer
                with self.subTest(runtime=runtime, answer=answer), self.assertRaises(agents.ProbeError):
                    self.proof(runtime, raw)

    def test_status_session_run_and_abort_must_be_consistent(self):
        for status in (None, "started", "in_flight", "timeout", "cancelled", "failed", "interrupted"):
            for runtime in ("openclaw", "hermes"):
                raw = response(runtime)
                raw["status"] = status
                with self.assertRaises(agents.ProbeError):
                    self.proof(runtime, raw)
        raw = response("openclaw")
        for change in ({"aborted": True}, {"error": {"message": SECRET}}, {"pendingToolCalls": [{}]},
                       {"yielded": True}, {"livenessState": "blocked"}, {"completion": {"refusal": True}},
                       {"toolSummary": {"calls": 1}}, {"agentHarnessResultClassification": "planning-only"}):
            bad = copy.deepcopy(raw)
            bad["result"]["meta"].update(change)
            with self.assertRaises(agents.ProbeError):
                self.proof("openclaw", bad)
        raw["result"]["meta"]["agentMeta"]["sessionId"] = "old-session"
        with self.assertRaises(agents.ProbeError):
            self.proof("openclaw", raw)
        raw = response("hermes")
        raw["run_id"] = "run_old"
        with self.assertRaises(agents.ProbeError):
            self.proof("hermes", raw)

    def test_model_fallback_media_errors_or_messaging_cannot_pass(self):
        for change in ({"fallbackAttempts": [{"model": "other"}]}, {"provider": "anthropic"}, {"model": "other"}):
            raw = response("openclaw")
            raw["result"]["meta"]["agentMeta"].update(change)
            with self.assertRaises(agents.ProbeError):
                self.proof("openclaw", raw)
        for location, changes in (("meta", {"executionTrace": {"fallbackUsed": True}}),
                                  ("result", {"didSendViaMessagingTool": True}),
                                  ("result", {"successfulCronAdds": 1})):
            raw = response("openclaw")
            (raw["result"]["meta"] if location == "meta" else raw["result"]).update(changes)
            with self.assertRaises(agents.ProbeError):
                self.proof("openclaw", raw)
        for change in ({"isError": True}, {"isReasoning": True}, {"mediaUrl": "secret-url"}):
            raw = response("openclaw")
            raw["result"]["payloads"][0].update(change)
            with self.assertRaises(agents.ProbeError):
                self.proof("openclaw", raw)

    def test_missing_zero_negative_boolean_or_invalid_token_counts_fail(self):
        for runtime in ("openclaw", "hermes"):
            for value in (None, 0, -1, True, "25", float("nan"), 1000000001):
                raw = response(runtime)
                usage = raw["result"]["meta"]["agentMeta"]["usage"] if runtime == "openclaw" else raw["usage"]
                usage["output" if runtime == "openclaw" else "output_tokens"] = value
                with self.assertRaisesRegex(agents.ProbeError, "usage_not_verified"):
                    self.proof(runtime, raw)

    def test_duplicate_json_nonfinite_unbounded_and_malformed_input_fail(self):
        for text in ('{"sum":1,"sum":2}', '{"sum":NaN}', '{"sum":Infinity}', b"\xff", "[", "x" * (agents.LIMIT + 1)):
            with self.assertRaises(agents.ProbeError):
                agents.strict_json(text)
        for challenge in ({}, {**CHALLENGE, "lhs": True}, {**CHALLENGE, "rhs": -1}, {**CHALLENGE, "nonce": "old"}):
            with self.assertRaises(agents.ProbeError):
                agents.prompt(challenge)
        self.assertNotIn(str(CHALLENGE["lhs"] + CHALLENGE["rhs"]), agents.prompt(CHALLENGE))
        self.assertIn(str(CHALLENGE["lhs"]), agents.prompt(CHALLENGE))

    def test_controller_rechecks_original_answer_usage_model_and_disarmed_timer(self):
        valid = self.proof("hermes")
        valid.update(watchdog="disarmed", duration_seconds=2)
        for changes in ({"watchdog": "armed"}, {"request_id": "old"}, {"served_model": "other"},
                        {"schema": True}, {"duration_seconds": float("inf")}, {"duration_seconds": True},
                        {"answer": {"sum": 0, "nonce": CHALLENGE["nonce"]}}):
            with self.assertRaises(agents.ProbeError):
                agents.validate_proof({**valid, **changes}, "hermes", MODEL, CHALLENGE, ID)


class AgentTransportTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.running, self.timer = [], True, False
        self.fail_paid, self.stop_fails, self.bad_disarm, self.bad_help = False, False, False, False
        self.runtime = "openclaw"
        self.raw = None

    def execute(self, args, timeout=10, input=None):
        self.calls.append((args, timeout, input))
        if args == agents.COMPOSE + ["ps", "-q", self.runtime]:
            return CONTAINER.encode()
        if "inspect" in args:
            self.assertEqual(args[-1], CONTAINER)
            return json.dumps({"Running": self.running, "Restarting": False, "Error": SECRET}).encode()
        if "--help" in args:
            return b"--params --expect-final --timeout --json" if self.bad_help else b"--expect-url --params --expect-final --timeout --json"
        if args[0] == "systemd-run":
            self.assertEqual(args[-5:], ["/usr/bin/docker", "stop", "-t", "5", CONTAINER])
            self.timer = True
            return b""
        if args[:2] == ["systemctl", "is-active"]:
            return b"active\n"
        if args[:2] == ["systemctl", "show"]:
            return b"active" if self.bad_disarm else b"inactive\n"
        if args[:2] == ["systemctl", "stop"]:
            self.timer = self.bad_disarm
            return b""
        if args[:2] == ["/usr/bin/docker", "stop"]:
            if self.stop_fails:
                raise agents.ProbeError("probe_command_failed")
            self.running = False
            return CONTAINER.encode()
        if args[:2] == ["/usr/bin/docker", "exec"]:
            self.assertTrue(self.timer)
            if self.fail_paid:
                raise RuntimeError(SECRET)
            if self.runtime == "openclaw":
                self.assertEqual(args[2], CONTAINER)
                self.assertIn("--expect-url", args)
                self.assertNotIn("--local", args)
                params = json.loads(args[-1])
                self.assertEqual(params["idempotencyKey"], ID)
                self.assertEqual(params["sessionId"], ID)
                self.assertIs(params["deliver"], False)
                return json.dumps(self.raw or response("openclaw")).encode()
            self.assertEqual(json.loads(input)["request_id"], ID)
            self.assertEqual(args[-1], agents.HERMES_SOURCE)
            return json.dumps({"run_id": RUN, "report": self.raw or response("hermes")}).encode()
        raise AssertionError("unexpected probe command")

    def probe(self):
        return agents.run_probe(self.runtime, MODEL, CHALLENGE, ID, execute=self.execute, now=lambda: 10)

    def test_both_transports_arm_before_paid_work_then_disarm_exact_owned_container(self):
        for runtime in ("openclaw", "hermes"):
            self.runtime, self.timer, self.calls = runtime, False, []
            proof = self.probe()
            self.assertEqual(proof["status"], "pass")
            self.assertFalse(self.timer)
            self.assertTrue(self.running)
            self.assertNotIn(SECRET, json.dumps(proof))
            self.assertEqual(len([call for call in self.calls if "exec" in call[0] and "--help" not in call[0]]), 1)

    def test_unsupported_pin_fails_before_timer_or_paid_work(self):
        self.bad_help = True
        self.assertEqual(self.probe()["status"], "fail")
        self.assertFalse(self.timer)
        self.assertFalse(any(call[0][0] == "systemd-run" for call in self.calls))

    def test_unobserved_result_stops_service_and_never_leaks_error_text(self):
        self.fail_paid = True
        result = self.probe()
        self.assertEqual(result["containment"], "stopped")
        self.assertFalse(self.running)
        self.assertFalse(self.timer)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_bad_proof_or_unverified_disarm_stops_container(self):
        self.raw = response("openclaw")
        self.raw["status"] = "in_flight"
        self.assertEqual(self.probe()["containment"], "stopped")
        self.raw, self.running, self.bad_disarm = None, True, True
        self.assertEqual(self.probe()["containment"], "stopped")

    def test_failed_direct_stop_leaves_vm_watchdog_armed(self):
        self.fail_paid = self.stop_fails = True
        result = self.probe()
        self.assertEqual(result["containment"], "uncertain")
        self.assertTrue(self.timer)
        self.assertFalse(any(call[0][:2] == ["systemctl", "stop"] for call in self.calls))

    def test_invalid_request_never_runs_commands(self):
        with self.assertRaises(agents.ProbeError):
            agents.run_probe("wrong", MODEL, CHALLENGE, ID, execute=self.execute)
        self.assertFalse(self.calls)


class HermesRequestTest(unittest.TestCase):
    def script(self, terminal=None, config=None, fail_create=False, features=None):
        args = {"model": MODEL, "request_id": ID, "prompt": agents.prompt(CHALLENGE)}
        calls, printed = [], io.StringIO()
        def open_request(request, timeout):
            calls.append(request)
            self.assertEqual(timeout, 10)
            self.assertTrue(request.full_url.startswith("http://127.0.0.1:8642/v1/"))
            self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
            self.assertEqual(request.get_header("Idempotency-key"), ID)
            if request.get_method() == "POST":
                if fail_create:
                    raise RuntimeError(SECRET)
                payload = json.loads(request.data)
                self.assertEqual(payload["provider"], "custom")
                self.assertEqual(payload["conversation_history"], [])
                data = {"run_id": RUN, "status": "started"}
            elif request.full_url.endswith("capabilities"):
                data = {"object": "hermes.api_server.capabilities", "auth": {"type": "bearer", "required": True},
                    "features": features if features is not None else {key: True for key in ("run_submission", "run_status", "run_stop")}}
            else:
                data = terminal or response("hermes")
            handle = Mock()
            handle.read.return_value = json.dumps(data).encode()
            handle.__enter__ = Mock(return_value=handle)
            handle.__exit__ = Mock(return_value=None)
            return handle
        opener = Mock(open=Mock(side_effect=open_request))
        settings = config or {"model": {"default": "fixture-model", "provider": "custom", "base_url": "https://api.openai.com/v1"}}
        with patch.dict(os.environ, {"API_SERVER_KEY": SECRET}), patch("sys.stdin", io.StringIO(json.dumps(args))), \
             patch("urllib.request.build_opener", return_value=opener), patch("pathlib.Path.read_text", return_value=settings if isinstance(settings, str) else json.dumps(settings)), \
             contextlib.redirect_stdout(printed):
            try:
                exec(compile(agents.HERMES_SOURCE, "hermes_request_probe", "exec"), {})
            except SystemExit:
                pass
        return calls, printed.getvalue()

    def test_api_key_stays_inside_container_and_creation_is_single_idempotent_call(self):
        calls, output = self.script()
        self.assertEqual(len([request for request in calls if request.get_method() == "POST"]), 1)
        self.assertEqual(json.loads(output)["report"]["status"], "completed")
        self.assertNotIn(SECRET, output)

    def test_unobserved_creation_is_not_retried_and_errors_are_fixed(self):
        calls, output = self.script(fail_create=True)
        self.assertEqual(len([request for request in calls if request.get_method() == "POST"]), 1)
        self.assertEqual(json.loads(output), {"status": "probe_failed"})
        self.assertNotIn(SECRET, output)

    def test_custom_route_and_run_contract_are_verified_before_creation(self):
        yaml_text = "model:\n  default: fixture-model\n  provider: custom\n  base_url: https://api.openai.com/v1\n"
        native_yaml = Mock(safe_load=Mock(return_value={"model": {
            "default": "fixture-model", "provider": "custom", "base_url": "https://api.openai.com/v1"}}))
        with patch.dict(sys.modules, {"yaml": native_yaml}):
            calls, output = self.script(config=yaml_text)
        native_yaml.safe_load.assert_called_once_with(yaml_text)
        self.assertEqual(json.loads(output)["report"]["status"], "completed")
        self.assertEqual(len([request for request in calls if request.get_method() == "POST"]), 1)
        for settings in ({"default": "other", "provider": "custom", "base_url": "https://api.openai.com/v1"},
                         {"default": "fixture-model", "provider": "custom", "base_url": "https://other.invalid/v1"}):
            calls, output = self.script(config={"model": settings})
            self.assertFalse(calls)
            self.assertEqual(json.loads(output), {"status": "probe_failed"})
        for features in ({}, {"run_submission": True, "run_status": True, "run_stop": False}):
            calls, output = self.script(features=features)
            self.assertFalse(any(request.get_method() == "POST" for request in calls))
            self.assertEqual(json.loads(output), {"status": "probe_failed"})


class BoundedCommandTest(unittest.TestCase):
    def test_actual_child_can_receive_only_the_explicit_peer_environment(self):
        with patch.dict(os.environ, {"AGENT_STACK_FAKE_SECRET": SECRET}):
            result = agents.invoke([sys.executable, "-c", "import os; assert 'AGENT_STACK_FAKE_SECRET' not in os.environ; print('isolated')"],
                env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.strip(), b"isolated")

    def test_actual_child_output_is_limited_on_both_streams(self):
        for stream in ("stdout", "stderr"):
            with self.assertRaisesRegex(agents.ProbeError, "probe_output_limit"):
                agents.invoke([sys.executable, "-c", "import sys; sys." + stream + ".write('x'*1100000)"], timeout=5)

    def test_actual_child_timeout_is_bounded_and_reaped(self):
        started = time.monotonic()
        with self.assertRaisesRegex(agents.ProbeError, "probe_command_timeout"):
            agents.invoke([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.15)
        self.assertLess(time.monotonic() - started, 3)

    def test_failed_child_does_not_print_captured_raw_secret(self):
        with self.assertRaisesRegex(agents.ProbeError, "probe_command_failed") as error:
            agents.invoke([sys.executable, "-c", "import sys; print(" + repr(SECRET) + "); sys.exit(1)"])
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(agents.invoke([sys.executable, "-c", "import sys; print(sys.stdin.read())"], input=b"fixture").strip(), b"fixture")


if __name__ == "__main__":
    unittest.main()
