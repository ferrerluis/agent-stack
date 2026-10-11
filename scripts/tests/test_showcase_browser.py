import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_browser as probe

TARGET = "as7-fixture.example.ts.net"
RUNNER = "runner.example.ts.net"
ADDRESS = "100.100.2.3"
SECRET = "fake-password-private"
ID = "12345678-1234-4234-8234-123456789abc"


def request():
    return {"schema": 1, "request_id": ID, "runner_dns": RUNNER, "target_dns": TARGET,
        "openclaw_url": probe.rebase_handoff("http://127.0.0.1:18789/#bootstrapToken=fake-handoff&bootstrapProfile=owner&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789", TARGET),
        "hermes_username": "fixture", "hermes_password": SECRET}


def status():
    return {"BackendState": "Running", "Self": {"DNSName": RUNNER + ".", "Online": True, "TailscaleIPs": ["100.100.1.2"]},
        "Peer": {"n1": {"DNSName": TARGET + ".", "Online": True, "TailscaleIPs": [ADDRESS]}}}


def peer():
    return {"runner_dns": RUNNER, "target_dns": TARGET, "address": ADDRESS, "runner_addresses": ["100.100.1.2"], "traffic": "tailscale_ping", "dns": "matches_peer"}


def route(url, method="GET", data=None):
    return Mock(request=Mock(url=url, method=method, post_data=json.dumps(data) if data is not None else None))


class FakeWS:
    def __init__(self, url):
        self.url, self.sent, self.closed, self.handler = url, [], False, None
        self.server = None

    def connect_to_server(self):
        self.server = FakeWS(self.url)
        return self.server

    def on_message(self, handler):
        self.handler = handler

    def send(self, message):
        self.sent.append(message)

    def close(self, **kwargs):
        self.closed = True

    def emit(self, data):
        self.handler(data if isinstance(data, str) else json.dumps(data))


def handshake(ws):
    ws.emit({"type": "req", "id": "connect1", "method": "connect", "params": {"role": "operator"}})
    ws.server.emit({"type": "res", "id": "connect1", "ok": True, "payload": {"type": "hello-ok",
        "auth": {"role": "operator", "scopes": ["operator.read"]}, "server": {"version": "2026.9.8"}}})
    ws.emit({"type": "req", "id": "read1", "method": "agents.list", "params": {}})
    ws.server.emit({"type": "res", "id": "read1", "ok": True, "payload": {"agents": []}})


class BrowserFixture:
    def __init__(self):
        self.contexts, self.launch_kwargs = [], None
        self.ui_ready, self.hello_ready, self.heading_ready = True, True, True
        self.login_ok, self.anonymous, self.blank = True, 401, False
        self.me = {"status": 200, "provider": "basic", "user_id": "fixture", "expires_at": 2000}
        self.status = {"status": 200, "auth_required": True, "auth_providers": ["basic"], "gateway_running": True, "version": "0.14.0"}
        self.chromium = Mock()
        self.chromium.launch.side_effect = self.launch
        self.browser = Mock()
        self.browser.new_context.side_effect = self.context

    def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser

    def context(self, **kwargs):
        context = Mock()
        context.kwargs = kwargs
        context.route.side_effect = lambda pattern, handler: setattr(context, "http", handler)
        context.route_web_socket.side_effect = lambda pattern, handler: setattr(context, "websocket", handler)
        page = Mock()
        context.new_page.return_value = page
        context.page = page
        self.contexts.append(context)
        index = len(self.contexts)
        form = Mock()
        form.count.return_value = 1
        form.is_visible.return_value = True
        form.locator.return_value.click.side_effect = lambda: self.login(context)
        root = Mock()
        root.is_visible.return_value = True
        root.inner_text.return_value = "" if self.blank else "Sessions Overview Gateway Running fixture rendered content"
        link = Mock()
        link.first.is_visible.return_value = True
        missing = Mock()
        missing.count.return_value = 0
        page.locator.side_effect = lambda selector: form if selector.startswith('form.provider-form[data') else root if selector == '#root' else link if selector.startswith('#root a') else missing
        page.get_by_role.return_value.wait_for.side_effect = None if self.heading_ready else RuntimeError(SECRET)
        page.goto.side_effect = lambda url, **kw: self.goto(context, index, url)
        def redirected(url, **kwargs):
            actual = getattr(self, 'hermes_redirect', 'https://' + TARGET + ':8443/sessions')
            matched = url == actual if isinstance(url, str) else url.search(actual) is not None
            if not self.login_ok or not matched:
                raise RuntimeError(SECRET)
            page.url = actual
        page.wait_for_url.side_effect = redirected
        page.wait_for_function.side_effect = None if self.ui_ready else RuntimeError(SECRET)
        page.wait_for_timeout.side_effect = None if self.hello_ready else probe.ProbeError('fixture_pending_read_failed')
        page.evaluate.side_effect = lambda script, *args: self.evaluate(context, script, *args)
        return context

    def goto(self, context, index, url):
        context.page.url = url
        r = route(url.split('#')[0])
        context.http(r)
        if index == 1 and self.hello_ready:
            ws = FakeWS("wss://" + TARGET + "/")
            context.websocket(ws)
            handshake(ws)
        return Mock(status=200)

    def evaluate(self, context, script, *args):
        if script == probe.OPENCLAW_READY:
            return self.ui_ready
        if args[0] == "/api/auth/me":
            if context.page.url.split('?')[0].endswith('/login'):
                return {"status": self.anonymous}
            return self.me
        return self.status

    def login(self, context):
        context.http(route("https://" + TARGET + ":8443/auth/password-login", "POST",
            {"provider": "basic", "username": "fixture", "password": SECRET, "next": "/sessions"}))


