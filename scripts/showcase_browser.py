#!/usr/bin/env python3
"""Private, read-only dashboard probe executed on the authorized second peer.

This component does not launch resources, enroll peers, or establish the entire
N1 verdict. The controller must bind the peer, source, owned VM and request.
Secrets arrive on stdin; reports contain only selected proof fields. Playwright
and its Chromium must already be installed on that peer (WebSocket routing is
required). Unsupported pinned UI/protocol contracts fail closed.
"""
import hashlib
import ipaddress
import json
import re
import signal
import socket
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from showcase_agents import ProbeError, invoke, need, strict_json

DNS = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net"
REQUEST = r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}"
READ_RPC = frozenset({"health", "status", "config.get", "config.schema", "agents.list", "models.list",
    "sessions.list", "sessions.resolve", "sessions.preview", "chat.history", "channels.status",
    "system-presence", "usage.status", "usage.cost", "skills.status", "cron.status", "cron.list",
    "node.list", "device.pair.list", "exec.approvals.get", "logs.tail", "agents.files.list",
    "users.self", "users.prefs.get", "update.status", "exec.approval.list", "plugin.approval.list",
    "openclaw.approval.list", "agent.identity.get", "models.authStatus", "sessions.describe",
    "question.list", "talk.catalog", "chat.startup", "chat.metadata", "progressCard.get",
    "sessions.branches.list", "sessions.companion.state", "plugins.controlUi.list", "board.get",
    "projects.list", "sessions.groups.list", "sessions.groups.defaults", "taskSuggestions.list",
    "system.info", "session.discussion.info", "sessions.catalog.list", "mentions.list"})
# The pinned UI subscribes and reports its own visibility/activity on connect.
# These cannot generate output or change app settings, and are not read proof.
UI_ACTIVITY_RPC = frozenset({"sessions.subscribe", "presence.activity",
    "sessions.observer.visibility", "sessions.messages.subscribe",
    "controlUi.sessionPullRequests.subscribe", "sessions.viewers.set", "plugin.surface.refresh"})
EMPTY_PARAMS_RPC = frozenset({"sessions.subscribe", "presence.activity", "users.self", "update.status",
    "exec.approval.list", "plugin.approval.list", "openclaw.approval.list", "question.list", "talk.catalog",
    "plugins.controlUi.list", "projects.list", "sessions.groups.list", "sessions.groups.defaults", "system.info",
    "mentions.list"})
OPENCLAW_READY = """() => {
  const app = document.querySelector('openclaw-app');
  const shell = document.querySelector('openclaw-app-shell');
  const s = shell?.context?.gateway?.snapshot;
  return Boolean(customElements.get('openclaw-app') && customElements.get('openclaw-app-shell') &&
    app && shell && s?.phase === 'connected' && s.hello?.type === 'hello-ok' &&
    !s.lastError && !document.querySelector('openclaw-login-gate') &&
    shell.getBoundingClientRect().width > 0 && shell.getBoundingClientRect().height > 0 &&
    shell.innerText.trim().length > 40);
}"""
HERMES_READ = """async path => {
  const r = await fetch(path, {credentials:'same-origin', cache:'no-store', redirect:'error'});
  if (r.status !== 200) return {status:r.status};
  const text = await r.text();
  if (text.length > 65536) return {status:0};
  const d = JSON.parse(text);
  return path === '/api/auth/me' ? {status:r.status, provider:d.provider, user_id:d.user_id,
    expires_at:d.expires_at} : {status:r.status, auth_required:d.auth_required,
    auth_providers:d.auth_providers, gateway_running:d.gateway_running, version:d.version};
}"""
BROWSER_PHASES = frozenset({"browser_launch", "openclaw_document", "openclaw_session",
    "openclaw_authenticated_read", "openclaw_settled_session", "openclaw_route", "hermes_login_document",
    "hermes_anonymous_session", "hermes_login_form", "hermes_login_submit",
    "hermes_session_redirect", "hermes_dashboard_render", "hermes_authenticated_session",
    "hermes_route"})
