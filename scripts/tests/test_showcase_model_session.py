import copy
from http.client import HTTPConnection
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_model_container as container
import showcase_model_gateway as gateway
import showcase_model_session as session
import showcase_controller as controller
import showcase_cleanup as cleanup
from showcase_policy import PolicyError
import test_showcase_remote as remote_fixtures
import test_showcase_controller as flow_fixtures
import test_showcase_model_prices as price_fixtures
from showcase_policy import Reservations
import tempfile
from test_showcase_model_gateway import Response, body, PRIVATE
from showcase_model_session_fixtures import FixtureGateway, FixtureTransport, FixtureSession, CAPABILITY

ROOT = Path(__file__).resolve().parents[2]


class ContainerRouteTest(unittest.TestCase):
    def packet(self, role="codex"):
        return {"role": role, "provider": "openai" if role == "codex" else "anthropic",
                "base_url": "http://host.docker.internal:23456/" + role, "credential": CAPABILITY,
                "nonce": "c" * 64, "bridge_ipv4": "172.17.0.1", "source_revision": "a" * 40}

    def invoke(self, packet, raw=None, addresses=None, status=200, kind="application/json", uid=1000):
        expected = {"schema": 1, "role": packet["role"], "nonce": packet["nonce"], "source_revision": packet["source_revision"]}
        response = SimpleNamespace(status=status, getheader=lambda _: kind,
                                   read=Mock(return_value=json.dumps(expected).encode() if raw is None else raw))
        connection = Mock(); connection.getresponse.return_value = response
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.17.0.1", 23456))]
        with patch.object(container.os, "geteuid", return_value=uid), \
             patch.object(container.socket, "getaddrinfo", return_value=address if addresses is None else addresses), \
             patch.object(container, "HTTPConnection", return_value=connection) as factory:
            result = container.probe(packet)
        return result, connection, factory, response

    def test_exact_private_role_route_dns_uid_nonce_and_bounded_http_are_checked(self):
        for role in ("codex", "claude"):
            packet = self.packet(role); result, connection, factory, response = self.invoke(packet)
            self.assertEqual(result["status"], "workspace_route_verified")
            self.assertEqual(result["uid"], 1000)
            self.assertNotIn(CAPABILITY, json.dumps(result))
            factory.assert_called_once_with("172.17.0.1", 23456, timeout=3)
            args = connection.request.call_args.args
            self.assertEqual(args[:2], ("POST", "/" + role + "/_transport"))
            self.assertEqual(json.loads(args[2]), {"nonce": packet["nonce"]})
            self.assertEqual(args[3]["Authorization"], "Bearer " + CAPABILITY)
            response.read.assert_called_once_with(1025); connection.close.assert_called_once()

    def test_wrong_user_dns_public_address_role_url_and_private_bundle_are_refused_before_http(self):
        packet = self.packet()
        changes = [{"role": "openclaw"}, {"provider": "anthropic"}, {"credential": "fake-provider-key"}, {"nonce": "bad"},
                   {"source_revision": "b" * 64}, {"bridge_ipv4": "8.8.8.8"}, {"bridge_ipv4": "127.0.0.1"},
                   {"base_url": "http://127.0.0.1:23456/codex"}, {"base_url": "http://host.docker.internal:65536/codex"},
                   {"base_url": "http://host.docker.internal:23456/claude"}, {"base_url": packet["base_url"] + "?override=x"},
                   {"extra": CAPABILITY}]
        for changed in changes:
            with self.subTest(fields=list(changed)), patch.object(container.os, "geteuid", return_value=1000), \
                 patch.object(container, "HTTPConnection") as connection, self.assertRaises(ValueError):
                container.probe(packet | changed)
            connection.assert_not_called()
        for addresses in ([], [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 23456))],
                          [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 23456, 0, 0))]):
            with self.assertRaises(ValueError): self.invoke(packet, addresses=addresses)
        with self.assertRaises(ValueError): self.invoke(packet, uid=0)

    def test_redirect_bad_reply_duplicate_nonfinite_boolean_schema_or_oversized_body_never_passes(self):
        packet = self.packet()
        for raw in (b'[]', b'{"schema":1,"schema":1}', b'{"schema":NaN}', b'x' * 1025,
                    json.dumps({"schema": True, "role": "codex", "nonce": packet["nonce"], "source_revision": "a" * 40}).encode(),
                    json.dumps({"schema": 1, "role": "claude", "nonce": packet["nonce"], "source_revision": "a" * 40}).encode()):
            with self.assertRaises(ValueError): self.invoke(packet, raw=raw)
        for status in (201, 301, 401):
            with self.assertRaises(ValueError): self.invoke(packet, status=status)
        with self.assertRaises(ValueError): self.invoke(packet, kind="text/event-stream")
        for raw in (b'{}' * 3000, b'[]', b'{"nonce":"a","nonce":"b"}', b'{"nonce":Infinity}'):
            with self.assertRaises(ValueError): container.decoded(raw)

    def test_actual_checked_source_stdin_failure_is_bounded_and_redacted(self):
        source = (ROOT / session.PROBE).read_text()
        for packet in (b'{"secret":"' + CAPABILITY.encode() + b'"}', b'x' * 4097):
            result = subprocess.run([sys.executable, "-B", "-I", "-c", source], input=packet, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout), {"schema": 1, "status": "workspace_route_unverified"})
            self.assertNotIn(CAPABILITY.encode(), result.stdout + result.stderr)


