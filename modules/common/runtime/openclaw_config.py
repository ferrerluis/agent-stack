#!/usr/bin/env python3
"""Seed only a fresh OpenClaw gateway before its container is first started."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import tempfile

COMPAT_ID = "agentstack-hf-tool-content"


def install_tool_compat(root, config):
    """Install the metadata-only native compatibility plugin for a fresh home."""
    root = Path(root)
    files = {
        "index.mjs": Path(__file__).with_name("openclaw_hf_tool_compat.mjs").read_text(),
        "openclaw.plugin.json": json.dumps({"id": COMPAT_ID, "activation": {"onStartup": True}, "configSchema": {"type": "object", "additionalProperties": False, "properties": {}}}),
        "package.json": json.dumps({"name": "@agentstack/hf-tool-content", "version": "0.1.0", "type": "module", "openclaw": {"extensions": ["./index.mjs"]}}),
    }
    if root.is_symlink():
        raise ValueError("refusing a symlinked plugin home")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(root, flags)
        stack.callback(os.close, directory_fd)
        if os.geteuid() == 0:
            os.fchown(directory_fd, 1000, 1000)
        for part in ("extensions", COMPAT_ID):
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            directory_fd = os.open(part, flags, dir_fd=directory_fd)
            stack.callback(os.close, directory_fd)
            if os.geteuid() == 0:
                os.fchown(directory_fd, 1000, 1000)
        for name, content in files.items():
            temporary = f".{name}.{os.getpid()}.new"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "w") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                    if os.geteuid() == 0:
                        os.fchown(file.fileno(), 1000, 1000)
                os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("plugins must be an object")
    if "allow" in plugins and (not isinstance(plugins["allow"], list) or not all(isinstance(name, str) for name in plugins["allow"])):
        raise ValueError("allowed plugins must be a list of names")
    if "allow" in plugins and COMPAT_ID not in plugins["allow"]:
        plugins["allow"].append(COMPAT_ID)
    plugins.setdefault("entries", {}).setdefault(COMPAT_ID, {})["enabled"] = True


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
    install_tool_compat(root, config)
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
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--seed")
    action.add_argument("--install-tool-compat", action="store_true")
    args = parser.parse_args()
    try:
        if args.install_tool_compat:
            # Managed HF updates enable the entry in their existing atomic
            # config update; this action only delivers the native plugin files.
            install_tool_compat(args.data_dir, {})
            changed = True
        else:
            changed = seed_config(args.data_dir, json.loads(Path(args.seed).read_text()))
        print("changed" if changed else "preserved")
    except (OSError, ValueError, TypeError):
        parser.error("OpenClaw seed configuration is invalid or could not be written")


if __name__ == "__main__":
    main()