BROWSER_FAILURE_REASONS = frozenset({"browser_http_denied", "browser_websocket_denied",
    "browser_write_denied", "browser_response_denied", "hermes_session_unverified",
    "hermes_dashboard_unverified", "browser_probe_failed"})


def dns(value):
    need(isinstance(value, str) and re.fullmatch(DNS, value), "tailnet_dns_invalid")
    return value


def origin(name, runtime):
    return "https://" + dns(name) + (":8443" if runtime == "hermes" else "")


def split_url(value):
    try:
        need(isinstance(value, str) and len(value) <= 8192 and not re.search(r"[\x00-\x20\\]", value), "browser_url_invalid")
        parsed = urlsplit(value)
        need(parsed.username is None and parsed.password is None and parsed.hostname and parsed.port in (None, 443, 8443, 18789), "browser_url_invalid")
        return parsed
    except ValueError:
        raise ProbeError("browser_url_invalid") from None


def rebase_handoff(browser_url, target_dns):
    """Only call with dashboard --json from the already verified owned container.

    Keep the one-time fragment opaque. Never accept a remote authority from that
    URL or put fragment credentials into an HTTP query string.
    """
    parsed = split_url(browser_url)
    need(parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
         and parsed.port == 18789 and parsed.path in ("", "/") and not parsed.query
         and parsed.fragment, "handoff_url_invalid")
    pairs = parse_qsl(parsed.fragment, keep_blank_values=True)
    need(pairs and len(pairs) <= 16 and len({k for k, _ in pairs}) == len(pairs)
         and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", k) and v and not re.search(r"[\x00-\x1f\x7f]", v) for k, v in pairs), "handoff_url_invalid")
    pairs = [(k, v) for k, v in pairs if k != "gatewayUrl"]
    need(dict(pairs).get("bootstrapToken"), "handoff_url_invalid")
    pairs.append(("gatewayUrl", "wss://" + dns(target_dns)))
    return urlunsplit(("https", target_dns, "/", "", urlencode(pairs)))


def request_valid(request):
    need(isinstance(request, dict) and set(request) == {"schema", "request_id", "runner_dns", "target_dns",
         "openclaw_url", "hermes_username", "hermes_password"}, "browser_request_invalid")
    need(type(request["schema"]) is int and request["schema"] == 1
         and isinstance(request["request_id"], str) and re.fullmatch(REQUEST, request["request_id"]), "browser_request_invalid")
    need(dns(request["runner_dns"]) != dns(request["target_dns"]), "second_peer_required")
    parsed = split_url(request["openclaw_url"])
    need(parsed.scheme == "https" and parsed.netloc == request["target_dns"] and parsed.path == "/"
         and not parsed.query and parsed.fragment, "handoff_url_invalid")
    pairs = parse_qsl(parsed.fragment, keep_blank_values=True)
    need(len(pairs) <= 16 and len({k for k, _ in pairs}) == len(pairs) and len(pairs) >= 2
         and dict(pairs).get("gatewayUrl") == "wss://" + request["target_dns"]
         and dict(pairs).get("bootstrapToken")
         and all(k and v and not re.search(r"[\x00-\x1f\x7f]", k + v) for k, v in pairs), "handoff_url_invalid")
    for name in ("hermes_username", "hermes_password"):
        need(isinstance(request[name], str) and 1 <= len(request[name]) <= 1024
             and not re.search(r"[\x00-\x1f\x7f]", request[name]), "browser_request_invalid")


def ts_ip(value):
    need(isinstance(value, str), "tailnet_ip_invalid")
    try:
        ip = ipaddress.ip_address(value)
        need(ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"), "tailnet_ip_invalid")
        return str(ip)
    except ValueError:
        raise ProbeError("tailnet_ip_invalid") from None


def peer_check(request, execute=invoke, resolve=socket.getaddrinfo):
    request_valid(request)
    status = strict_json(execute(["tailscale", "status", "--json"], timeout=15))
    need(isinstance(status, dict) and status.get("BackendState") == "Running" and not status.get("Health"), "peer_not_ready")
    own = status.get("Self")
    need(isinstance(own, dict) and own.get("Online") is True
         and own.get("DNSName") == request["runner_dns"] + ".", "runner_peer_mismatch")
    peers = status.get("Peer")
    need(isinstance(peers, dict), "target_peer_unverified")
    matches = [v for v in peers.values() if isinstance(v, dict) and v.get("DNSName") == request["target_dns"] + "."]
    need(len(matches) == 1 and matches[0].get("Online") is True, "target_peer_unverified")
    target = matches[0]
    need(isinstance(own.get("TailscaleIPs"), list) and own["TailscaleIPs"]
         and isinstance(target.get("TailscaleIPs"), list) and target["TailscaleIPs"], "tailnet_ip_invalid")
    self_ips, target_ips = {ts_ip(v) for v in own["TailscaleIPs"]}, {ts_ip(v) for v in target["TailscaleIPs"]}
    need(not self_ips & target_ips, "second_peer_required")
    address = sorted(target_ips, key=lambda v: (ipaddress.ip_address(v).version, v))[0]
    pong = execute(["tailscale", "ping", "--c=1", "--timeout=10s", "--until-direct=false", address], timeout=15)
    need(isinstance(pong, bytes) and len(pong) <= 4096
         and re.fullmatch(rb"pong from [^\r\n]+ \(" + re.escape(address.encode()) + rb"\) via [^\r\n]+ in [^\r\n]+\s*", pong), "peer_traffic_unverified")
    try:
        resolved = {str(ipaddress.ip_address(row[4][0])) for row in resolve(request["target_dns"], 443, type=socket.SOCK_STREAM)}
    except (OSError, ValueError, IndexError, TypeError):
        raise ProbeError("tailnet_dns_unverified") from None
    need(resolved and resolved <= target_ips, "tailnet_dns_unverified")
    return {"runner_dns": request["runner_dns"], "target_dns": request["target_dns"], "address": address,
            "runner_addresses": sorted(self_ips), "traffic": "tailscale_ping", "dns": "matches_peer"}


class NetworkGuard:
    def __init__(self, name, runtime):
        self.origin, self.runtime = origin(name, runtime), runtime
        self.failed, self.login_count = False, 0
        self.hello, self.read_rpc = False, False
        self.failure_reason = None
        self.failure_request = None
        self.canvas_url, self.canvas_refresh_count = None, 0

    def deny(self, reason, request=None):
        # Keep the first fixed clue; URLs, credentials and message bodies stay private.
        if not self.failed:
            self.failure_reason = reason
            if reason == "browser_write_denied" and isinstance(request, dict):
                method, params = request.get("method"), request.get("params")
                if isinstance(method, str) and len(method) <= 128:
                    keys = sorted(params) if isinstance(params, dict) and len(params) <= 64 else None
                    self.failure_request = {
                        "method_sha256": hashlib.sha256(method.encode()).hexdigest(),
                        "parameter_keys_sha256": hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()}
        self.failed = True

    def http(self, route):
        request = route.request
        try:
            parsed = split_url(request.url)
            need(parsed.scheme == "https" and "https://" + parsed.netloc == self.origin and not parsed.fragment)
            need(not any(c in parsed.path.lower() for c in ("%", "//", "..")))
            if request.method not in ("GET", "HEAD"):
                need(self.runtime == "hermes" and request.method == "POST" and parsed.path == "/auth/password-login"
                     and not parsed.query and self.login_count == 0)
                data = strict_json(request.post_data)
                need(isinstance(data, dict) and set(data) == {"provider", "username", "password", "next"}
                     and data["provider"] == "basic" and data["next"] == "/sessions")
                self.login_count += 1
            else:
                need(not parsed.path.startswith(("/auth/", "/api/auth/native")))
            route.continue_()
        except (ProbeError, TypeError):
            self.deny("browser_http_denied")
            route.abort()

    def websocket(self, ws):
        try:
            parsed = split_url(ws.url)
            need(self.runtime == "openclaw" and parsed.scheme == "wss"
                 and "https://" + parsed.netloc == self.origin and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment)
            server = ws.connect_to_server()
        except ProbeError:
            self.deny("browser_websocket_denied")
            ws.close(code=1008, reason="probe_route_denied")
            return

        pending = {}

        def send(message):
            data = None
            try:
                if self.failed:
                    return
                data = strict_json(message)
                need(isinstance(data, dict) and data.get("type") == "req" and isinstance(data.get("id"), str)
                     and data.get("method") in READ_RPC | UI_ACTIVITY_RPC | {"connect"})
                method, params = data["method"], data.get("params")
                if method in EMPTY_PARAMS_RPC:
                    need(params == {})  # In particular, no update refreshCheckout flag.
                if method in UI_ACTIVITY_RPC:
                    need(self.hello and isinstance(params, dict))
                    if method == "plugin.surface.refresh":
                        # Renew only this connection's server-issued canvas capability.
                        # The pinned handler updates a lease, never config or model output.
                        need(set(params) == {"surface", "observedUrl"} and params["surface"] == "canvas"
                             and self.canvas_url is not None and params["observedUrl"] == self.canvas_url
                             and self.canvas_refresh_count < 8)
                        self.canvas_refresh_count += 1
                    elif method == "sessions.observer.visibility":
                        need(set(params) == {"visible"} and type(params["visible"]) is bool)
                    elif method == "sessions.messages.subscribe":
                        need({"key", "subscriptionId"} <= set(params) <= {"key", "subscriptionId", "includeApprovals"}
                             and all(isinstance(params[key], str) and 0 < len(params[key]) <= limit
                                     and not re.search(r"[\x00-\x1f\x7f]", params[key])
                                     for key, limit in (("key", 1024), ("subscriptionId", 256))))
                        if "includeApprovals" in params:
                            need(type(params["includeApprovals"]) is bool)
                    elif method in ("controlUi.sessionPullRequests.subscribe", "sessions.viewers.set"):
                        need(set(params) == {"sessionKeys"} and isinstance(params["sessionKeys"], list)
                             and len(params["sessionKeys"]) <= 200
                             and all(isinstance(key, str) and 0 < len(key) <= 512 and key == key.strip()
                                     and not re.search(r"[\x00-\x1f\x7f]", key) for key in params["sessionKeys"]))
                        # Notification/viewer state only; no force-refresh keys or app changes.
                if method in ("board.get", "taskSuggestions.list", "session.discussion.info"):
                    keys = {"agentId"} if method == "taskSuggestions.list" else {"agentId", "sessionKey"}
                    need(isinstance(params, dict) and set(params) <= keys
                         and all(isinstance(value, str) and 0 < len(value) <= (256 if key == "agentId" else 1024)
                                 and not re.search(r"[\x00-\x1f\x7f]", value) for key, value in params.items()))
                if method == "sessions.catalog.list":
                    need(isinstance(params, dict) and set(params) <= {"agentId", "allowPartialResults", "limitPerHost", "progressId"})
                    for key in ("agentId", "progressId"):
                        if key in params:
                            need(isinstance(params[key], str) and 0 < len(params[key]) <= 256
                                 and not re.search(r"[\x00-\x1f\x7f]", params[key]))
                    if "allowPartialResults" in params:
                        need(type(params["allowPartialResults"]) is bool)
                    if "limitPerHost" in params:
                        need(type(params["limitPerHost"]) is int and 1 <= params["limitPerHost"] <= 200)
                if method == "models.authStatus":
                    need(isinstance(params, dict) and set(params) <= {"agentId"}
                         and ("agentId" not in params or isinstance(params["agentId"], str)
                              and 0 < len(params["agentId"]) <= 256
                              and not re.search(r"[\x00-\x1f\x7f]", params["agentId"])))
                need(data["id"] not in pending and len(pending) < 256)
                pending[data["id"]] = data["method"]
                server.send(message)  # Forward unchanged to the real gateway; never synthesize a reply.
            except (ProbeError, TypeError):
                self.deny("browser_write_denied", data)
                ws.close(code=1008, reason="probe_write_denied")
                server.close(code=1008, reason="probe_write_denied")
        ws.on_message(send)

        def receive(message):
            try:
                data = strict_json(message)
                if isinstance(data, dict) and data.get("type") == "res":
                    method = pending.pop(data.get("id"), None)
                    if method == "connect":
                        payload = data.get("payload")
                        need(data.get("ok") is True and isinstance(payload, dict) and payload.get("type") == "hello-ok")
                        auth = payload.get("auth")
                        need(isinstance(auth, dict) and auth.get("role") == "operator"
                             and isinstance(auth.get("scopes"), list) and
                             any(scope in auth["scopes"] for scope in ("operator.read", "operator.admin")))
                        self.hello = True
                        urls = payload.get("pluginSurfaceUrls")
                        canvas = urls.get("canvas") if isinstance(urls, dict) else None
                        if isinstance(canvas, str) and 0 < len(canvas.strip()) <= 8192:
                            self.canvas_url = canvas.strip()
                    elif method == "plugin.surface.refresh" and self.hello and data.get("ok") is True:
                        payload = data.get("payload")
                        urls = payload.get("pluginSurfaceUrls") if isinstance(payload, dict) and payload.get("surface") == "canvas" else None
                        canvas = urls.get("canvas") if isinstance(urls, dict) else None
                        if isinstance(canvas, str) and 0 < len(canvas.strip()) <= 8192:
                            self.canvas_url = canvas.strip()
                    elif method in READ_RPC and self.hello and data.get("ok") is True:
                        self.read_rpc = True
                ws.send(message)  # Preserve actual responses and events; do not return raw data.
            except (ProbeError, TypeError):
                self.deny("browser_response_denied")
                ws.close(code=1008, reason="probe_response_denied")
                server.close(code=1008, reason="probe_response_denied")
        server.on_message(receive)


def verify_hermes(me, status, username, now):
    need(isinstance(me, dict) and me.get("status") == 200 and me.get("provider") == "basic"
         and me.get("user_id") == username and type(me.get("expires_at")) in (int, float)
         and now < me["expires_at"] <= now + 31536000, "hermes_session_unverified")
    need(isinstance(status, dict) and status.get("status") == 200 and status.get("auth_required") is True
         and isinstance(status.get("auth_providers"), list) and "basic" in status["auth_providers"]
         and status.get("gateway_running") is True and isinstance(status.get("version"), str)
         and re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", status["version"]), "hermes_dashboard_unverified")
    return {"authentication": "basic_session", "rendered": True, "gateway_running": True, "version": status["version"]}


def browser_check(request, peer, playwright, now=time.time, diagnostic=None):
    request_valid(request)
    need(peer.get("runner_dns") == request["runner_dns"] and peer.get("target_dns") == request["target_dns"]
         and peer.get("traffic") == "tailscale_ping" and peer.get("dns") == "matches_peer", "peer_proof_required")
    address = ts_ip(peer.get("address"))
    mapped = "[" + address + "]" if ":" in address else address
    browser = context = guard = None
    results = {}
    if diagnostic is None:
        diagnostic = {}
    try:
        diagnostic['phase'] = 'browser_launch'
        browser = playwright.chromium.launch(headless=True, timeout=20000,
            args=["--no-proxy-server", "--host-resolver-rules=MAP " + request["target_dns"] + " " + mapped],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC"})
        for runtime in ("openclaw", "hermes"):
            context = browser.new_context(ignore_https_errors=False, service_workers="block", accept_downloads=False,
                locale="en-US", viewport={"width": 1280, "height": 900})
            need(callable(getattr(context, "route_web_socket", None)), "browser_websocket_routing_required")
            context.set_default_timeout(30000)
            guard = NetworkGuard(request["target_dns"], runtime)
            context.route("**/*", guard.http)
            context.route_web_socket("**/*", guard.websocket)
            page = context.new_page()
            if runtime == "openclaw":
                diagnostic['phase'] = 'openclaw_document'
                reply = page.goto(request["openclaw_url"], wait_until="domcontentloaded", timeout=30000)
                need(reply is not None and reply.status == 200, "openclaw_document_failed")
                diagnostic['phase'] = 'openclaw_session'
                page.wait_for_function(OPENCLAW_READY, timeout=30000)
                need(page.evaluate(OPENCLAW_READY) is True, "openclaw_session_unverified")
                diagnostic['phase'] = 'openclaw_authenticated_read'
                deadline = time.monotonic() + 10
                while not guard.failed and not (guard.hello and guard.read_rpc) and time.monotonic() < deadline:
                    page.wait_for_timeout(100)  # Pump browser events while an actual read is pending.
                need(guard.hello and guard.read_rpc, "openclaw_authenticated_read_unverified")
                diagnostic['phase'] = 'openclaw_settled_session'
                page.wait_for_timeout(5000)  # Catch startup calls scheduled after the first render/read.
                need(page.evaluate(OPENCLAW_READY) is True and not guard.failed, "openclaw_session_unverified")
                results[runtime] = {"authentication": "gateway_hello", "rendered": True, "read_rpc": True}
            else:
                diagnostic['phase'] = 'hermes_login_document'
                reply = page.goto(guard.origin + "/login?next=%2Fsessions", wait_until="domcontentloaded", timeout=30000)
                need(reply is not None and reply.status == 200, "hermes_login_failed")
                diagnostic['phase'] = 'hermes_anonymous_session'
                me = page.evaluate(HERMES_READ, "/api/auth/me")
                need(isinstance(me, dict) and me.get("status") == 401, "hermes_anonymous_access_not_denied")
                diagnostic['phase'] = 'hermes_login_form'
                form = page.locator('form.provider-form[data-provider="basic"]')
                need(form.count() == 1 and form.is_visible(), "hermes_login_form_missing")
                form.locator('input[name="username"]').fill(request["hermes_username"])
                form.locator('input[name="password"]').fill(request["hermes_password"])
                diagnostic['phase'] = 'hermes_login_submit'
                form.locator('button[type="submit"]').click()
                diagnostic['phase'] = 'hermes_session_redirect'
                page.wait_for_url(re.compile(r"^" + re.escape(guard.origin) + r"/sessions(?:\?[^#]*)?\Z"), timeout=30000)
                diagnostic['phase'] = 'hermes_dashboard_render'
                page.get_by_role("heading", name="Sessions", exact=True).wait_for(state="visible")
                need(page.locator('#root a[href="/sessions"]').first.is_visible()
                     and page.locator("#root").is_visible() and len(page.locator("#root").inner_text().strip()) > 40
                     and page.locator('form.provider-form').count() == 0, "hermes_dashboard_not_rendered")
                diagnostic['phase'] = 'hermes_authenticated_session'
                results[runtime] = verify_hermes(page.evaluate(HERMES_READ, "/api/auth/me"),
                    page.evaluate(HERMES_READ, "/api/status"), request["hermes_username"], now())
                need(guard.login_count == 1, "hermes_login_not_observed")
            diagnostic['phase'] = runtime + '_route'
            parsed = split_url(page.url)
            need(parsed.scheme == "https" and "https://" + parsed.netloc == guard.origin and not guard.failed, "browser_route_not_verified")
            results[runtime]["origin"] = guard.origin
            context.close()
            context = None
        return results
    except (Exception, KeyboardInterrupt):
        if guard is not None and guard.failed:
            if guard.failure_request is not None:
                diagnostic['request_fingerprint'] = guard.failure_request
            raise ProbeError(guard.failure_reason) from None
        raise
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            if browser is not None:
                browser.close()


def run_probe(request, execute=invoke, resolve=socket.getaddrinfo, factory=None, now=time.time):
    started = now()
    peer = peer_check(request, execute, resolve)
    if factory is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ProbeError("peer_browser_dependency_missing") from None
        factory = sync_playwright
    try:
        with factory() as playwright:
            apps = browser_check(request, peer, playwright, now)
    except ProbeError:
        raise
    except (Exception, KeyboardInterrupt):
        raise ProbeError("browser_probe_failed") from None
    # Bind to this controller request without recording the private handoff URL.
    return {"schema": 1, "status": "pass", "request_id": request["request_id"], "peer": peer,
            "apps": apps, "duration_seconds": round(now() - started, 3)}


def validate_proof(proof, request):
    request_valid(request)
    need(isinstance(proof, dict) and set(proof) == {"schema", "status", "request_id", "peer", "apps", "duration_seconds"}
         and type(proof["schema"]) is int and proof["schema"] == 1 and proof["status"] == "pass"
         and proof["request_id"] == request["request_id"], "browser_proof_invalid")
    peer, apps = proof["peer"], proof["apps"]
    need(isinstance(peer, dict) and set(peer) == {"runner_dns", "target_dns", "address", "runner_addresses", "traffic", "dns"}
         and peer["runner_dns"] == request["runner_dns"] and peer["target_dns"] == request["target_dns"]
         and peer["traffic"] == "tailscale_ping" and peer["dns"] == "matches_peer", "browser_proof_invalid")
    ts_ip(peer["address"])
    need(isinstance(peer["runner_addresses"], list) and 1 <= len(peer["runner_addresses"]) <= 4
         and peer["runner_addresses"] == sorted(set(peer["runner_addresses"]))
         and all(ts_ip(value) != peer["address"] for value in peer["runner_addresses"]), "browser_proof_invalid")
    need(isinstance(apps, dict) and set(apps) == {"openclaw", "hermes"}, "browser_proof_invalid")
    for runtime, auth in (("openclaw", "gateway_hello"), ("hermes", "basic_session")):
        row = apps[runtime]
        expected = {"authentication", "rendered", "origin"} | ({"gateway_running", "version"} if runtime == "hermes" else {"read_rpc"})
        need(isinstance(row, dict) and set(row) == expected and row["authentication"] == auth and row["rendered"] is True
             and row["origin"] == origin(request["target_dns"], runtime), "browser_proof_invalid")
        if runtime == "hermes":
            need(row["gateway_running"] is True and isinstance(row["version"], str)
                 and re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", row["version"]), "browser_proof_invalid")
        else:
            need(row["read_rpc"] is True, "browser_proof_invalid")
    need(type(proof["duration_seconds"]) in (int, float) and 0 <= proof["duration_seconds"] < 150, "browser_proof_invalid")
    return {**proof, "proof_sha256": hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def main():
    def timeout(_signum, _frame):
        raise ProbeError("browser_deadline_exceeded")
    signal.signal(signal.SIGALRM, timeout)
    signal.signal(signal.SIGTERM, timeout)
    signal.alarm(150)
    try:
        request = strict_json(sys.stdin.buffer.read(16385))
        need(len(json.dumps(request)) <= 16384, "browser_request_invalid")
        proof = run_probe(request)
        validate_proof(proof, request)
        print(json.dumps(proof, sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt):
        # Browser errors can include the URL or filled values. Never print them.
        print('{"schema":1,"status":"fail"}')
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