class PeerTest(unittest.TestCase):
    def check(self, raw=None, pong=None, ips=None):
        execute = Mock(side_effect=[json.dumps(status() if raw is None else raw).encode(),
            b'pong from as7-fixture (100.100.2.3) via DERP(fixture) in 1ms\n' if pong is None else pong])
        resolver = Mock(return_value=[(2, 1, 6, '', (value, 443)) for value in (ips or [ADDRESS])])
        result = probe.peer_check(request(), execute, resolver)
        return result, execute

    def test_actual_peer_ping_and_dns_are_required_relay_is_valid(self):
        result, execute = self.check()
        self.assertEqual(result, peer())
        self.assertEqual(execute.call_args_list[1].args[0], ["tailscale", "ping", "--c=1", "--timeout=10s", "--until-direct=false", ADDRESS])
        self.assertNotIn(SECRET, str(execute.call_args_list))

    def test_self_or_unready_or_wrong_runner_does_not_pass(self):
        for mutator in (lambda s: s.update(BackendState="Stopped"), lambda s: s.update(Health=["problem"]),
            lambda s: s["Self"].update(Online=False), lambda s: s["Self"].update(DNSName=TARGET + "."),
            lambda s: s["Self"].update(TailscaleIPs=[ADDRESS])):
            raw = status(); mutator(raw)
            with self.assertRaises(probe.ProbeError):
                self.check(raw=raw)

    def test_missing_duplicate_offline_wrong_ip_target_is_refused(self):
        for mutator in (lambda s: s.update(Peer={}), lambda s: s["Peer"].update(n2=copy.deepcopy(s["Peer"]["n1"])),
            lambda s: s["Peer"]["n1"].update(Online=False), lambda s: s["Peer"]["n1"].update(TailscaleIPs=["203.0.113.1"]),
            lambda s: s["Peer"]["n1"].update(TailscaleIPs=[]), lambda s: s["Peer"]["n1"].update(TailscaleIPs=[None])):
            raw = status(); mutator(raw)
            with self.assertRaises(probe.ProbeError):
                self.check(raw=raw)

    def test_success_exit_without_correct_pong_and_public_dns_cannot_pass(self):
        for pong in (b'', b'local is Tailscale IP\n', b'pong from other (100.100.1.2) via direct in 1ms\n', b'failure\npong from as7-fixture (100.100.2.3) via direct in 1ms\n'):
            with self.assertRaisesRegex(probe.ProbeError, "peer_traffic_unverified"):
                self.check(pong=pong)
        for ips in (["127.0.0.1"], ["203.0.113.5"], [ADDRESS, "203.0.113.5"], ["100.100.1.2"]):
            with self.assertRaisesRegex(probe.ProbeError, "tailnet_dns_unverified"):
                self.check(ips=ips)

    def test_ipv6_and_dns_failure(self):
        self.assertEqual(probe.ts_ip("fd7a:115c:a1e0::1"), "fd7a:115c:a1e0::1")
        for ip in ("::1", "fe80::1", "100.128.0.1", "100.63.255.255", "1.2.3.4", None):
            with self.assertRaises(probe.ProbeError):
                probe.ts_ip(ip)
        raw = json.dumps(status()).encode()
        with self.assertRaisesRegex(probe.ProbeError, "tailnet_dns_unverified"):
            probe.peer_check(request(), Mock(side_effect=[raw, b'pong from as7-fixture (100.100.2.3) via direct in 1ms']), Mock(side_effect=OSError(SECRET)))


