"""Only test fixtures: cloud SSH/Docker/model replies are replaced, ledger is real."""
from contextlib import contextmanager
import copy
import hashlib
import json
import secrets

import showcase_model_session as session
import test_showcase_model_spend as terms
import showcase_model_completion as completion
import showcase_model_completion_fixtures as completions
import showcase_runtime_routes as routes
import showcase_runtime_probe as probe
from showcase_policy import require

CAPABILITY = "7" * 64
PRIVATE_ROUTE = "http://host.docker.internal:23456/"


class FixtureGateway:
    def __init__(self, spend, reader):
        self.spend, self.reader = spend, reader
        self.alive, self.observed, self.sealed = False, set(), set()
        self.started_roles = None
        self.diagnostic_roles = set()
    def gate_task_starts(self): self.started_roles = set()
    def authorize_role(self, role):
        require(self.alive and role not in self.started_roles, "model_role_already_used")
        require(role not in self.diagnostic_roles, "model_diagnostic_in_progress")
        self.started_roles.add(role)
    @contextmanager
    def diagnostic_window(self, role):
        require(self.alive and role not in self.started_roles and role not in self.diagnostic_roles, "model_diagnostic_refused")
        self.diagnostic_roles.add(role)
        try: yield
        except BaseException:
            self.seal_role(role)
            raise
        finally: self.diagnostic_roles.discard(role)
    def __enter__(self): self.alive = True; return self
    def healthy(self): return self.alive
    def client(self, role):
        require(role not in self.sealed and self.alive, "model_gateway_closed")
        return {"role": role, "provider": self.spend.policy["roles"][role]["provider"],
                "base_url": PRIVATE_ROUTE + role, "credential": CAPABILITY}
    def new_probe(self, role, kind="channel"):
        return {**self.client(role), "nonce": secrets.token_hex(32)}
    def probe_observed(self, role, nonce): return (role, nonce) in self.observed
    def seal_role(self, role): self.sealed.add(role)
    def close(self): self.alive = False


class FixtureTransport:
    def __init__(self, checks, gateway):
        self.checks, self.gateway, self.alive = checks, gateway, False
        self.ready = {"bridge_ipv4": "172.17.0.1", "bridge_port": 23456}
    def __enter__(self):
        evidence = self.checks.evidence()
        baseline = evidence.get("after_reboot") or evidence["baseline"]
        self.proof = {"schema": 1, "status": "private_channel_verified", "run_id": self.checks.run.name,
                      "source_revision": self.checks.attempt.prepared["source_revision"], "boot_id": baseline["boot_id"],
                      "checksum": baseline["checksum"], **self.ready, "relay_source_sha256": "a" * 64,
                      "probe_nonce": secrets.token_hex(32), "native_routes": "unverified"}
        self.alive = True; return self
    def healthy(self): return self.alive
    def client(self, role): return self.gateway.client(role)
    def close(self): self.alive = False


