#!/usr/bin/env python3
"""Credential-free container checks on the first suitable owned showcase VM.

Templates are rendered by the controller with its verified Terraform binary.
The VM needs no Terraform, cloud credentials, model keys or package installs.
This library does not provision a VM or turn deferred checks into a pass.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import uuid

SCRIPT_PATH = "scripts/test_containers.py"
PROBE_PATH = "scripts/showcase_container_probe.py"
FILES = {"Dockerfile", "workspace-entrypoint.sh", "workspace-codex-update.sh", "workspace-codex-control.py",
         "workspace-cli-task.py", "para-memory-drive/SKILL.md"}
MAX_BUNDLE = 512 * 1024
WORK_SECONDS = 2700
CLEANUP_SECONDS = 240
WATCHDOG_SECONDS = 3000


class PolicyError(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise PolicyError(reason)


def command(args, env, timeout=60):
    child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True, umask=0o077)
    try:
        output, errors = child.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()
        raise PolicyError("container_command_failed") from None
    require(child.returncode == 0 and len(output) + len(errors) <= 65536, "container_command_failed")
    return output


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_bundle(value):
    require(isinstance(value, dict) and set(value) == {"schema", "request_id", "source_revision", "script", "script_sha256", "pins", "contexts"}
            and type(value["schema"]) is int and value["schema"] == 1, "container_bundle_invalid")
    require(isinstance(value["request_id"], str) and str(uuid.UUID(value["request_id"], version=4)) == value["request_id"]
            and isinstance(value["source_revision"], str) and re.fullmatch(r"[a-f0-9]{40}", value["source_revision"]), "container_bundle_invalid")
    require(isinstance(value["script"], str) and 0 < len(value["script"].encode()) < 65536
            and hashlib.sha256(value["script"].encode()).hexdigest() == value["script_sha256"], "container_source_changed")
    pins = value["pins"]
    require(isinstance(pins, dict) and set(pins) == {"codex_release", "claude_release", "platform"}
            and all(isinstance(pins[name], str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pins[name]) for name in ("codex_release", "claude_release"))
            and pins["platform"] == "linux/amd64", "container_pins_invalid")
    require(isinstance(value["contexts"], dict) and set(value["contexts"]) == {"legacy", "showcase"}, "container_context_invalid")
    for context in value["contexts"].values():
        require(isinstance(context, dict) and set(context) == FILES
                and all(isinstance(content, str) and 0 < len(content.encode()) < 65536 for content in context.values()), "container_context_invalid")
    require(len(json.dumps(value).encode()) <= MAX_BUNDLE, "container_bundle_too_large")
    return digest(value)


def checked_module(checkout):
    source = Path(checkout) / SCRIPT_PATH
    require(source.is_file() and not source.is_symlink(), "checked_container_source_required")
    module = types.ModuleType("checked_container_suite")
    module.__file__ = str(source)
    exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
    return module


def render_bundle(checkout, revision, pins, terraform="terraform", execute=None):
    from showcase_attempt import command as controller_command, isolated_environment
    execute = controller_command if execute is None else execute
    suite = checked_module(checkout)
    # Render with only locals and fake test inputs. No provider init or state.
    def run(args, env, timeout=60, input=None, check=True):
        raw = execute(args, env=env, timeout=timeout, input=None if input is None else input.encode())
        require(isinstance(raw, bytes) and len(raw) <= MAX_BUNDLE, "container_render_output_invalid")
        return subprocess.CompletedProcess(args, 0, raw.decode(), "")
    suite.execute = run
    contexts = {}
    with tempfile.TemporaryDirectory(prefix="agent-stack-container-render-") as temp:
        root = Path(temp)
        for label, claude in (("legacy", None), ("showcase", pins["claude_release"])):
            directory = root / label
            directory.mkdir(mode=0o700)
            context = suite.render_workspace(directory, isolated_environment(directory), pins["codex_release"], claude, terraform)
            contexts[label] = {path: (context / path).read_text() for path in sorted(FILES)}
    source = (Path(checkout) / SCRIPT_PATH).read_text()
    value = {"schema": 1, "request_id": str(uuid.uuid4()), "source_revision": revision,
             "script": source, "script_sha256": hashlib.sha256(source.encode()).hexdigest(),
             "pins": {name: pins[name] for name in ("codex_release", "claude_release")}, "contexts": contexts}
    value["pins"]["platform"] = "linux/amd64"
    validate_bundle(value)
    return value


def expected_names(token):
    return {
        "container": ["as-test-" + token + "-" + label + suffix for label in ("legacy", "showcase") for suffix in ("", "-missing-password")],
        "image": ["agent-stack-container-test:" + token + "-" + label for label in ("legacy", "showcase")],
        "volume": ["as-test-" + token + "-" + label + "-" + suffix for label in ("legacy", "showcase") for suffix in ("home", "keys")],
    }


def cleanup_source(token):
    # The timer can only remove these fresh names with this exact label. It
    # never adopts label matches or prunes shared images, caches or volumes.
    return """import json,os,subprocess
