"""Free native confinement check; never loads credentials or calls a model."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

CHECKS = ("inside_write", "outside_denied", "git_denied", "codex_denied")


def invoke(args, directory, env):
    child = subprocess.Popen(args, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        output, errors = child.communicate(timeout=40)
        assert child.returncode == 0
        return output, errors
    except subprocess.TimeoutExpired:
        # A failed free probe must not leave nested sandbox processes running.
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        child.communicate()
        raise


def verify(report, nonce):
    assert isinstance(nonce, str) and re.fullmatch(r"[a-f0-9]{32}", nonce)
    assert isinstance(report, dict) and type(report.get("schema")) is int and report["schema"] == 1 and report.get("status") == "workspace_sandbox_verified"
    assert report.get("nonce") == nonce and type(report.get("uid")) is int and report["uid"] == 1000
    assert type(report.get("paid_requests")) is int and report["paid_requests"] == 0
    assert isinstance(report.get("checks"), dict) and set(report["checks"]) == set(CHECKS)
    assert all(report["checks"][name] is True for name in CHECKS)
    assert report.get("unchanged_sentinels") is True and report.get("capabilities_zero") is True
    assert report.get("seccomp_enabled") is True and report.get("no_new_privileges") is True
    assert report.get("apparmor") == "agent-stack-workspace-codex (enforce)"
    assert all(isinstance(report.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", report[key]) for key in ("inside_sha256", "stdout_sha256", "stderr_sha256"))
    assert report["inside_sha256"] == hashlib.sha256(nonce.encode()).hexdigest()
    assert isinstance(report.get("boot_id"), str) and re.fullmatch(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", report["boot_id"])
    return report


def run(nonce):
    assert os.geteuid() == 1000 and isinstance(nonce, str) and re.fullmatch(r"[a-f0-9]{32}", nonce)
    home = Path(os.environ["HOME"])
    assert home.is_dir() and not home.is_symlink() and home.stat().st_uid == 1000
    parent = home / ".agent-stack/confinement"
    assert not any(p.is_symlink() for p in (parent, *parent.parents))
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert parent.stat().st_uid == 1000 and parent.stat().st_mode & 0o077 == 0
    with tempfile.TemporaryDirectory(prefix="check-", dir=parent) as temporary:
        root = Path(temporary); workspace = root / "workspace"; workspace.mkdir(mode=0o700)
        client = root / "client"; client.mkdir(mode=0o700)
        (workspace / "input.json").write_text(json.dumps({"nonce": nonce}))
        sentinels = [root / "outside.txt"]
        for name in (".git", ".codex"):
            (workspace / name).mkdir(mode=0o700); sentinels.append(workspace / name / "marker")
        for path in sentinels: path.write_text(nonce)
        code = """import errno,json,os
from pathlib import Path
x=json.loads(Path('input.json').read_text());Path('inside.txt').write_text(x['nonce'])
r={'inside_write':True,'uid':os.geteuid()}
for name,path in [('outside_denied',Path('../outside.txt')),('git_denied',Path('.git/marker')),('codex_denied',Path('.codex/marker'))]:
 try:path.write_text('changed');r[name]=False
 except OSError as e:r[name]=e.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
print(json.dumps(r))
"""
        # Fresh config and no saved auth, injected proxies or preload settings.
        env = {"HOME": str(client), "CODEX_HOME": str(client), "PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
        output, errors = invoke(["codex", "sandbox", "-P", ":workspace", "-C", str(workspace), "--", "python3", "-c", code], workspace, env)
        result = json.loads(output); assert result.pop("uid") == 1000
        inside = workspace / "inside.txt"
        assert inside.is_file() and not inside.is_symlink() and inside.stat().st_nlink == 1 and inside.read_text() == nonce
        unchanged = all(p.is_file() and not p.is_symlink() and p.read_text() == nonce for p in sentinels)
        fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        report = {"schema": 1, "status": "workspace_sandbox_verified", "nonce": nonce, "uid": os.geteuid(), "paid_requests": 0,
                  "checks": result, "unchanged_sentinels": unchanged, "capabilities_zero": int(fields["CapEff"].strip(), 16) == 0,
                  "seccomp_enabled": fields["Seccomp"].strip() == "2", "no_new_privileges": fields["NoNewPrivs"].strip() == "1",
                  "apparmor": Path("/proc/self/attr/current").read_text().strip(), "inside_sha256": hashlib.sha256(inside.read_bytes()).hexdigest(),
                  "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                  "stdout_sha256": hashlib.sha256(output).hexdigest(), "stderr_sha256": hashlib.sha256(errors).hexdigest()}
        return verify(report, nonce)


def main():
    try:
        request = json.load(sys.stdin)
        assert set(request) == {"nonce"}
        print(json.dumps(run(request["nonce"]), sort_keys=True))
        return 0
    except Exception:
        print('{"schema":1,"status":"workspace_sandbox_failed","paid_requests":0}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
