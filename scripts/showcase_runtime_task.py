#!/usr/bin/env python3
"""One checked Hermes Runs task inside the captured runtime container.

The app key stays in child memory. Provider keys never arrive here; the native
named route contains only a temporary gateway capability. No submit retries.
"""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import sys
import time

import showcase_agents as agents
import showcase_runtime_probe as probe
import showcase_runtime_routes as routes
import showcase_runtime_environment as environment


def need(value):
    if not value: raise ValueError("runtime_task_unverified")


def hermes(packet, now=time.monotonic, sleep=time.sleep, tools=None):
    need(isinstance(packet, dict) and set(packet) == {"route", "config_sha256", "environment_sha256", "source_revision", "challenge", "request_id"})
    route = packet["route"]; routes.route_valid(route)
    need(route["role"] == "hermes" and os.geteuid() == 10000 and isinstance(packet["request_id"], str)
         and re.fullmatch(agents.REQUEST, packet["request_id"]))
    agents.challenge_valid(packet["challenge"])
    native_packet = {k: packet[k] for k in ("route", "config_sha256", "environment_sha256", "source_revision")}
    native_packet.update(action="native", nonce="")

    def selected():
        native = probe.hermes_probe(native_packet)
        raw = Path("/opt/data/config.yaml").read_bytes()
        need(routes.sha(raw) == packet["config_sha256"])
        config = routes.decoded(raw)  # The checked transaction writes JSON.
        need(config.get("platform_toolsets", {}).get("api_server") == [] and config.get("mcp_servers") == {})
        resolver = tools
        if resolver is None:
            from hermes_cli.tools_config import _get_platform_tools
            resolver = _get_platform_tools
        # The native API uses this same resolver, including plugin/default MCP.
        need(resolver(config, "api_server") == set())
        return native["native_route"]

    native = selected()
    raw_environment = Path("/opt/data/.env").read_bytes()
    need(routes.sha(raw_environment) == packet["environment_sha256"])
    values, _ = environment.assignments(raw_environment)
    key = values.get("API_SERVER_KEY", os.environ.get("API_SERVER_KEY"))
    need(isinstance(key, str) and 0 < len(key) <= 4096 and not any(c.isspace() for c in key))
    deadline = now() + 165

    def request(method, path, body=None):
        need(re.fullmatch(r"/v1/(?:capabilities|runs(?:/run_[A-Za-z0-9_-]{1,100}(?:/stop)?)?)", path))
        remaining = deadline - now(); need(remaining > 0)
        connection = HTTPConnection("127.0.0.1", 8642, timeout=min(10, remaining))
        try:
            payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
            connection.request(method, path, body=payload, headers={"Authorization": "Bearer " + key,
                "Content-Type": "application/json", "Idempotency-Key": packet["request_id"]})
            response = connection.getresponse()
            kind = response.getheader("Content-Type")
            need(response.status == (202 if method == "POST" and path == "/v1/runs" else 200)
                 and isinstance(kind, str) and kind.split(";", 1)[0].strip() == "application/json")
            raw = response.read(agents.LIMIT + 1); need(now() < deadline)
            value = agents.strict_json(raw); need(isinstance(value, dict))
            return value
        finally: connection.close()

    capabilities = request("GET", "/v1/capabilities")
    need(capabilities.get("object") == "hermes.api_server.capabilities"
         and capabilities.get("auth") == {"type": "bearer", "required": True})
    features = capabilities.get("features"); need(isinstance(features, dict))
    need(all(features.get(k) is True for k in ("run_submission", "run_status", "run_stop"))
         and isinstance(features.get("runs_idempotency"), dict) and features["runs_idempotency"].get("supported") is True)
    created = request("POST", "/v1/runs", {"input": agents.prompt(packet["challenge"]), "provider": "as7-budget",
        "model": route["model"], "conversation_history": []})
    run_id = created.get("run_id")
    need(isinstance(run_id, str) and re.fullmatch(r"run_[A-Za-z0-9_-]{1,100}", run_id)
         and created.get("status") in ("queued", "started", "running") and created.get("replayed") is False)
    try:
        while now() < deadline:
            report = request("GET", "/v1/runs/" + run_id)
            need(report.get("run_id") == run_id)
            if report.get("status") in ("completed", "failed", "cancelled", "interrupted"):
                break
            need(report.get("status") in ("queued", "started", "running"))
            sleep(min(1, max(0, deadline - now())))
        else: raise ValueError("runtime_task_unverified")
        result = agents.parse_turn("hermes", report, route["provider"] + "/" + route["model"],
            packet["challenge"], packet["request_id"], run_id, native_provider="custom")
        need(selected() == native)
        return result
    except BaseException:
        # One best-effort cancellation, never another submission. The outer
        # captured-container watchdog covers timeout/lost POST/lost SSH too.
        try: request("POST", "/v1/runs/" + run_id + "/stop", {})
        except BaseException: pass
        raise


def main():
    try:
        raw = sys.stdin.buffer.read(4097); need(0 < len(raw) <= 4096)
        result = hermes(routes.decoded(raw))
    except BaseException as error:
        result = {"schema": 1, "status": "runtime_task_unverified", "diagnostics": probe.failure_summary(error)}
    print(json.dumps(result))
    return 1 if result["status"] == "runtime_task_unverified" else 0


if __name__ == "__main__": sys.exit(main())