class RouteTest(unittest.TestCase):
    def test_owned_loopback_handoff_only_secret_remains_fragment(self):
        req = request(); probe.request_valid(req)
        self.assertTrue(req["openclaw_url"].startswith('https://' + TARGET + '/#'))
        self.assertIn('bootstrapToken=fake-handoff', req["openclaw_url"])
        self.assertIn('gatewayUrl=wss%3A%2F%2F' + TARGET, req["openclaw_url"])
        for url in ("https://external.test/#bootstrapToken=secret", "http://localhost:18789/?token=secret#bootstrapToken=secret",
            "http://localhost:18789/#token=secret", "http://localhost:18789/#bootstrapToken=a&bootstrapToken=b",
            "http://localhost:18789/#bootstrapToken=%00", "http://localhost:18789/x#bootstrapToken=a"):
            with self.assertRaises(probe.ProbeError):
                probe.rebase_handoff(url, TARGET)

    def test_wrong_origin_query_duplicate_fragment_and_request_types(self):
        for changes in ({"runner_dns": TARGET}, {"target_dns": "target.external.test"}, {"schema": True}, {"request_id": "stale"},
            {"hermes_password": "x\n"}, {"openclaw_url": request()["openclaw_url"].replace('https://', 'http://', 1)},
            {"openclaw_url": request()["openclaw_url"].replace('/#', '/?token=secret#')},
            {"openclaw_url": request()["openclaw_url"] + '&gatewayUrl=evil'}, {"extra": SECRET}):
            raw = request(); raw.update(changes)
            with self.assertRaises(probe.ProbeError):
                probe.request_valid(raw)

    def test_https_exact_origin_get_allowed_external_redirect_and_plaintext_denied(self):
        for url, allowed in (("https://" + TARGET + ":8443/api/status", True), ("https://" + TARGET + ":8443/assets/ui.js", True),
            ("http://" + TARGET + ":8443/", False), ("https://external.test/api/status", False), ("https://" + TARGET + "/", False),
            ("https://user:secret@" + TARGET + ":8443/", False), ("https://" + TARGET + ":8443/auth/logout", False),
            ("https://" + TARGET + ":8443/%61uth/logout", False)):
            guard = probe.NetworkGuard(TARGET, "hermes"); r = route(url); guard.http(r)
            self.assertEqual(r.continue_.call_count, int(allowed))
            self.assertEqual(r.abort.call_count, int(not allowed))

    def test_only_one_basic_login_to_sessions_may_write(self):
        guard = probe.NetworkGuard(TARGET, "hermes")
        data = {"provider": "basic", "username": "fixture", "password": SECRET, "next": "/sessions"}
        r = route(guard.origin + '/auth/password-login', 'POST', data); guard.http(r)
        self.assertEqual(guard.login_count, 1); r.continue_.assert_called_once()
        r = route(guard.origin + '/auth/password-login', 'POST', data); guard.http(r); r.abort.assert_called_once()
        for path in ('/api/chat', '/v1/runs', '/api/cron', '/api/pty', '/api/config'):
            guard = probe.NetworkGuard(TARGET, "hermes"); r = route(guard.origin + path, 'POST', data); guard.http(r)
            r.abort.assert_called_once(); r.continue_.assert_not_called()
        for changes in ({"next": "/chat"}, {"provider": "oauth"}, {"extra": SECRET}):
            guard = probe.NetworkGuard(TARGET, "hermes"); bad = dict(data); bad.update(changes)
            r = route(guard.origin + '/auth/password-login', 'POST', bad); guard.http(r); r.abort.assert_called_once()

    def test_socket_only_real_owned_wss_openclaw_root(self):
        for runtime, url in (("hermes", "wss://" + TARGET + ':8443/ws'), ("openclaw", 'ws://' + TARGET),
            ("openclaw", 'wss://external.test'), ("openclaw", 'wss://' + TARGET + '/pty'),
            ("openclaw", 'wss://' + TARGET + '?token=secret')):
            guard = probe.NetworkGuard(TARGET, runtime); ws = FakeWS(url); guard.websocket(ws)
            self.assertTrue(ws.closed); self.assertIsNone(ws.server)

    def test_real_handshake_read_response_forwarded_unchanged(self):
        guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET); guard.websocket(ws); handshake(ws)
        self.assertTrue(guard.hello and guard.read_rpc); self.assertFalse(guard.failed)
        self.assertEqual(len(ws.server.sent), 2); self.assertEqual(len(ws.sent), 2)
        self.assertEqual(json.loads(ws.server.sent[1])['method'], 'agents.list')

    def connect(self, ws):
        ws.emit({"type": "req", "id": "connect1", "method": "connect", "params": {"role": "operator"}})
        ws.server.emit({"type": "res", "id": "connect1", "ok": True, "payload": {"type": "hello-ok",
            "auth": {"role": "operator", "scopes": ["operator.read"]}}})

    def test_pinned_ui_startup_reads_and_subscriptions_retain_real_connection(self):
        guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET)
        guard.websocket(ws); self.connect(ws)
        calls = [('sessions.subscribe', {}), ('sessions.list', {"limit": 50}), ('config.get', {}),
            ('update.status', {}), ('presence.activity', {}), ('users.self', {}), ('agents.list', {}),
            ('exec.approval.list', {}), ('plugin.approval.list', {}), ('openclaw.approval.list', {}),
            ('sessions.observer.visibility', {"visible": True}), ('users.prefs.get', {"keys": ["ui.theme"]}),
            ('agent.identity.get', {"agentId": "main"}), ('models.authStatus', {"agentId": "main"}),
            ('sessions.describe', {"key": "agent:main:main"}), ('question.list', {}), ('talk.catalog', {}),
            ('sessions.messages.subscribe', {"key": "agent:main:main", "subscriptionId": "fixture-subscription", "includeApprovals": True}),
            ('chat.startup', {"sessionKey": "agent:main:main", "limit": 50, "maxBytes": 65536}),
            ('chat.metadata', {"sessionKey": "agent:main:main", "agentId": "main"}),
            ('progressCard.get', {"sessionKey": "agent:main:main"}),
            ('sessions.branches.list', {"sessionKey": "agent:main:main"}),
            ('sessions.companion.state', {"sessionKey": "agent:main:main", "agentId": "main"})]
        for index, (method, params) in enumerate(calls):
            request = {"type": "req", "id": "ui" + str(index), "method": method, "params": params}
            ws.emit(request)
            self.assertFalse(guard.failed, method)
            self.assertEqual(json.loads(ws.server.sent[-1]), request)
            reply = {"type": "res", "id": request["id"], "ok": True, "payload": {}}
            ws.server.emit(reply)
            self.assertEqual(json.loads(ws.sent[-1]), reply)
        self.assertTrue(guard.hello and guard.read_rpc); self.assertFalse(ws.closed)

    def test_activity_and_subscriptions_alone_are_not_authenticated_data_reads(self):
        guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET)
        guard.websocket(ws); self.connect(ws)
        for index, (method, params) in enumerate((('sessions.subscribe', {}), ('presence.activity', {}),
                ('sessions.observer.visibility', {"visible": True}),
                ('sessions.messages.subscribe', {"key": "agent:main:main", "subscriptionId": "fixture", "includeApprovals": True}),
                ('controlUi.sessionPullRequests.subscribe', {"sessionKeys": ['agent:main:main']}),
                ('sessions.viewers.set', {"sessionKeys": []}))):
            ws.emit({"type": "req", "id": str(index), "method": method, "params": params})
            ws.server.emit({"type": "res", "id": str(index), "ok": True, "payload": {}})
        self.assertTrue(guard.hello); self.assertFalse(guard.read_rpc or guard.failed)

    def test_advertised_startup_catalog_reads_are_forwarded_without_changes(self):
        guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET)
        guard.websocket(ws); self.connect(ws)
        calls = [(method, {}) for method in ('plugins.controlUi.list', 'projects.list',
                 'sessions.groups.list', 'sessions.groups.defaults', 'system.info')]
        calls += [('board.get', {'agentId': 'main', 'sessionKey': 'agent:main:main'}),
                  ('taskSuggestions.list', {'agentId': 'main'}),
                  ('session.discussion.info', {'agentId': 'main', 'sessionKey': 'agent:main:main'}),
                  ('sessions.catalog.list', {'agentId': 'main', 'allowPartialResults': True,
                   'limitPerHost': 100, 'progressId': 'fixture-progress'})]
        for index, (method, params) in enumerate(calls):
            message = {'type': 'req', 'id': str(index), 'method': method, 'params': params}
            ws.emit(message)
            self.assertEqual(json.loads(ws.server.sent[-1]), message)
        self.assertFalse(guard.failed)

    def test_catalog_and_viewer_calls_reject_changes_refresh_and_unbounded_keys(self):
        calls = [(method, {'refresh': True}) for method in ('plugins.controlUi.list', 'projects.list',
                 'sessions.groups.list', 'sessions.groups.defaults', 'system.info', 'board.get',
                 'taskSuggestions.list', 'session.discussion.info', 'sessions.catalog.list')]
        calls += [('sessions.catalog.list', {'limitPerHost': v}) for v in (True, 0, 201, 1.5)]
        calls += [('sessions.catalog.list', {'allowPartialResults': 1})]
        for method in ('controlUi.sessionPullRequests.subscribe', 'sessions.viewers.set'):
            calls += [(method, value) for value in ({'sessionKeys': [], 'refreshSessionKeys': []},
                {'sessionKeys': [], 'create': True}, {'sessionKeys': 'main'}, {'sessionKeys': [1]},
                {'sessionKeys': ['x' * 513]}, {'sessionKeys': ['a'] * 201},
                {'sessionKeys': [' bad ']}, {'sessionKeys': ['bad\nkey']})]
        for method, params in calls:
            with self.subTest(method=method, params=params):
                guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET)
                guard.websocket(ws); self.connect(ws); before = len(ws.server.sent)
                ws.emit({'type': 'req', 'id': 'bad', 'method': method, 'params': params})
                self.assertTrue(guard.failed and ws.closed)
                self.assertEqual(len(ws.server.sent), before)

    def test_startup_refresh_flags_and_malformed_activity_are_not_forwarded(self):
        calls = [('update.status', {"refreshCheckout": True}), ('users.self', {"extra": True}),
            ('presence.activity', {"wake": True}), ('sessions.subscribe', {"sessionKey": "other"}),
            ('sessions.observer.visibility', {"visible": 1}), ('sessions.observer.visibility', {"visible": True, "sessionKey": "other"}),
            ('sessions.messages.subscribe', {"key": "agent:main:main"}),
            ('sessions.messages.subscribe', {"key": "agent:main:main", "subscriptionId": "fixture", "includeApprovals": 1}),
            ('sessions.messages.subscribe', {"key": "agent:main:main", "subscriptionId": "fixture", "run": True}),
            ('models.authStatus', {"agentId": "main", "refresh": True})]
        for method, params in calls:
            with self.subTest(method=method, params=params):
                guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET)
                guard.websocket(ws); self.connect(ws); before = len(ws.server.sent)
                ws.emit({"type": "req", "id": "bad", "method": method, "params": params})
                self.assertTrue(guard.failed and ws.closed); self.assertEqual(len(ws.server.sent), before)

    def test_activity_requires_real_hello_and_failure_stops_queued_reads(self):
        guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET); guard.websocket(ws)
        ws.emit({"type": "req", "id": "early", "method": "sessions.subscribe", "params": {}})
        self.assertTrue(guard.failed); self.assertFalse(ws.server.sent)
        ws.emit({"type": "req", "id": "queued", "method": "agents.list", "params": {}})
        self.assertFalse(ws.server.sent)

    def test_paid_or_unknown_or_malformed_socket_request_never_forwarded(self):
        for message in ({"type": "req", "id": "1", "method": "agent"}, {"type": "req", "id": "1", "method": "chat.send"},
            {"type": "req", "id": "1", "method": "cron.add"}, {"type": "req", "id": "1", "method": []},
            {"type": "req", "id": "1", "method": "config.patch"},
            *({"type": "req", "id": "1", "method": method, "params": {}} for method in
              ('update.run', 'models.probe', 'users.prefs.set', 'approval.resolve', 'talk.voice.complete',
               'sessions.companion.ask', 'sessions.branches.switch', 'progressCard.refresh',
               'plugins.controlUi.reload', 'board.put', 'projects.create', 'sessions.groups.update')),
            'not-json', '{"type":"req","type":"req"}'):
            guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET); guard.websocket(ws); ws.emit(message)
            self.assertTrue(guard.failed); self.assertEqual(ws.server.sent, []); self.assertTrue(ws.closed)

    def test_101_stale_or_rejected_hello_is_not_authentication(self):
        for reply in ({"type": "res", "id": "stale", "ok": True, "payload": {"type": "hello-ok"}},
            {"type": "res", "id": "c", "ok": False}, {"type": "res", "id": "c", "ok": True, "payload": {"type": "hello-ok", "auth": {"role": "node", "scopes": ["operator.read"]}}},
            {"type": "res", "id": "c", "ok": True, "payload": {"type": "hello-ok", "auth": {"role": "operator", "scopes": []}}}):
            guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET); guard.websocket(ws)
            ws.emit({"type": "req", "id": "c", "method": "connect"}); ws.server.emit(reply)
            self.assertFalse(guard.hello)
        guard = probe.NetworkGuard(TARGET, 'openclaw'); ws = FakeWS('wss://' + TARGET); guard.websocket(ws)
        ws.emit({"type": "req", "id": "r", "method": "health"}); ws.server.emit({"type": "res", "id": "r", "ok": True, "payload": {}})
        self.assertFalse(guard.read_rpc)


