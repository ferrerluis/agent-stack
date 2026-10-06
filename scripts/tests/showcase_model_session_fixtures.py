"""Only test fixtures: cloud SSH/Docker/model replies are replaced, ledger is real."""
import copy
import hashlib
import json
import secrets

import showcase_model_session as session
import test_showcase_model_spend as terms
from showcase_policy import require

CAPABILITY = "7" * 64
PRIVATE_ROUTE = "http://host.docker.internal:23456/"


class FixtureGateway:
    def __init__(self, spend, reader):
        self.spend, self.reader = spend, reader
        self.alive, self.observed, self.sealed = False, set(), set()
    def __enter__(self): self.alive = True; return self
    def healthy(self): return self.alive
    def client(self, role):
        require(role not in self.sealed and self.alive, "model_gateway_closed")
        return {"role": role, "provider": self.spend.policy["roles"][role]["provider"],
                "base_url": PRIVATE_ROUTE + role, "credential": CAPABILITY}
    def new_probe(self, role, kind="channel"):
        return {**self.client(role), "nonce": hashlib.sha256((role + kind).encode()).hexdigest()}
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

    def fixture_start(self):
        if self.state == "new": self.start("before_reboot")

    def runtime_client(self, role):
        self.fixture_start()
        self.active()
        return self.transport.client(role)

    def workspace_client(self, role):
        self.fixture_start()
        return super().workspace_client(role)

    def fake_response(self, role, body):
        # The fake SSH/model task admits a real durable hold, with no key
        # retrieval or network. Production never calls this test-only method.
        sha = hashlib.sha256(body).hexdigest()
        self.spend.reserve(role, sha, 60)
        self.spend.received(role, sha)

    def fake_probe(self, packet):
        self.gateway.observed.add((packet["role"], packet["nonce"]))
        return {"schema": 1, "status": "workspace_route_verified", "role": packet["role"], "provider": packet["provider"],
                "uid": 1000, "bridge_ipv4": packet["bridge_ipv4"], "nonce": packet["nonce"],
                "source_revision": packet["source_revision"], "base_url_sha256": hashlib.sha256(packet["base_url"].encode()).hexdigest()}
