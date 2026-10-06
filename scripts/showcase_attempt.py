#!/usr/bin/env python3
"""Private, credential-free preparation and bounded commands for showcase attempts.

Preparation exports one committed revision, never the operator's worktree or
state. It creates a test-only key and non-secret profile settings. It neither
reserves spend nor runs Terraform/cloud/model commands. Launch and acceptance
must be implemented by the controller before a prepared attempt can be used.
"""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import signal
import stat
import subprocess
import sys
import tarfile
import time

from showcase_policy import PROVIDERS, PolicyError, require, validate_inputs
from showcase_preflight import PreflightError, read_secret
from showcase_cleanup import private_read, private_write, scope

ROOT = Path(__file__).resolve().parents[1]
HASH_PATTERN = r"\$6\$(?:rounds=[1-9][0-9]{3,8}\$)?[./A-Za-z0-9]{8,16}\$[./A-Za-z0-9]{86}"
KEY_PATH = ".ssh/id_ed25519_agent_stack"
MAX_ARCHIVE = 32 * 1024 * 1024
PROFILE = {
    "enabled_services": ["openclaw", "hermes", "workspace"], "agent_channel": "none",
    "model_providers_enabled": ["openai", "anthropic"], "fallback_models": [], "openai_auth_mode": "api_key",
    "openclaw_config_mode": "manage", "hermes_config_mode": "auto", "hermes_dashboard_enabled": True,
    "hermes_api_server_enabled": True, "workspace_claude_enabled": True, "workspace_codex_auto_update_enabled": False,
    "tailscale_enabled": True, "tailscale_mode": "host", "public_domain_enabled": False,
    "vpn_enabled": False, "host_codex_cli_enabled": False, "generate_repo_ssh_config": True, "gcp_pin_boot_image": True,
    "aws_showcase_cost_controls": True,
}


def command(args, env=None, timeout=60, input=None):
    """Capture output privately; kill the process group on every interruption.

    Callers must never print raw stdout/stderr. No secret may be in args.
    """
    require(timeout > 0, "attempt_deadline_reached")
    try:
        process = subprocess.Popen(args, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, umask=0o077)
    except OSError:
        raise PolicyError("command_unavailable") from None
    try:
        output, errors = process.communicate(input, timeout=timeout)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise PolicyError("command_timed_out") from None
        raise
    require(process.returncode == 0, "command_failed")
    return output


def private_directory(path, create=False):
    path = Path(path).absolute()
    # Do not follow an operator-controlled symlink in any ancestor, including
    # nonexistent leaves of a path about to be created.
    for part in (path, *path.parents):
        require(not part.is_symlink(), "private_directory_required")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir() and path.stat().st_uid == os.geteuid()
            and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "private_directory_required")
    return path


def safe_archive(data):
    require(0 < len(data) <= MAX_ARCHIVE, "source_archive_too_large")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError:
        raise PolicyError("invalid_source_archive") from None
    rows, total = {}, 0
    with archive:
        for member in archive:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and path.parts and all(part not in ("..", ".") for part in path.parts)
                    and not re.search(r"[\x00-\x1f\x7f]", member.name), "unsafe_source_path")
            require(member.isdir() or member.isfile(), "source_links_or_special_files_refused")
            # Even a tracked file cannot bring state/auth/operator inputs into
            # a new attempt. Fake test keys have .txt filenames and are inert.
            forbidden = {".git", ".terraform", ".aws", ".codex", ".claude", ".showcase-runs"}
            require(not forbidden.intersection(path.parts) and not any(part.startswith(".tfstate") for part in path.parts)
                    and not re.search(r"(?:\.tfstate(?:\..*)?|\.tfvars(?:\.json)?|\.tfplan(?:\..*)?|\.pem|\.key)$", path.name)
                    and path.name not in ("id_ed25519", "id_ed25519_agent_stack", "id_rsa", "credentials", "auth.json"),
                    "sensitive_source_file_refused")
            if member.isdir():
                continue
            require(member.name not in rows and 0 <= member.size <= MAX_ARCHIVE, "invalid_source_archive")
            total += member.size
            require(total <= MAX_ARCHIVE and len(rows) < 4096, "source_archive_too_large")
            stream = archive.extractfile(member)
            require(stream is not None, "invalid_source_archive")
            content = stream.read(MAX_ARCHIVE + 1)
            require(len(content) == member.size, "invalid_source_archive")
            rows[member.name] = (content, 0o700 if member.mode & 0o111 else 0o600)
    require({"main.tf", "variables.tf", "examples/showcase/terraform.tfvars.example"} <= rows.keys(), "agent_stack_source_required")
    return rows


