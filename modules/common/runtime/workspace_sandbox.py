#!/usr/bin/env python3
"""Install scoped Docker profiles needed by Codex's unprivileged bubblewrap.

Upstream defaults: moby/profiles 2ceae35d351c156cb5a8efc0fdc4a08cf94569d8.
See workspace_sandbox_LICENSE.txt. No host capability or unconfined mode is
added. Kernel capability checks still govern mounts outside a user namespace.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

PROFILE = "agent-stack-workspace-codex"
SECCOMP = "workspace-seccomp.json"
BUNDLE = "workspace-sandbox-profiles.json"
UPSTREAM = "2ceae35d351c156cb5a8efc0fdc4a08cf94569d8"
SOURCE_HASHES = {"seccomp": "6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6",
                 "apparmor": "976f9817805cbc43fa4a367feb57eceba081d3055f5cd5439cea16b6cbc9f203"}
NAMESPACE_SYSCALLS = ("clone", "unshare", "mount", "umount2", "pivot_root", "setns")
MARKER = "# AgentStack workspace confinement; managed by workspace-sandbox.py\n"


def require_root():
    if os.geteuid() != 0:
        raise ValueError("workspace_sandbox_root_required")


def checked_parent(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("workspace_sandbox_path_refused")
    info = path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("workspace_sandbox_path_refused")
    return path


def read_owned(path, missing=False):
    path = checked_parent(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing: return None
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 131072:
            raise ValueError("workspace_sandbox_file_refused")
        return stream.read(131073)


def replace_owned(path, data):
    path = checked_parent(path)
    read_owned(path, missing=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def render(bundle, abi4=False):
    if not isinstance(bundle, dict) or set(bundle) != {"schema", "upstream_revision", "seccomp", "apparmor"} or bundle["schema"] != 1 or bundle["upstream_revision"] != UPSTREAM:
        raise ValueError("workspace_sandbox_bundle_refused")
    for key, expected in SOURCE_HASHES.items():
        if not isinstance(bundle[key], str) or hashlib.sha256(bundle[key].encode()).hexdigest() != expected:
            raise ValueError("workspace_sandbox_bundle_refused")
    seccomp = json.loads(bundle["seccomp"])
    assert seccomp["defaultAction"] == "SCMP_ACT_ERRNO"
    # Keep conditional capability rules and clone3's ENOSYS fallback intact.
    seccomp["syscalls"].append({"names": list(NAMESPACE_SYSCALLS), "action": "SCMP_ACT_ALLOW"})
    apparmor = bundle["apparmor"].replace('"default"', '"' + PROFILE + '"')
    apparmor = apparmor.replace("  deny mount,", "  mount,\n  pivot_root,")
    if abi4:
        # ABI 4 separates Unix networking and mediates unprivileged userns.
        apparmor = apparmor.replace("  network,", "  network,\n  network unix,\n  userns,")
    apparmor = MARKER + "abi <abi/" + ("4.0" if abi4 else "3.0") + ">,\n" + apparmor
    return (json.dumps(seccomp, sort_keys=True, separators=(",", ":")) + "\n").encode(), apparmor.encode()


def parser(args):
    result = subprocess.run(["/usr/sbin/apparmor_parser", *args], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("workspace_sandbox_profile_load_failed")


def loaded(profiles_path):
    return PROFILE + " (enforce)" in Path(profiles_path).read_text().splitlines()


def install(app=Path("/opt/agent-stack"), apparmor_dir=Path("/etc/apparmor.d"),
            profiles_path=Path("/sys/kernel/security/apparmor/profiles")):
    require_root()
    app, apparmor_dir = Path(app), Path(apparmor_dir)
    seccomp_path, apparmor_path = app / SECCOMP, apparmor_dir / PROFILE
    old = {p: read_owned(p, missing=True) for p in (seccomp_path, apparmor_path)}
    if old[apparmor_path] is not None and not old[apparmor_path].startswith(MARKER.encode()):
        raise ValueError("workspace_sandbox_unmanaged_profile_refused")
    seccomp, apparmor = render(json.loads(read_owned(app / BUNDLE)), abi4=(apparmor_dir / "abi/4.0").is_file())
    try:
        replace_owned(apparmor_path, apparmor)
        parser(["-r", str(apparmor_path)])
        if not loaded(profiles_path):
            raise ValueError("workspace_sandbox_profile_not_enforcing")
        replace_owned(seccomp_path, seccomp)
    except Exception:
        # Restore both durable files. The caller refuses service startup.
        for path, before in old.items():
            if before is None:
                if path == apparmor_path:
                    try: parser(["-R", str(path)])
                    except Exception: pass
                path.unlink(missing_ok=True)
            else: replace_owned(path, before)
        if old[apparmor_path] is not None:
            try: parser(["-r", str(apparmor_path)])
            except Exception: pass
        raise
    return {"status": "workspace_sandbox_installed", "profile": PROFILE,
            "seccomp_sha256": hashlib.sha256(seccomp).hexdigest(), "apparmor_sha256": hashlib.sha256(apparmor).hexdigest()}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        print(json.dumps(install(), sort_keys=True))
    except Exception:
        print('{"status":"workspace_sandbox_install_failed"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
