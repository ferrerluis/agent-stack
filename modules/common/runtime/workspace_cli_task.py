#!/usr/bin/env python3
"""Run one API-key coding task as UID 1000; secret arrives only through stdin."""
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

PROMPT = "Read input.json. Write result.json containing the integer sum of lhs and rhs as sum, and the unchanged nonce as nonce. Then report completion."


def verify_result(directory, challenge):
    directory = Path(directory)
    output = directory / "result.json"
    if not output.is_file() or output.is_symlink() or output.stat().st_nlink != 1 or output.stat().st_size > 4096:
        return False
    try:
        actual = json.loads(output.read_text())
        unchanged = json.loads((directory / "input.json").read_text()) == challenge
        return unchanged and isinstance(actual, dict) and type(actual.get("sum")) is int and actual["sum"] == challenge["lhs"] + challenge["rhs"] and actual.get("nonce") == challenge["nonce"]
    except (OSError, ValueError):
        return False


def model_usage(cli, output):
    try:
        if cli == "codex":
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            if any(not isinstance(event, dict) or event.get("type") in ("error", "turn.failed") for event in events):
                return None
            completed = [event for event in events if event.get("type") == "turn.completed"]
            if len(completed) != 1:
                return None
            usage = completed[0].get("usage")
        else:
            report = json.loads(output)
            if not isinstance(report, dict) or report.get("type") != "result" or report.get("subtype") != "success" or report.get("is_error") is not False or report.get("permission_denials"):
                return None
            usage = report.get("usage")
        if not isinstance(usage, dict):
            return None
        counts = {name: usage.get(name) for name in ("input_tokens", "output_tokens")}
        if any(type(value) is not int or value < 0 for value in counts.values()) or counts["output_tokens"] == 0:
            return None
        return counts
    except (ValueError, TypeError):
        return None


def command(cli, model):
    if cli == "codex":
        return ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write",
                "-c", 'approval_policy="never"', "--model", model, PROMPT]
    return ["claude", "-p", "--model", model, "--permission-mode", "dontAsk", "--tools", "Read,Write,Edit",
            "--allowedTools", "Read", "Write", "Edit", "--output-format", "json", "--no-session-persistence",
            "--max-turns", "6", "--max-budget-usd", "0.25", PROMPT]


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


def run_task(cli, model, api_key, timeout):
    failed = {"schema": 1, "cli": cli, "status": "fail"}
    if os.geteuid() != 1000:
        return {**failed, "reason": "workspace_user_required"}
    if not api_key or len(api_key) > 65536 or any(char.isspace() for char in api_key):
        return {**failed, "reason": "invalid_api_key"}
    home = Path(os.environ["HOME"])
    if home.stat().st_uid != 1000:
        return {**failed, "reason": "workspace_home_required"}
    root = home / ".agent-stack/acceptance"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix=cli + "-", dir=root))
    challenge = {"lhs": secrets.randbelow(1000000), "rhs": secrets.randbelow(1000000), "nonce": secrets.token_hex(16)}
    input_file = directory / "input.json"
    input_file.write_text(json.dumps(challenge))
    input_file.chmod(0o600)
    env = dict(os.environ)
    # Authentication for one CLI cannot accidentally fall back to another tool.
    for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(key, None)
    env["CODEX_API_KEY" if cli == "codex" else "ANTHROPIC_API_KEY"] = api_key
    env["DISABLE_AUTOUPDATER"] = "1"
    started = time.monotonic()
    code, output = invoke(command(cli, model), directory, env, timeout)
    usage = model_usage(cli, output) if code == 0 else None
    verified = verify_result(directory, challenge)
    passed = code == 0 and usage is not None and verified
    return {"schema": 1, "cli": cli, "status": "pass" if passed else "fail",
            "reason": "verified" if passed else ("timeout" if code == 124 else "execution_or_verification_failed"),
            "fixture": str(directory.relative_to(home)), "verified": verified,
            "model_usage": usage, "input_sha256": hashlib.sha256(input_file.read_bytes()).hexdigest(),
            "duration_seconds": round(time.monotonic() - started, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", choices=("codex", "claude"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,199}", args.model) or not 1 <= args.timeout <= 180:
        parser.error("select a model ID and a timeout between 1 and 180 seconds")
    os.umask(0o077)
    api_key = sys.stdin.read(65537).strip()
    try:
        result = run_task(args.cli, args.model, api_key, args.timeout)
    except (OSError, ValueError, KeyError):
        result = {"schema": 1, "cli": args.cli, "status": "fail", "reason": "task_unavailable"}
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