class FixtureSession(session.ModelSession):
    def __init__(self, checks):
        policy = terms.policy(checks.config, checks.provider)
        policy["source_revision"] = checks.attempt.prepared["source_revision"]
        super().__init__(checks, policy, gateway_factory=FixtureGateway, transport_factory=FixtureTransport)
        self.lifecycle_calls = []

    def runtime_lifecycle_command(self, target, packet, timeout):
        # Native VM/Docker/doctor/readiness replies only; the production
        # session, source/identity validators, ledger and sequencing are real.
        self.lifecycle_calls.append(copy.deepcopy(packet))
        role = packet.get("role") or packet["route"]["role"]
        if packet["action"] == "restore":
            require(not self.gateway.alive and not self.transport.alive, "fixture_restore_before_revoke")
            return {"schema": 1, "status": "runtime_config_restored", "role": role,
                    "binding": packet["binding"], "original_sha256": "c" * 64}
        route = packet["route"]; config = routes.compile_config({}, route)
        if role == "openclaw":
            native = probe.native_openclaw({"valid": True, "configRevisionHash": "b" * 64, "appliedConfigHash": "b" * 64,
                "config": config, "issues": [], "warnings": [], "legacyIssues": []}, route)
        else:
            actual = {"provider": "custom", "model": route["model"], "requested_provider": "as7-budget", "source": "custom_provider:as7-budget",
                      "api_mode": config["model"]["api_mode"], "base_url": config["model"]["base_url"], "api_key": route["credential"]}
            native = probe.native_hermes(config, route, lambda **_: actual)
        value = {"schema": 1, "status": "runtime_reload_verified", "role": role, "binding": packet["binding"],
            "container_id": hashlib.sha256(role.encode()).hexdigest(),
            "image_ref": "ghcr.io/openclaw/openclaw@" + self.checks.config["pins"]["openclaw_digest"] if role == "openclaw" else self.checks.config["pins"]["hermes_image"],
            "image_id": "sha256:" + "d" * 64, "config_sha256": "b" * 64, "native_route": native, "watchdog": "armed"}
        if role == "hermes": value["environment_sha256"] = "e" * 64
        if packet["action"] == "doctor":
            require(role in self.gateway.diagnostic_roles and role not in self.gateway.started_roles, "fixture_doctor_not_isolated")
            checks = {"service." + role: "pass", role + ".doctor": "pass"}
            if role == "hermes": checks.update({"hermes.config": "pass", "hermes.readiness": "pass"}); value["readiness_sha256"] = "a" * 64
            value.update(status="runtime_doctor_verified", uid=1000 if role == "openclaw" else 10000,
                         health="healthy", watchdog="disarmed", doctor_sha256="f" * 64, checks=checks)
        elif packet["action"] == "verify":
            value.update(status="runtime_route_verified", watchdog="disarmed")
            value["container_probe"] = {"schema": 1, "status": "runtime_container_route_verified", "role": role,
                "provider": route["provider"], "uid": 1000 if role == "openclaw" else 10000, "config_sha256": "b" * 64,
                "source_revision": packet["binding"]["source_revision"], "nonce": packet["nonce"]}
            if role == "hermes": value["container_probe"].update(environment_sha256="e" * 64, native_route=native)
            self.gateway.observed.add((role, packet["nonce"]))
        else: require(packet["action"] == "prepare", "invalid_fixture_action")
        return value

    def fixture_start(self):
        if self.state == "new": self.start("before_reboot")

    def runtime_client(self, role):
        self.fixture_start()
        self.active()
        return self.transport.client(role)

    def workspace_client(self, role):
        self.fixture_start()
        return super().workspace_client(role)

    def fake_response(self, role, body, answer=None, usage=None):
        # The fake SSH/model task admits a real durable hold, with no key
        # retrieval or network. Production never calls this test-only method.
        sha = hashlib.sha256(body).hexdigest()
        self.spend.reserve(role, sha, 60)
        row = self.policy["roles"][role]
        api = "messages" if row["provider"] == "anthropic" else "responses"
        value = completions.response(api, text=json.dumps(answer) if answer is not None else "fixture final answer",
                                     response_id="fixture_" + role + "_" + sha)
        value["model"] = row["model"]
        if usage is not None:
            if api == "messages":
                value["usage"] = {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                                  "cache_read_input_tokens": usage["cache_read_tokens"], "cache_creation_input_tokens": usage["cache_write_tokens"]}
            else:
                inp = usage["input_tokens"] + (usage["cache_read_tokens"] + usage["cache_write_tokens"] if role != "codex" else 0)
                value["usage"] = {"input_tokens": inp, "output_tokens": usage["output_tokens"], "total_tokens": inp + usage["output_tokens"],
                                  "input_tokens_details": {"cached_tokens": usage["cache_read_tokens"], "cache_write_tokens": usage["cache_write_tokens"]}}
        path = "/v1/messages" if api == "messages" else "/v1/responses"
        request = {"stream": False, "max_tokens" if api == "messages" else "max_output_tokens": row["output_tokens_upper_bound"]}
        proof = completion.completion(row["provider"], row["model"], path, "application/json", completions.encoded(value), request, row)
        require(proof is not None, "invalid_fixture_completion")
        self.spend.received(role, sha, proof)

    def fake_probe(self, packet):
        self.gateway.observed.add((packet["role"], packet["nonce"]))
        return {"schema": 1, "status": "workspace_route_verified", "role": packet["role"], "provider": packet["provider"],
                "uid": 1000, "bridge_ipv4": packet["bridge_ipv4"], "nonce": packet["nonce"],
                "source_revision": packet["source_revision"], "base_url_sha256": hashlib.sha256(packet["base_url"].encode()).hexdigest()}
