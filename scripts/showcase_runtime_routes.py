#!/usr/bin/env python3
"""Reversible, owned-showcase runtime configuration; never a paid task runner.

Only the checked controller supplies packets on private stdin. An installed
route is not proof of native provider selection, generation or doctor success.
The original file is retained privately before any change and restoration
refuses to overwrite a file changed by another writer.
"""
import base64
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

ROOT = Path("/opt/agent-stack/data")
LIMIT = 262144
OWNERS = {"openclaw": 1000, "hermes": 10000}
FILES = {"openclaw": "openclaw/openclaw.json", "hermes": "hermes/config.yaml"}
OPENCLAW_SECRET_FILE = "openclaw/.as7-model-secrets.json"
OPENCLAW_SECRET_PATH = "/home/node/.openclaw/.as7-model-secrets.json"
BINDING = {"run_id", "source_revision", "boot_id", "checksum"}
ROUTE = {"role", "provider", "model", "base_url", "credential", "bridge_ipv4", "context_tokens", "output_tokens"}


def need(value):
    if not value: raise ValueError("runtime_config_route_unverified")


def decoded(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result); result[key] = value
        return result
    def constant(_): raise ValueError("runtime_config_route_unverified")
    need(isinstance(raw, bytes) and 0 < len(raw) <= LIMIT)
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    need(isinstance(value, dict))
    return value


