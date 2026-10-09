#!/usr/bin/env python3
"""Internal loopback model gateway; owned-VM routing remains to be integrated.

No CLI, public bind, management endpoint, caller-supplied token, provider key or
upstream URL is accepted. One ephemeral capability per role authenticates only
its selected route; upstream keys remain in this controller's memory. Every
upstream request uses the original durable model-spending ledger. Native pinned
runtime/tool compatibility and the full cost/launch gates remain unverified.
"""
from contextlib import contextmanager
import copy
import hmac
from http.client import LineTooLong
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import socket
import threading
import time

from showcase_model_spend import ModelSpend, RequestGate, ROLES, MAX_BODY, scope_rejection_diagnostic
from showcase_policy import PolicyError, require

FRAME_SECONDS = 5
MAX_CLIENTS = 4
# Decorations emitted by the pinned Claude CLI. They grant no upstream
# feature: the gateway always forwards its own stable, bounded request.
CLAUDE_SDK_MARKERS = {"interleaved-thinking-2025-05-14", "mid-conversation-system-2026-04-07",
                     "claude-code-20250219", "effort-2025-11-24"}
ERRORS = {
    "model_gateway_auth_required": (401, "authentication_error"),
    "model_gateway_route_refused": (404, "invalid_request_error"),
    "model_gateway_frame_refused": (400, "invalid_request_error"),
    "model_gateway_body_too_large": (413, "invalid_request_error"),
    "model_gateway_header_refused": (400, "invalid_request_error"),
    "model_gateway_method_refused": (405, "invalid_request_error"),
    "model_gateway_closed": (503, "api_error"),
    "model_task_start_required": (403, "permission_error"),
    "model_transport_probe_refused": (400, "invalid_request_error"),
    "model_request_scope_changed": (400, "invalid_request_error"),
    "invalid_model_request": (400, "invalid_request_error"),
    "model_paid_feature_refused": (400, "invalid_request_error"),
    "model_endpoint_or_body_refused": (400, "invalid_request_error"),
    "model_request_already_admitted": (409, "invalid_request_error"),
    "model_request_limit_reached": (403, "permission_error"),
    "model_completion_unverified": (403, "permission_error"),
    "model_budget_exhausted": (403, "permission_error"),
    "model_work_deadline_reached": (403, "permission_error"),
    "active_model_reservation_required": (403, "permission_error"),
    "model_spend_scope_changed": (403, "permission_error"),
    "model_spend_policy_changed": (403, "permission_error"),
    "onepassword_unavailable_or_locked": (503, "api_error"),
    "model_request_failed": (502, "api_error"),
}


