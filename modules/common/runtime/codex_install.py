#!/usr/bin/env python3
"""Install Codex standalone, with a verified official npm native fallback.

Only a missing standalone endpoint (HTTP 404/410) permits the fallback. An
installer execution failure, auth/TLS error or invalid npm artifact is fatal.
No Node/npm, package lifecycle script, login or model request is needed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request

INSTALLER = "https://chatgpt.com/codex/install.sh"
REGISTRY = "https://registry.npmjs.org/@openai/codex/"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta)(?:\.[0-9]+)?)?\Z")
STABLE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
PLATFORMS = {
    ("Linux", "x86_64"): ("linux", "x64", "x86_64-unknown-linux-musl"),
    ("Linux", "aarch64"): ("linux", "arm64", "aarch64-unknown-linux-musl"),
    ("Linux", "arm64"): ("linux", "arm64", "aarch64-unknown-linux-musl"),
    ("Darwin", "x86_64"): ("darwin", "x64", "x86_64-apple-darwin"),
    ("Darwin", "arm64"): ("darwin", "arm64", "aarch64-apple-darwin"),
}


def need(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        need(newurl.startswith("https://"), "insecure Codex download redirect")
        if req.full_url.startswith(REGISTRY):
            need(newurl.startswith(REGISTRY), "npm download left official package origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, target: Path, limit: int) -> None:
    opener = urllib.request.build_opener(HTTPSOnly())
    request = urllib.request.Request(url, headers={"User-Agent": "AgentStack-Codex-installer"})
    with opener.open(request, timeout=60) as response, target.open("xb") as output:
        need(response.status == 200, "Codex download was not successful")
        size = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            need(size <= limit, "Codex download exceeds size limit")
            output.write(chunk)


def metadata(selector: str, directory: Path, filename: str) -> dict:
    target = directory / filename
    download(REGISTRY + selector, target, 2 * 1024 * 1024)
    value = json.loads(target.read_bytes())
    need(type(value) is dict and value.get("name") == "@openai/codex", "unexpected npm package")
    return value


def select_release(release: str, value: dict) -> str:
    version = value.get("version")
    need(type(version) is str and VERSION.fullmatch(version), "invalid Codex version metadata")
    if release in ("stable", "latest"):
        need(STABLE.fullmatch(version), "stable Codex resolved to a prerelease")
    elif release in ("alpha", "beta"):
        need(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-" + release + r"(?:\.[0-9]+)?", version), "wrong Codex release channel")
    else:
        need(version == release, "explicit Codex release changed")
    return version


def verified_archive(archive: Path, integrity: str) -> None:
    need(type(integrity) is str and integrity.startswith("sha512-"), "npm SHA512 integrity required")
    expected = base64.b64decode(integrity[7:], validate=True)
    need(len(expected) == 64, "invalid npm SHA512 integrity")
    digest = hashlib.sha512()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    need(hmac.compare_digest(digest.digest(), expected), "Codex npm integrity mismatch")


def archive_members(bundle: tarfile.TarFile, triple: str) -> list[tarfile.TarInfo]:
    members = []
    seen, total = set(), 0
    prefix = "package/vendor/" + triple + "/"
    for member in bundle:
        members.append(member)
        need(len(members) <= 4096, "unexpected Codex archive member count")
        name = PurePosixPath(member.name)
        need(not name.is_absolute() and ".." not in name.parts and "\\" not in member.name
             and name.parts and name.parts[0] == "package", "unsafe Codex archive path")
        canonical = str(name)
        need(canonical not in seen, "duplicate Codex archive path")
        seen.add(canonical)
        need(member.isfile() or member.isdir(), "Codex archive links/devices are forbidden")
        need(member.size <= 1024**3, "Codex archive file too large")
        total += member.size
        need(total <= 2 * 1024**3, "Codex archive expands beyond limit")
        if member.isfile() and member.name.startswith("package/vendor/"):
            need(member.name.startswith(prefix), "wrong native platform in Codex archive")
    need(members, "empty Codex archive")
    return members


def unpack_bundle(archive: Path, target: Path, native_version: str, os_name: str, cpu: str, triple: str) -> None:
    prefix = "package/vendor/" + triple + "/"
    with tarfile.open(archive, "r:gz") as bundle:
        members = archive_members(bundle, triple)
        packages = [m for m in members if m.name == "package/package.json" and m.isfile()]
        need(len(packages) == 1 and packages[0].size <= 65536, "native npm package manifest missing")
        package = json.loads(bundle.extractfile(packages[0]).read())
        need(package.get("name") == "@openai/codex" and package.get("version") == native_version
             and package.get("os") == [os_name] and package.get("cpu") == [cpu], "native npm manifest identity mismatch")
        for member in members:
            if not member.name.startswith(prefix):
                continue
            relative = PurePosixPath(member.name[len(prefix):])
            if str(relative) == ".":
                continue
            destination = target.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with bundle.extractfile(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
    # Older official native bundles used codex/codex and path/rg. Preserve all
    # their files while normalizing to the standalone bin/codex contract.
    legacy = target / "codex"
    if not (target / "bin/codex").exists() and legacy.is_dir():
        need((legacy / "codex").is_file(), "native Codex executable missing")
        legacy.rename(target / "bin")
        if (target / "path").is_dir() and not (target / "codex-path").exists():
            (target / "path").rename(target / "codex-path")
    binary = target / "bin/codex"
    need(binary.is_file() and os.access(binary, os.X_OK) and not (target / "codex").exists(), "invalid standalone Codex bundle")
    (target / "codex").symlink_to("bin/codex")
    target.chmod(0o755)
    for directory in target.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o755)


def version_of(binary: Path, home: Path) -> str:
    result = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True, timeout=30,
                            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home)})
    need(len(result.stdout) <= 4096, "Codex version output too large")
    match = re.fullmatch(r"codex-cli ([^\s]+)\s*", result.stdout)
    need(match is not None and VERSION.fullmatch(match[1]), "Codex executable version unavailable")
    return match[1]


def point_link(path: Path, target: Path) -> None:
    need(not path.exists() or path.is_symlink(), "refusing to overwrite a non-symlink Codex path")
    temporary = path.with_name("." + path.name + ".codex-install-" + str(os.getpid()))
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(mode=0o755)
        path.chmod(0o755)
    need(path.is_dir() and not path.is_symlink(), "invalid Codex installation directory")


def install_native(home: Path, bin_dir: Path, release: str, temporary: Path) -> None:
    platform_key = (platform.system(), platform.machine())
    need(platform_key in PLATFORMS, "unsupported Codex native platform")
    os_name, cpu, triple = PLATFORMS[platform_key]
    selector = "latest" if release == "stable" else release
    main = metadata(selector, temporary, "package.json")
    version = select_release(release, main)
    native_version = version + "-" + os_name + "-" + cpu
    alias = main.get("optionalDependencies", {}).get("@openai/codex-" + os_name + "-" + cpu)
    need(alias == "npm:@openai/codex@" + native_version, "official native Codex alias missing")
    native = metadata(native_version, temporary, "native-package.json")
    need(native.get("version") == native_version and native.get("os") == [os_name]
         and native.get("cpu") == [cpu], "wrong native npm metadata")
    url = REGISTRY + "-/codex-" + native_version + ".tgz"
    need(native["dist"]["tarball"] == url, "unexpected Codex native artifact URL")
    archive = temporary / "native.tgz"
    download(url, archive, 512 * 1024 * 1024)
    verified_archive(archive, native["dist"]["integrity"])
    stage = temporary / "release"
    stage.mkdir(mode=0o755)
    unpack_bundle(archive, stage, native_version, os_name, cpu, triple)
    need(version_of(stage / "bin/codex", home) == version, "native Codex executable version mismatch")
    standalone = home / "packages/standalone"
    releases = standalone / "releases"
    for directory in (home / "packages", standalone, releases):
        install_directory(directory)
    destination = releases / version
    need(not destination.exists() and not destination.is_symlink(), "Codex release already exists; refusing replacement")
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    install_directory(bin_dir)
    need(not (standalone / "current").exists() or (standalone / "current").is_symlink(), "invalid Codex current path")
    need(not (bin_dir / "codex").exists() or (bin_dir / "codex").is_symlink(), "invalid Codex command path")
    stage.rename(destination)
    point_link(standalone / "current", destination)
    point_link(bin_dir / "codex", standalone / "current/bin/codex")
    need(version_of(bin_dir / "codex", home) == version, "installed Codex readiness check failed")


def install(home: Path, bin_dir: Path, release: str) -> None:
    need(release in ("stable", "latest", "alpha", "beta") or VERSION.fullmatch(release), "invalid CODEX_RELEASE")
    home.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    install_directory(home)
    # Native artifacts are large: keep staging on the install filesystem, not /tmp.
    with tempfile.TemporaryDirectory(prefix=".codex-install-", dir=home) as directory:
        temporary = Path(directory)
        script = temporary / "install.sh"
        try:
            download(INSTALLER, script, 2 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in (404, 410):
                raise
            print("[codex] Standalone endpoint unavailable; verifying official npm native bundle.", flush=True)
            install_native(home, bin_dir, release, temporary)
            return
        # AgentStack calls the stable channel "stable"; the official script calls it "latest".
        installer_release = "latest" if release == "stable" else release
        env = dict(os.environ, CODEX_NON_INTERACTIVE="1", CODEX_HOME=str(home), CODEX_INSTALL_DIR=str(bin_dir), CODEX_RELEASE=installer_release)
        subprocess.run(["sh", str(script)], check=True, env=env, timeout=300)
        version = version_of(home / "packages/standalone/current/bin/codex", home)
        select_release(release, {"version": version})
        need(version_of(bin_dir / "codex", home) == version, "standalone Codex command not ready")


def main() -> None:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    bin_dir = Path(os.environ.get("CODEX_INSTALL_DIR", str(Path.home() / ".local/bin")))
    install(home, bin_dir, os.environ.get("CODEX_RELEASE", "stable"))


if __name__ == "__main__":
    main()
