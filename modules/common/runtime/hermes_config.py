#!/usr/bin/env python3
"""Seed a fresh Hermes config as JSON (a YAML subset); preserve existing files."""
import argparse
import json
import os
from pathlib import Path
import tempfile


def seed_config(data_dir, seed, mode="auto"):
    root = Path(data_dir)
    target = root / "config.yaml"
    if mode == "preserve" or (mode == "auto" and target.exists()):
        return False
    if target.is_symlink():
        raise ValueError("refusing a symlinked Hermes configuration")
    provider, separator, model = seed["model_ref"].partition("/")
    if not separator or not model or provider not in ("anthropic", "openai", "google", "groq"):
        raise ValueError("Hermes needs an anthropic/openai/google/groq provider-prefixed model")
    model_config = {"default": model, "provider": {"google": "gemini", "openai": "custom"}.get(provider, provider)}
    if provider == "openai":
        model_config["base_url"] = "https://api.openai.com/v1"
    config = {}
    if target.exists():
        # AgentStack seeds JSON; fail safely for operator-owned YAML in manage
        # mode rather than discarding channels, tools, profiles or auth settings.
        config = json.loads(target.read_text())
        if not isinstance(config, dict):
            raise ValueError("existing Hermes configuration must be an object")
    config["model"] = model_config
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".config-", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(config, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            temporary.chmod(0o600)
            if os.geteuid() == 0:
                os.chown(temporary, 10000, 10000)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--mode", choices=("auto", "manage", "preserve"), default="auto")
    args = parser.parse_args()
    try:
        changed = seed_config(args.data_dir, json.loads(Path(args.seed).read_text()), args.mode)
        print("changed" if changed else "preserved")
    except (OSError, ValueError, KeyError):
        parser.error("Hermes seed configuration is invalid or could not be written")


if __name__ == "__main__":
    main()
