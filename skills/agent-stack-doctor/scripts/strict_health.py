#!/usr/bin/env python3
"""Strict remote health checks with a sanitized JSON result and failing exit code."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

SERVICES = {"openclaw", "hermes", "workspace"}
COMPOSE = ["docker", "compose", "-f", "/opt/agent-stack/docker-compose.yml"]
HERMES_CONFIG_PROBE = """import json,os,stat,yaml
from pathlib import Path
p=Path('/opt/data/config.yaml')
e=p.with_name('.env')
c=yaml.safe_load(p.read_text())
m=c.get('model',{})
keys={'anthropic':'ANTHROPIC_API_KEY','custom':'OPENAI_API_KEY','openai':'OPENAI_API_KEY','gemini':'GEMINI_API_KEY','groq':'GROQ_API_KEY'}
s=p.stat()
config_private=s.st_uid==os.geteuid() and s.st_gid==os.getegid() and stat.S_IMODE(s.st_mode) in (0o600,0o640)
t=e.stat()
env_private=t.st_uid==os.geteuid() and stat.S_IMODE(t.st_mode)==0o600
print(json.dumps({'ok':bool(m.get('default') and m.get('provider') and (os.environ.get(keys.get(m.get('provider'),'')) or m.get('api_key'))) and config_private and env_private}))
"""


def run(command, timeout=30):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def parse_json(output):
    try:
        return json.loads(output)
    except (ValueError, TypeError):
        return None


def required_checks(services, require_claude=False, tailscale=False):
    required = {"service." + service for service in services}
    if "openclaw" in services:
        required.add("openclaw.doctor")
    if "hermes" in services:
        required.update(("hermes.config", "hermes.readiness", "hermes.doctor"))
    if "workspace" in services:
        required.update(("workspace.uid", "workspace.codex"))
        if require_claude:
            required.add("workspace.claude")
    if tailscale:
        required.update(("tailscale.online", "tailscale.routes"))
    return required


def accepted(report, required):
    if not isinstance(report, dict) or report.get("schema") != 1:
        return False
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    by_name = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            return False
        if check["name"] in by_name or check.get("status") not in ("pass", "fail"):
            return False
        by_name[check["name"]] = check["status"]
    return required <= by_name.keys() and all(status == "pass" for status in by_name.values())


def doctor_healthy(report):
    if not isinstance(report, dict) or report.get("schemaVersion") != 1 or report.get("ok") is not True:
        return False
    count = report.get("checksRun")
    skipped = report.get("checksSkipped")
    findings = report.get("findings")
    warnings = report.get("warnings", [])
    if type(skipped) is not int or skipped != 0 or not isinstance(warnings, list) or any(
        not isinstance(item, dict) or item.get("errorCode") == "OPENCLAW_STATE_LEASE_ABORTED" for item in warnings
    ):
        return False
    return type(count) is int and count > 0 and isinstance(findings, list) and all(
        isinstance(item, dict) and item.get("severity") in ("info", "notice") for item in findings
    )


def hermes_doctor_healthy(output):
    # The native CLI has no JSON flag. Its return code plus the completed
    # summary must establish that static checks ran and found no issues.
    # Optional, unconfigured OAuth/tools may print advisory warnings; Hermes
    # records required problems in the issue list and failing summary.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).replace("\r", "\n")
    sections = ("Python Environment", "Required Packages", "Configuration Files", "Directory Structure", "API Connectivity")
    return (all("◆ " + section in text for section in sections)
            and text.count("✓") >= 5
            and len(re.findall(r"All checks passed!", text)) == 1
            and not re.search(r"Found [0-9]+ issue|require manual intervention|Traceback|✗|could not check|couldn't check", text, re.I))


def routes_healthy(serve, dns_name, services):
    if not isinstance(serve, dict) or not dns_name:
        return False
    tcp, web = serve.get("TCP"), serve.get("Web")
    if not isinstance(tcp, dict) or not isinstance(web, dict):
        return False
    for service, port, backend in (("openclaw", "443", "18789"), ("hermes", "8443", "9119")):
        if service not in services:
            continue
        listener = tcp.get(port)
        site = web.get(dns_name.rstrip(".") + ":" + port)
        if not isinstance(listener, dict) or listener.get("HTTPS") is not True or not isinstance(site, dict):
            return False
        handlers = site.get("Handlers")
        handler = handlers.get("/") if isinstance(handlers, dict) else None
        if not isinstance(handler, dict) or handler.get("Proxy") != "http://127.0.0.1:" + backend:
            return False
    # Serve must remain private to the tailnet.
    funnel = serve.get("AllowFunnel", {})
    return isinstance(funnel, dict) and not any(funnel.values())


def remote_report(services, require_claude, check_tailscale, tailscale_mode):
    checks = []

    def record(name, passed):
        checks.append({"name": name, "status": "pass" if passed else "fail"})

    for service in services:
        code, output = run(COMPOSE + ["ps", "--all", "-q", service])
        container = output.strip() if code == 0 else ""
        code, output = run(["docker", "inspect", container]) if container else (1, "")
        inspection = parse_json(output)
        state = inspection[0].get("State", {}) if isinstance(inspection, list) and len(inspection) == 1 and isinstance(inspection[0], dict) else {}
        state = state if isinstance(state, dict) else {}
        health = state.get("Health")
        record("service." + service, code == 0 and state.get("Running") is True and isinstance(health, dict) and health.get("Status") == "healthy")
        if service == "openclaw":
            code, output = run(["docker", "exec", container, "openclaw", "doctor", "--lint", "--json", "--severity-min", "warning"]) if container else (1, "")
            report = parse_json(output)
            record("openclaw.doctor", code == 0 and doctor_healthy(report))
        elif service == "hermes":
            python = "/opt/hermes/.venv/bin/python"
            code, output = run(["docker", "exec", "--user", "10000:10000", container, python, "-c", HERMES_CONFIG_PROBE]) if container else (1, "")
            report = parse_json(output)
            record("hermes.config", code == 0 and isinstance(report, dict) and report.get("ok") is True)
            code, output = run(["docker", "exec", "--user", "10000:10000", container, python, "/usr/local/libexec/agent-stack-hermes-health.py"]) if container else (1, "")
            report = parse_json(output)
            record("hermes.readiness", code == 0 and isinstance(report, dict) and report.get("status") == "pass")
            code, output = run(["docker", "exec", "--user", "10000:10000", "-e", "NO_COLOR=1",
                                container, "/opt/hermes/.venv/bin/hermes", "doctor"], timeout=90) if container else (1, "")
            record("hermes.doctor", code == 0 and hermes_doctor_healthy(output))
        elif service == "workspace":
            prefix = ["docker", "exec", "--user", "1000", container]
            code, output = run(prefix + ["id", "-u"]) if container else (1, "")
            record("workspace.uid", code == 0 and output.strip() == "1000")
            for cli in ("codex", "claude") if require_claude else ("codex",):
                # The entrypoint supplies the configured HOME/PATH to SSH shells.
                probe = 'user=$(getent passwd 1000 | cut -d: -f1); export HOME="/home/$user"; export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"; ' + cli + ' --version'
                code, output = run(prefix + ["sh", "-c", probe]) if container else (1, "")
                record("workspace." + cli, code == 0 and bool(output.strip()))
    if check_tailscale:
        mode = tailscale_mode
        if mode == "auto":
            try:
                env = Path("/opt/agent-stack/.env").read_text().splitlines()
                mode = next(line.split("=", 1)[1] for line in env if line.startswith("TAILSCALE_MODE="))
            except (OSError, StopIteration):
                mode = "unknown"
        prefix = ["tailscale"] if mode == "host" else []
        if mode == "sidecar":
            code, output = run(COMPOSE + ["ps", "-q", "tailscale"])
            if code == 0 and output.strip():
                prefix = ["docker", "exec", output.strip(), "tailscale", "--socket=/tmp/tailscaled.sock"]
        code, output = run(prefix + ["status", "--json"]) if prefix else (1, "")
        status = parse_json(output)
        self_status = status.get("Self") if isinstance(status, dict) else None
        dns_name = self_status.get("DNSName", "") if isinstance(self_status, dict) else ""
        record("tailscale.online", code == 0 and isinstance(self_status, dict) and self_status.get("Online") is True
               and isinstance(dns_name, str) and bool(dns_name) and not status.get("Health"))
        code, output = run(prefix + ["serve", "status", "--json"]) if prefix else (1, "")
        serve = parse_json(output)
        record("tailscale.routes", code == 0 and isinstance(dns_name, str) and routes_healthy(serve, dns_name, services))
    return {"schema": 1, "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ssh-wrapper", default=str(Path(__file__).resolve().parents[3] / "bin/agent-stack-ssh"))
    parser.add_argument("--services", default="openclaw,hermes")
    parser.add_argument("--require-claude", action="store_true")
    parser.add_argument("--check-tailscale", action="store_true")
    parser.add_argument("--tailscale-mode", choices=("auto", "host", "sidecar"), default="auto")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    services = args.services.split(",")
    if not services or len(set(services)) != len(services) or set(services) - SERVICES or args.timeout <= 0:
        parser.error("select distinct supported services and a positive timeout")
    if args.remote:
        report = remote_report(services, args.require_claude, args.check_tailscale, args.tailscale_mode)
        code = 0
    else:
        remote_args = ["sudo", "python3", "-", "--remote", "--services", args.services, "--tailscale-mode", args.tailscale_mode]
        if args.require_claude:
            remote_args.append("--require-claude")
        if args.check_tailscale:
            remote_args.append("--check-tailscale")
        try:
            result = subprocess.run([args.ssh_wrapper, "--", shlex.join(remote_args)], input=Path(__file__).read_text(), capture_output=True, text=True, timeout=args.timeout)
            report = parse_json(result.stdout)
            code = result.returncode
        except (OSError, subprocess.TimeoutExpired):
            report, code = None, 1
    required = required_checks(services, args.require_claude, args.check_tailscale)
    passed = code == 0 and accepted(report, required)
    # Emit only trusted names/statuses, even when a remote error contains secrets.
    checks = report.get("checks", []) if isinstance(report, dict) else []
    safe = [{"name": item["name"], "status": item["status"]} for item in checks if isinstance(item, dict) and item.get("name") in required and item.get("status") in ("pass", "fail")]
    result = {"schema": 1, "status": "pass" if passed else "fail", "checks": safe}
    if args.json or args.remote:
        print(json.dumps(result))
    else:
        print("AgentStack strict health: " + result["status"])
        for item in safe:
            print(item["name"] + ": " + item["status"])
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