os.environ.clear(); os.environ.update(PATH='/usr/local/bin:/usr/bin:/bin',LANG='C.UTF-8')
names=""" + repr(expected_names(token)) + """
token=""" + repr(token) + """
for kind in ('container','image','volume'):
    for name in names[kind]:
        try:
            raw=subprocess.run(['/usr/bin/docker',kind,'inspect',name],capture_output=True,timeout=10,check=True)
            info=json.loads(raw.stdout)[0]
            config=info if kind=='volume' else info['Config']
            if config['Labels'].get('agent-stack.container-test')!=token: continue
            args=['/usr/bin/docker',kind,'rm']+(['-f'] if kind=='container' else [])+[name]
            subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=True)
        except Exception: pass
"""


def names_absent(checker, token):
    for kind, expected in expected_names(token).items():
        if kind == "image":
            args = ("image", "ls", "--format", "{{.Repository}}:{{.Tag}}")
        elif kind == "container":
            args = ("container", "ls", "-a", "--format", "{{.Names}}")
        else:
            args = ("volume", "ls", "--format", "{{.Name}}")
        raw = checker.docker(*args, timeout=10).stdout
        require(isinstance(raw, str) and len(raw.encode()) <= 1024 * 1024
                and not set(expected).intersection(raw.splitlines()), "container_name_collision_or_cleanup_failed")


def profile_valid(row, label, pins):
    expected = {"profile": label, "status": "pass", "uid": 1000, "ssh": "pass", "persistence": "pass",
                "codex_release": pins["codex_release"], "claude_release": pins["claude_release"] if label == "showcase" else None,
                "platform": "linux/amd64", "model_execution": "not_tested"}
    require(isinstance(row, dict) and row == expected and type(row["uid"]) is int, "container_profile_failed")


def validate_proof(proof, bundle, boot):
    sha = validate_bundle(bundle)
    require(isinstance(proof, dict) and set(proof) == {"schema", "status", "request_id", "source_revision", "bundle_sha256", "boot_id",
            "profiles", "cleanup", "watchdog", "duration_seconds", "model_execution"} and type(proof["schema"]) is int
            and proof["schema"] == 1 and proof["status"] == "pass" and proof["request_id"] == bundle["request_id"]
            and proof["source_revision"] == bundle["source_revision"] and proof["bundle_sha256"] == sha and proof["boot_id"] == boot
            and proof["cleanup"] == "pass" and proof["watchdog"] == "disarmed" and proof["model_execution"] == "not_tested"
            and type(proof["duration_seconds"]) in (int, float) and 0 <= proof["duration_seconds"] < WORK_SECONDS + CLEANUP_SECONDS,
            "container_proof_invalid")
    require(isinstance(proof["profiles"], list) and len(proof["profiles"]) == 2, "container_proof_invalid")
    for label, row in zip(("legacy", "showcase"), proof["profiles"]):
        profile_valid(row, label, bundle["pins"])
    return {**proof, "proof_sha256": digest(proof)}


def run_vm(bundle, execute=command, now=time.monotonic, suite_factory=None, scratch=None):
    sha = validate_bundle(bundle)
    require(os.geteuid() == 0 and sys.platform == "linux", "owned_linux_vm_required")
    started = now()
    token = bundle["request_id"].replace("-", "")[:16]
    unit = "as7-container-" + token
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    require(re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot), "container_boot_unverified")
    report = {"schema": 1, "status": "fail", "cleanup": "uncertain", "watchdog": "unverified"}
    directory = Path(tempfile.mkdtemp(prefix=unit + "-", dir=scratch))
    directory.chmod(0o700)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(directory), "LANG": "C.UTF-8", "TZ": "UTC",
           "DOCKER_CONFIG": str(directory / "docker-client")}
    checker = None
    armed = False
    success = False
    suite = None
    phase = "preflight"
    diagnostics = None
    cleanup_file = directory / "cleanup.py"
    cleanup_file.write_text(cleanup_source(token))
    cleanup_file.chmod(0o600)
    def stop(_signum, _frame):
        raise PolicyError("container_deadline_reached")
    def failure(error, stage):
        # Unknown exceptions can carry operator secrets. Only the verified,
        # credential-free suite's typed failures may supply private details.
        error_type = getattr(suite, "CheckFailed", None)
        if not isinstance(error_type, type) or not issubclass(error_type, Exception) or not isinstance(error, error_type):
            return None
        reason = str(error)
        if re.fullmatch(r"[a-z_]{1,80}(?:: [A-Za-z0-9_.-]{1,64})?", reason) is None:
            return None
        detail = getattr(error, "details", None)
        value = {"phase": stage, "reason": reason}
        if isinstance(detail, dict) and set(detail) == {"program", "exit_code", "timed_out", "stdout", "stderr", "truncated"}:
            if (isinstance(detail["program"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", detail["program"])
                    and type(detail["exit_code"]) is int and type(detail["timed_out"]) is bool and type(detail["truncated"]) is bool
                    and all(isinstance(detail[key], str) and len(detail[key]) <= 8192 for key in ("stdout", "stderr"))):
                value["command"] = detail
        return value
    old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGHUP)}
    for sig in old_handlers:
        signal.signal(sig, stop)
    try:
        if suite_factory is None:
            suite = types.ModuleType("checked_container_suite")
            suite.__file__ = str(directory / "scripts/test_containers.py")
            exec(compile(bundle["script"], suite.__file__, "exec"), suite.__dict__)
        else:
            suite = suite_factory()
        # Every child, including Docker and SSH, gets only this environment.
        checker = suite.ContainerChecks(env, token)
        checker.docker("info", timeout=20)
        # Successful listings prove every proposed name is fresh before a
        # timer can target it. A failed inspect cannot establish absence.
        names_absent(checker, token)
        for suffix in (".timer", ".service"):
            require(execute(["systemctl", "show", "--property=LoadState", "--value", unit + suffix], env=env, timeout=10).strip() == b"not-found",
                    "container_unit_collision")
        armed = True  # Retain recovery source even if timer creation is uncertain.
        execute(["systemd-run", "--quiet", "--unit=" + unit, "--on-active=" + str(WATCHDOG_SECONDS) + "s",
                 "--timer-property=AccuracySec=1s", "--property=RuntimeMaxSec=240s",
                 "/usr/bin/python3", "-I", str(cleanup_file)], env=env, timeout=20)
        require(execute(["systemctl", "is-active", unit + ".timer"], env=env, timeout=10).strip() == b"active", "container_watchdog_unverified")
        report["watchdog"] = "armed"
        signal.alarm(WORK_SECONDS)
        profiles = []
        for label, claude in (("legacy", None), ("showcase", bundle["pins"]["claude_release"])):
            phase = label
            work = directory / label
            work.mkdir(mode=0o700)
            context = work / "context"
            context.mkdir(mode=0o700)
            for name, content in bundle["contexts"][label].items():
                path = context / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text(content)
                path.chmod(0o600)
            row = checker.profile(work, bundle["pins"]["codex_release"], claude, "linux/amd64", context=context)
            profile_valid(row, label, bundle["pins"])
            profiles.append(row)
        report.update({"schema": 1, "status": "pass", "request_id": bundle["request_id"], "source_revision": bundle["source_revision"],
                       "bundle_sha256": sha, "boot_id": boot, "profiles": profiles, "model_execution": "not_tested"})
    except BaseException as error:
        diagnostics = failure(error, phase)
        report["status"] = "fail"
    finally:
        signal.alarm(CLEANUP_SECONDS)
        try:
            require(checker is not None and checker.cleanup(), "container_cleanup_unverified")
            names_absent(checker, token)
            report["cleanup"] = "pass"
            if armed:
                execute(["systemctl", "stop", unit + ".timer", unit + ".service"], env=env, timeout=20)
                require(execute(["systemctl", "show", "--property=ActiveState", "--value", unit + ".timer"], env=env, timeout=10).strip() == b"inactive",
                        "container_watchdog_disarm_unverified")
                report["watchdog"] = "disarmed"
            report["duration_seconds"] = round(now() - started, 3)
            if report["status"] == "pass":
                validate_proof(report, bundle, boot)
                success = True
        except BaseException as error:
            if diagnostics is None:
                diagnostics = failure(error, "cleanup")
            report["status"] = "fail"
        finally:
            signal.alarm(0)
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            # Leave the captured cleanup program for an armed timer on failure.
            if success or not armed or report["watchdog"] == "disarmed":
                shutil.rmtree(directory)
    if not success:
        result = {"schema": 1, "status": "fail", "cleanup": report["cleanup"], "watchdog": report["watchdog"]}
        if diagnostics is not None:
            result["diagnostics"] = diagnostics
        return result
    return report


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_BUNDLE + 1)
        require(len(raw) <= MAX_BUNDLE, "container_bundle_too_large")
        bundle = json.loads(raw)
        report = run_vm(bundle)
    except BaseException:
        report = {"schema": 1, "status": "fail", "cleanup": "uncertain", "watchdog": "unverified"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
