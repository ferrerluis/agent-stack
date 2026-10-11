#!/usr/bin/env python3
"""Opt-in dependency repair for one immutable Hermes base image.

Only the declared npm manifests, installation and compiled interfaces change.
User data, Python dependencies, native doctors and the upstream dispatcher stay
unchanged. npm/build children receive no app, model or operator credentials.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

BUNDLE_SHA256 = "e3042d88a382511da313badfa1407a1d2add45054b372e393578fba99ae6a8ac"
REVISION = "749220ef0007f8d87bd1531f1c24b0fe93816385"
BUNDLE = Path("/usr/local/libexec/agent-stack-hermes-npm-patch.json")
ROOT = Path("/opt/hermes")
FILES = {"package.json", "package-lock.json", "web/package.json", "ui-tui/package.json"}
OUTPUTS = ("hermes_cli/web_dist", "ui-tui/dist")
MARKER = ".agent-stack-npm-patch.json"


def need(value):
    if not value:
        raise ValueError("hermes_npm_patch_unverified")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def regular(path, owner=0):
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022)
    return path.read_bytes()


def atomic(path, data, mode=0o644):
    temporary = path.with_name(path.name + ".agent-stack-new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Patch:
    def __init__(self, root=ROOT, bundle=BUNDLE, owner=0):
        self.root, self.owner = Path(root), owner
        self.stage = "inputs"
        self.directory(self.root)
        raw = regular(Path(bundle), owner)
        need(sha(raw) == BUNDLE_SHA256)
        self.bundle = json.loads(raw)
        need(self.bundle["schema"] == 1 and self.bundle["upstream_revision"] == REVISION
             and set(self.bundle["files"]) == FILES)
        need(regular(self.root / ".hermes_build_sha", owner).decode().strip() == REVISION)
        need(sha(regular(self.root / ".npmrc", owner)) == self.bundle["npm_config_sha256"])
        for name, entry in self.bundle["files"].items():
            need(sha(entry["content"].encode()) == entry["patched_sha256"])
            path = self.path(name)
            need(sha(regular(path, owner)) in {entry["original_sha256"], entry["patched_sha256"]})

    def directory(self, path):
        info = path.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == self.owner and not info.st_mode & 0o022)

    def path(self, relative):
        path = self.root / relative
        need(not Path(relative).is_absolute() and ".." not in Path(relative).parts)
        for parent in reversed(path.relative_to(self.root).parents):
            self.directory(self.root / parent)
        return path

    def output_hashes(self):
        result = {}
        for name in OUTPUTS:
            directory = self.path(name)
            self.directory(directory)
            entries = []
            for path in sorted(directory.rglob("*")):
                if path.is_dir():
                    self.directory(path)
                else:
                    entries.append([str(path.relative_to(directory)), sha(regular(path, self.owner))])
            need(entries)
            result[name] = sha(json.dumps(entries, separators=(",", ":")).encode())
        return result

    def installed(self):
        for name, version in self.bundle["installed_versions"].items():
            need(json.loads(regular(self.path(name + "/package.json"), self.owner))["version"] == version)
        for name, entry in self.bundle["files"].items():
            need(sha(regular(self.path(name), self.owner)) == entry["patched_sha256"])

    def verify(self):
        self.installed()
        marker = json.loads(regular(self.root / MARKER, self.owner))
        need(marker == {"schema": 1, "bundle_sha256": BUNDLE_SHA256,
                        "upstream_revision": REVISION, "outputs": self.output_hashes()})
        return marker

    def apply(self, runner):
        if (self.root / MARKER).exists() or (self.root / MARKER).is_symlink():
            self.stage = "restart_verify"
            return self.verify()
        self.stage = "manifests"
        for name, entry in self.bundle["files"].items():
            atomic(self.path(name), entry["content"].encode())
        self.stage = "npm_install"
        runner(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                "--workspace", "web", "--workspace", "ui-tui", "--include-workspace-root"], self.root)
        for name in ("web", "ui-tui"):
            self.stage = "web_build" if name == "web" else "tui_build"
            runner(["npm", "run", "build"], self.root / name)
        self.stage = "installed_verify"
        self.installed()
        self.stage = "outputs_verify"
        marker = {"schema": 1, "bundle_sha256": BUNDLE_SHA256,
                  "upstream_revision": REVISION, "outputs": self.output_hashes()}
        self.stage = "marker_write"
        atomic(self.root / MARKER, (json.dumps(marker, sort_keys=True) + "\n").encode())
        self.stage = "marker_verify"
        return self.verify()


class Runner:
    def __init__(self, home=Path("/var/cache/agent-stack-hermes-npm"), seconds=480, owner=0):
        self.deadline = time.monotonic() + seconds
        self.returncode = None
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = home.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == owner and stat.S_IMODE(info.st_mode) == 0o700)
        for name in ("user.npmrc", "global.npmrc"):
            path = home / name
            if not path.exists() and not path.is_symlink():
                atomic(path, b"", 0o600)
            need(regular(path, owner) == b"")
        self.environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
            "npm_config_cache": str(home / "cache"), "npm_config_userconfig": str(home / "user.npmrc"),
            "npm_config_globalconfig": str(home / "global.npmrc"), "npm_config_registry": "https://registry.npmjs.org/",
            "npm_config_fetch_retries": "2", "npm_config_fetch_retry_mintimeout": "1000",
            "npm_config_fetch_retry_maxtimeout": "3000", "npm_config_fetch_timeout": "30000",
            "npm_config_install_links": "false"}

    def __call__(self, command, cwd):
        self.returncode = None
        remaining = self.deadline - time.monotonic()
        need(remaining > 0)
        process = subprocess.Popen(command, cwd=cwd, env=self.environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, umask=0o022)
        try:
            output, _ = process.communicate(timeout=remaining)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        # Children have a cleared environment; bounded output is useful for
        # installation/build failures and contains only public package data.
        self.returncode = process.returncode
        if process.returncode:
            sys.stderr.buffer.write(output[-65536:])
            raise ValueError("hermes_npm_patch_command_failed")


def failure_details(error, stage, runner):
    """Fixed stages and numeric OS/process facts only; never exception text."""
    stages = {"inputs", "restart_verify", "manifests", "npm_install", "web_build", "tui_build",
              "installed_verify", "outputs_verify", "marker_write", "marker_verify", "runner", "dispatcher"}
    result = {"stage": stage if stage in stages else "inputs", "kind": "verification"}
    if isinstance(error, subprocess.TimeoutExpired):
        result["kind"] = "timeout"
    elif isinstance(error, OSError):
        result["kind"] = "os_error"
        if type(error.errno) is int:
            result["errno"] = error.errno
    elif isinstance(error, KeyError):
        result["kind"] = "missing_field"
    code = getattr(runner, "returncode", None)
    if stage in {"npm_install", "web_build", "tui_build"} and type(code) is int and code != 0:
        result.update(kind="command_exit", returncode=code)
    return result


def main():
    patch, runner, stage = None, None, "inputs"
    try:
        patch = Patch()
        if sys.argv[1:] == ["--verify"]:
            print(json.dumps(patch.verify(), sort_keys=True))
            return 0
        need(os.geteuid() == 0)
        stage = "runner"
        runner = Runner()
        stage = "inputs"
        patch.apply(runner)
        stage = "dispatcher"
        print("[hermes] Pinned npm dependency repair and interface builds verified.", flush=True)
        os.execv("/opt/hermes/docker/entrypoint-dispatch.sh", ["entrypoint-dispatch.sh", *sys.argv[1:]])
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print("[hermes] Pinned npm dependency repair failed; startup refused.", file=sys.stderr)
        detail_stage = stage if stage in {"runner", "dispatcher"} else getattr(patch, "stage", stage)
        print("[hermes] Repair failure details: " + json.dumps(failure_details(error, detail_stage, runner), sort_keys=True),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