def settings(config, provider, prefix, public_key, profile_source):
    profile = {}
    for line in profile_source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        row = re.fullmatch(r"([a-z][a-z0-9_]*)\s*=\s*(.+)", line.strip())
        require(row is not None and row[1] not in profile, "invalid_showcase_profile")
        try:
            profile[row[1]] = json.loads(row[2])
        except ValueError:
            raise PolicyError("invalid_showcase_profile") from None
    # A changed profile must be reviewed in the controller too; never let an
    # example silently enable public access or a different authentication mode.
    require(profile == PROFILE, "showcase_profile_changed")
    pins, models, cloud = config["pins"], config["models"], config["providers"][provider]
    values = {**profile, "cloud_provider": provider, "project_name": prefix, "allowed_ssh_cidr": config["runner"]["ssh_cidr"],
              "admin_username": "admin", "admin_password": "", "admin_password_ssh_scope": "disabled",
              "workspace_username": "user", "workspace_ssh_public_keys": [public_key], "ssh_public_key": public_key,
              "repo_ssh_private_key_path": KEY_PATH, "repo_ssh_identity_file": "./" + KEY_PATH,
              "openclaw_version": pins["openclaw_version"], "openclaw_image_digest": pins["openclaw_digest"],
              "hermes_image": pins["hermes_image"], "workspace_codex_release": pins["codex_release"],
              "workspace_claude_release": pins["claude_release"], "default_model": models["openclaw"], "hermes_model": models["hermes"]}
    # Do not write blank secret variables here: .tfvars has priority over the
    # controller's later in-memory TF_VAR_* injection.
    values.update({
        "aws": {"aws_region": cloud["region"], "aws_instance_type": cloud["size"], "aws_existing_volume_id": ""},
        "digitalocean": {"do_region": cloud["region"], "do_droplet_size": cloud["size"], "do_project_id": cloud.get("project_id", ""),
                         "do_existing_volume_id": "", "do_existing_volume_name": ""},
        "gcp": {"gcp_project_id": cloud.get("project_id", ""), "gcp_region": cloud["region"], "gcp_zone": cloud.get("zone", ""), "gcp_machine_type": cloud["size"]},
        "hetzner": {"hcloud_location": cloud["region"], "hcloud_server_type": cloud["size"], "hcloud_existing_volume_id": ""},
    }[provider])
    return values


