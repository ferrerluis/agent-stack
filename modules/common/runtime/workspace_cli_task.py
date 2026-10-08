#!/usr/bin/env python3
"""Run one coding task as UID 1000; private auth arrives only through stdin."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time

PROMPT = "Read input.json. Write result.json containing only the integer sum of lhs and rhs as sum, and the unchanged nonce as nonce. Then reply with only the same JSON object."


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError("duplicate_json_field")
            value[key] = item
        return value
    def constant(_):
        raise ValueError("invalid_json_number")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def challenge_valid(value):
    if (not isinstance(value, dict) or set(value) != {"lhs", "rhs", "nonce"}
            or any(type(value[key]) is not int or not 0 <= value[key] < 1000000 for key in ("lhs", "rhs"))
            or not isinstance(value["nonce"], str) or re.fullmatch(r"[a-f0-9]{32}", value["nonce"]) is None):
        raise ValueError("invalid_task_challenge")


def verify_result(directory, challenge):
    directory = Path(directory)
    output = directory / "result.json"
    if not output.is_file() or output.is_symlink() or output.stat().st_nlink != 1 or output.stat().st_size > 4096:
        return False
    try:
        actual = strict_json(output.read_text())
        unchanged = strict_json((directory / "input.json").read_text()) == challenge
        return unchanged and isinstance(actual, dict) and set(actual) == {"sum", "nonce"} and type(actual.get("sum")) is int and actual["sum"] == challenge["lhs"] + challenge["rhs"] and actual.get("nonce") == challenge["nonce"]
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def model_usage(cli, output):
    try:
        if cli not in ("codex", "claude") or not isinstance(output, str) or len(output.encode()) > 4 * 1024 * 1024:
            return None
        if cli == "codex":
            events = [strict_json(line) for line in output.splitlines() if line.strip()]
            if any(not isinstance(event, dict) or event.get("type") in ("error", "turn.failed") for event in events):
                return None
            completed = [event for event in events if event.get("type") == "turn.completed"]
            if len(completed) != 1:
                return None
            usage = completed[0].get("usage")
        else:
            report = strict_json(output)
            if not isinstance(report, dict) or report.get("type") != "result" or report.get("subtype") != "success" or report.get("is_error") is not False or report.get("permission_denials"):
                return None
            usage = report.get("usage")
        if not isinstance(usage, dict):
            return None
        counts = {name: usage.get(name) for name in ("input_tokens", "output_tokens")}
        counts["cache_read_tokens"] = usage.get("cached_input_tokens" if cli == "codex" else "cache_read_input_tokens", 0)
        counts["cache_write_tokens"] = usage.get("cache_write_input_tokens" if cli == "codex" else "cache_creation_input_tokens", 0)
        if any(type(value) is not int or not 0 <= value <= 1000000000 for value in counts.values()) or counts["output_tokens"] == 0:
            return None
        return counts
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def gateway_client(raw, cli):
    """Only the private, role-scoped local gateway bundle is accepted."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_gateway_field")
            value[key] = item
        return value
    if not isinstance(raw, str) or len(raw.encode()) > 4096 or cli not in ("codex", "claude"):
        raise ValueError("invalid_model_gateway")
    value = json.loads(raw, object_pairs_hook=unique)
    provider = "openai" if cli == "codex" else "anthropic"
    if (not isinstance(value, dict) or set(value) != {"role", "provider", "base_url", "credential"}
            or any(not isinstance(item, str) for item in value.values())
            or value["role"] != cli or value["provider"] != provider
            or not re.fullmatch(r"[a-f0-9]{64}", value["credential"])):
        raise ValueError("invalid_model_gateway")
    route = re.fullmatch(r"http://(?:127\.0\.0\.1|host\.docker\.internal):([1-9][0-9]{0,4})/" + cli, value["base_url"])
    if route is None or int(route[1]) > 65535:
        raise ValueError("invalid_model_gateway")
    return value


