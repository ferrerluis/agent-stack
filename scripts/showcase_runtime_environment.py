#!/usr/bin/env python3
"""Reversible Hermes environment staging for a stopped owned runtime.

Remove only inactive model placeholders; retain app authentication. Refuse
real or unknown provider credentials. The lifecycle owns the shared lock.
"""
import base64
import re
import stat

import showcase_runtime_routes as routes

INACTIVE = "as7-model-gateway-not-active"
APP_CREDENTIALS = {"API_SERVER_KEY", "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    "OPENCLAW_GATEWAY_TOKEN", "TAILSCALE_AUTH_KEY", "ADMIN_PASSWORD", "POSTGRES_PASSWORD"}
ROUTE_NAMES = {"OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_KEY", "ANTHROPIC_AUTH_TOKEN",
    "HERMES_ACP_AUTH_METHOD", "HERMES_ACP_AUTO_APPROVE", "HERMES_COPILOT_ACP_COMMAND", "HERMES_COPILOT_ACP_ARGS",
    "COPILOT_CLI_PATH", "COPILOT_ACP_BASE_URL", "CODEX_HOME", "HERMES_MANAGED_DIR", "OP_SERVICE_ACCOUNT_TOKEN"}
ROUTE_NAMES.update({"HOME", "HERMES_HOME", "PATH", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "HERMES_SECRETS_DIR", "HERMES_PROFILES_DIR", "XDG_CONFIG_HOME"})


def need(value):
    if not value: raise ValueError("runtime_environment_unverified")


def assignments(raw):
    need(isinstance(raw, bytes) and 0 < len(raw) <= routes.LIMIT and b"\x00" not in raw and b"\r" not in raw)
    text = raw.decode("utf-8"); need(text.endswith("\n"))
    result = {}; lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append((None, line)); continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]{0,79})=([^\n]*)\n", line)
        need(match and match[1] not in result)
        result[match[1]] = match[2]; lines.append((match[1], line))
    return result, lines


def model_setting(name):
    return name not in APP_CREDENTIALS and (name in ROUTE_NAMES
        or name.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_KEY", "_BASE_URL", "_AUTH_JSON", "_AUTH_FILE"))
        or name.startswith(("OPENAI_", "ANTHROPIC_", "AWS_", "AZURE_", "GOOGLE_", "GCP_")))


def compile_environment(raw, route):
    routes.route_valid(route); need(route["role"] == "hermes")
    values, lines = assignments(raw)
    for name, value in values.items():
        if model_setting(name):
            # Replace public endpoints, never real tokens, OAuth files,
            # shell expansion, cloud identities or alternate runtimes.
            endpoint = name.endswith("_BASE_URL") and bool(re.fullmatch(r"https://[A-Za-z0-9./:_-]{1,250}", value))
            stable_home = name in ("HOME", "HERMES_HOME") and value == "/opt/data"
            need(value in ("", INACTIVE) or endpoint or stable_home)
    kept = "".join(line for name, line in lines if name is None or not model_setting(name))
    base = route["base_url"] + ("/v1" if route["provider"] == "openai" else "")
    result = (kept + "OPENAI_BASE_URL=" + base + "\n").encode()
    selected_environment(result, route)
    return result


def selected_environment(raw, route):
    routes.route_valid(route); need(route["role"] == "hermes")
    values, _ = assignments(raw)
    selected = {name: value for name, value in values.items() if model_setting(name)}
    need(selected == {"OPENAI_BASE_URL": route["base_url"] + ("/v1" if route["provider"] == "openai" else "")})


class EnvironmentTransaction:
    """Shares the config journal/lock; its caller keeps Hermes stopped."""
    def __init__(self, transaction):
        self.tx = transaction
        self.target, self.record = transaction.root / "hermes/.env", transaction.journal / "hermes.env.json"

    def install(self, binding, route):
        with self.tx.lock(): return self._install(binding, route)

    def _install(self, binding, route):
        routes.binding_valid(binding)
        need(not self.record.exists() and not self.record.is_symlink())
        info = routes.checked_path(self.target, owner=routes.OWNERS["hermes"]); before = self.target.read_bytes()
        after = compile_environment(before, route)
        snapshot = {"schema": 1, "kind": "hermes_environment", "binding": binding,
            "original": base64.b64encode(before).decode(), "original_sha256": routes.sha(before),
            "installed_sha256": routes.sha(after), "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
        packet = routes.encoded(snapshot); need(len(packet) <= routes.LIMIT)
        routes.atomic_write(self.record, packet, 0, 0, 0o600, exclusive=True)
        need(routes.checked_path(self.target, owner=routes.OWNERS["hermes"]) == info and self.target.read_bytes() == before)
        routes.atomic_write(self.target, after, info.st_uid, info.st_gid, 0o600)
        need(self.target.read_bytes() == after)
        return routes.sha(after)

    def restore(self, binding):
        with self.tx.lock(): return self._restore(binding)

    def _restore(self, binding):
        routes.binding_valid(binding)
        info = routes.checked_path(self.record, owner=0); need(stat.S_IMODE(info.st_mode) == 0o600)
        snapshot = routes.decoded(self.record.read_bytes())
        need(set(snapshot) == {"schema", "kind", "binding", "original", "original_sha256", "installed_sha256", "uid", "gid", "mode"}
             and type(snapshot["schema"]) is int and snapshot["schema"] == 1 and snapshot["kind"] == "hermes_environment"
             and snapshot["binding"] == binding and type(snapshot["uid"]) is int and snapshot["uid"] == routes.OWNERS["hermes"]
             and type(snapshot["gid"]) is int and snapshot["gid"] == routes.OWNERS["hermes"]
             and type(snapshot["mode"]) is int and snapshot["mode"] in (0o600, 0o640))
        original = base64.b64decode(snapshot["original"], validate=True)
        need(0 < len(original) <= routes.LIMIT and routes.sha(original) == snapshot["original_sha256"]
             and isinstance(snapshot["installed_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", snapshot["installed_sha256"]))
        routes.checked_path(self.target, owner=routes.OWNERS["hermes"])
        need(routes.sha(self.target.read_bytes()) in (snapshot["original_sha256"], snapshot["installed_sha256"]))
        routes.atomic_write(self.target, original, snapshot["uid"], snapshot["gid"], snapshot["mode"])
        need(self.target.read_bytes() == original)
        self.record.unlink(); routes.fsync_dir(self.tx.journal)
        return routes.sha(original)
