#!/usr/bin/env python3
"""Credential-free workspace build, SSH and persistence checks on Docker."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LABEL = "agent-stack.container-test"


class CheckFailed(Exception):
    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.details = details


def command_details(args, stdout, stderr, status, timed_out=False):
    # Only the credential-free suite consumes these bounded diagnostics. Do
    # not include arguments, environment variables or stdin in the report.
    limit = 8192
    return {"program": Path(args[0]).name[:64], "exit_code": status, "timed_out": timed_out,
            "stdout": stdout[-limit:], "stderr": stderr[-limit:],
            "truncated": len(stdout) > limit or len(stderr) > limit}


def execute(args, env, timeout=60, input=None, check=True):
    try:
        process = subprocess.Popen(args, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError:
        raise CheckFailed("command_unavailable") from None
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise CheckFailed("command_timed_out", command_details(args, stdout, stderr, process.returncode, True)) from None
        raise
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise CheckFailed("command_failed: " + Path(args[0]).name,
                          command_details(args, stdout, stderr, result.returncode)) from None
    return result


def isolated_env(directory):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TF_", "AWS_", "ARM_", "GOOGLE_", "GCLOUD_"))
           and key not in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                           "GEMINI_API_KEY", "GROQ_API_KEY", "META_API_KEY", "MODEL_API_KEY", "DIGITALOCEAN_TOKEN", "HCLOUD_TOKEN", "TAILSCALE_AUTH_KEY")}
    env.update(DOCKER_CONFIG=str(directory / "docker-client"), TF_DATA_DIR=str(directory / "terraform-data"), TF_INPUT="0")
    config = directory / "terraform.rc"
    config.write_text("")
    env["TF_CLI_CONFIG_FILE"] = str(config)
    return env


def render_workspace(directory, env, codex_release, claude_release, terraform="terraform", muse_release=None,
                     opencode_release=None, opencode_model=""):
    values = {"workspace_username": "testuser", "workspace_codex_release": codex_release,
              "workspace_claude_enabled": bool(claude_release), "workspace_claude_release": claude_release or "",
              "workspace_muse_enabled": bool(muse_release), "workspace_muse_release": muse_release or "",
              "workspace_opencode_enabled": bool(opencode_release), "workspace_opencode_release": opencode_release or "",
              "workspace_opencode_model": opencode_model}
    templates = ROOT / "modules/common/templates/runtime"
    sources = {
        "Dockerfile": "workspace.Dockerfile.tpl", "workspace-entrypoint.sh": "workspace-entrypoint.sh.tpl",
        "workspace-codex-update.sh": "workspace-codex-update.sh.tpl", "workspace-codex-control.py": "workspace-codex-control.py.tpl",
    }
    render_dir = directory / "render"
    render_dir.mkdir()
    rows = [json.dumps(name) + " = templatefile(" + json.dumps(str(templates / source)) + ", " + json.dumps(values) + ")"
            for name, source in sources.items()]
    # This isolated config has only locals; no provider, resource, state input,
    # credential, backend, or live apply is involved in template rendering.
    (render_dir / "main.tf").write_text("locals {\n bundle = {\n" + "\n".join(rows) + "\n }\n}\n")
    result = execute([terraform, "-chdir=" + str(render_dir), "console", "-no-color"], env, input="jsonencode(local.bundle)\n")
    try:
        bundle = json.loads(json.loads(result.stdout))
    except (TypeError, ValueError):
        raise CheckFailed("template_render_failed") from None
    context = directory / "context"
    context.mkdir()
    for name in sources:
        if not isinstance(bundle.get(name), str) or not bundle[name]:
            raise CheckFailed("template_render_incomplete")
        (context / name).write_text(bundle[name])
    shutil.copyfile(ROOT / "modules/common/runtime/workspace_cli_task.py", context / "workspace-cli-task.py")
    (context / "para-memory-drive").mkdir()
    shutil.copyfile(ROOT / "skills/para-memory-drive/SKILL.md", context / "para-memory-drive/SKILL.md")
    return context


def pinned_version(output, expected):
    return bool(re.search(r"(?<![0-9.])" + re.escape(expected) + r"(?![0-9.])", output))


class ContainerChecks:
    def __init__(self, env, token):
        self.env, self.token = env, token
        self.owned = {"container": [], "volume": [], "image": []}

    def docker(self, *args, **kwargs):
        return execute(["docker", *args], self.env, **kwargs)

    def remember(self, kind, name):
        self.owned[kind].append(name)
        return name

    def inspect_owned(self, kind, name):
        result = self.docker(kind, "inspect", name, check=False)
        if result.returncode != 0:
            return False
        try:
            info = json.loads(result.stdout)[0]
            if not isinstance(info, dict):
                raise CheckFailed("resource_ownership_unverified")
            config = info if kind == "volume" else info.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if not isinstance(labels, dict) or labels.get(LABEL) != self.token:
                raise CheckFailed("resource_ownership_mismatch")
        except (ValueError, KeyError, TypeError, IndexError):
            raise CheckFailed("resource_ownership_unverified") from None
        return True

    def cleanup(self):
        passed = True
        for kind in ("container", "image", "volume"):
            for name in reversed(self.owned[kind]):
                try:
                    if self.inspect_owned(kind, name):
                        args = [kind, "rm"] + (["-f"] if kind == "container" else []) + [name]
                        self.docker(*args, timeout=120)
                except CheckFailed:
                    passed = False
        # Query successful listings independently; a failed inspect by itself
        # cannot distinguish deletion from a disconnected Docker daemon.
        for kind, names in self.owned.items():
            try:
                if kind == "image":
                    remaining = self.docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").stdout.splitlines()
                elif kind == "container":
                    remaining = self.docker("container", "ls", "-a", "--format", "{{.Names}}").stdout.splitlines()
                else:
                    remaining = self.docker("volume", "ls", "--format", "{{.Name}}").stdout.splitlines()
                passed = passed and not (set(names) & set(remaining))
            except CheckFailed:
                passed = False
        return passed

    def ssh_port(self, name):
        binding = self.docker("port", name, "22/tcp").stdout.strip()
        if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", binding):
            raise CheckFailed("workspace_port_not_loopback")
        port = binding.rsplit(":", 1)[1]
        if not 1 <= int(port) <= 65535:
            raise CheckFailed("workspace_port_not_loopback")
        return port

    def wait_ssh(self, name, port, key, known_hosts):
        command = ["ssh", "-F", "/dev/null", "-i", str(key), "-p", str(port), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                   "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + str(known_hosts),
                   "-o", "ConnectTimeout=3", "testuser@127.0.0.1", "id -u"]
        details = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = execute(command, self.env, timeout=10, check=False)
            if result.returncode == 0 and result.stdout.strip() == "1000":
                return command[:-1]
            details = command_details(command, result.stdout, result.stderr, result.returncode)
            inspection = json.loads(self.docker("container", "inspect", name).stdout)[0]
            if inspection.get("State", {}).get("Running") is not True:
                raise CheckFailed("workspace_stopped_before_ssh", details)
            time.sleep(2)
        raise CheckFailed("workspace_ssh_timeout", details)

    def profile(self, directory, codex_release, claude_release, platform, context=None):
        profile = "showcase" if claude_release else "legacy"
        context = render_workspace(directory, self.env, codex_release, claude_release) if context is None else context
        image = self.remember("image", "agent-stack-container-test:" + self.token + "-" + profile)
        self.docker("build", "--platform", platform, "--label", LABEL + "=" + self.token, "-t", image, str(context), timeout=1800)
        home = self.remember("volume", "as-test-" + self.token + "-" + profile + "-home")
        keys = self.remember("volume", "as-test-" + self.token + "-" + profile + "-keys")
        for volume in (home, keys):
            self.docker("volume", "create", "--label", LABEL + "=" + self.token, volume)
        key_dir = directory / "keys"
        key_dir.mkdir(mode=0o700)
        key = key_dir / "ssh-test-key"
        execute(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], self.env)
        run_env = dict(self.env, WORKSPACE_PASSWORD="fake-container-test-password", WORKSPACE_PASSWORD_HASH="", WORKSPACE_USERNAME="testuser",
                       WORKSPACE_AUTHORIZED_KEYS_BASE64=base64.b64encode(key.with_suffix(".pub").read_bytes()).decode())
        if claude_release:
            hashed = execute(["openssl", "passwd", "-6", "-stdin"], self.env, input="fake-container-test-password\n").stdout.strip()
            if not re.fullmatch(r"\$6\$[./A-Za-z0-9]{8,16}\$[./A-Za-z0-9]{86}", hashed):
                raise CheckFailed("fixture_password_hash_failed")
            run_env.update(WORKSPACE_PASSWORD="", WORKSPACE_PASSWORD_HASH=hashed)
        container = self.remember("container", "as-test-" + self.token + "-" + profile)
        execute(["docker", "run", "-d", "--platform", platform, "--name", container, "--label", LABEL + "=" + self.token,
                 "-p", "127.0.0.1::22", "-v", home + ":/home/testuser", "-v", keys + ":/var/lib/agent-stack-workspace/ssh-host-keys",
                 "-e", "WORKSPACE_PASSWORD", "-e", "WORKSPACE_PASSWORD_HASH", "-e", "WORKSPACE_USERNAME", "-e", "WORKSPACE_AUTHORIZED_KEYS_BASE64", image], run_env)
        port = self.ssh_port(container)
        ssh = self.wait_ssh(container, port, key, directory / "known_hosts")
        host_key_before = self.docker("exec", container, "ssh-keygen", "-lf", "/var/lib/agent-stack-workspace/ssh-host-keys/ssh_host_ed25519_key.pub").stdout.strip()
        for tool, expected in (("codex", codex_release), ("claude", claude_release)):
            if expected:
                result = execute(ssh + [tool + " --version"], self.env)
                if not pinned_version(result.stdout, expected):
                    raise CheckFailed("wrong_" + tool + "_version")
        if not claude_release:
            result = execute(ssh + ["command -v claude"], self.env, check=False)
            if result.returncode == 0:
                raise CheckFailed("legacy_workspace_unexpected_claude")
        probe = "import json,os,pathlib; p=pathlib.Path.home()/'container-test-marker'; p.write_text('persisted'); print(json.dumps({'uid':os.geteuid(),'owner':p.stat().st_uid,'home':str(pathlib.Path.home())}))"
        result = execute(ssh + [shlex.join(["python3", "-c", probe])], self.env)
        if json.loads(result.stdout) != {"uid": 1000, "owner": 1000, "home": "/home/testuser"}:
            raise CheckFailed("workspace_identity_or_home_failed")
        # Restart and recreate exercise persistence and stable SSH trust.
        self.docker("restart", container, timeout=120)
        # Docker may choose a different ephemeral host port at every start.
        # Refresh both readiness and subsequent commands after the restart.
        port = self.ssh_port(container)
        ssh = self.wait_ssh(container, port, key, directory / "known_hosts")
        host_key = self.docker("exec", container, "ssh-keygen", "-lf", "/var/lib/agent-stack-workspace/ssh-host-keys/ssh_host_ed25519_key.pub").stdout
        if not host_key_before or host_key.strip() != host_key_before:
            raise CheckFailed("persisted_host_key_changed")
        execute(ssh + ["test \"$(cat ~/container-test-marker)\" = persisted"], self.env)
        self.docker("container", "rm", "-f", container)
        execute(["docker", "run", "-d", "--platform", platform, "--name", container, "--label", LABEL + "=" + self.token,
                 "-p", "127.0.0.1::22", "-v", home + ":/home/testuser", "-v", keys + ":/var/lib/agent-stack-workspace/ssh-host-keys",
                 "-e", "WORKSPACE_PASSWORD", "-e", "WORKSPACE_PASSWORD_HASH", "-e", "WORKSPACE_USERNAME", "-e", "WORKSPACE_AUTHORIZED_KEYS_BASE64", image], run_env)
        port = self.ssh_port(container)
        # The assigned port may change. Verify the persisted host-key fingerprint
        # directly too, rather than treating fresh trust on a new port as proof.
        host_key = self.docker("exec", container, "ssh-keygen", "-lf", "/var/lib/agent-stack-workspace/ssh-host-keys/ssh_host_ed25519_key.pub").stdout
        ssh = self.wait_ssh(container, port, key, directory / "recreated_known_hosts")
        execute(ssh + ["test \"$(cat ~/container-test-marker)\" = persisted"], self.env)
        if not host_key_before or host_key.strip() != host_key_before:
            raise CheckFailed("persisted_host_key_changed")
        for tool in ("codex", "claude") if claude_release else ("codex",):
            binary = "/usr/local/bin/" + tool
            self.docker("exec", container, "mv", binary, binary + ".test-disabled")
            try:
                result = execute(ssh + [tool + " --version"], self.env, check=False)
                if result.returncode == 0:
                    raise CheckFailed("missing_binary_was_accepted")
            finally:
                self.docker("exec", container, "mv", binary + ".test-disabled", binary)
        # A deliberately missing password must fail startup before SSH begins.
        negative = self.remember("container", container + "-missing-password")
        result = self.docker("run", "--name", negative, "--label", LABEL + "=" + self.token, image, timeout=60, check=False)
        if result.returncode == 0 or "WORKSPACE_PASSWORD is required" not in result.stderr + result.stdout:
            raise CheckFailed("missing_password_did_not_fail")
        return {"profile": profile, "status": "pass", "uid": 1000, "ssh": "pass", "persistence": "pass",
                "codex_release": codex_release, "claude_release": claude_release, "platform": platform,
                "model_execution": "not_tested"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("showcase",), default="showcase")
    parser.add_argument("--codex-release", required=True)
    parser.add_argument("--claude-release", required=True)
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    args = parser.parse_args()
    if not all(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release) for release in (args.codex_release, args.claude_release)):
        parser.error("supply exact numeric CLI versions")
    if any(shutil.which(tool) is None for tool in ("docker", "terraform", "ssh", "ssh-keygen", "openssl")):
        print(json.dumps({"status": "fail", "reason": "docker_terraform_ssh_openssl_required", "model_execution": "not_tested"}))
        return 1
    def interrupted(signum, frame):
        raise CheckFailed("interrupted")
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    result = {"schema": 1, "status": "fail", "profiles": [], "model_execution": "not_tested"}
    with tempfile.TemporaryDirectory(prefix="agent-stack-container-tests-") as temp:
        directory = Path(temp)
        env = isolated_env(directory)
        checker = ContainerChecks(env, uuid.uuid4().hex[:16])
        try:
            checker.docker("info", timeout=20)
            for label, release in (("legacy", None), ("showcase", args.claude_release)):
                work = directory / label
                work.mkdir()
                result["profiles"].append(checker.profile(work, args.codex_release, release, args.platform))
            result["status"] = "pass"
        except (CheckFailed, ValueError, KeyError, TypeError):
            result["reason"] = "container_build_or_check_failed"
        finally:
            result["cleanup"] = "pass" if checker.cleanup() else "fail"
            if result["cleanup"] != "pass":
                result["status"] = "fail"
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
