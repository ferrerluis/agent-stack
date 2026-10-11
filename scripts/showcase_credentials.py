"""Explicit temporary test credentials; ordinary deployments still use op://.

The sole file root is the user-authorized TODO7 setup, never an arbitrary path
from an input file or environment variable. Contents never enter diagnostics.
"""
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

AUTH_ROOT = Path(__file__).resolve().parents[1] / ".showcase-runs/cloud-setup-01a10f17/auth"
MAX_SECRET = 1024 * 1024
_refresh_lock = threading.Lock()
_refreshed_at = None


def refresh_access_token(reference):
    """The authorized OAuth helper owns renewal; API tokens last one hour."""
    global _refreshed_at
    if file_path(reference) != AUTH_ROOT / "tailscale-access-token":
        return
    with _refresh_lock:
        if _refreshed_at is not None and time.monotonic() - _refreshed_at < 3000:
            return
        helper = AUTH_ROOT.parent / "scripts/tailscale-cli.py"
        try:
            if helper.is_symlink() or helper.stat().st_uid != os.geteuid() or not helper.is_file():
                raise ValueError("temporary_credential_refresh_failed")
            result = subprocess.run([sys.executable, "-B", str(helper), "refresh"],
                                    capture_output=True, timeout=45, check=False)
            if result.returncode != 0:
                raise ValueError("temporary_credential_refresh_failed")
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("temporary_credential_refresh_failed") from None
        _refreshed_at = time.monotonic()


def file_path(reference):
    if not isinstance(reference, str) or not reference.startswith("file:///"):
        return None
    parts = urlsplit(reference)
    if (parts.scheme != "file" or parts.netloc or parts.query or parts.fragment
            or re.search(r"[%\s\x00-\x1f\x7f]", reference)):
        return None
    path = Path(parts.path)
    if (not path.is_absolute() or ".." in parts.path.split("/")
            or "." in parts.path.split("/") or "//" in parts.path
            or not path.is_relative_to(AUTH_ROOT) or path == AUTH_ROOT):
        return None
    return path


def read_file(reference):
    """Open each component without following links, then check the actual FD."""
    path = file_path(reference)
    if path is None:
        raise ValueError("temporary_credential_refused")
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        current = Path("/")
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            current /= component
            if current == AUTH_ROOT or current.is_relative_to(AUTH_ROOT):
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("temporary_credential_refused")
        secret_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(secret_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or not 0 < before.st_size <= MAX_SECRET):
                raise ValueError("temporary_credential_refused")
            raw = stream.read(MAX_SECRET + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("temporary_credential_refused")
            value = raw.decode("utf-8").rstrip("\r\n")
            if not value:
                raise ValueError("temporary_credential_refused")
            return value
    except (OSError, UnicodeError):
        raise ValueError("temporary_credential_refused") from None
    finally:
        if fd is not None:
            os.close(fd)
