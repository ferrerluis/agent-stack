import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, IncompleteRead
import io
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_model_gateway as gateway
import showcase_model_spend as spend
from showcase_policy import PolicyError, Reservations
from test_showcase_policy import config, quote, NOW
import test_showcase_model_spend as fixtures
import showcase_model_completion_fixtures as completions
import showcase_task_evidence as task_evidence

PRIVATE = "fixture-upstream-key-never-to-client-or-output"
RUN = "as7-fixture01"
SSE_FIRST = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"first"}\n\n'
SSE_DONE = b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n'


class Response:
    status = 200
    def __init__(self, chunks=None, kind="application/json"):
        self.chunks = list(chunks if chunks is not None else [b'{"fixture":"ok"}'])
        self.headers = {"Content-Type": kind}
        self.closed = False

    def read1(self, limit):
        if not self.chunks: return b""
        raw = self.chunks.pop(0)
        if isinstance(raw, BaseException): raise raw
        if callable(raw): raw = raw()
        if len(raw) > limit: self.chunks.insert(0, raw[limit:])
        return raw[:limit]

    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True


def body(path="/v1/responses", text="fixture", **extra):
    return fixtures.body(path, text, **extra)


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = config(); self.ledger = Reservations(Path(self.temp.name) / "batch", self.config)
        self.reservation = self.ledger.reserve("aws", RUN, quote(), now=NOW)
        self.policy = fixtures.policy(self.config); self.clock = [NOW]
        self.spend = spend.ModelSpend(self.ledger, RUN, "aws", "a" * 40, self.policy, now=lambda: self.clock[0])
        self.reader = Mock(return_value=PRIVATE)
        self.opener = SimpleNamespace(open=Mock(side_effect=lambda *args, **kwargs: Response()))
        self.gateway = gateway.BudgetGateway(self.spend, self.reader, self.opener)
        self.gateway.__enter__(); self.addCleanup(self.gateway.close)

    def account(self): return json.loads((self.ledger.directory / "reservations.json").read_text())["attempts"][0]

    def headers(self, role):
        bundle = self.gateway.client(role)
        return {"Content-Type": "application/json", "Authorization": "Bearer " + bundle["credential"]}

    def request(self, role="codex", path="/v1/responses", raw=None, headers=None, method="POST"):
        connection = HTTPConnection("127.0.0.1", self.gateway.server.server_port, timeout=3)
        try:
            connection.request(method, "/" + role + path, body(path) if raw is None else raw, self.headers(role) if headers is None else headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: connection.close()

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.gateway.server.server_port), timeout=3) as connection:
            connection.sendall(request); connection.shutdown(socket.SHUT_WR)
            raw = bytearray()
            while True:
                chunk = connection.recv(65536)
                if not chunk: break
                raw.extend(chunk)
            return bytes(raw)

    def test_all_four_roles_use_distinct_capabilities_fixed_provider_auth_and_original_private_ledger(self):
        tokens = [self.gateway.client(role)["credential"] for role in spend.ROLES]
        self.assertEqual(len(set(tokens)), 4)
        for role, path in (("codex", "/v1/responses"), ("openclaw", "/v1/chat/completions"), ("hermes", "/v1/messages"), ("claude", "/v1/messages")):
            headers = self.headers(role)
            if role == "hermes": headers = {"Content-Type": "application/json", "x-api-key": self.gateway.client(role)["credential"], "anthropic-version": "2023-06-01"}
            status, response_headers, raw = self.request(role, path, headers=headers)
            self.assertEqual(status, 200); self.assertEqual(raw, b'{"fixture":"ok"}')
            self.assertNotIn(PRIVATE, str(response_headers)); self.assertNotIn(PRIVATE.encode(), raw)
            request = self.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, spend.ORIGINS[self.policy["roles"][role]["provider"]] + path)
            forwarded = dict(request.header_items())
            self.assertNotIn(headers.get("Authorization", headers.get("x-api-key")), str(forwarded))
            self.assertIn(PRIVATE, str(forwarded))
        self.reader.assert_called_once()
        account = self.account(); self.assertEqual(account["model_usd"], "1.00")
        self.assertEqual(len(account["model_requests"]), 4)
        self.assertTrue(all(row["status"] == "response_received" for row in account["model_requests"]))
        self.assertNotIn(PRIVATE, json.dumps(account)); self.assertTrue(all(token not in json.dumps(account) for token in tokens))

    def test_native_scope_refusal_records_shape_and_source_without_names_values_or_spending(self):
        token = self.gateway.client("codex")["credential"]
        metadata = {"session_id": PRIVATE, "private-field-" + PRIVATE: token}
        status, _, raw = self.request(raw=body(text=PRIVATE, client_metadata=metadata))
        self.assertEqual(status, 400)
        receipt = self.gateway.rejection_receipt(); self.assertEqual(len(receipt["requests"]), 1)
        report = receipt["requests"][0]
        self.assertEqual(report["reason"], "model_request_scope_changed")
        self.assertIs(type(report["verifier_line"]), int)
        self.assertEqual(len(report["verifier_sha256"]), 64)
        self.assertTrue(report["model_matches_policy"])
        self.assertEqual(report["client_metadata"]["unknown_field_count"], 1)
        self.assertEqual(report["client_metadata"]["known_fields"]["session_id"], {"kind": "string", "utf8_bytes": len(PRIVATE)})
        serialized = json.dumps(receipt).encode() + raw
        self.assertNotIn(PRIVATE.encode(), serialized); self.assertNotIn(token.encode(), serialized)
        self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.assertEqual(self.account()["model_requests"], [])

    def test_root_turn_tracking_does_not_admit_internal_or_custom_tools_or_spend(self):
        for extra in ({"client_metadata": {"root_turn_id": True}},
                      {"client_metadata": {"root_turn_id": PRIVATE}, "input": [{"type": "additional_tools", "tools": []}]},
                      {"client_metadata": {"root_turn_id": PRIVATE}, "tools": [{"type": "custom", "name": "apply_patch"}]}):
            with self.subTest(extra=extra):
                status, _, raw = self.request(raw=body(**extra))
                self.assertEqual(status, 400); self.assertNotIn(PRIVATE.encode(), raw)
        self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.assertEqual(self.account()["model_requests"], [])

    def test_scope_report_limit_and_snapshots_do_not_relax_refused_models_or_cache_keys(self):
        cases = [body(model=PRIVATE, prompt_cache_retention="24h", **{PRIVATE: PRIVATE}),
                 body(prompt_cache_key=True), body(client_metadata={})]
        for raw in cases + [cases[0]] * 6:
            self.assertEqual(self.request(raw=raw)[0], 400)
        receipt = self.gateway.rejection_receipt()
        self.assertEqual(len(receipt["requests"]), 8); self.assertTrue(receipt["truncated"])
        first, cache, metadata = receipt["requests"][:3]
        self.assertFalse(first["model_matches_policy"]); self.assertEqual(first["unknown_field_count"], 1)
        self.assertIn("prompt_cache_retention", first["fields"])
        self.assertEqual(cache["prompt_cache_key"], {"kind": "boolean", "uuid_shape": False})
        self.assertEqual(metadata["client_metadata"]["field_count"], 0)
        self.assertNotIn(PRIVATE, json.dumps(receipt))
        receipt["requests"][0]["reason"] = "changed"
        self.assertEqual(self.gateway.rejection_receipt()["requests"][0]["reason"], "model_request_scope_changed")
        self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.assertEqual(self.account()["model_requests"], [])

    def test_pinned_claude_sdk_decorations_are_validated_then_removed_upstream(self):
        headers = self.headers("claude") | {"anthropic-version": "2023-06-01",
                    "anthropic-beta": ",".join(sorted(gateway.CLAUDE_SDK_MARKERS))}
        raw = body("/v1/messages", metadata={"user_id": "fixture-client-tracking"})
        status, _, _ = self.request("claude", "/v1/messages?beta=true", raw, headers)
        self.assertEqual(status, 200)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, spend.ORIGINS["anthropic"] + "/v1/messages")
        self.assertNotIn("metadata", json.loads(request.data))
        self.assertNotIn("anthropic-beta", {name.lower() for name, _ in request.header_items()})
        self.assertEqual(json.loads(request.data)["max_tokens"], 128)
        self.assertEqual(len(self.account()["model_requests"]), 1)

    def test_claude_sdk_marker_allowance_cannot_enable_other_routes_or_unpriced_features(self):
        for role, path in (("claude", "/v1/messages?beta=false"), ("claude", "/v1/messages?beta=true&extra=1"),
                           ("hermes", "/v1/messages?beta=true"), ("codex", "/v1/responses?beta=true"),
                           ("claude", "/_transport?beta=true")):
            with self.subTest(role=role, path=path):
                status, _, _ = self.request(role, path, body("/v1/messages"))
                self.assertEqual(status, 404)
        for role, marker in (("claude", "unpriced-feature"), ("claude", "effort-2025-11-24,effort-2025-11-24"),
                             ("claude", ""), ("claude", "effort-2025-11-24 ,claude-code-20250219"), ("hermes", "effort-2025-11-24")):
            with self.subTest(role=role, marker=marker):
                status, _, _ = self.request(role, "/v1/messages", headers=self.headers(role) | {"anthropic-beta": marker})
                self.assertEqual(status, 400)
        self.assertFalse(self.account()["model_requests"])
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_complete_sse_is_forwarded_incrementally_unchanged_and_recorded_after_terminal(self):
        frames = [completions.frame(row) for row in completions.stream_rows(completions.response())]
        waiting, release = threading.Event(), threading.Event()
        def final():
            waiting.set(); self.assertTrue(release.wait(3)); return frames[-1]
        self.opener.open.side_effect = lambda *args, **kwargs: Response(frames[:-1] + [final], "text/event-stream")
        connection = HTTPConnection("127.0.0.1", self.gateway.server.server_port, timeout=3)
        try:
            connection.request("POST", "/codex/v1/responses", body(), self.headers("codex"))
            response = connection.getresponse(); first = response.read1(len(frames[0]))
            self.assertEqual(first, frames[0]); self.assertTrue(waiting.wait(1))
            hold = self.account()["model_requests"][0]; self.assertEqual(hold["status"], "uncertain"); self.assertNotIn("completion", hold)
            release.set(); self.assertEqual(first + response.read(), b"".join(frames))
        finally: release.set(); connection.close()
        hold = self.account()["model_requests"][0]
        self.assertEqual(hold["completion"]["kind"], "text"); self.assertEqual(hold["held_usd"], "0.020640000")

    def test_failed_http200_sse_preserves_original_wire_bytes_without_completion_or_refund(self):
        raw = completions.frame({"type": "error", "error": {"type": "overloaded_error", "message": "fixture provider failure"}})
        self.opener.open.side_effect = lambda *args, **kwargs: Response([raw[:17], raw[17:]], "text/event-stream")
        status, _, actual = self.request()
        self.assertEqual(status, 200); self.assertEqual(actual, raw)
        hold = self.account()["model_requests"][0]
        self.assertEqual(hold["status"], "response_received"); self.assertNotIn("completion", hold)
        self.assertEqual(hold["held_usd"], "0.020640000"); self.assertEqual(self.opener.open.call_count, 1)

    def test_incremental_sse_reaches_actual_client_before_upstream_finishes_and_preserves_terminal_event(self):
        allow_finish, waiting = threading.Event(), threading.Event()
        def later():
            waiting.set()
            if not allow_finish.wait(3): raise TimeoutError("fixture stream waited")
            return SSE_DONE
        response = Response([SSE_FIRST, later], "text/event-stream")
        self.opener.open.side_effect = lambda *args, **kwargs: response
        connection = HTTPConnection("127.0.0.1", self.gateway.server.server_port, timeout=3)
        try:
            connection.request("POST", "/codex/v1/responses", body(), self.headers("codex"))
            result = connection.getresponse(); first = result.read1(len(SSE_FIRST))
            self.assertEqual(first, SSE_FIRST); self.assertFalse(allow_finish.is_set())
            self.assertTrue(waiting.wait(1)); self.assertEqual(self.account()["model_requests"][0]["status"], "uncertain")
            allow_finish.set(); self.assertEqual(result.read(), SSE_DONE)
        finally: allow_finish.set(); connection.close()
        self.assertTrue(response.closed); self.assertEqual(self.account()["model_requests"][0]["status"], "response_received")

    def test_workspace_helper_and_real_fake_cli_children_use_gateway_holds_and_verify_fresh_files(self):
        # Real helper/subprocess/local HTTP/ledger/file verification. Native
        # CLI behavior and paid upstream requests deliberately remain fixtures.
        spec = importlib.util.spec_from_file_location("workspace_gateway_task", Path(__file__).resolve().parents[2] /
                                                     "modules/common/runtime/workspace_cli_task.py")
        task = importlib.util.module_from_spec(spec); spec.loader.exec_module(task)
        root = Path(self.temp.name); home = root / "workspace-home"; home.mkdir()
        binaries = root / "bin"; binaries.mkdir()
        fixture = '''#!/usr/bin/env python3
import json, os, pathlib, sys, tomllib, urllib.request
cli = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]; model = args[args.index("--model") + 1]
challenge = pathlib.Path("input.json").read_text()
if cli == "codex":
    settings = {}
    for i, arg in enumerate(args):
        if arg == "-c": settings.update(tomllib.loads(args[i + 1]))
    provider = settings["model_providers"][settings["model_provider"]]
    endpoint = provider["base_url"] + "/responses"
    credential = os.environ[provider["env_key"]]
    body = {"model": model, "input": challenge, "max_output_tokens": 4096}
else:
    endpoint = os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages"
    credential = os.environ["ANTHROPIC_AUTH_TOKEN"]
    body = {"model": model, "messages": [{"role": "user", "content": challenge}], "max_tokens": 4096}
request = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + credential})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request, timeout=5) as response: value = json.loads(response.read())
text = value["output"][0]["content"][0]["text"] if cli == "codex" else value["content"][0]["text"]
answer = json.loads(text)
pathlib.Path("result.json").write_text(json.dumps(answer))
usage = value["usage"]
if cli == "codex":
    usage = {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
             "cached_input_tokens": usage["input_tokens_details"]["cached_tokens"]}
print(json.dumps({"type": "turn.completed", "usage": usage} if cli == "codex" else {"type": "result", "subtype": "success", "is_error": False, "usage": usage}))
'''
        for cli in ("codex", "claude"):
            binary = binaries / cli; binary.write_text(fixture); binary.chmod(0o700)
        def upstream(request, **kwargs):
            payload = json.loads(request.data)
            raw = payload["input"] if request.full_url.endswith("/responses") else payload["messages"][0]["content"]
            challenge = json.loads(raw)
            self.assertEqual(payload.get("max_output_tokens", payload.get("max_tokens")), 128)
            answer = {"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]}
            value = completions.response("responses" if request.full_url.endswith("/responses") else "messages",
                                         text=json.dumps(answer), response_id="resp_" + task_evidence.input_hash(challenge))
            value["model"] = payload["model"]
            return Response([completions.encoded(value)])
        self.opener.open.side_effect = upstream
        original_stat = Path.stat
        def stat(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == home:
                items = list(value); items[4] = 1000; return os.stat_result(items)
            return value
        original_invoke = task.invoke
        fixture_errors = []
        def invoke(*args, **kwargs):
            code, output, errors = original_invoke(*args, **kwargs)
            if code:
                # This child is a fake CLI with fixture keys. Keep its traceback
                # useful on other test hosts without printing role credentials.
                for secret in (PRIVATE, *self.gateway.tokens.values()):
                    errors = errors.replace(secret, "[fixture credential]")
                fixture_errors.append(errors[-3000:])
            return code, output, errors
        host_geteuid = os.geteuid
        task_os = SimpleNamespace(**{**vars(os), "geteuid": lambda: 1000})
        with patch.dict(os.environ, {"HOME": str(home), "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                                     "OPENAI_API_KEY": PRIVATE, "ANTHROPIC_API_KEY": PRIVATE, "https_proxy": PRIVATE}), \
             patch.object(task, "os", task_os), patch.object(Path, "stat", stat), \
             patch.object(task, "invoke", side_effect=invoke):
            self.assertIs(os.geteuid, host_geteuid, "The task UID fixture must not change ledger ownership checks.")
            for cli in ("codex", "claude"):
                bundle = self.gateway.client(cli)
                challenge = {"lhs": 17, "rhs": 26, "nonce": ("a1" if cli == "codex" else "b2") * 16}
                result = task.run_task(cli, self.config["models"][cli], json.dumps(bundle), 10, model_gateway=True, challenge=challenge)
                self.assertEqual(result["status"], "pass", (result, fixture_errors))
                self.assertTrue(result["verified"])
                directory = home / result["fixture"]
                self.assertEqual({p.name for p in directory.iterdir()}, {"input.json", "result.json"})
                self.assertEqual(result["input_sha256"], task_evidence.input_hash(challenge))
                actual = json.loads((directory / "result.json").read_text())
                self.assertEqual(task_evidence.answer_hash(actual), task_evidence.expected_answer(challenge))
                holds = [hold for hold in self.account()["model_requests"] if hold["role"] == cli]
                proof = task_evidence.correlate(cli, challenge, result["model_usage"], holds, self.policy["roles"][cli])
                self.assertEqual(proof["status"], "task_provider_correlated")
                for private in (PRIVATE, bundle["credential"]):
                    self.assertNotIn(private, json.dumps(result))
                    self.assertNotIn(private, "".join(p.read_text() for p in directory.iterdir()))
        holds = self.account()["model_requests"]
        self.assertEqual({hold["role"] for hold in holds}, {"codex", "claude"})
        self.assertEqual(len(holds), 2)
        self.assertTrue(all(hold["status"] == "response_received" for hold in holds))
        self.reader.assert_called_once()

    def test_registered_private_transport_nonce_is_single_use_and_does_not_read_keys_or_spend(self):
        proof = self.gateway.new_probe("codex")
        raw = json.dumps({"nonce": proof["nonce"]}, separators=(",", ":")).encode()
        self.assertEqual(self.request(path="/_transport", raw=b'{"nonce":"unknown"}')[0], 400)
        self.assertEqual(self.request(path="/_transport", raw=raw)[0], 200)
        self.assertTrue(self.gateway.probe_observed("codex", proof["nonce"]))
        self.assertEqual(self.request(path="/_transport", raw=raw)[0], 400)
        with self.assertRaises(PolicyError): self.gateway.new_probe("codex")
        self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.assertEqual(self.account()["model_requests"], [])

    def test_required_transport_failure_revokes_auth_clients_and_dispatch(self):
        active = [True]; self.gateway.require_transport(lambda: active[0])
        with self.assertRaises(PolicyError): self.gateway.require_transport(lambda: True)
        active[0] = False
        self.assertFalse(self.gateway.healthy())
        with self.assertRaises(PolicyError): self.gateway.client("codex")
        self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.assertEqual(self.account()["model_requests"], [])

    def test_channel_and_workspace_probes_are_separate_single_use_nonbillable_checks(self):
        first = self.gateway.new_probe("codex")
        with self.assertRaises(PolicyError): self.gateway.new_probe("codex", kind="workspace")
        raw = json.dumps({"nonce": first["nonce"]}, separators=(",", ":")).encode()
        self.assertEqual(self.request(path="/_transport", raw=raw)[0], 200)
        second = self.gateway.new_probe("codex", kind="workspace")
        self.assertNotEqual(second["nonce"], first["nonce"])
        self.assertEqual(self.request(path="/_transport", raw=raw)[0], 400)
        raw = json.dumps({"nonce": second["nonce"]}, separators=(",", ":")).encode()
        self.assertEqual(self.request(path="/_transport", raw=raw)[0], 200)
        self.assertTrue(self.gateway.probe_observed("codex", second["nonce"]))
        for kind in ("channel", "workspace", "override"):
            with self.assertRaises(PolicyError): self.gateway.new_probe("codex", kind=kind)
        with self.assertRaises(PolicyError): self.gateway.new_probe("hermes", kind="workspace")
        self.assertEqual(self.account()["model_requests"], [])
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_sealing_one_role_revokes_its_capability_and_leaves_other_original_role_allocations_available(self):
        headers = self.headers("codex")
        self.gateway.seal_role("codex")
        self.assertEqual(self.request(headers=headers)[0], 401)
        with self.assertRaises(PolicyError): self.gateway.client("codex")
        self.assertEqual(self.request(role="claude", path="/v1/messages")[0], 200)
        self.assertEqual([row["role"] for row in self.account()["model_requests"]], ["claude"])

    def test_sealing_an_inflight_role_interrupts_its_stream_and_retains_the_uncertain_hold(self):
        waiting, release = threading.Event(), threading.Event()
        def pause():
            waiting.set()
            if not release.wait(3): raise TimeoutError("fixture upstream delay")
            return SSE_DONE
        self.opener.open.side_effect = lambda *args, **kwargs: Response([SSE_FIRST, pause], "text/event-stream")
        connection = HTTPConnection("127.0.0.1", self.gateway.server.server_port, timeout=3)
        try:
            connection.request("POST", "/codex/v1/responses", body(), self.headers("codex"))
            response = connection.getresponse(); self.assertEqual(response.read1(len(SSE_FIRST)), SSE_FIRST)
            self.assertTrue(waiting.wait(1))
            # Release only after revocation, so the next upstream chunk must
            # fail permission rather than delivering a completion/refund.
            original = self.gateway.close_socket
            def stop(sock): original(sock); release.set()
            with patch.object(self.gateway, "close_socket", side_effect=stop): self.gateway.seal_role("codex")
            with self.assertRaises(IncompleteRead): response.read()
            self.assertEqual(self.account()["model_requests"][0]["status"], "uncertain")
            self.assertEqual(self.account()["model_requests"][0]["held_usd"], "0.020640000")
            self.assertTrue(self.gateway.client("claude")["credential"])
        finally: release.set(); connection.close()

    def test_transport_failure_interrupts_stream_and_keeps_the_original_hold(self):
        active = [True]; self.gateway.require_transport(lambda: active[0])
        allow, waiting = threading.Event(), threading.Event()
        def later():
            waiting.set(); self.assertTrue(allow.wait(3)); return SSE_DONE
        self.opener.open.side_effect = lambda *a, **k: Response([SSE_FIRST, later], "text/event-stream")
        connection = HTTPConnection("127.0.0.1", self.gateway.server.server_port, timeout=3)
        try:
            connection.request("POST", "/codex/v1/responses", body(), self.headers("codex"))
            response = connection.getresponse(); self.assertEqual(response.read1(len(SSE_FIRST)), SSE_FIRST)
            self.assertTrue(waiting.wait(1)); active[0] = False; allow.set()
            with self.assertRaises((IncompleteRead, OSError)): response.read()
        finally: allow.set(); connection.close()
        self.assertEqual(len(self.account()["model_requests"]), 1)
        self.assertEqual(self.account()["model_requests"][0]["status"], "uncertain")

    def test_private_probe_expiry_cleanup_and_malformed_duplicate_input_are_not_billable(self):
        proof = self.gateway.new_probe("codex")
        raw = json.dumps({"nonce": proof["nonce"]}, separators=(",", ":")).encode()
        for bad in (b'{}', raw[:-1] + b',"nonce":"wrong"}', raw + b'\n', b'[NaN]'):
            self.assertEqual(self.request(path="/_transport", raw=bad)[0], 400)
        self.clock[0] = self.reservation["work_deadline"]
        self.assertEqual(self.request(path="/_transport", raw=raw)[0], 403)
        self.assertFalse(self.gateway.probe_observed("codex", proof["nonce"]))
        self.assertEqual(self.account()["model_requests"], [])
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_replayed_tool_call_and_output_payloads_keep_matching_ids_without_resetting_budget(self):
        self.opener.open.side_effect = lambda *args, **kwargs: Response([completions.encoded(
            completions.response(response_id="resp_" + str(self.opener.open.call_count)), True)], "text/event-stream")
        call = {"type": "function_call", "name": "write", "call_id": "fixture-call", "arguments": "{}"}
        output = {"type": "function_call_output", "call_id": "fixture-call", "output": "fixture tool result"}
        status, _, _ = self.request(raw=body(input=[{"role": "user", "content": "first"}]))
        self.assertEqual(status, 200)
        status, _, _ = self.request(raw=body(input=[call, output]))
        self.assertEqual(status, 200)
        forwarded = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(forwarded["input"], [call, output]); self.assertEqual(forwarded["max_output_tokens"], 128)
        self.assertEqual(len(self.account()["model_requests"]), 2)

    def test_missing_wrong_cross_role_provider_key_or_duplicate_credentials_do_not_admit_requests(self):
        wrong = self.headers("claude")
        for headers in ({"Content-Type": "application/json"}, wrong,
                        self.headers("codex") | {"Authorization": "Bearer " + PRIVATE},
                        self.headers("codex") | {"x-api-key": self.gateway.client("codex")["credential"]}):
            with self.subTest(headers=list(headers)):
                status, _, raw = self.request(headers=headers); self.assertEqual(status, 401)
                self.assertNotIn(PRIVATE.encode(), raw)
        token = self.gateway.client("codex")["credential"]
        reply = self.raw((f'POST /codex/v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}').encode())
        self.assertIn(b'400', reply.split(b'\r\n', 1)[0]); self.assertEqual(self.account()["model_requests"], [])
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_routes_methods_queries_encoding_absolute_urls_and_wrong_provider_are_refused_before_key(self):
        for role, path in (("codex", "/v1/messages"), ("claude", "/v1/responses"), ("codex", "/v1/responses?token=" + PRIVATE),
                           ("codex", "/v1/%72esponses"), ("codex", "/v1/responses/../messages")):
            status, _, raw = self.request(role, path, raw=b'{}'); self.assertEqual(status, 404); self.assertNotIn(PRIVATE.encode(), raw)
        for method in ("GET", "PUT", "PATCH", "DELETE", "CONNECT", "OPTIONS"):
            status, _, _ = self.request(method=method); self.assertEqual(status, 405)
        self.reader.assert_not_called(); self.opener.open.assert_not_called(); self.assertEqual(self.account()["model_requests"], [])

    def test_chunked_compressed_beta_origin_expect_wrong_version_and_invalid_lengths_do_not_spend(self):
        for extra in ({"Transfer-Encoding": "chunked"}, {"Content-Encoding": "gzip"}, {"anthropic-beta": "unpriced-feature"},
                      {"Origin": "https://evil.invalid"}, {"Expect": "100-continue"}, {"anthropic-version": "2099-01-01"},
                      {"Content-Length": "0"}, {"Content-Length": "-1"}, {"Content-Length": "01"}, {"Content-Type": "text/plain"},
                      {"Content-Length": str(spend.MAX_BODY + 1)}):
            status, _, _ = self.request(headers=self.headers("codex") | extra, raw=b'{}')
            self.assertIn(status, (400, 413))
        self.assertEqual(self.account()["model_requests"], []); self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_bounded_header_and_duplicate_or_incomplete_body_are_rejected_without_echoing_private_data(self):
        token = self.gateway.client("codex")["credential"]
        prefix = f'POST /codex/v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\n'
        for tail in ('Content-Length: 3\r\n\r\n{}', 'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}',
                     'X-Private: ' + PRIVATE * 400 + '\r\nContent-Length: 2\r\n\r\n{}'):
            reply = self.raw((prefix + tail).encode())
            self.assertIn(b'400', reply.split(b'\r\n', 1)[0]); self.assertNotIn(PRIVATE.encode(), reply); self.assertNotIn(token.encode(), reply)
        self.assertEqual(self.account()["model_requests"], []); self.reader.assert_not_called()

    def test_duplicate_restart_and_role_quota_fail_without_repeating_provider_requests(self):
        self.opener.open.side_effect = lambda *args, **kwargs: Response([completions.encoded(
            completions.response(response_id="resp_" + str(self.opener.open.call_count)), True)], "text/event-stream")
        self.assertEqual(self.request()[0], 200); self.assertEqual(self.request()[0], 409)
        self.assertEqual(self.request(raw=body(text="second"))[0], 200)
        self.assertEqual(self.request(raw=body(text="third"))[0], 403)
        self.gateway.close()
        reopened = gateway.BudgetGateway(self.spend, self.reader, self.opener); reopened.__enter__(); self.addCleanup(reopened.close)
        self.gateway = reopened
        self.assertEqual(self.request(raw=body(text="second"))[0], 409)
        self.assertEqual(self.request(raw=body(text="fourth"))[0], 403)
        self.assertEqual(self.opener.open.call_count, 2); self.assertEqual(len(self.account()["model_requests"]), 2)

    def test_http_network_locked_secret_or_unknown_internal_error_keeps_full_hold_and_redacts_reason(self):
        for role, error in zip(spend.ROLES, (URLError(PRIVATE), HTTPError("https://evil.invalid", 429, PRIVATE, {}, io.BytesIO(PRIVATE.encode())),
                                           spend.PreflightError("onepassword_unavailable_or_locked"), RuntimeError(PRIVATE))):
            self.opener.open.side_effect = error
            path = "/v1/messages" if self.policy["roles"][role]["provider"] == "anthropic" else "/v1/responses"
            status, _, raw = self.request(role, path, raw=body(path, role))
            self.assertIn(status, (502, 503)); self.assertNotIn(PRIVATE.encode(), raw)
        account = self.account(); self.assertEqual(len(account["model_requests"]), 4)
        self.assertTrue(all(row["status"] == "uncertain" and row["held_usd"] == "0.020640000" for row in account["model_requests"]))

    def test_upstream_failed_sse_is_preserved_and_interrupted_sse_never_finishes_or_refunds(self):
        failure = b'event: response.failed\ndata: {"type":"response.failed","response":{"status":"failed"}}\n\n'
        self.opener.open.side_effect = lambda *args, **kwargs: Response([failure], "text/event-stream")
        self.assertEqual(self.request()[2], failure)
        status, _, raw = self.request(raw=body(text="repeat-failed"))
        self.assertEqual(status, 403); self.assertIn(b'model_completion_unverified', raw)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertIn("completion_rejection", self.account()["model_requests"][0])
        # A different role remains available; an interrupted transport keeps
        # its original uncertain hold and never invents a parser rejection.
        self.opener.open.side_effect = lambda *args, **kwargs: Response([SSE_FIRST, URLError(PRIVATE)], "text/event-stream")
        with self.assertRaises(IncompleteRead): self.request("openclaw", raw=body(text="interrupted"))
        account = self.account(); self.assertEqual(account["model_requests"][-1]["status"], "uncertain")
        self.assertNotIn("completion_rejection", account["model_requests"][-1])
        self.assertEqual(account["reserved_usd"], "2.00")

    def test_oversized_stream_terminates_without_successful_http_completion_or_refund(self):
        self.opener.open.side_effect = lambda *args, **kwargs: Response([b'x' * (spend.MAX_RESPONSE + 1)], "text/event-stream")
        with self.assertRaises(IncompleteRead): self.request()
        self.assertEqual(self.account()["model_requests"][0]["status"], "uncertain")

    def test_concurrent_role_quota_is_atomic_and_shared_secret_retrieved_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda index: self.request(raw=body(text=str(index)))[0], range(4)))
        self.assertEqual(results.count(200), 2); self.assertEqual(results.count(403), 2)
        self.reader.assert_called_once(); self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(len(self.account()["model_requests"]), 2)

    def test_closing_while_secret_read_is_pending_revokes_dispatch_and_waits_for_worker_and_key_erasure(self):
        reading, release = threading.Event(), threading.Event()
        def read(reference):
            reading.set()
            if not release.wait(3): raise TimeoutError("fixture reader waited")
            return PRIVATE
        self.reader.side_effect = read
        with ThreadPoolExecutor(max_workers=2) as pool:
            def request():
                try: self.request()
                except (OSError, EOFError): pass
            pending = pool.submit(request); self.assertTrue(reading.wait(1))
            closed = pool.submit(self.gateway.close)
            deadline = time.monotonic() + 1
            while self.gateway.state != "stopping" and time.monotonic() < deadline: time.sleep(.01)
            self.assertEqual(self.gateway.state, "stopping"); self.assertFalse(closed.done()); release.set()
            pending.result(timeout=3); closed.result(timeout=3)
        self.opener.open.assert_not_called(); self.assertEqual(self.gateway.state, "closed")
        self.assertFalse(self.gateway.keys); self.assertFalse(self.gateway.tokens); self.assertFalse(self.gateway.connections)
        self.assertEqual(self.account()["model_requests"][0]["status"], "uncertain")

    def test_context_cleanup_erases_capabilities_closes_listener_and_refuses_reuse_or_unfunded_listener(self):
        port = self.gateway.server.server_port; self.gateway.close(); self.gateway.close()
        self.assertFalse(self.gateway.thread.is_alive()); self.assertFalse(self.gateway.watchdog.is_alive())
        with self.assertRaises(OSError): socket.create_connection(("127.0.0.1", port), timeout=.2)
        with self.assertRaises(PolicyError): self.gateway.client("codex")
        with self.assertRaisesRegex(PolicyError, "model_gateway_already_used"): self.gateway.__enter__()
        changed = copy.deepcopy(self.policy); changed["roles"]["codex"]["input_usd_per_million_upper_bound"] = "100"
        candidate = gateway.BudgetGateway(spend.ModelSpend(self.ledger, RUN, "aws", "a" * 40, changed, now=lambda: NOW), self.reader, self.opener)
        with self.assertRaises(PolicyError): candidate.__enter__()
        self.assertEqual(candidate.state, "closed"); self.assertIsNone(candidate.server); self.reader.assert_not_called()

    def test_absolute_framing_deadline_and_max_connections_close_slow_clients_without_spending(self):
        with patch.object(gateway, "FRAME_SECONDS", .15):
            connections = [socket.create_connection(("127.0.0.1", self.gateway.server.server_port), timeout=2) for _ in range(4)]
            try:
                for connection in connections: connection.sendall(b'POST /codex/v1/responses HTTP/1.1\r\nHost: localhost\r\n')
                deadline = time.monotonic() + 1
                while len(self.gateway.connections) < 4 and time.monotonic() < deadline: time.sleep(.005)
                self.assertEqual(len(self.gateway.connections), 4)
                with socket.create_connection(("127.0.0.1", self.gateway.server.server_port), timeout=2) as extra:
                    extra.sendall(b'GET / HTTP/1.1\r\n\r\n'); self.assertEqual(extra.recv(100), b'')
                for connection in connections: self.assertEqual(connection.recv(100), b'')
            finally:
                for connection in connections: connection.close()
        self.assertEqual(self.account()["model_requests"], []); self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_no_request_body_or_credentials_are_logged_on_malformed_routes_or_unknown_internal_errors(self):
        output, errors = io.StringIO(), io.StringIO()
        self.opener.open.side_effect = RuntimeError(PRIVATE)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.request(path="/v1/responses?private=" + PRIVATE)
            self.request()
        self.assertEqual(output.getvalue(), ""); self.assertEqual(errors.getvalue(), "")

    def test_closed_original_reservation_or_changed_scope_cannot_dispatch_while_listener_exists(self):
        self.ledger.operate(lambda state: state["attempts"][0].update(status="cleaned"))
        status, _, raw = self.request()
        self.assertEqual(status, 403); self.assertIn(b'active_model_reservation_required', raw)
        self.assertEqual(self.account()["model_requests"], []); self.reader.assert_not_called(); self.opener.open.assert_not_called()
        self.ledger.operate(lambda state: state["attempts"][0].update(status="active"))
        self.config["providers"]["aws"]["region"] = "foreign"
        status, _, raw = self.request(); self.assertEqual(status, 403); self.assertIn(b'model_spend_scope_changed', raw)
        self.reader.assert_not_called(); self.opener.open.assert_not_called()

    def test_failed_background_watchdog_revokes_client_and_request_authority(self):
        self.gateway.stop_watchdog.set(); self.gateway.watchdog.join(timeout=1)
        self.assertFalse(self.gateway.healthy())
        with self.assertRaisesRegex(PolicyError, "model_gateway_closed"): self.gateway.client("codex")
        with self.assertRaisesRegex(PolicyError, "model_gateway_closed"): self.gateway.key("op://vault/fixture/key")
        self.reader.assert_not_called(); self.opener.open.assert_not_called(); self.assertEqual(self.account()["model_requests"], [])
