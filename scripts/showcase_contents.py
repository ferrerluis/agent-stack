"""Observe resolved images, installed host tools and Hermes's startup repair.

Executed on the owned VM via pinned repo SSH. No image builds, downloads,
credentials, mutable tags or recipes can manufacture this observation.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    # Ubuntu 22.04 uses Python 3.10, before hashlib.file_digest exists.
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def tree_hash(root, names):
    """Hash actual bytes, executable modes and symlink destinations, not mtimes.

    Workspace links are recorded without traversing them; their targets are
    covered by the image layers or another explicitly hashed repair tree.
    Refuse changing files and unsupported filesystem entries.
    """
    root = Path(root); rows = []
    for name in sorted(names):
        base = root / name
        assert base.exists() and not base.is_symlink()
        paths = [base] + sorted(base.rglob("*")) if base.is_dir() else [base]
        for path in paths:
            before = path.lstat(); mode = stat.S_IMODE(before.st_mode)
            relative = str(path.relative_to(root))
            if stat.S_ISLNK(before.st_mode):
                target = os.readlink(path)
                # npm workspace links must remain inside the pinned source tree.
                assert path.resolve().is_relative_to(root.resolve())
                rows.append([relative, "link", target])
            elif stat.S_ISREG(before.st_mode):
                rows.append([relative, "file", mode, file_hash(path)])
            else:
                assert stat.S_ISDIR(before.st_mode)
                rows.append([relative, "dir", mode])
            after = path.lstat()
            assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)
    assert rows
    return digest(rows)


# npm ci mutates root and both workspaces; include every installed dependency,
# workspace source/build output, manifests and the repair verification marker.
HERMES_PATHS = ("package.json", "package-lock.json", "node_modules", "web", "ui-tui",
                "hermes_cli/web_dist", ".agent-stack-npm-patch.json")


def run(*args, input=None, timeout=30):
    options = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(args, **options, capture_output=True, check=True, timeout=timeout).stdout


def containers():
    result = {}
    for role in ("openclaw", "hermes", "workspace"):
        ids = run("docker", "compose", "-f", "/opt/agent-stack/docker-compose.yml", "ps", "-a", "-q", role).split()
        assert len(ids) == 1
        row = json.loads(run("docker", "inspect", ids[0].decode()))[0]
        assert row["State"]["Running"] and not any(row["State"][k] for k in ("OOMKilled", "Dead", "Restarting"))
        assert row["Config"]["Labels"]["com.docker.compose.service"] == role
        assert row["Config"]["Labels"]["com.docker.compose.project"] == "agent-stack"
        result[role] = {"container_id": row["Id"], "image_id": row["Image"]}
    return result


def observe(binding, source, boot_image):
    assert os.geteuid() == 0
    applied = json.loads(Path("/opt/agent-stack/.last-apply.json").read_bytes())
    assert applied["status"] == "success" and applied["checksum"] == binding["checksum"]
    before = containers(); images = {}
    for role, row in before.items():
        image = json.loads(run("docker", "image", "inspect", row["image_id"]))[0]
        assert image["Id"] == row["image_id"]
        images[role] = {"image_id": image["Id"], "layers": image["RootFS"]["Layers"],
                        "os": image["Os"], "architecture": image["Architecture"], "variant": image.get("Variant", "")}
    assert isinstance(source, bytes) and source
    repair = run("docker", "exec", "-i", "--user", "0", before["hermes"]["container_id"],
                 "python3", "-B", "-I", "-", "--hermes-tree", input=source, timeout=180).decode().strip()
    assert len(repair) == 64
    packages = run("dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\n")
    host = {"packages_sha256": hashlib.sha256(packages).hexdigest(), "kernel": os.uname().release,
            "os_release_sha256": file_hash(Path("/etc/os-release")),
            "tools": {name: file_hash(Path(name)) for name in ("/usr/bin/docker", "/usr/bin/tailscale", "/usr/sbin/sshd")}}
    assert before == containers()
    assert packages == run("dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\n")
    return {"schema": 1, "status": "resolved_contents_verified", "binding": binding,
            "containers": before, "images": images, "hermes_repair_sha256": repair, "host": host, "boot_image": boot_image}


def main():
    if sys.argv[1:] == ["--hermes-tree"]:
        first = tree_hash("/opt/hermes", HERMES_PATHS)
        assert first == tree_hash("/opt/hermes", HERMES_PATHS)
        print(first)
        return
    # The controller passes checked source as base64 in stdin JSON, not disk.
    import base64
    request = json.load(sys.stdin)
    source = base64.b64decode(request["source"], validate=True)
    print(json.dumps(observe(request["binding"], source, request["boot_image"]), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"schema":1,"status":"resolved_contents_failed"}')
        sys.exit(1)
