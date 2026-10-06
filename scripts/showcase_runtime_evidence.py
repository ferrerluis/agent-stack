"""Strict public runtime task receipts; no credentials or raw responses saved."""
import copy
import re

import showcase_agents as agents
import showcase_runtime_routes as routes
from showcase_runtime_probe import native_proof_valid

IDENTITY = {"binding", "role", "container_id", "image_ref", "image_id", "config_sha256", "native_route"}
TURN = {"status", "model", "request_id", "transport", "model_usage", "duration_seconds", "answer_sha256"}
RAW_TURN = {"schema", "runtime", "status", "request_id", "served_model", "transport", "answer", "model_usage", "watchdog", "duration_seconds"}


def need(value):
    if not value: raise ValueError("runtime_task_unverified")


def identity(ready, doctor, route, binding, image):
    routes.route_valid(route); routes.binding_valid(binding); role = route["role"]
    keys = IDENTITY | ({"environment_sha256"} if role == "hermes" else set())
    need(isinstance(ready, dict) and set(ready) == keys | {"schema", "status", "container_probe", "watchdog"}
         and type(ready.get("schema")) is int and ready["schema"] == 1 and ready.get("status") == "runtime_route_verified"
         and ready.get("role") == role and ready.get("binding") == binding and ready.get("image_ref") == image
         and ready.get("watchdog") == "disarmed")
    need(isinstance(ready.get("container_id"), str) and re.fullmatch(r"[a-f0-9]{64}", ready["container_id"])
         and isinstance(ready.get("image_id"), str) and re.fullmatch(r"sha256:[a-f0-9]{64}", ready["image_id"]))
    native_proof_valid(route, ready["native_route"])
    selected = {key: ready[key] for key in keys}
    required = {"service." + role: "pass", role + ".doctor": "pass"}
    if role == "hermes": required.update({"hermes.config": "pass", "hermes.readiness": "pass"})
    need(isinstance(doctor, dict) and set(doctor) == keys | {"schema", "status", "uid", "health", "watchdog", "doctor_sha256", "checks"}
         | ({"readiness_sha256"} if role == "hermes" else set())
         and all(doctor.get(k) == v for k, v in selected.items())
         and type(doctor.get("schema")) is int and doctor["schema"] == 1 and doctor.get("status") == "runtime_doctor_verified"
         and type(doctor.get("uid")) is int and doctor["uid"] == routes.OWNERS[role]
         and doctor.get("health") == "healthy" and doctor.get("watchdog") == "disarmed" and doctor.get("checks") == required)
    for key in ("config_sha256", "doctor_sha256") + (("environment_sha256", "readiness_sha256") if role == "hermes" else ()):
        need(isinstance(doctor.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", doctor[key]))
    container = ready["container_probe"]
    expected = {"schema": 1, "status": "runtime_container_route_verified", "role": role, "provider": route["provider"],
                "uid": routes.OWNERS[role], "config_sha256": doctor["config_sha256"], "source_revision": binding["source_revision"]}
    if role == "hermes": expected.update(environment_sha256=doctor["environment_sha256"], native_route=doctor["native_route"])
    need(isinstance(container, dict) and set(container) == set(expected) | {"nonce"}
         and type(container.get("schema")) is int and type(container.get("uid")) is int
         and all(container.get(k) == v for k, v in expected.items())
         and isinstance(container.get("nonce"), str) and re.fullmatch(r"[a-f0-9]{64}", container["nonce"]))
    return {**selected, "doctor_sha256": doctor["doctor_sha256"], "uid": routes.OWNERS[role], "health": "healthy", "watchdog": "disarmed"}


def validate(report, ready, doctor, route, binding, image, challenge, request_id, stored=False):
    agents.challenge_valid(challenge)
    need(isinstance(request_id, str) and re.fullmatch(agents.REQUEST, request_id))
    expected = identity(ready, doctor, route, binding, image)
    expected.update(schema=1, status="runtime_task_verified", request_id=request_id,
                    challenge_sha256=routes.sha(routes.encoded(challenge)))
    need(isinstance(report, dict) and set(report) == set(expected) | {"turn"}
         and type(report.get("schema")) is int and type(report.get("uid")) is int
         and all(report.get(k) == v for k, v in expected.items()))
    role, model = route["role"], route["provider"] + "/" + route["model"]
    turn = report["turn"]
    if stored:
        need(isinstance(turn, dict) and set(turn) == TURN)
        raw = {"schema": 1, "runtime": role, "status": turn.get("status"), "request_id": turn.get("request_id"),
               "served_model": turn.get("model"), "transport": turn.get("transport"), "watchdog": "disarmed",
               "answer": {"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]},
               "model_usage": turn.get("model_usage"), "duration_seconds": turn.get("duration_seconds")}
        checked = agents.validate_proof(raw, role, model, challenge, request_id)
        need(checked == turn)
    else:
        need(isinstance(turn, dict) and set(turn) == RAW_TURN)
        checked = agents.validate_proof(turn, role, model, challenge, request_id)
        need(turn["model_usage"] == checked["model_usage"])
    return {**copy.deepcopy(expected), "turn": checked}
