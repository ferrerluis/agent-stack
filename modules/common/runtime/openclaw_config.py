#!/usr/bin/env python3
"""Seed only a fresh OpenClaw gateway before its container is first started."""
import argparse
import json
import os
from pathlib import Path
import tempfile


def seed_config(data_dir, seed):
    root = Path(data_dir)
    target = root / "openclaw.json"
    if target.is_symlink():
        raise ValueError("refusing a symlinked OpenClaw configuration")
    if target.exists():
        if not target.is_file():
            raise ValueError("OpenClaw configuration is not a regular file")
        return False
    if not isinstance(seed, dict) or root.is_symlink():
        raise ValueError("invalid gateway seed or data directory")
    bind = seed.get("gateway_bind")
    token = seed.get("gateway_token")
    trust_loopback = seed.get("trusted_loopback_proxies_enabled", False)
    if bind not in ("lan", "loopback") or not isinstance(token, str) or not token.strip():
        raise ValueError("invalid gateway seed")
    if type(trust_loopback) is not bool or (trust_loopback and bind != "loopback"):
        raise ValueError("invalid loopback proxy seed")
    config = {"gateway": {"mode": "local", "bind": bind, "auth": {"mode": "token", "token": token},
                          "controlUi": {"allowedOrigins": ["http://localhost:18789", "http://127.0.0.1:18789"]}}}
    if trust_loopback:
        config["gateway"]["trustedProxies"] = ["127.0.0.1", "::1"]
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".openclaw-", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(config, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            temporary.chmod(0o600)
            if os.geteuid() == 0:
                os.chown(temporary, 1000, 1000)
            # Create without replacing a file another process may have written
            # since the initial existence check.
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file():
                    raise ValueError("OpenClaw configuration is not a regular file") from None
                return False
        finally:
            temporary.unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args()
    try:
        changed = seed_config(args.data_dir, json.loads(Path(args.seed).read_text()))
        print("changed" if changed else "preserved")
    except (OSError, ValueError, TypeError):
        parser.error("OpenClaw seed configuration is invalid or could not be written")


if __name__ == "__main__":
    main()
