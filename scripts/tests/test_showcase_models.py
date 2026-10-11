import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_models as models
from showcase_policy import PolicyError
from test_showcase_policy import config, NOW, REF

SECRET = "fixture-private-model-key"


class FakeModels:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def retrieve(self, provider, model, key):
        self.calls.append((provider, model, key))
        return copy.deepcopy(self.responses.get((provider, model),
                             {"id": model, "object" if provider == "openai" else "type": "model"}))


class ModelChecksTest(unittest.TestCase):
    def check(self, value=None, http=None, reader=None, clock=None):
        return models.check_models(config() if value is None else value,
            Mock(return_value=SECRET) if reader is None else reader,
            FakeModels() if http is None else http, (lambda: NOW) if clock is None else clock)

    def test_four_roles_deduplicate_models_and_selected_fields_without_claiming_generation(self):
        http, reader = FakeModels(), Mock(return_value=SECRET)
        result = self.check(http=http, reader=reader)
        self.assertEqual(len(http.calls), 2)
        reader.assert_called_once_with(REF)
        self.assertEqual(set(result["roles"]), {"openclaw", "hermes", "codex", "claude"})
        self.assertEqual(result["generation"], "not_tested")
        self.assertEqual(result["launch_gate"], "unverified")
        self.assertIn("model_prices", result["not_verified"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_distinct_models_and_keys_use_correct_providers_and_never_retrieve_cloud_password(self):
        value = config()
        value["models"].update(openclaw="anthropic/runtime-a", hermes="openai/runtime-b", codex="coding-a", claude="coding-b")
        refs = {"openai_api_key": "op://vault/openai/key", "anthropic_api_key": "op://vault/anthropic/key"}
        value["secret_refs"].update(refs)
        http = FakeModels()
        reader = Mock(side_effect=lambda ref: "fixture-openai" if ref == refs["openai_api_key"] else "fixture-anthropic")
        result = self.check(value, http, reader)
        self.assertEqual(http.calls, [("anthropic", "runtime-a", "fixture-anthropic"), ("openai", "runtime-b", "fixture-openai"),
            ("openai", "coding-a", "fixture-openai"), ("anthropic", "coding-b", "fixture-anthropic")])
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(result["successful_read_queries"], 4)

    def test_unsupported_route_or_id_fails_before_secret_or_http(self):
        for bad in ("evil/model", "openai/../../credentials", "openai/model?secret", "openai/model\nheader", "openai/model/child", "openai/", "openai/" + "x" * 201):
            value = config(); value["models"]["openclaw"] = bad
            reader, http = Mock(return_value=SECRET), FakeModels()
            with self.subTest(bad=bad), self.assertRaises(PolicyError): self.check(value, http, reader)
            reader.assert_not_called(); self.assertEqual(http.calls, [])

    def test_plaintext_ref_or_bad_clock_or_lifetime_fails_before_secret(self):
        for field in ("reference", "seconds", "clock"):
            value = config(); clock = lambda: NOW
            if field == "reference": value["secret_refs"]["openai_api_key"] = SECRET
            if field == "seconds": value["attempt_seconds"] = True
            if field == "clock": clock = lambda: float("nan")
            reader = Mock(return_value=SECRET)
            with self.subTest(field=field), self.assertRaises(PolicyError): self.check(value, reader=reader, clock=clock)
            reader.assert_not_called()

    def test_wrong_or_alias_model_id_or_shape_fails_without_publishing_response(self):
        for response in ({"id": SECRET, "object": "model"}, {"id": "fixture-model", "object": "list"}, [],
                         {"id": "fixture-model", "object": "model", "shutdown_date": "2026-99-99"}):
            http = FakeModels(); http.responses[("openai", "fixture-model")] = response
            with self.subTest(response=response), self.assertRaises(PolicyError) as caught: self.check(http=http)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_shutdown_before_full_attempt_horizon_refused_and_later_date_allowed(self):
        http = FakeModels()
        date = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%d")
        http.responses[("openai", "fixture-model")] = {"id": "fixture-model", "object": "model", "shutdown_date": date}
        with self.assertRaisesRegex(PolicyError, "model_shuts_down_before_attempt_ends"): self.check(http=http)
        http.responses[("openai", "fixture-model")]["shutdown_date"] = "2099-01-01"
        self.assertEqual(self.check(http=http)["status"], "metadata_read_checks_passed")

    def test_expired_measurement_and_finish_horizon_rechecked(self):
        for final in (NOW + 301, NOW - 1, float("inf")):
            clock = iter((NOW, final))
            with self.subTest(final=final), self.assertRaisesRegex(PolicyError, "model_metadata_measurement_expired"):
                self.check(clock=lambda: next(clock))
        tomorrow = datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp()
        beginning = tomorrow - 14401
        http = FakeModels(); http.responses[("openai", "fixture-model")] = {
            "id": "fixture-model", "object": "model", "shutdown_date": "2026-10-06"}
        clock = iter((beginning, beginning + 2))
        with self.assertRaisesRegex(PolicyError, "model_shuts_down_before_attempt_ends"):
            self.check(http=http, clock=lambda: next(clock))

    def test_header_control_whitespace_nonascii_oversize_or_empty_secret_prevents_request(self):
        for key in ("", " key", "key\r\nheader", "kèy", "x" * 65537, None):
            http = FakeModels()
            with self.subTest(key_type=type(key).__name__), self.assertRaisesRegex(PolicyError, "invalid_model_key_field"):
                self.check(http=http, reader=Mock(return_value=key))
            self.assertEqual(http.calls, [])

    def test_unrelated_provider_body_fields_are_excluded(self):
        http = FakeModels()
        http.responses[("openai", "fixture-model")] = {"id": "fixture-model", "object": "model", "owned_by": SECRET,
            "limits": {"key": SECRET}, "capabilities": [SECRET]}
        result = self.check(http=http)
        self.assertEqual(set(result["roles"]["codex"]), {"provider", "model", "status"})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_cloud_session_reuses_only_selected_model_key_fields_in_memory(self):
        from showcase_terraform import CloudSession
        session = CloudSession.__new__(CloudSession)
        session.config = config()
        session.config["secret_refs"].update(openai_api_key="op://vault/openai/key", anthropic_api_key="op://vault/anthropic/key")
        session.secrets = {}; session.reader = Mock(return_value=SECRET)
        http = FakeModels()
        for _ in range(2): self.assertEqual(session.model_metadata(http)["launch_gate"], "unverified")
        self.assertEqual([call.args[0] for call in session.reader.call_args_list], ["op://vault/openai/key", "op://vault/anthropic/key"])
        self.assertEqual(len(http.calls), 4)


class TransportTest(unittest.TestCase):
    def transport(self, body=b'{"id":"fixture-model","object":"model"}', status=200):
        http = models.ModelHTTP()
        response = Mock(status=status)
        response.read.return_value = body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        http.opener.open = Mock(return_value=response)
        return http, response

    def test_get_fixed_origin_encoded_id_and_exact_auth_headers(self):
        for provider in ("openai", "anthropic"):
            http, response = self.transport()
            http.retrieve(provider, "ft:model:name", SECRET)
            request = http.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, models.ORIGINS[provider] + "/v1/models/ft%3Amodel%3Aname")
            self.assertEqual(request.get_method(), "GET"); self.assertIsNone(request.data)
            self.assertEqual(http.opener.open.call_args.kwargs, {"timeout": 20})
            headers = {key.lower(): value for key, value in request.header_items()}
            if provider == "openai":
                self.assertEqual(headers["authorization"], "Bearer " + SECRET)
                self.assertNotIn("x-api-key", headers)
            else:
                self.assertEqual(headers["x-api-key"], SECRET)
                self.assertEqual(headers["anthropic-version"], "2023-06-01")
                self.assertNotIn("authorization", headers)
            response.read.assert_called_once_with(models.MAX_RESPONSE + 1)

    def test_proxy_and_redirect_disabled(self):
        with patch.object(models, "build_opener", return_value=Mock()) as build:
            models.ModelHTTP()
            self.assertEqual(build.call_args.args[0].proxies, {})
            redirect = build.call_args.args[1]
            with self.assertRaisesRegex(PolicyError, "model_redirect_refused"):
                redirect.redirect_request(None, None, 302, SECRET, {}, "https://evil.test/" + SECRET)

    def test_http_errors_are_fixed_redacted_and_not_retried(self):
        for status, reason in ((401, "model_key_rejected"), (403, "model_metadata_access_denied"), (404, "model_not_found"),
                               (429, "model_metadata_rate_limited"), (500, "model_metadata_request_failed")):
            http, _ = self.transport()
            http.opener.open.side_effect = HTTPError("https://evil.test/" + SECRET, status, SECRET, {}, io.BytesIO(SECRET.encode()))
            with self.subTest(status=status), self.assertRaisesRegex(PolicyError, reason) as caught:
                http.retrieve("openai", "fixture-model", SECRET)
            self.assertNotIn(SECRET, str(caught.exception)); self.assertEqual(http.opener.open.call_count, 1)

    def test_malformed_oversize_duplicate_nonfinite_or_failed_network_response_redacted(self):
        for body in (b"not-json", b"[]", b'\xff', b'{"id":1,"id":2}', b'{"id":NaN}', b"x" * (models.MAX_RESPONSE + 1)):
            http, _ = self.transport(body)
            with self.subTest(body_length=len(body)), self.assertRaises(PolicyError): http.retrieve("openai", "fixture-model", SECRET)
        http, _ = self.transport(); http.opener.open.side_effect = URLError(SECRET)
        with self.assertRaisesRegex(PolicyError, "model_metadata_request_failed") as caught: http.retrieve("openai", "fixture-model", SECRET)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_bad_endpoint_id_or_key_fails_before_http(self):
        for provider, model, key in (("evil", "model", SECRET), ("openai", "https://evil.test", SECRET), ("openai", "model", SECRET + "\n")):
            http, _ = self.transport()
            with self.assertRaises(PolicyError): http.retrieve(provider, model, key)
            http.opener.open.assert_not_called()
