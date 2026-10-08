import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_light as light
from showcase_policy import PolicyError
from test_showcase_policy import config


def snapshot(pins=None):
    pins = config()["pins"] if pins is None else pins
    refs = {"openclaw": "ghcr.io/openclaw/openclaw@" + pins["openclaw_digest"],
            "hermes": pins["hermes_image"], "workspace": "agent-stack-workspace:local"}
    return {"schema": 1, "status": "pass", "checksum": "a" * 64, "roles": {
        role: {"container_id": str(index) * 64, "image_id": "sha256:" + str(index) * 64,
               "image_ref": refs[role], "started_at": "2026-10-08T20:00:00.000Z", "restarts": 0,
               "running": True, "restarting": False, "dead": False, "oom": False, "exit_code": 0}
        for index, role in enumerate(light.SERVICES, 1)}}


def proof():
    return {"schema": 1, "status": "vm_and_containers_verified", "run_id": "as7-fixture01",
            "source_revision": "c" * 40, "scope": {"provider": "aws"},
            "target": {"vm_id": "i-owned", "checksum": "a" * 64},
            "samples": [{"elapsed_seconds": elapsed, "snapshot": snapshot()} for elapsed in range(0, 121, 20)]}


class LightProofTest(unittest.TestCase):
    def check(self, value):
        return light.validate(value, "as7-fixture01", "c" * 40, {"provider": "aws"}, config()["pins"])

    def test_two_minutes_of_identical_running_owned_services_pass(self):
        self.assertEqual(self.check(proof()), proof())

    def test_stopped_restarted_replaced_or_wrong_image_containers_cannot_pass(self):
        changes = ({"running": False}, {"restarting": True}, {"dead": True}, {"oom": True}, {"exit_code": 1},
                   {"restarts": 1}, {"container_id": "b" * 64}, {"image_id": "sha256:" + "b" * 64},
                   {"image_ref": "mutable:latest"}, {"started_at": "2026-10-08T20:01:00.000Z"}, {"running": 1})
        for role in light.SERVICES:
            for changeset in changes:
                value = proof(); value["samples"][-1]["snapshot"]["roles"][role].update(changeset)
                with self.subTest(role=role, changes=changeset), self.assertRaises(PolicyError):
                    self.check(value)

    def test_wrong_scope_checksum_missing_service_or_sparse_short_observations_refuse(self):
        for mutate in (lambda p: p.update(source_revision="d" * 40), lambda p: p.update(scope={"provider": "hetzner"}),
                       lambda p: p["target"].update(checksum="b" * 64),
                       lambda p: p["samples"][-1]["snapshot"]["roles"].pop("hermes"),
                       lambda p: p["samples"].pop(), lambda p: p.update(samples=[p["samples"][0], p["samples"][1], p["samples"][-1]]),
                       lambda p: p["samples"][1].update(elapsed_seconds=0),
                       lambda p: p["samples"][-1].update(elapsed_seconds=float("nan"))):
            value = proof(); mutate(value)
            with self.assertRaises(PolicyError): self.check(value)