class HeaderReader:
    def __init__(self, source): self.source, self.count = source, 0

    def readline(self, size):
        raw = self.source.readline(min(size, 8193))
        self.count += len(raw)
        if len(raw) > 8192 or self.count > 32768:
            raise LineTooLong("header")
        return raw


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CLIENTS
    allow_reuse_address = False

    def verify_request(self, request, address):
        gateway = self.gateway
        with gateway.condition:
            if not gateway.healthy() or address[0] != "127.0.0.1" or len(gateway.connections) >= MAX_CLIENTS:
                return False
            gateway.connections[request] = {"deadline": gateway.now() + FRAME_SECONDS, "phase": "framing"}
            return True

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self.gateway.condition:
                self.gateway.connections.pop(request, None)
                self.gateway.condition.notify_all()

    def handle_error(self, request, address):
        # Default socketserver tracebacks can include request bodies or keys.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentStack-model-gateway/1"
    sys_version = ""

    def setup(self):
        self.request.settimeout(FRAME_SECONDS)
        super().setup()
        self.started_reply = False
        self.request_version, self.requestline, self.command = "HTTP/1.1", "", None

    def log_message(self, *args): pass
    def log_error(self, *args): pass

    def parse_request(self):
        if len(self.raw_requestline) > 4096:
            self.safe_error("model_gateway_frame_refused")
            return False
        source = self.rfile
        self.rfile = HeaderReader(source)
        try:
            return super().parse_request()
        finally:
            self.rfile = source

    def send_error(self, *args, **kwargs):
        self.safe_error("model_gateway_frame_refused")

    def handle_expect_100(self):
        self.safe_error("model_gateway_header_refused")
        return False

    def safe_error(self, reason):
        if self.started_reply: return
        reason = reason if reason in ERRORS else "model_request_failed"
        code, category = ERRORS[reason]
        body = json.dumps({"type": "error", "error": {"type": category, "code": reason, "message": reason}}).encode()
        try:
            self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers(); self.wfile.write(body); self.wfile.flush()
        except (OSError, ValueError, AttributeError): pass

    def start(self, kind):
        require(kind in ("application/json", "text/event-stream"), "model_request_failed")
        self.started_reply = True
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers(); self.wfile.flush()

    def write(self, raw):
        require(self.server.gateway.permitted(self.request), "model_gateway_closed")
        self.wfile.write(format(len(raw), "x").encode() + b"\r\n" + raw + b"\r\n")
        self.wfile.flush()

    def complete(self):
        require(self.server.gateway.permitted(self.request), "model_gateway_closed")
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()

    def reject_method(self): self.safe_error("model_gateway_method_refused")
    do_GET = do_HEAD = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = do_CONNECT = reject_method

    def do_POST(self):
        gateway, role, path, raw = self.server.gateway, None, None, None
        try:
            match = re.fullmatch(r"/(openclaw|hermes|codex|claude)(/v1/(?:responses|chat/completions|messages)|/_transport)(\?beta=true)?", self.path)
            require(match is not None, "model_gateway_route_refused")
            role, path, query = match.groups()
            require(query is None or (role == "claude" and path == "/v1/messages"), "model_gateway_route_refused")
            provider = gateway.spend.policy["roles"][role]["provider"]
            require(path == "/_transport" or (path == "/v1/messages" if provider == "anthropic" else path in ("/v1/responses", "/v1/chat/completions")), "model_gateway_route_refused")
            for name in ("Authorization", "x-api-key", "Content-Length", "Content-Type", "Host", "anthropic-version", "anthropic-beta"):
                require(len(self.headers.get_all(name, [])) <= 1, "model_gateway_header_refused")
            auth, api_key = self.headers.get("Authorization"), self.headers.get("x-api-key")
            require(not (auth and api_key) and (provider == "anthropic" or api_key is None), "model_gateway_auth_required")
            token = auth[7:] if auth and auth.startswith("Bearer ") else (api_key if provider == "anthropic" and not auth else None)
            require(isinstance(token, str) and re.fullmatch(r"[a-f0-9]{64}", token)
                    and gateway.authenticated(role, token), "model_gateway_auth_required")
            require(self.request_version == "HTTP/1.1" and self.headers.get("Host")
                    and not any(self.headers.get_all(name) for name in ("Transfer-Encoding", "Content-Encoding", "Expect", "Origin")), "model_gateway_header_refused")
            beta = self.headers.get("anthropic-beta")
            if beta is not None:
                markers = beta.split(",")
                require(role == "claude" and path == "/v1/messages" and 1 <= len(markers) <= len(CLAUDE_SDK_MARKERS)
                        and len(markers) == len(set(markers)) and set(markers) <= CLAUDE_SDK_MARKERS,
                        "model_gateway_header_refused")
            require(self.headers.get("anthropic-version") in (None, "2023-06-01"), "model_gateway_header_refused")
            require(self.headers.get("Content-Type", "").split(";", 1)[0].strip() == "application/json", "model_gateway_frame_refused")
            length = self.headers.get("Content-Length", "")
            require(re.fullmatch(r"[1-9][0-9]{0,7}", length), "model_gateway_frame_refused")
            require(int(length) <= MAX_BODY, "model_gateway_body_too_large")
            raw = self.rfile.read(int(length))
            require(len(raw) == int(length), "model_gateway_frame_refused")
            gateway.begin_forward(self.request, role)
            if path == "/_transport":
                reply = json.dumps(gateway.probe_reply(role, raw), separators=(",", ":")).encode()
                self.start("application/json"); self.write(reply); self.complete()
                return
            require(gateway.dispatch_allowed(role), "model_task_start_required")
            gate = RequestGate(gateway.spend, gateway.key, gateway.opener, now=gateway.now,
                               permit=lambda: gateway.permitted(self.request))
            gate.forward(role, path, raw, gateway.timeout, sink=self)
        except PolicyError as error:
            if str(error) == "model_request_scope_changed":
                try: gateway.record_scope_rejection(role, path, raw, error)
                except BaseException: pass  # Diagnostic failure cannot change the refusal.
            self.safe_error(str(error))
        except BaseException:
            self.safe_error("model_request_failed")
        finally:
            self.close_connection = True