def prepare(config, provider, prefix, batch, revision="HEAD", repo=ROOT):
    validate_inputs(config, provider)
    require(isinstance(prefix, str) and re.fullmatch(r"as7-[a-z0-9-]{8,30}", prefix) is not None, "invalid_run_id")
    require(revision == "HEAD" or re.fullmatch(r"[a-f0-9]{40}", revision) is not None, "exact_revision_required")
    # Git export cannot accidentally include local tfvars/state or unfinished
    # edits. Preserve the selected full SHA and every exported file hash.
    full = command(["git", "-C", str(repo), "rev-parse", "--verify", revision + "^{commit}"]).decode().strip()
    require(re.fullmatch(r"[a-f0-9]{40}", full) is not None, "exact_revision_required")
    rows = safe_archive(command(["git", "-C", str(repo), "archive", "--format=tar", full]))
    batch = private_directory(batch, create=True)
    attempts = private_directory(batch / "attempts", create=True)
    run = attempts / prefix
    require(not run.exists() and not run.is_symlink(), "attempt_directory_already_exists")
    run.mkdir(mode=0o700)
    # Failure leaves a private recovery directory. Never remove another run's
    # directory or discard a potentially valuable recovery record.
    private_write(run / "preparation.json", {"schema": 1, "run_id": prefix, "source_revision": full, "stage": "preparing", "scope": scope(config, provider)})
    checkout = run / "repo"
    checkout.mkdir(mode=0o700)
    manifest = {}
    for name, (content, mode) in rows.items():
        target = checkout / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as file:
            file.write(content)
        target.chmod(mode)
        manifest[name] = hashlib.sha256(content).hexdigest()
    key_dir = private_directory(checkout / ".ssh", create=True)
    command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", prefix, "-f", str(key_dir / Path(KEY_PATH).name)])
    key = key_dir / Path(KEY_PATH).name
    require(key.is_file() and not key.is_symlink() and stat.S_IMODE(key.stat().st_mode) == 0o600, "private_key_required")
    public = key.with_name(key.name + ".pub").read_text().strip()
    require(re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+ " + re.escape(prefix), public) is not None, "test_public_key_required")
    values = settings(config, provider, prefix, public, (checkout / "examples/showcase/terraform.tfvars.example").read_text())
    private_write(checkout / "terraform.tfvars.json", values)
    (key_dir / "known_hosts").touch(mode=0o600, exist_ok=False)
    for name in ("terraform-data", "home", "docker-client"):
        private_directory(run / name, create=True)
    (run / "terraform.rc").touch(mode=0o600, exist_ok=False)
    private_write(run / "inputs.json", config)
    record = {"schema": 1, "run_id": prefix, "source_revision": full, "scope": scope(config, provider), "stage": "prepared",
              "source_files": manifest, "settings_sha256": hashlib.sha256((checkout / "terraform.tfvars.json").read_bytes()).hexdigest(),
              "public_key_sha256": hashlib.sha256(public.encode()).hexdigest(), "prepared_at": time.time(),
              "spend_reserved": False, "live_matrix": "not_tested"}
    private_write(run / "preparation.json", record)
    return record


def verify_prepared(run, config, provider):
    run = private_directory(run)
    record = private_read(run / "preparation.json")
    require(record.get("schema") == 1 and record.get("stage") == "prepared" and record.get("scope") == scope(config, provider)
            and record.get("run_id") == run.name and re.fullmatch(r"[a-f0-9]{40}", record.get("source_revision", "")) is not None,
            "prepared_scope_mismatch")
    require(private_read(run / "inputs.json") == config, "prepared_inputs_changed")
    checkout = private_directory(run / "repo")
    require(isinstance(record.get("source_files"), dict) and record["source_files"], "invalid_preparation_record")
    # Revalidate paths before hashing. A edited manifest cannot read outside
    # the attempt, and a changed/symlinked source cannot reach an apply.
    for name, expected in record["source_files"].items():
        path = PurePosixPath(name)
        require(path.parts and not path.is_absolute() and ".." not in path.parts and isinstance(expected, str), "invalid_preparation_record")
        target = checkout / name
        require(all(not part.is_symlink() for part in (target, *target.parents)) and target.is_file()
                and hashlib.sha256(target.read_bytes()).hexdigest() == expected, "prepared_source_changed")
    generated = {"terraform.tfvars.json", ".terraform.lock.hcl", "terraform.tfstate", "terraform.tfstate.backup",
                 KEY_PATH, KEY_PATH + ".pub", ".ssh/known_hosts", ".ssh/config"}
    for target in checkout.rglob("*"):
        require(not target.is_symlink(), "prepared_source_changed")
        if target.is_dir():
            continue
        require(target.is_file() and (target.relative_to(checkout).as_posix() in record["source_files"] or
                target.relative_to(checkout).as_posix() in generated), "unexpected_prepared_file")
    require(hashlib.sha256((checkout / "terraform.tfvars.json").read_bytes()).hexdigest() == record.get("settings_sha256"), "prepared_settings_changed")
    private_directory(checkout / ".ssh")
    key = checkout / KEY_PATH
    require(key.is_file() and not key.is_symlink() and stat.S_IMODE(key.stat().st_mode) == 0o600, "private_key_required")
    public = command(["ssh-keygen", "-y", "-f", str(key)]).decode().strip()
    expected = (key.with_name(key.name + ".pub")).read_text().strip()
    require(" ".join(expected.split()[:2]) == " ".join(public.split()[:2])
            and hashlib.sha256(expected.encode()).hexdigest() == record.get("public_key_sha256"), "prepared_key_changed")
    return record


def workspace_password_hash(reference, secret_reader=read_secret):
    """One 1Password read, one stdin-only hash command, no plaintext file."""
    password = secret_reader(reference)
    require(isinstance(password, str) and 14 <= len(password.encode()) <= 128
            and not re.search(r"[\x00-\x1f\x7f]", password), "invalid_workspace_password")
    # Salt is random and non-secret. Passing only it as an argument avoids
    # exposing the password to process listings or shell interpolation.
    salt = "".join(secrets.choice("./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz") for _ in range(16))
    output = command(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=password.encode() + b"\n").decode().strip()
    require(re.fullmatch(HASH_PATTERN, output) is not None and output.startswith("$6$" + salt + "$"), "password_hash_failed")
    return output


def isolated_environment(run):
    run = private_directory(run)
    # Explicit allowlist: discard TF_CLI_ARGS, TF_VAR_*, personal provider,
    # proxy, coding-tool, 1Password and SSH-agent credentials/settings.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ") if key in os.environ}
    env.update(TF_DATA_DIR=str(run / "terraform-data"), TF_CLI_CONFIG_FILE=str(run / "terraform.rc"),
               TF_INPUT="0", TF_IN_AUTOMATION="1", PYTHONDONTWRITEBYTECODE="1", DOCKER_CONFIG=str(run / "docker-client"),
               AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_EC2_METADATA_DISABLED="true")
    return env


def remaining_timeout(attempt, limit, cleanup=False, now=None):
    now = time.time() if now is None else now
    key = "deadline" if cleanup else "work_deadline"
    require(isinstance(attempt, dict) and type(attempt.get(key)) in (int, float) and math.isfinite(attempt[key])
            and type(limit) in (int, float) and math.isfinite(limit) and limit > 0
            and type(now) in (int, float) and math.isfinite(now), "invalid_attempt_deadline")
    # Expiry stops all new work/spend. Recovery must still remove owned
    # resources after an interrupted or overlong attempt; bound each cleanup
    # operation, never turn the deadline into a reason to leave a VM running.
    if cleanup:
        return limit
    remaining = attempt[key] - now
    require(remaining > 0, "attempt_deadline_reached")
    return min(limit, remaining)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    try:
        record = prepare(private_read(args.config), args.provider, args.run_id, args.batch, args.revision)
        report = {key: record[key] for key in ("run_id", "source_revision", "stage", "spend_reserved", "live_matrix")}
        report["launch_ready"] = False
    except (PolicyError, PreflightError) as error:
        report = {"stage": "failed", "reason": str(error), "launch_ready": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError):
        report = {"stage": "failed", "reason": "invalid_preparation_input", "launch_ready": False}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["stage"] == "prepared" else 1


if __name__ == "__main__":
    sys.exit(main())