class BrowserTest(unittest.TestCase):
    def check(self, fixture=None):
        fixture = fixture or BrowserFixture()
        return probe.browser_check(request(), peer(), fixture, now=lambda: 1000), fixture

    def test_fresh_tls_checked_contexts_private_routes_and_sanitized_proof(self):
        apps, fixture = self.check()
        self.assertEqual(apps['openclaw']['read_rpc'], True)
        self.assertEqual(apps['hermes']['authentication'], 'basic_session')
        self.assertNotIn(SECRET, json.dumps(apps)); self.assertNotIn('fake-handoff', json.dumps(apps))
        self.assertEqual(len(fixture.contexts), 2)
        for context in fixture.contexts:
            self.assertFalse(context.kwargs['ignore_https_errors'])
            self.assertEqual(context.kwargs['service_workers'], 'block')
            self.assertFalse(context.kwargs['accept_downloads'])
            self.assertNotIn('storage_state', context.kwargs)
            context.close.assert_called_once()
        fixture.browser.close.assert_called_once()
        self.assertEqual(fixture.launch_kwargs['args'], ['--no-proxy-server', '--host-resolver-rules=MAP ' + TARGET + ' ' + ADDRESS])
        self.assertNotIn('proxy', fixture.launch_kwargs)
        self.assertNotIn('HOME', fixture.launch_kwargs['env'])

    def test_blank_or_login_only_or_unpaired_app_cannot_pass_and_closes(self):
        for attr in ('ui_ready', 'hello_ready', 'heading_ready', 'login_ok'):
            fixture = BrowserFixture(); setattr(fixture, attr, False)
            with self.assertRaises(Exception): self.check(fixture)
            fixture.browser.close.assert_called_once()
            for context in fixture.contexts: context.close.assert_called_once()
        fixture = BrowserFixture(); fixture.blank = True
        with self.assertRaisesRegex(probe.ProbeError, 'hermes_dashboard_not_rendered'): self.check(fixture)

    def test_native_profile_query_after_login_still_requires_authenticated_rendered_sessions(self):
        for suffix in ('', '?profile=default', '?profile=team%20one&tab=recent'):
            with self.subTest(suffix=suffix):
                fixture = BrowserFixture()
                fixture.hermes_redirect = 'https://' + TARGET + ':8443/sessions' + suffix
                apps, fixture = self.check(fixture)
                self.assertEqual(apps['hermes']['authentication'], 'basic_session')
                self.assertTrue(apps['hermes']['rendered'])
                self.assertEqual(fixture.contexts[1].page.url, fixture.hermes_redirect)
                fixture.contexts[1].page.get_by_role.assert_called_once_with('heading', name='Sessions', exact=True)

    def test_profile_redirect_cannot_accept_another_origin_path_fragment_or_plaintext(self):
        origin = 'https://' + TARGET + ':8443'
        urls = ('http://' + TARGET + ':8443/sessions', 'https://' + TARGET + '/sessions',
                'https://outside.invalid/sessions', origin + '/sessions/other',
                origin + '/%73essions', origin + '/sessions#other',
                origin + '/sessions?profile=default#other',
                'https://outside.invalid/?next=' + origin + '/sessions')
        for url in urls:
            with self.subTest(url=url):
                fixture = BrowserFixture(); fixture.hermes_redirect = url
                diagnostic = {}
                with self.assertRaises(RuntimeError):
                    probe.browser_check(request(), peer(), fixture, diagnostic=diagnostic)
                self.assertEqual(diagnostic, {'phase': 'hermes_session_redirect'})
                fixture.browser.close.assert_called_once()

    def test_failure_phase_identifies_actual_failed_browser_step(self):
        for attr, phase in (('ui_ready', 'openclaw_session'),
                            ('login_ok', 'hermes_session_redirect'),
                            ('heading_ready', 'hermes_dashboard_render')):
            fixture = BrowserFixture(); setattr(fixture, attr, False)
            diagnostic = {}
            with self.assertRaises(RuntimeError):
                probe.browser_check(request(), peer(), fixture, diagnostic=diagnostic)
            self.assertEqual(diagnostic, {'phase': phase})
            self.assertNotIn(SECRET, json.dumps(diagnostic))
            fixture.browser.close.assert_called_once()

    def test_first_route_refusal_survives_subsequent_browser_timeout(self):
        fixture = BrowserFixture()
        original = fixture.goto
        def denied(context, index, url):
            reply = original(context, index, url)
            if index == 1:
                context.http(route('https://outside.invalid/?private=' + SECRET))
                context.websocket(FakeWS('wss://outside.invalid/'))
                context.page.wait_for_function.side_effect = RuntimeError(SECRET)
            return reply
        fixture.goto = denied
        diagnostic = {}
        with self.assertRaisesRegex(probe.ProbeError, '^browser_http_denied$') as caught:
            probe.browser_check(request(), peer(), fixture, diagnostic=diagnostic)
        self.assertEqual(diagnostic, {'phase': 'openclaw_session'})
        self.assertNotIn(SECRET, str(caught.exception))
        fixture.browser.close.assert_called_once()

    def test_read_response_can_arrive_after_the_connected_shell_renders(self):
        fixture = BrowserFixture(); fixture.hello_ready = False
        original = fixture.goto
        def delayed(context, index, url):
            reply = original(context, index, url)
            if index == 1:
                ws = FakeWS('wss://' + TARGET); context.websocket(ws)
                ws.emit({'type': 'req', 'id': 'c', 'method': 'connect', 'params': {'role': 'operator'}})
                ws.server.emit({'type': 'res', 'id': 'c', 'ok': True, 'payload': {'type': 'hello-ok',
                    'auth': {'role': 'operator', 'scopes': ['operator.read']}}})
                def complete(_):
                    ws.emit({'type': 'req', 'id': 'r', 'method': 'health'})
                    ws.server.emit({'type': 'res', 'id': 'r', 'ok': True, 'payload': {}})
                context.page.wait_for_timeout.side_effect = complete
            return reply
        fixture.goto = delayed
        self.check(fixture)
        self.assertEqual([call.args for call in fixture.contexts[0].page.wait_for_timeout.call_args_list],
                         [(100,), (5000,)])

    def test_rendered_connection_that_drops_during_startup_cannot_pass(self):
        fixture = BrowserFixture(); original = fixture.context
        def dropping(**kwargs):
            context = original(**kwargs)
            if len(fixture.contexts) == 1:
                context.page.wait_for_timeout.side_effect = lambda _ms: setattr(fixture, 'ui_ready', False)
            return context
        fixture.browser.new_context.side_effect = dropping
        diagnostic = {}
        with self.assertRaisesRegex(probe.ProbeError, '^openclaw_session_unverified$'):
            probe.browser_check(request(), peer(), fixture, diagnostic=diagnostic)
        self.assertEqual(diagnostic, {'phase': 'openclaw_settled_session'})
        fixture.browser.close.assert_called_once()

    def test_public_status_and_wrong_or_expired_cookie_cannot_pass(self):
        for changes in ({'provider': 'oauth'}, {'user_id': 'other'}, {'status': 401}, {'expires_at': 999}, {'expires_at': float('inf')}):
            fixture = BrowserFixture(); fixture.me.update(changes)
            with self.assertRaisesRegex(probe.ProbeError, 'hermes_session_unverified'): self.check(fixture)
        for changes in ({'auth_required': False}, {'auth_providers': []}, {'gateway_running': False}, {'status': 200, 'version': SECRET + '/bad'}):
            fixture = BrowserFixture(); fixture.status.update(changes)
            with self.assertRaisesRegex(probe.ProbeError, 'hermes_dashboard_unverified'): self.check(fixture)
        fixture = BrowserFixture(); fixture.anonymous = 200
        with self.assertRaisesRegex(probe.ProbeError, 'hermes_anonymous_access_not_denied'): self.check(fixture)

    def test_peer_proof_and_websocket_route_support_required(self):
        for changes in ({'runner_dns': TARGET}, {'traffic': 'online'}, {'address': '127.0.0.1'}, {'dns': 'unchecked'}):
            fixture = BrowserFixture(); raw = peer(); raw.update(changes)
            with self.assertRaises(probe.ProbeError): probe.browser_check(request(), raw, fixture)
            fixture.chromium.launch.assert_not_called()
        fixture = BrowserFixture()
        original = fixture.context
        def no_ws(**kwargs):
            context = original(**kwargs); context.route_web_socket = None; return context
        fixture.browser.new_context.side_effect = no_ws
        with self.assertRaisesRegex(probe.ProbeError, 'browser_websocket_routing_required'): self.check(fixture)
        fixture.contexts[0].close.assert_called_once(); fixture.browser.close.assert_called_once()

    def test_controller_rejects_stale_extra_unrendered_or_unverified_proof(self):
        apps, _ = self.check()
        raw = {'schema': 1, 'status': 'pass', 'request_id': ID, 'peer': peer(), 'apps': apps, 'duration_seconds': 1.2}
        checked = probe.validate_proof(raw, request()); self.assertEqual(len(checked['proof_sha256']), 64)
        for mutate in (lambda p: p.update(request_id='stale'), lambda p: p.update(secret=SECRET), lambda p: p.update(duration_seconds=150),
            lambda p: p.update(duration_seconds=float('nan')), lambda p: p['peer'].update(traffic='online'),
            lambda p: p['apps']['openclaw'].update(read_rpc=False), lambda p: p['apps']['hermes'].update(rendered=False),
            lambda p: p['apps']['hermes'].update(origin='http://' + TARGET), lambda p: p['apps']['openclaw'].update(secret=SECRET)):
            bad = copy.deepcopy(raw); mutate(bad)
            with self.assertRaises(probe.ProbeError): probe.validate_proof(bad, request())

    def test_run_report_bound_and_browser_exceptions_do_not_expose_secrets(self):
        fixture = BrowserFixture()
        factory = lambda: contextlib.nullcontext(fixture)
        execute = Mock(side_effect=[json.dumps(status()).encode(), b'pong from as7-fixture (100.100.2.3) via direct in 1ms'])
        resolver = Mock(return_value=[(2, 1, 6, '', (ADDRESS, 443))])
        report = probe.run_probe(request(), execute, resolver, factory, now=lambda: 1000)
        probe.validate_proof(report, request())
        self.assertNotIn(SECRET, json.dumps(report)); self.assertNotIn('fake-handoff', json.dumps(report))
        fixture = BrowserFixture(); fixture.chromium.launch.side_effect = RuntimeError(SECRET + request()['openclaw_url'])
        execute = Mock(side_effect=[json.dumps(status()).encode(), b'pong from as7-fixture (100.100.2.3) via direct in 1ms'])
        with self.assertRaisesRegex(probe.ProbeError, '^browser_probe_failed$'):
            probe.run_probe(request(), execute, resolver, lambda: contextlib.nullcontext(fixture))

    def test_main_error_output_only_fixed_status_and_alarm_is_disarmed(self):
        stdout = io.StringIO()
        stdin = Mock(buffer=io.BytesIO(json.dumps(request()).encode()))
        with patch.object(sys, 'stdin', stdin), contextlib.redirect_stdout(stdout), patch.object(probe, 'run_probe', side_effect=RuntimeError(SECRET)), patch.object(probe.signal, 'signal'), patch.object(probe.signal, 'alarm') as alarm:
            self.assertEqual(probe.main(), 1)
        self.assertEqual(json.loads(stdout.getvalue()), {'schema': 1, 'status': 'fail'})
        self.assertEqual(alarm.call_args_list[-1].args, (0,))


if __name__ == '__main__':
    unittest.main()