def command(cli, model, gateway=None):
    if cli == "codex":
        args = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write",
                "-c", 'approval_policy="never"', "--model", model]
        if gateway is not None:
            provider = "agent_stack_showcase_budget"
            settings = {"model_provider": provider, "web_search": "disabled",
                        "features.multi_agent": False, "features.goals": False, "features.shell_snapshot": False,
                        "model_providers." + provider: {
                            "name": "AgentStack showcase budget gateway", "base_url": gateway["base_url"] + "/v1",
                            "env_key": "AGENT_STACK_MODEL_CREDENTIAL", "wire_api": "responses", "requires_openai_auth": False,
                            "supports_websockets": False, "request_max_retries": 0, "stream_max_retries": 0,
                            "stream_idle_timeout_ms": 60000}}
            for key, value in settings.items():
                if isinstance(value, dict):
                    # TOML inline table; no credential is in any argument.
                    encoded = "{ " + ", ".join(name + " = " + json.dumps(item) for name, item in value.items()) + " }"
                else:
                    encoded = json.dumps(value)
                args.extend(["-c", key + "=" + encoded])
        return args + [PROMPT]
    args = ["claude", "-p", "--model", model, "--permission-mode", "dontAsk", "--tools", "Read,Write,Edit",
            "--allowedTools", "Read", "Write", "Edit", "--output-format", "json", "--no-session-persistence",
            "--max-turns", "6"]
    if gateway is None:
        args.extend(["--max-budget-usd", "0.25"])
    else:
        args.extend(["--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
    # --mcp-config and --allowedTools accept multiple values. End option
    # parsing explicitly so the task cannot become a config filename.
    return args + ["--", PROMPT]


def gateway_environment(cli, model, gateway, client_home):
    # A fresh client home and a small allowlist prevent saved auth, cloud
    # routing, proxies, injected headers or native preload variables leaking
    # into this single task. The credential is never written to config.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TERM") if key in os.environ}
    env.setdefault("PATH", os.defpath)
    env.update({"HOME": str(client_home), "XDG_CONFIG_HOME": str(client_home / "config"),
                "XDG_CACHE_HOME": str(client_home / "cache"), "XDG_DATA_HOME": str(client_home / "data"),
                "DISABLE_AUTOUPDATER": "1"})
    if cli == "codex":
        env.update({"CODEX_HOME": str(client_home / "codex"), "AGENT_STACK_MODEL_CREDENTIAL": gateway["credential"]})
    else:
        env.update({"CLAUDE_CONFIG_DIR": str(client_home / "claude"), "ANTHROPIC_BASE_URL": gateway["base_url"],
                    "ANTHROPIC_AUTH_TOKEN": gateway["credential"], "ANTHROPIC_MODEL": model,
                    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
                    "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"})
    return env


def invoke(command_args, directory, env, timeout):
    process = subprocess.Popen(command_args, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        # Stop descendants too; a timed-out parent must not leave model work running.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        return 124, ""


def run_task(cli, model, api_key, timeout, model_gateway=False, challenge=None):
    failed = {"schema": 1, "cli": cli, "status": "fail"}
    if os.geteuid() != 1000:
        return {**failed, "reason": "workspace_user_required"}
    if challenge is not None:
        try:
            challenge_valid(challenge)
        except (ValueError, TypeError):
            return {**failed, "reason": "invalid_task_challenge"}
    gateway = None
    if model_gateway:
        try:
            gateway = gateway_client(api_key, cli)
        except (ValueError, TypeError, RecursionError):
            return {**failed, "reason": "invalid_model_gateway"}
    elif not api_key or len(api_key) > 65536 or any(char.isspace() for char in api_key):
        return {**failed, "reason": "invalid_api_key"}
    home = Path(os.environ["HOME"])
    if home.stat().st_uid != 1000:
        return {**failed, "reason": "workspace_home_required"}
    root = home / ".agent-stack/acceptance"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix=cli + "-", dir=root))
    if challenge is None:
        challenge = {"lhs": secrets.randbelow(1000000), "rhs": secrets.randbelow(1000000), "nonce": secrets.token_hex(16)}
    input_file = directory / "input.json"
    input_file.write_text(json.dumps(challenge, sort_keys=True, separators=(",", ":")))
    input_file.chmod(0o600)
    env = dict(os.environ)
    # Authentication for one CLI cannot accidentally fall back to another tool.
    for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(key, None)
    started = time.monotonic()
    if gateway is None:
        env["CODEX_API_KEY" if cli == "codex" else "ANTHROPIC_API_KEY"] = api_key
        env["DISABLE_AUTOUPDATER"] = "1"
        code, output = invoke(command(cli, model), directory, env, timeout)
    else:
        with tempfile.TemporaryDirectory(prefix="client-", dir=directory) as client_temp:
            # The pinned Codex CLI requires CODEX_HOME to exist before startup.
            # This task owns the fresh directory; no saved auth is copied.
            if cli == "codex":
                (Path(client_temp) / "codex").mkdir(mode=0o700)
            code, output = invoke(command(cli, model, gateway), directory,
                                  gateway_environment(cli, model, gateway, Path(client_temp)), timeout)
    usage = model_usage(cli, output) if code == 0 else None
    verified = verify_result(directory, challenge)
    passed = code == 0 and usage is not None and verified
    result = {"schema": 1, "cli": cli, "status": "pass" if passed else "fail",
            "reason": "verified" if passed else ("timeout" if code == 124 else "execution_or_verification_failed"),
            "fixture": str(directory.relative_to(home)), "verified": verified,
            "model_usage": usage, "input_sha256": hashlib.sha256(input_file.read_bytes()).hexdigest(),
            "duration_seconds": round(time.monotonic() - started, 2)}
    if gateway is not None:
        # This records requested client routing, not proof of billable model
        # success. The controller must independently match gateway receipts.
        result["model_route"] = {"kind": "budget_gateway", "role": cli, "provider": gateway["provider"],
                                 "base_url_sha256": hashlib.sha256(gateway["base_url"].encode()).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", choices=("codex", "claude"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--model-gateway", action="store_true", help="read a private local role bundle from stdin instead of an API key")
    parser.add_argument("--challenge", help="fresh, public controller-issued task JSON")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,199}", args.model) or not 1 <= args.timeout <= 180:
        parser.error("select a model ID and a timeout between 1 and 180 seconds")
    os.umask(0o077)
    api_key = sys.stdin.read(65537).strip()
    try:
        challenge = strict_json(args.challenge) if args.challenge is not None and len(args.challenge) <= 4096 else None
        if args.challenge is not None and challenge is None: raise ValueError("invalid_task_challenge")
        result = run_task(args.cli, args.model, api_key, args.timeout, model_gateway=args.model_gateway, challenge=challenge)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        result = {"schema": 1, "cli": args.cli, "status": "fail", "reason": "task_unavailable"}
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