class OriginalAccountSessionTest(unittest.TestCase):
    def test_measured_terms_conversion_keeps_the_original_plan_source_full_context_and_explicit_limits(self):
        terms = price_fixtures.TermsTest("runTest"); terms.setUp()
        receipt = terms.measure(); limits = terms.limits()
        with tempfile.TemporaryDirectory() as temp:
            ledger = Reservations(Path(temp) / "batch", terms.config)
            checks = SimpleNamespace(ledger=ledger, config=terms.config, provider="aws", now=lambda: price_fixtures.NOW,
                                     run=Path(temp) / "as7-fixture01", attempt=SimpleNamespace(
                                         prepared={"source_revision": "a" * 40}, record=lambda: {"plan_sha256": "b" * 64}))
            value = session.ModelSession.from_measured_terms(checks, receipt, limits)
            self.assertEqual(value.policy["roles"]["codex"]["context_tokens_upper_bound"], 1050000)
            self.assertEqual(value.policy["roles"]["codex"]["max_requests"], 2)
            self.assertEqual(value.state, "new"); self.assertIsNone(value.gateway)
            for changes in ({"plan_sha256": "c" * 64}, {"source_revision": "c" * 40}, {"complete_quote": True}):
                with self.assertRaises(PolicyError): session.ModelSession.from_measured_terms(checks, receipt | changes, limits)
            checks.now = lambda: price_fixtures.NOW + 301
            with self.assertRaisesRegex(PolicyError, "model_terms_measurement_expired"):
                session.ModelSession.from_measured_terms(checks, receipt, limits)

    def make(self, real_gateway=False):
        fixture = remote_fixtures.RemoteChecksTest("runTest"); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.checks.bootstrap()
        if real_gateway:
            fixture.reader = Mock(return_value=PRIVATE)
            fixture.opener = SimpleNamespace(open=Mock(return_value=Response()))
            def factory(spend, reader): return gateway.BudgetGateway(spend, fixture.reader, fixture.opener)
            fixture.models = session.ModelSession(fixture.checks, fixture.models.policy, factory, FixtureTransport)
            fixture.checks.models = None
        self.addCleanup(lambda: fixture.models.close() if fixture.models.state not in ("closed", "closed_unverified") else None)
        fixture.models.start("before_reboot")
        return fixture

    def request(self, fixture, role="codex"):
        bundle = fixture.models.gateway.client(role)
        path = "/v1/responses" if bundle["provider"] == "openai" else "/v1/messages"
        connection = HTTPConnection("127.0.0.1", fixture.models.gateway.server.server_port, timeout=3)
        try:
            connection.request("POST", "/" + role + path, body(path),
                               {"Content-Type": "application/json", "Authorization": "Bearer " + bundle["credential"]})
            response = connection.getresponse(); response.read(); return response.status
        finally: connection.close()

    def account(self, fixture):
        return cleanup.private_read(fixture.ledger.directory / "reservations.json")["attempts"][0]

    def test_real_loopback_dispatch_receipt_matches_original_quote_policy_and_retained_worst_case_hold(self):
        fixture = self.make(real_gateway=True)
        begin = fixture.models.begin_task("codex")
        self.assertEqual(self.request(fixture), 200)
        receipt = fixture.models.task_receipt("codex", begin)
        account = self.account(fixture)
        self.assertEqual(receipt["requests"], account["model_requests"])
        self.assertEqual(receipt["quote_sha256"], account["quote_sha256"])
        self.assertEqual(receipt["source_revision"], fixture.prepared["source_revision"])
        self.assertEqual(receipt["status"], "gateway_responses_received")
        self.assertEqual(account["model_usd"], "1.00")
        self.assertEqual(account["model_requests"][0]["held_usd"], "0.020640000")
        self.assertNotIn(PRIVATE, json.dumps(receipt)); fixture.reader.assert_called_once()
        with self.assertRaisesRegex(PolicyError, "model_gateway_closed"): fixture.models.gateway.client("codex")
        with self.assertRaisesRegex(PolicyError, "model_role_already_used"): fixture.models.begin_task("codex")
        self.assertTrue(fixture.models.gateway.client("claude")["credential"])

    def test_no_actual_request_cannot_validate_a_claimed_native_success(self):
        fixture = self.make(real_gateway=True); start = fixture.models.begin_task("codex")
        with self.assertRaisesRegex(PolicyError, "model_task_receipt_unverified"):
            fixture.models.task_receipt("codex", start)
        fixture.reader.assert_not_called(); fixture.opener.open.assert_not_called()
        with self.assertRaises(PolicyError): fixture.models.gateway.client("codex")
        self.assertEqual(self.account(fixture)["model_requests"], [])

    def test_uncertain_held_response_and_forged_start_cannot_pass_or_refund(self):
        fixture = self.make(); start = fixture.models.begin_task("codex")
        fixture.models.spend.reserve("codex", "b" * 64, 60)
        with self.assertRaisesRegex(PolicyError, "model_task_receipt_unverified"):
            fixture.models.task_receipt("codex", start)
        self.assertEqual(self.account(fixture)["model_requests"][0]["status"], "uncertain")
        self.assertEqual(self.account(fixture)["model_usd"], "1.00")
        fresh = self.make(); start = fresh.models.begin_task("codex")
        fresh.models.fake_response("codex", b"fixture-response")
        for changes in ({"role": "claude"}, {"started_at": start["started_at"] - 1}, {"boot_id": remote_fixtures.NEW_BOOT},
                        {"policy_sha256": "b" * 64}, {"extra": True}):
            with self.assertRaisesRegex(PolicyError, "model_task_receipt_unverified"):
                fresh.models.task_receipt("codex", start | changes)
        self.assertEqual(fresh.models.task_receipt("codex", start)["status"], "gateway_responses_received")
        with self.assertRaises(PolicyError): fresh.models.task_receipt("codex", start)

    def test_workspace_route_proof_is_independent_of_the_probe_echo_and_never_stores_capability(self):
        fixture = self.make()
        bundle = fixture.models.workspace_client("codex")
        self.assertEqual(bundle["credential"], CAPABILITY)
        record = cleanup.private_read(fixture.checks.path)
        self.assertEqual(record["model_channels"]["before_reboot"]["workspace_routes"]["codex"]["uid"], 1000)
        self.assertNotIn(CAPABILITY, json.dumps(record))
        with self.assertRaisesRegex(PolicyError, "workspace_route_already_invoked"): fixture.models.workspace_client("codex")
        fresh = self.make()
        fresh.models.gateway.probe_observed = lambda *args: False
        with self.assertRaisesRegex(PolicyError, "workspace_model_route_unverified"): fresh.models.workspace_client("codex")
        self.assertNotIn("workspace_routes", cleanup.private_read(fresh.checks.path)["model_channels"]["before_reboot"])

    def test_changed_native_route_claim_or_missing_budget_session_revokes_without_real_key_retrieval(self):
        fixture = self.make()
        fixture.task_mutation = lambda report: report["model_route"].update(base_url_sha256="a" * 64)
        fixture.session.secret = Mock(side_effect=AssertionError("real key must stay controller-side"))
        with self.assertRaisesRegex(PolicyError, "workspace_model_route_unverified"):
            fixture.checks.workspace_task("codex")
        record = cleanup.private_read(fixture.checks.path)
        self.assertEqual(record["tasks"]["codex"]["status"], "uncertain")
        self.assertEqual(record["model_channels"]["before_reboot"]["shutdown"], "verified")
        self.assertEqual(fixture.models.state, "closed"); fixture.session.secret.assert_not_called()
        fresh = self.make(); fresh.checks.models = None
        before = len(fresh.calls)
        with self.assertRaisesRegex(PolicyError, "model_session_required"): fresh.checks.workspace_task("codex")
        self.assertEqual(len(fresh.calls), before)

    def test_deadline_channel_scope_and_current_boot_changes_revoke_task_authority(self):
        for kind in ("transport", "boot", "scope", "deadline"):
            fixture = self.make()
            if kind == "transport": fixture.models.transport.alive = False
            elif kind == "boot":
                record = cleanup.private_read(fixture.checks.path); record["baseline"]["boot_id"] = remote_fixtures.NEW_BOOT
                cleanup.private_write(fixture.checks.path, record)
            elif kind == "scope": fixture.models.policy["source_revision"] = "0" * 40
            else: fixture.clock = self.account(fixture)["work_deadline"]
            with self.subTest(kind=kind), self.assertRaises(PolicyError): fixture.models.begin_task("codex")
            self.assertEqual(self.account(fixture)["model_requests"], [])

    def test_reboot_reopens_with_the_same_allocation_and_preserves_old_holds_without_model_replay(self):
        fixture = self.make()
        start = fixture.models.begin_task("codex"); fixture.models.fake_response("codex", b"before-reboot")
        fixture.models.task_receipt("codex", start)
        before = copy.deepcopy(self.account(fixture)); nonce = fixture.models.transport.proof["probe_nonce"]
        fixture.models.close()
        record = cleanup.private_read(fixture.checks.path)
        record["after_reboot"] = {**record["baseline"], "boot_id": remote_fixtures.NEW_BOOT}; record["checks"]["reboot"] = "pass"
        cleanup.private_write(fixture.checks.path, record)
        proof = fixture.models.start("after_reboot")
        self.assertEqual(proof["boot_id"], remote_fixtures.NEW_BOOT); self.assertNotEqual(proof["probe_nonce"], nonce)
        self.assertEqual(self.account(fixture), before)
        with self.assertRaisesRegex(PolicyError, "model_role_already_used"): fixture.models.begin_task("codex")
        with self.assertRaisesRegex(PolicyError, "model_session_already_invoked"): fixture.models.start("after_reboot")

    def test_original_allocation_is_checked_before_any_channel_or_secret_and_runtime_routes_remain_guarded(self):
        fixture = remote_fixtures.RemoteChecksTest("runTest"); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.checks.bootstrap()
        policy = copy.deepcopy(fixture.models.policy); policy["roles"]["codex"]["input_usd_per_million_upper_bound"] = "100"
        factory = Mock(); candidate = session.ModelSession(fixture.checks, policy, factory, FixtureTransport)
        with self.assertRaisesRegex(PolicyError, "model_bounds_exceed_reservation"): candidate.start("before_reboot")
        factory.assert_not_called()
        fresh = self.make()
        candidate = session.ModelSession(fresh.checks, fresh.models.policy, FixtureGateway, FixtureTransport)
        candidate.gateway, candidate.transport = fresh.models.gateway, fresh.models.transport
        candidate.stage, candidate.state = "before_reboot", "verified"
        with self.assertRaisesRegex(PolicyError, "runtime_model_route_required"): candidate.runtime_client("hermes")
        self.assertEqual(self.account(fresh)["model_requests"], [])

    def test_shutdown_tries_both_components_and_preserves_uncertainty_instead_of_reopening(self):
        fixture = self.make()
        fixture.models.transport.close = Mock(side_effect=RuntimeError(PRIVATE))
        closed = fixture.models.gateway.close = Mock()
        with self.assertRaisesRegex(PolicyError, "model_session_shutdown_unverified") as caught: fixture.models.close()
        self.assertNotIn(PRIVATE, str(caught.exception)); closed.assert_called_once()
        self.assertEqual(cleanup.private_read(fixture.checks.path)["model_channels"]["before_reboot"]["shutdown"], "uncertain")
        with self.assertRaisesRegex(PolicyError, "model_session_shutdown_unverified"): fixture.models.close()


