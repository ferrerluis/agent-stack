#!/usr/bin/env python3
"""Seed a fresh Hermes config as JSON (a YAML subset); preserve existing files."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import tempfile

CLI_TARGET = "/opt/hermes/.venv/bin/hermes"


def install_command_link(data_dir):
    """Install a missing CLI link; retain every existing operator entry."""
    root = Path(data_dir).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("refusing a symlinked Hermes home")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as stack:
        fd = os.open(root, flags)
        stack.callback(os.close, fd)
        for name in (".local", "bin"):
            created = False
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            fd = os.open(name, flags, dir_fd=fd)
            stack.callback(os.close, fd)
            if created and os.geteuid() == 0:
                os.fchown(fd, 10000, 10000)
        try:
            os.symlink(CLI_TARGET, "hermes", dir_fd=fd)
        except FileExistsError:
            return False
        if os.geteuid() == 0:
            os.chown("hermes", 10000, 10000, dir_fd=fd, follow_symlinks=False)
    return True


def model_route(reference, fallback=False):
    provider, separator, model = reference.partition("/")
    if not separator or not model or provider not in ("anthropic", "openai", "google", "groq", "meta"):
        raise ValueError("Hermes needs an anthropic/openai/google/groq/meta provider-prefixed model")
    route = {"provider": {"google": "gemini", "openai": "custom", "meta": "meta-ai"}.get(provider, provider),
             "model" if fallback else "default": model}
    if provider == "openai":
        route["base_url"] = "https://api.openai.com/v1"
    return route


def seed_config(data_dir, seed, mode="auto"):
    root = Path(data_dir)
    target = root / "config.yaml"
    if mode == "preserve" or (mode == "auto" and target.exists()):
        return False
    if target.is_symlink():
        raise ValueError("refusing a symlinked Hermes configuration")
    model_config = model_route(seed["model_ref"])
    fallbacks = None
    if "fallback_models" in seed:
        if not isinstance(seed["fallback_models"], list) or not all(isinstance(ref, str) for ref in seed["fallback_models"]):
            raise ValueError("fallback_models must be a list of model references")
        fallbacks = [model_route(ref, fallback=True) for ref in seed["fallback_models"]]
    config = {}
    if target.exists():
        # Hermes migrations rewrite the JSON seed as YAML, including flow maps.
        # Preserve all unrelated settings when managing that migrated config.
        text = target.read_text()
        try:
            config = json.loads(text)
        except json.JSONDecodeError:
            try:
                import yaml
            except ImportError as error:
                raise ValueError("PyYAML is required to manage existing Hermes YAML") from error
            try:
                config = yaml.safe_load(text)
            except yaml.YAMLError as error:
                raise ValueError("existing Hermes YAML could not be read safely") from error
        if not isinstance(config, dict):
            raise ValueError("existing Hermes configuration must be an object")
    config["model"] = model_config
    if fallbacks is not None:
        config["fallback_providers"] = fallbacks
        config.pop("fallback_model", None)
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
        install_command_link(args.data_dir)
        print("changed" if changed else "preserved")
    except (OSError, ValueError, KeyError):
        parser.error("Hermes seed configuration is invalid or could not be written")


if __name__ == "__main__":
    main()
