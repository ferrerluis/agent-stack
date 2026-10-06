import copy
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_agents as agents
import showcase_runtime_task as task
import showcase_runtime_routes as routes
import test_showcase_agents as turns
import test_showcase_runtime_routes as configurations

APP_KEY = "fixture-local-app-key"


class HermesTaskTest(unittest.TestCase):
    def setUp(self):
        self.requests = []; self.clock = 0.0; self.native_count = 0
        self.capabilities = {"object": "hermes.api_server.capabilities", "auth": {"type": "bearer", "required": True},
            "features": {"run_submission": True, "run_status": True, "run_stop": True, "runs_idempotency": {"supported": True}}}
        self.created = {"run_id": turns.RUN, "status": "queued", "replayed": False}; self.poll_status = 200
        self.poll_type = "application/json"; self.poll_raw = None
        self.report = turns.response("hermes")
        self.report.update(completed=True, partial=False, interrupted=False)
        self.report["runtime"]["requested"] = {"provider": "as7-budget", "model": "fixture-model"}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def serve(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else None
                owner.requests.append((self.command, self.path, dict(self.headers), body))
                if self.path == "/v1/capabilities": value, status, kind = owner.capabilities, 200, "application/json"
                elif self.path == "/v1/runs": value, status, kind = owner.created, 202, "application/json"
                elif self.path.endswith("/stop"): value, status, kind = {"status": "cancelled"}, 200, "application/json"
                else: value, status, kind = owner.report, owner.poll_status, owner.poll_type
                raw = owner.poll_raw if self.path == "/v1/runs/" + turns.RUN and owner.poll_raw is not None else json.dumps(value).encode()
                self.send_response(status); self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
            do_GET = do_POST = serve
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def run_task(self, provider="openai", tools=None, native_change=False, uid=10000, config_change=False):
        route = configurations.route("hermes", provider)
        raw = routes.encoded(routes.compile_config({}, route))
        environment = ("API_SERVER_KEY=" + APP_KEY + "\n").encode()
        packet = {"route": route, "config_sha256": routes.sha(raw), "environment_sha256": routes.sha(environment),
                  "source_revision": "a" * 40, "challenge": turns.CHALLENGE, "request_id": turns.ID}
        def native(value):
            self.assertEqual(value["action"], "native"); self.native_count += 1
            return {"native_route": {"fixture": "changed" if native_change and self.native_count > 1 else "verified"}}
        def connection(host, port, timeout):
            self.assertEqual((host, port), ("127.0.0.1", 8642)); self.assertGreater(timeout, 0); self.assertLessEqual(timeout, 10)
            return HTTPConnection(host, self.server.server_port, timeout=timeout)
        def sleep(seconds): self.clock += seconds
        with patch.object(task.os, "geteuid", return_value=uid), patch.object(task.probe, "hermes_probe", side_effect=native), \
             patch.object(task.Path, "read_bytes", autospec=True, side_effect=lambda p: environment if p.name == ".env" else (b'{}' if config_change else raw)), \
             patch.object(task, "HTTPConnection", side_effect=connection):
            return task.hermes(packet, now=lambda: self.clock, sleep=sleep, tools=tools or Mock(return_value=set()))

    def test_both_provider_modes_submit_named_route_once_over_real_authenticated_local_http(self):
        for provider in ("openai", "anthropic"):
            self.requests.clear(); proof = self.run_task(provider)
            self.assertEqual(proof["served_model"], provider + "/fixture-model")
            self.assertEqual(proof["answer"]["sum"], 826364)
            self.assertNotIn(APP_KEY, json.dumps(proof)); self.assertNotIn(configurations.CAPABILITY, json.dumps(proof))
            self.assertEqual([(m, p) for m, p, _, _ in self.requests],
                [("GET", "/v1/capabilities"), ("POST", "/v1/runs"), ("GET", "/v1/runs/" + turns.RUN)])
            request = self.requests[1]
            body = json.loads(request[3]); self.assertEqual(body["provider"], "as7-budget"); self.assertEqual(body["model"], "fixture-model")
            self.assertEqual(body["conversation_history"], []); self.assertNotIn("session_id", body)
            self.assertIn(agents.prompt(turns.CHALLENGE), body.values())
            for _, _, headers, _ in self.requests:
                self.assertEqual(headers["Authorization"], "Bearer " + APP_KEY)
                self.assertEqual(headers["Idempotency-Key"], turns.ID)

    def test_native_tool_resolution_root_uid_or_changed_config_fail_before_http_submission(self):
        for kwargs in ({"tools": Mock(return_value={"mcp-personal"})}, {"uid": 0}, {"config_change": True}):
            self.requests.clear()
            with self.assertRaises(ValueError): self.run_task(**kwargs)
            self.assertEqual(self.requests, [])

    def test_missing_auth_idempotency_or_cancellation_capability_cannot_submit(self):
        original = copy.deepcopy(self.capabilities)
        for change in ("auth", "idempotency", "stop"):
            self.capabilities = copy.deepcopy(original); self.requests.clear()
            if change == "auth": self.capabilities["auth"]["required"] = False
            elif change == "idempotency": self.capabilities["features"]["runs_idempotency"]["supported"] = False
            else: self.capabilities["features"]["run_stop"] = False
            with self.assertRaises(ValueError): self.run_task()
            self.assertEqual(len(self.requests), 1)

    def test_false_terminal_native_model_or_requested_route_causes_one_cancel_without_resubmission(self):
        original = copy.deepcopy(self.report)
        for change in ("completed", "partial", "interrupted", "route", "model", "provider", "error", "nonce"):
            self.requests.clear(); self.report = copy.deepcopy(original)
            if change in ("completed", "partial", "interrupted"): self.report[change] = change != "completed"
            elif change == "route": self.report["runtime"]["requested"]["provider"] = "auto"
            elif change in ("model", "provider"): self.report["runtime"][change] = "wrong"
            elif change == "error": self.report["error"] = "fixture failure"
            else: self.report["output"] = '{"sum":826364,"nonce":"stale"}'
            with self.assertRaises(agents.ProbeError): self.run_task()
            self.assertEqual(sum(p == "/v1/runs" for _, p, _, _ in self.requests), 1)
            self.assertEqual(self.requests[-1][1], "/v1/runs/" + turns.RUN + "/stop")

    def test_redirect_oversize_duplicate_json_nonfinite_and_wrong_mime_fail_without_replay(self):
        for status, kind, raw in ((302, "application/json", None), (200, "text/plain", None),
                                  (200, "application/json", b"x" * (agents.LIMIT + 1)),
                                  (200, "application/json", b'{"status":"completed","status":"completed"}'),
                                  (200, "application/json", b'{"usage":NaN}')):
            self.requests.clear(); self.poll_status = status; self.poll_type = kind; self.poll_raw = raw
            with self.assertRaises((ValueError, agents.ProbeError)): self.run_task()
            self.assertEqual(sum(p == "/v1/runs" for _, p, _, _ in self.requests), 1)
            self.assertEqual(self.requests[-1][1], "/v1/runs/" + turns.RUN + "/stop")

    def test_task_rechecks_native_route_and_tools_after_completed_response(self):
        for kwargs in ({"native_change": True}, {"tools": Mock(side_effect=[set(), {"terminal"}])}):
            self.native_count = 0; self.requests.clear()
            with self.assertRaises(ValueError): self.run_task(**kwargs)
            self.assertEqual(sum(p == "/v1/runs" for _, p, _, _ in self.requests), 1)
            self.assertTrue(self.requests[-1][1].endswith("/stop"))

    def test_never_completed_run_is_bounded_without_repeated_submission(self):
        self.report["status"] = "running"
        with self.assertRaises(ValueError): self.run_task()
        self.assertEqual(self.clock, 165)
        self.assertEqual(sum(p == "/v1/runs" for _, p, _, _ in self.requests), 1)

    def test_unknown_or_stale_run_identifier_never_polls_an_unvalidated_path(self):
        for value in ("run_bad/../escape", turns.ID, None):
            self.requests.clear(); self.created["run_id"] = value
            with self.assertRaises(ValueError): self.run_task()
            self.assertEqual(len(self.requests), 2)
        self.created["run_id"] = turns.RUN
        for replayed in (True, None, 0):
            self.created["replayed"] = replayed; self.requests.clear()
            with self.assertRaises(ValueError): self.run_task()
            self.assertEqual(len(self.requests), 2)
        self.created["replayed"] = False; self.report["run_id"] = "run_stale"
        with self.assertRaises(ValueError): self.run_task()
        self.assertTrue(self.requests[-1][1].endswith("/stop"))


if __name__ == "__main__": unittest.main()
