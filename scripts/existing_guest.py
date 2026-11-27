#!/usr/bin/env python3
"""Read-only preflight of an explicit user-prepared guest. Never manages its host."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ALIAS = "agent-stack-preflight-guest"
REMOTE_PROBE = r'''
import json, os, pathlib, platform
release = {}
for line in pathlib.Path('/etc/os-release').read_text().splitlines():
    if '=' in line:
        key, value = line.split('=', 1)
        release[key] = value.strip('"')
app = pathlib.Path('/opt/agent-stack')
data = app / 'data'
parts = [pathlib.Path('/opt'), app, data]
symlinks = any(p.is_symlink() for p in parts)
bad_type = any(p.exists() and not p.is_dir() for p in parts)
state = 'missing'
if data.is_dir() and not symlinks:
    marker = data / '.agent-stack-existing-guest.json'
    managed = marker.is_file() and not marker.is_symlink()
    state = 'managed' if managed else ('unmanaged' if next(data.iterdir(), None) else 'empty')
storage = data if data.is_dir() and not symlinks else pathlib.Path('/opt')
fs = os.statvfs(storage)
memory = next(line for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
print(json.dumps({
    'os': release.get('ID'), 'version': release.get('VERSION_ID'),
    'architecture': platform.machine(), 'memory_mb': int(memory.split()[1]) // 1024,
    'data_state': state, 'data_mounted': os.path.ismount(data),
    'unsafe_path': symlinks or bad_type,
    'disk_free_gb': fs.f_bavail * fs.f_frsize // 1024**3
}))
'''


class PreflightError(ValueError):
    pass


def validate_query(query: dict) -> None:
    for field, pattern in {
        "address": r"[A-Za-z0-9][A-Za-z0-9.-]*",
        "username": r"[a-z_][a-z0-9_-]{0,31}",
        "host_key": r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+",
    }.items():
        if not re.fullmatch(pattern, query.get(field, "")):
            raise PreflightError(f"invalid {field}")
    if query["username"] == "root" or not 1 <= int(query["port"]) <= 65535:
        raise PreflightError("use an existing non-root account and valid SSH port")
    if not query["identity_file"].startswith("/") or re.search(r"[\r\n]", query["identity_file"]):
        raise PreflightError("identity_file must be an absolute existing path")
    if query["architecture"] not in {"auto", "arm64", "amd64"}:
        raise PreflightError("architecture must be auto, arm64 or amd64")
    if any(query.get(flag) not in {"true", "false"} for flag in ("allow_existing_data", "require_data_mount")):
        raise PreflightError("invalid data-path policy")


def ssh_argv(query: dict, known_hosts: str) -> list[str]:
    validate_query(query)
    return ["ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"HostKeyAlias={ALIAS}",
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=15",
            "-i", query["identity_file"], "-p", str(query["port"]),
            f"{query['username']}@{query['address']}", "python3", "-"]


def validate_facts(query: dict, facts: dict) -> dict[str, str]:
    validate_query(query)
    if (facts.get("os"), facts.get("version")) != ("ubuntu", "24.04"):
        raise PreflightError("supported prepared guest is Ubuntu 24.04")
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(facts.get("architecture"))
    if architecture is None or query["architecture"] not in {"auto", architecture}:
        raise PreflightError("guest must be supported arm64/amd64 and match the expected architecture")
    if facts.get("unsafe_path"):
        raise PreflightError("/opt/agent-stack/data and its parents must be real directories, not symlinks")
    if facts.get("data_state") not in {"missing", "empty", "managed", "unmanaged"}:
        raise PreflightError("cannot establish guest data-path state")
    if facts["data_state"] == "unmanaged" and query["allow_existing_data"] != "true":
        raise PreflightError("nonempty unmarked data directory requires explicit reviewed reuse")
    if query["require_data_mount"] == "true" and not facts.get("data_mounted"):
        raise PreflightError("user-prepared data mount is required but missing; AgentStack never mounts a disk")
    if facts.get("memory_mb", 0) < 2048 or facts.get("disk_free_gb", 0) < 2:
        raise PreflightError("guest requires at least 2048 MiB RAM and 2 GiB free storage; size the selected workload separately")
    return {"status": "ready", "architecture": architecture, "data_state": facts["data_state"]}


def main() -> None:
    try:
        query = json.load(sys.stdin)
        validate_query(query)
        if not Path(query["identity_file"]).is_file():
            raise PreflightError("existing guest identity file not found")
        with tempfile.TemporaryDirectory(prefix="agent-stack-guest-trust-") as directory:
            known_hosts = Path(directory) / "known_hosts"
            known_hosts.write_text(f"{ALIAS} {query['host_key']}\n")
            known_hosts.chmod(0o600)
            result = subprocess.run(ssh_argv(query, str(known_hosts)), input=REMOTE_PROBE,
                                    text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise PreflightError("verified SSH guest probe failed; check the supplied endpoint, port, identity and Python 3")
        print(json.dumps(validate_facts(query, json.loads(result.stdout))))
    except PreflightError as error:
        print(f"existing-guest preflight: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("existing-guest preflight: unable to validate guest facts; no target changes were made", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