class ControllerModelLifecycleTest(unittest.TestCase):
    def make(self):
        owner = flow_fixtures.FullRunTest("runTest"); self.addCleanup(owner.doCleanups)
        return owner.make()

    def test_missing_session_refuses_before_reservation_apply_or_secret_use(self):
        fixture = self.make(); fixture.controller.models = None
        with self.assertRaisesRegex(PolicyError, "measured_model_session_required"):
            fixture.controller.run_attempt(fixture.readiness, flow_fixtures.quote())
        self.assertEqual(fixture.tf_commands, []); self.assertIsNone(fixture.controller.reservation())

    def test_each_channel_start_close_and_reboot_reopen_failure_cleans_and_cannot_claim_cells(self):
        for kind in ("start", "close", "reopen"):
            fixture = self.make()
            if kind == "start": fixture.models.start = Mock(side_effect=RuntimeError(PRIVATE))
            elif kind == "close": fixture.models.transport_factory = lambda checks, gateway: SimpleNamespace(
                __enter__=Mock(side_effect=RuntimeError(PRIVATE)), close=Mock(side_effect=RuntimeError(PRIVATE)))
            else:
                original = fixture.models.start
                def start(stage):
                    if stage == "after_reboot": raise RuntimeError(PRIVATE)
                    return original(stage)
                fixture.models.start = start
            result = fixture.controller.run_attempt(fixture.readiness, flow_fixtures.quote())
            self.assertEqual(result["cleanup"], "verified"); self.assertEqual(result["status"], "fail")
            self.assertEqual(set(result["cells"].values()), {"unverified"}); self.assertNotIn(PRIVATE, json.dumps(result))

    def test_native_receipt_and_closed_channel_proofs_are_rechecked_against_the_actual_original_account(self):
        fixture = self.make(); result = fixture.controller.run_attempt(fixture.readiness, flow_fixtures.quote())
        self.assertEqual(result["status"], "pass")
        record = cleanup.private_read(fixture.controller.path); evidence = cleanup.private_read(fixture.checks.path)
        observed = record["acceptance"]["observed"]
        for change in (lambda value: value["codex"]["gateway_receipt"].update(quote_sha256="b" * 64),
                       lambda value: value["codex"]["gateway_receipt"].update(schema=True),
                       lambda value: value["codex"]["gateway_receipt"].update(extra="unverified"),
                       lambda value: value["hermes"]["gateway_receipt"]["requests"].clear(),
                       lambda value: value["claude"]["gateway_receipt"].update(boot_id=remote_fixtures.NEW_BOOT)):
            values = copy.deepcopy(observed); changed = copy.deepcopy(evidence); change(values)
            # Change the echo too; matching two client-controlled echoes still
            # cannot replace the authoritative original ledger.
            for role in ("codex", "claude"): changed["tasks"][role] = values[role]
            for role in ("openclaw", "hermes"): changed["agents"][role].update(values[role])
            with self.assertRaisesRegex(PolicyError, "model_task_receipt_unverified"):
                fixture.controller.verdict(values, changed)
        evidence["model_channels"]["after_reboot"]["shutdown"] = "uncertain"
        with self.assertRaisesRegex(PolicyError, "model_channel_proof_changed"): fixture.controller.verdict(observed, evidence)


if __name__ == "__main__": unittest.main()