class BudgetGateway:
    """Single-use local listener. Client bundles contain only role capabilities."""
    def __init__(self, spend, secret_reader, opener=None, timeout=60, now=time.monotonic):
        require(isinstance(spend, ModelSpend) and callable(secret_reader) and type(timeout) is int and 1 <= timeout <= 80,
                "invalid_model_gateway")
        self.spend, self.reader, self.opener, self.timeout, self.now = spend, secret_reader, opener, timeout, now
        self.condition = threading.Condition(); self.key_lock = threading.Lock()
        self.state, self.connections, self.keys, self.tokens = "new", {}, {}, {}
        self.transport, self.probes, self.probe_kinds = None, {}, set()
        self.server = self.thread = self.watchdog = None
        self.stop_watchdog = threading.Event()
        self.started_roles = None
        self.diagnostic_roles = set()
        self.scope_rejections, self.scope_rejections_truncated = [], False

    def record_scope_rejection(self, role, path, raw, error):
        diagnostic = scope_rejection_diagnostic(self.spend.policy, role, path, raw, error)
        if diagnostic is None: return
        with self.condition:
            if len(self.scope_rejections) < 8: self.scope_rejections.append(diagnostic)
            else: self.scope_rejections_truncated = True

    def rejection_receipt(self):
        with self.condition:
            return {"schema": 1, "requests": copy.deepcopy(self.scope_rejections), "truncated": self.scope_rejections_truncated}

    def gate_task_starts(self):
        """The model session calls this before listening; it cannot be undone."""
        with self.condition:
            require(self.state == "new" and self.started_roles is None, "model_gateway_already_used")
            self.started_roles = set()

    def authorize_role(self, role):
        with self.condition:
            require(self.healthy() and role in ROLES and role in self.tokens, "model_gateway_closed")
            require(role not in self.diagnostic_roles, "model_diagnostic_in_progress")
            self.spend.activate(self.timeout)
            if self.started_roles is not None:
                require(role not in self.started_roles, "model_role_already_used")
                self.started_roles.add(role)

    def dispatch_allowed(self, role):
        with self.condition:
            return self.healthy() and role in self.tokens and role not in self.diagnostic_roles and (self.started_roles is None or role in self.started_roles)

    @contextmanager
    def diagnostic_window(self, role):
        with self.condition:
            require(self.healthy() and self.started_roles is not None and role in self.tokens and role in ("openclaw", "hermes")
                    and role not in self.started_roles and role not in self.diagnostic_roles, "model_diagnostic_refused")
            self.diagnostic_roles.add(role)
        try: yield
        except BaseException:
            self.seal_role(role)
            raise
        finally:
            with self.condition: self.diagnostic_roles.discard(role)

    def __enter__(self):
        with self.condition:
            require(self.state == "new", "model_gateway_already_used")
            self.state = "starting"
        try:
            self.spend.activate(self.timeout)
            self.tokens = {role: secrets.token_hex(32) for role in ROLES}
            self.server = GatewayServer(("127.0.0.1", 0), Handler)
            self.server.gateway = self
            self.state = "running"
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            self.watchdog = threading.Thread(target=self.expire_connections, daemon=True)
            self.thread.start(); self.watchdog.start()
            return self
        except BaseException:
            self.close()
            raise

    def client(self, role):
        with self.condition:
            require(self.healthy() and role in ROLES and role in self.tokens, "model_gateway_closed")
            return {"role": role, "provider": self.spend.policy["roles"][role]["provider"],
                    "base_url": "http://127.0.0.1:" + str(self.server.server_port) + "/" + role,
                    "credential": self.tokens[role]}

    def authenticated(self, role, token):
        with self.condition:
            return self.healthy() and hmac.compare_digest(self.tokens.get(role, ""), token)

    def require_transport(self, healthy):
        with self.condition:
            require(self.healthy() and self.transport is None and callable(healthy), "model_transport_required")
            self.transport = healthy

    def new_probe(self, role, kind="channel"):
        with self.condition:
            bundle = self.client(role)
            require(kind in ("channel", "workspace", "runtime") and (kind != "workspace" or role in ("codex", "claude"))
                    and (kind != "runtime" or role in ("openclaw", "hermes")),
                    "model_transport_probe_refused")
            require((role, kind) not in self.probe_kinds and (role not in self.probes or self.probes[role]["observed"]),
                    "model_transport_probe_already_used")
            nonce = secrets.token_hex(32)
            self.probes[role] = {"nonce": nonce, "deadline": self.now() + 15, "observed": False}
            self.probe_kinds.add((role, kind))
            return {**bundle, "nonce": nonce}

    def probe_reply(self, role, raw):
        with self.condition:
            row = self.probes.get(role)
            require(self.healthy() and row is not None and not row["observed"] and self.now() < row["deadline"]
                    and raw == json.dumps({"nonce": row["nonce"]}, separators=(",", ":")).encode(), "model_transport_probe_refused")
            self.spend.activate(1)
            row["observed"] = True
            return {"schema": 1, "role": role, "nonce": row["nonce"], "source_revision": self.spend.policy["source_revision"]}

    def probe_observed(self, role, nonce):
        with self.condition:
            row = self.probes.get(role)
            return self.healthy() and row is not None and row["observed"] and hmac.compare_digest(row["nonce"], nonce)

    def healthy(self):
        try:
            return self.state == "running" and all(thread is not None and thread.is_alive() for thread in (self.thread, self.watchdog)) \
                and (self.transport is None or self.transport())
        except Exception:
            return False

    def permitted(self, request):
        with self.condition:
            row = self.connections.get(request)
            return self.healthy() and row is not None and self.now() < row["deadline"] \
                and (row.get("role") is None or row["role"] in self.tokens)

    def begin_forward(self, request, role=None):
        with self.condition:
            require(self.permitted(request) and (role is None or role in self.tokens), "model_gateway_closed")
            self.connections[request] = {"phase": "forwarding", "role": role,
                                        "deadline": self.now() + self.timeout + 20 + FRAME_SECONDS}

    def seal_role(self, role):
        """Revoke one task's capability before checking its original ledger holds."""
        with self.condition:
            require(role in ROLES and self.healthy(), "model_gateway_closed")
            self.tokens.pop(role, None)
            connections = [connection for connection, row in self.connections.items() if row.get("role") == role]
        for connection in connections: self.close_socket(connection)
        end = time.monotonic() + 2
        with self.condition:
            while any(row.get("role") == role for row in self.connections.values()) and time.monotonic() < end:
                self.condition.wait(min(.05, max(0, end - time.monotonic())))
            require(not any(row.get("role") == role for row in self.connections.values()), "model_role_shutdown_unverified")

    def key(self, reference):
        with self.key_lock:
            require(self.healthy(), "model_gateway_closed")
            if reference not in self.keys: self.keys[reference] = self.reader(reference)
            return self.keys[reference]

    @staticmethod
    def close_socket(connection):
        try: connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass

    def expire_connections(self):
        while not self.stop_watchdog.wait(.05):
            with self.condition:
                expired = [connection for connection, row in self.connections.items() if not self.healthy() or self.now() >= row["deadline"]]
            for connection in expired: self.close_socket(connection)

    def close(self):
        with self.condition:
            if self.state == "closed": return
            self.state = "stopping"
            self.tokens.clear()
            connections = list(self.connections)
        for connection in connections: self.close_socket(connection)
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive(): self.server.shutdown()
            self.server.server_close()
        deadline = time.monotonic() + self.timeout + 20 + 2 * FRAME_SECONDS
        with self.condition:
            while self.connections and time.monotonic() < deadline:
                self.condition.wait(min(.1, max(0, deadline - time.monotonic())))
            require(not self.connections, "model_gateway_shutdown_unverified")
        self.stop_watchdog.set()
        for thread in (self.thread, self.watchdog):
            if thread is not None and thread.ident is not None: thread.join(timeout=1)
            require(thread is None or not thread.is_alive(), "model_gateway_shutdown_unverified")
        self.keys.clear(); self.probes.clear(); self.probe_kinds.clear(); self.state = "closed"

    def __exit__(self, *args): self.close()
