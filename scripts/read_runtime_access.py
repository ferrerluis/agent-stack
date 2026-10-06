#!/usr/bin/env python3
"""Terraform external data: collect only non-secret, observed tailnet access."""
import json
from pathlib import Path
import re
import subprocess
import sys


def collect(query):
    unavailable = {"tailscale_dns": "", "status": "unavailable"}
    host, user = query.get("host", ""), query.get("user", "")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host) or host.startswith("-"):
        return unavailable
    root = Path(query["repo_root"]).resolve()
    try:
        result = subprocess.run([
            str(root / "bin/agent-stack-ssh"), "--batch", "--known-hosts", str(root / ".ssh/known_hosts"),
            "--key", str(root / query["key_path"]), "--host", user + "@" + host, "--",
            "cat /opt/agent-stack/.access.json",
        ], capture_output=True, text=True, timeout=25)
        access = json.loads(result.stdout)
        dns = access.get("tailscale_dns", "") if isinstance(access, dict) else ""
        if result.returncode == 0 and isinstance(access, dict) and access.get("schema") == 1 and isinstance(dns, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.ts\.net", dns):
            return {"tailscale_dns": dns, "status": "observed"}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        pass
    # An unavailable host must not block Terraform destroy. Strict acceptance
    # separately requires these URLs and validates the live routes from a peer.
    return unavailable


def main():
    try:
        query = json.load(sys.stdin)
        result = collect(query) if isinstance(query, dict) else {"tailscale_dns": "", "status": "unavailable"}
    except (ValueError, KeyError, TypeError):
        result = {"tailscale_dns": "", "status": "unavailable"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