def encoded(value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    need(len(raw) <= LIMIT)
    return raw


def sha(raw): return hashlib.sha256(raw).hexdigest()


def binding_valid(binding):
    need(isinstance(binding, dict) and set(binding) == BINDING and all(isinstance(v, str) for v in binding.values()))
    need(re.fullmatch(r"as7-[a-z0-9-]{8,30}", binding["run_id"])
         and re.fullmatch(r"[a-f0-9]{40}", binding["source_revision"])
         and re.fullmatch(r"[a-f0-9]{64}", binding["checksum"])
         and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", binding["boot_id"]))


def route_valid(route):
    need(isinstance(route, dict) and set(route) == ROUTE)
    need(all(isinstance(route[key], str) for key in ("role", "provider", "model", "base_url", "credential", "bridge_ipv4")))
    need(route["role"] in OWNERS and route["provider"] in ("openai", "anthropic")
         and isinstance(route["model"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,179}", route["model"])
         and isinstance(route["credential"], str) and re.fullmatch(r"[a-f0-9]{64}", route["credential"]))
    need(type(route["context_tokens"]) is int and 4096 <= route["context_tokens"] <= 10000000
         and type(route["output_tokens"]) is int and 16 <= route["output_tokens"] <= 4096)
    address = ipaddress.ip_address(route["bridge_ipv4"])
    need(address.version == 4 and any(address in ipaddress.ip_network(net) for net in
         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
    match = re.fullmatch(r"http://" + re.escape(str(address)) + r":([1-9][0-9]{0,4})/" + route["role"], route["base_url"])
    need(match and int(match[1]) <= 65535)


def compile_config(original, route, file_secrets=False):
    """Temporary single model/transport and no tools; retain app settings."""
    route_valid(route); need(isinstance(original, dict) and type(file_secrets) is bool)
    result = copy.deepcopy(original)
    provider, model, base = route["provider"], route["model"], route["base_url"]
    if route["role"] == "openclaw":
        agents = result.setdefault("agents", {}); need(isinstance(agents, dict) and not agents.get("list"))
        defaults = agents.setdefault("defaults", {}); need(isinstance(defaults, dict))
        # These can send a task to another runtime/endpoint. Do not guess how
        # to migrate an unexpected operator or image configuration.
        need(not any(defaults.get(key) for key in ("agentRuntime", "cliBackends", "compaction", "subagents")))
        result["models"] = {"mode": "replace", "providers": {provider: {
            "baseUrl": base + ("/v1" if provider == "openai" else ""),
            "apiKey": route["credential"], "auth": "api-key",
            "api": "openai-completions" if provider == "openai" else "anthropic-messages",
            "timeoutSeconds": 60,
            "models": [{"id": model, "name": model, "input": ["text"],
                        "contextWindow": route["context_tokens"], "maxTokens": route["output_tokens"]}]}}}
        reference = provider + "/" + model
        defaults["model"] = {"primary": reference, "fallbacks": []}
        defaults["models"] = {reference: {}}
        defaults["imageModel"] = {"primary": reference, "fallbacks": []}
        defaults["utilityModel"] = ""  # Pinned API: a defined empty value disables auxiliary utility routing.
        tools = result.setdefault("tools", {}); need(isinstance(tools, dict))
        tools["deny"] = ["*"]  # This arithmetic acceptance turn has no tools.
        # The arithmetic turn cannot use workshop or auxiliary memory models.
        skills = result.setdefault("skills", {}); need(isinstance(skills, dict))
        workshop = skills.setdefault("workshop", {}); need(isinstance(workshop, dict))
        workshop["autonomous"] = {"mode": "off"}
        result["memory"] = {"search": {"enabled": False}}
        plugins = result.setdefault("plugins", {}); need(isinstance(plugins, dict))
        slots = plugins.setdefault("slots", {}); entries = plugins.setdefault("entries", {})
        need(isinstance(slots, dict) and isinstance(entries, dict))
        slots["memory"] = "none"; entries["memory-core"] = {"enabled": False}
        if file_secrets:
            gateway = result.get("gateway"); need(isinstance(gateway, dict) and gateway.get("bind") == "loopback")
            auth = gateway.get("auth"); need(isinstance(auth, dict) and auth.get("mode") == "token"
                and isinstance(auth.get("token"), str) and 0 < len(auth["token"]) <= 4096)
            origins = gateway.get("controlUi", {}).get("allowedOrigins")
            need(isinstance(origins, list) and all(isinstance(value, str) for value in origins))
            tailnet = [value for value in origins if re.fullmatch(
                r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net", value)]
            need(len(tailnet) == 1 and gateway.get("publicOrigin") in (None, tailnet[0]) and not result.get("secrets"))
            gateway["publicOrigin"] = tailnet[0]
            result["secrets"] = {"providers": {"as7-budget": {"source": "file", "path": OPENCLAW_SECRET_PATH, "mode": "json"}}}
            auth["token"] = {"source": "file", "provider": "as7-budget", "id": "/gateway"}
            result["models"]["providers"][provider]["apiKey"] = {"source": "file", "provider": "as7-budget", "id": "/model"}
    else:
        # A dedicated name avoids the builtin Anthropic route and its env/OAuth
        # resolution. Native resolution must still be independently measured.
        need(not result.get("model_routes") and not result.get("model_aliases") and not result.get("secrets"))
        result["model"] = {"provider": "as7-budget", "default": model,
            "base_url": base + ("/v1" if provider == "openai" else ""), "api_key": route["credential"],
            "api_mode": "chat_completions" if provider == "openai" else "anthropic_messages"}
        result["providers"] = {"as7-budget": {"api": result["model"]["base_url"], "api_key": route["credential"],
            "transport": "openai_chat" if provider == "openai" else "anthropic_messages",
            "discover_models": False, "models": [model]}}
        result.pop("custom_providers", None)
        result["fallback_providers"] = []
        # Existing explicitly routed auxiliary slots can escape the main route.
        need(not result.get("auxiliary"))
        platforms = result.setdefault("platform_toolsets", {}); need(isinstance(platforms, dict))
        platforms["api_server"] = []
        result["mcp_servers"] = {}
    encoded(result)
    return result


def checked_path(path, owner=None, directory=False):
    path = Path(path)
    for parent in (path, *path.parents): need(not parent.is_symlink())
    info = path.stat()
    need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= LIMIT)
    if owner is not None:
        need(info.st_uid == owner and info.st_gid == owner and
             stat.S_IMODE(info.st_mode) in ((0o700,) if directory else (0o600, 0o640)))
    return info


def file_version(info):
    """Reads may update access time; identity and edits must still agree."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def fsync_dir(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def atomic_write(path, raw, uid, gid, mode, exclusive=False):
    checked_path(path.parent, directory=True)
    need(not path.is_symlink())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".as7-route-", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode); os.fchown(handle.fileno(), uid, gid)
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        if exclusive:
            os.link(temporary, path); temporary.unlink()
        else: temporary.replace(path)
        fsync_dir(path.parent)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


class ConfigurationTransaction:
    """Root-only VM file transaction; no credentials, HTTP or Docker execution."""
    def __init__(self, root=ROOT):
        need(os.geteuid() == 0)
        self.root = Path(root)
        checked_path(self.root, directory=True)
        self.journal = self.root / ".as7-runtime-routes"
        need(not self.journal.is_symlink())
        self.journal.mkdir(mode=0o700, exist_ok=True)
        checked_path(self.journal, owner=0, directory=True)
        fsync_dir(self.root)

    @contextmanager
    def lock(self):
        fd = os.open(self.journal / "transaction.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == 0
                 and info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally: os.close(fd)

    def install(self, binding, route):
        with self.lock(): return self._install(binding, route)

    def _install(self, binding, route, source=None, file_secrets=False):
        binding_valid(binding); route_valid(route)
        role = route["role"]; target = self.root / FILES[role]; record = self.journal / (role + ".json")
        need(not record.exists() and not record.is_symlink())
        info = checked_path(target, owner=OWNERS[role]); before = target.read_bytes()
        if source is None:
            original = decoded(before)
        else:
            # YAML is decoded inside the captured pinned Hermes container. The
            # exact bytes must still match after it stops, before any journal
            # or replacement is written. No host YAML dependency is needed.
            need(role == "hermes" and isinstance(source, dict) and set(source) == {"config", "config_sha256"}
                 and source["config_sha256"] == sha(before) and isinstance(source["config"], dict))
            original = source["config"]
        need(type(file_secrets) is bool and (not file_secrets or role == "openclaw"))
        after = encoded(compile_config(original, route, file_secrets=file_secrets))
        snapshot = {"schema": 1, "role": role, "binding": binding, "original": base64.b64encode(before).decode(),
            "original_sha256": sha(before), "installed_sha256": sha(after), "uid": info.st_uid, "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode)}
        secret_target = self.root / OPENCLAW_SECRET_FILE
        if file_secrets:
            need(not secret_target.exists() and not secret_target.is_symlink())
            secret_raw = encoded({"gateway": original["gateway"]["auth"]["token"], "model": route["credential"]})
            snapshot["secret_file_sha256"] = sha(secret_raw)
        packet = encoded(snapshot); need(len(packet) <= LIMIT)
        atomic_write(record, packet, 0, 0, 0o600, exclusive=True)
        # A crash here retains restoration authority, whether the original or
        # installed file is observed. No operation silently repeats install.
        need(file_version(checked_path(target, owner=OWNERS[role])) == file_version(info) and target.read_bytes() == before)
        if file_secrets:
            atomic_write(secret_target, secret_raw, info.st_uid, info.st_gid, 0o600, exclusive=True)
            self.verify_secret_file(binding)
        atomic_write(target, after, info.st_uid, info.st_gid, 0o600)
        need(target.read_bytes() == after)
        return {"schema": 1, "status": "runtime_config_installed", "role": role, "binding": binding,
                "config_sha256": sha(after), "base_url_sha256": sha(route["base_url"].encode()), "native_contract": "unverified"}

    def verify_secret_file(self, binding):
        record = self.journal / "openclaw.json"; info = checked_path(record, owner=0)
        need(stat.S_IMODE(info.st_mode) == 0o600)
        snapshot = decoded(record.read_bytes())
        need(snapshot.get("binding") == binding and snapshot.get("role") == "openclaw"
             and type(snapshot.get("schema")) is int and snapshot["schema"] == 1
             and isinstance(snapshot.get("secret_file_sha256"), str)
             and re.fullmatch(r"[a-f0-9]{64}", snapshot["secret_file_sha256"]))
        target = self.root / OPENCLAW_SECRET_FILE; info = checked_path(target, owner=OWNERS["openclaw"])
        need(stat.S_IMODE(info.st_mode) == 0o600 and info.st_gid == OWNERS["openclaw"]
             and sha(target.read_bytes()) == snapshot["secret_file_sha256"])
        return target

    def restore(self, binding, role):
        with self.lock(): return self._restore(binding, role)

    def _restore(self, binding, role):
        binding_valid(binding); need(role in OWNERS)
        record = self.journal / (role + ".json"); info = checked_path(record, owner=0)
        need(stat.S_IMODE(info.st_mode) == 0o600)
        snapshot = decoded(record.read_bytes())
        fields = {"schema", "role", "binding", "original", "original_sha256", "installed_sha256", "uid", "gid", "mode"}
        file_secrets = "secret_file_sha256" in snapshot
        need(set(snapshot) == (fields | {"secret_file_sha256"} if file_secrets else fields)
             and (not file_secrets or role == "openclaw" and isinstance(snapshot["secret_file_sha256"], str)
                  and re.fullmatch(r"[a-f0-9]{64}", snapshot["secret_file_sha256"]))
             and type(snapshot["schema"]) is int and snapshot["schema"] == 1 and snapshot["role"] == role
             and snapshot["binding"] == binding and snapshot["uid"] == OWNERS[role] and type(snapshot["uid"]) is int
             and snapshot["gid"] == OWNERS[role] and type(snapshot["gid"]) is int
             and type(snapshot["mode"]) is int and snapshot["mode"] in (0o600, 0o640))
        before = base64.b64decode(snapshot["original"], validate=True)
        need(0 < len(before) <= LIMIT and sha(before) == snapshot["original_sha256"]
             and re.fullmatch(r"[a-f0-9]{64}", snapshot["installed_sha256"]))
        target = self.root / FILES[role]; checked_path(target, owner=OWNERS[role])
        need(sha(target.read_bytes()) in (snapshot["original_sha256"], snapshot["installed_sha256"]))
        secret_target = self.root / OPENCLAW_SECRET_FILE
        if file_secrets:
            need(not secret_target.is_symlink())
            if secret_target.exists(): self.verify_secret_file(binding)
        atomic_write(target, before, snapshot["uid"], snapshot["gid"], snapshot["mode"])
        need(target.read_bytes() == before)
        if file_secrets and secret_target.exists():
            self.verify_secret_file(binding).unlink(); fsync_dir(secret_target.parent)
        record.unlink(); fsync_dir(self.journal)
        return {"schema": 1, "status": "runtime_config_restored", "role": role, "binding": binding,
                "original_sha256": sha(before)}


def inactive_runtime(role):
    """No writes while a native runtime can edit its own configuration."""
    need(role in OWNERS)
    def run(args):
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=True)
        need(len(result.stdout) <= 4096 and len(result.stderr) <= 4096)
        return result.stdout
    identifier = run(["/usr/bin/docker", "compose", "-f", "/opt/agent-stack/docker-compose.yml", "ps", "-a", "-q", role]).decode().strip()
    need(re.fullmatch(r"[a-f0-9]{64}", identifier))
    state = decoded(run(["/usr/bin/docker", "inspect", "--format", "{{json .State}}", identifier]))
    need(state.get("Running") is False and state.get("Restarting") is False)


def main():
    try:
        need(os.geteuid() == 0)
        packet = decoded(sys.stdin.buffer.read(LIMIT + 1))
        need(set(packet) in ({"action", "binding", "route"}, {"action", "binding", "role"}))
        binding_valid(packet["binding"])
        need(Path("/proc/sys/kernel/random/boot_id").read_text().strip() == packet["binding"]["boot_id"])
        applied = decoded(Path("/opt/agent-stack/.last-apply.json").read_bytes())
        need(applied.get("status") == "success" and applied.get("checksum") == packet["binding"]["checksum"]
             and sorted(applied.get("services", [])) == ["hermes", "openclaw", "workspace"])
        need((packet["action"] == "install" and "route" in packet) or
             (packet["action"] == "restore" and "role" in packet))
        role = packet["route"]["role"] if packet["action"] == "install" else packet["role"]
        inactive_runtime(role)
        transaction = ConfigurationTransaction()
        if packet["action"] == "install" and "route" in packet:
            report = transaction.install(packet["binding"], packet["route"])
        else:
            need(packet["action"] == "restore" and "role" in packet)
            report = transaction.restore(packet["binding"], packet["role"])
    except Exception:
        report = {"schema": 1, "status": "runtime_config_route_unverified"}
    print(json.dumps(report))
    return 1 if report["status"] == "runtime_config_route_unverified" else 0


if __name__ == "__main__": sys.exit(main())
