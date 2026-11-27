#!/usr/bin/env python3
"""Versioned, credential-free first setup; never hires, resumes, or wakes agents.

The temporary trusted listener is confined to this companion container's
loopback interface. It is never used again once initialization is complete.
The public service always requires Paperclip authentication. Mesh ingress is
an operator-approved, separate operation.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid


class SetupError(RuntimeError):
    pass


def write_private(path: Path, value: str) -> None:
    if path.is_symlink():
        raise SetupError(f"Refusing a symlink: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    write_private(path, json.dumps(value, indent=2) + "\n")


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise SetupError(f"Refusing a symlink: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SetupError(f"Expected an object: {path}")
    return value


class Runtime:
    def __init__(self, home: Path, command: tuple[str, ...] = ("/usr/local/bin/paperclipai",)):
        self.home = home
        self.root = home / ".paperclip"
        self.instance = self.root / "instances/default"
        self.config_path = self.instance / "config.json"
        self.state_path = self.root / "agent-stack-bootstrap.json"
        self.codex_home = self.instance / "codex-home"
        self.log_path = self.instance / "logs/agent-stack-launch.log"
        self.command = command
        self.child: subprocess.Popen | None = None

    def environment(self) -> dict[str, str]:
        # Do not inherit workspace passwords, model keys, provider profiles, or
        # an existing Codex login. The CLI loads only this instance's own .env.
        return {
            "HOME": str(self.home),
            "USER": self.home.name,
            "LOGNAME": self.home.name,
            "PATH": f"{self.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PAPERCLIP_HOME": str(self.root),
            "PAPERCLIP_INSTANCE_ID": "default",
            "CODEX_HOME": str(self.codex_home),
            "PAPERCLIP_NO_BROWSER": "true",
            "PAPERCLIP_OPEN_ON_LISTEN": "false",
        }

    def fresh_config(self, *, bootstrap: bool) -> dict:
        return {
            "$meta": {"version": 1, "source": "onboard", "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            "database": {
                "mode": "embedded-postgres",
                "embeddedPostgresDataDir": str(self.instance / "db"),
                "embeddedPostgresPort": 54329,
                "backup": {"enabled": True, "intervalMinutes": 60, "retentionDays": 7, "dir": str(self.instance / "data/backups")},
            },
            "logging": {"mode": "file", "logDir": str(self.instance / "logs")},
            "server": {
                "deploymentMode": "local_trusted" if bootstrap else "authenticated",
                "exposure": "private",
                "bind": "loopback" if bootstrap else "lan",
                "host": "127.0.0.1" if bootstrap else "0.0.0.0",
                "port": 3101 if bootstrap else 3100,
                "allowedHostnames": ["localhost", "127.0.0.1"] + ([] if bootstrap else ["paperclip"]),
                "serveUi": not bootstrap,
            },
            "auth": {"baseUrlMode": "auto", "disableSignUp": False},
            "storage": {"provider": "local_disk", "localDisk": {"baseDir": str(self.instance / "data/storage")}},
            "secrets": {"provider": "local_encrypted", "strictMode": True, "localEncrypted": {"keyFilePath": str(self.instance / "secrets/master.key")}},
            "telemetry": {"enabled": False},
            "updates": {"checkEnabled": False},
        }

    def prepare(self) -> dict:
        if any(path.is_symlink() for path in (self.root, self.root / "instances", self.instance)):
            raise SetupError("Paperclip directories must not be symlinks")
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.state_path.exists():
            state = read_json(self.state_path)
            if state.get("schema") != 1 or state.get("phase") not in {"initializing", "complete"}:
                raise SetupError("Unrecognized bootstrap state; refusing to reseed")
            uuid.UUID(state["setupId"])
            if state["phase"] == "complete":
                uuid.UUID(state["companyId"])
            return state
        # Never downgrade an existing instance to trusted mode or adopt its data.
        if (self.root / "instances").exists() or (self.root / "cli").exists():
            raise SetupError("Existing Paperclip state has no AgentStack marker; manual adoption is required")
        state = {"schema": 1, "phase": "initializing", "setupId": str(uuid.uuid4()), "companyId": None}
        write_json(self.state_path, state)
        return state

    def prepare_secrets(self) -> None:
        env_file = self.instance / ".env"
        key_file = self.instance / "secrets/master.key"
        if any(path.is_symlink() for path in (env_file, key_file, key_file.parent, self.codex_home)):
            raise SetupError("Paperclip keys and coding-agent home must not be symlinks")
        if not env_file.exists():
            write_private(env_file, "".join(f"{key}={secrets.token_hex(32)}\n" for key in (
                "PAPERCLIP_AGENT_JWT_SECRET", "BETTER_AUTH_SECRET", "PAPERCLIP_TOOL_ACTION_SIGNING_SECRET",
            )))
        if not key_file.exists():
            write_private(key_file, secrets.token_hex(32) + "\n")
        self.codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)

    def api(self, method: str, path: str, value: dict | None = None):
        body = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request("http://127.0.0.1:3101/api" + path, data=body, method=method, headers={"Content-Type": "application/json"})
        # Bypass ambient HTTP proxies, including in developer test environments.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise SetupError(f"Paperclip setup API returned HTTP {error.code}; see private logs") from None

    @staticmethod
    def is_setup_draft(agent: dict, company_id: str, agent_id: str) -> bool:
        return (
            agent.get("id") == agent_id
            and agent.get("companyId") == company_id
            and agent.get("status") == "paused"
            and agent.get("lifecycleState") == "paused"
            and "setup" in agent.get("lifecycleHolds", [])
            and agent.get("adapterType") == "codex_local"
            and agent.get("runtimeConfig", {}).get("heartbeat", {}).get("enabled") is False
        )

    def seed(self, state: dict) -> None:
        companies = self.api("GET", "/companies")
        if state["companyId"]:
            matching = [company for company in companies if company["id"] == state["companyId"]]
        else:
            # A retry after a lost create response must recover the same company.
            matching = [company for company in companies if company.get("name") == "ACME Co"]
        if len(matching) > 1 or len(companies) > 1:
            raise SetupError("Unexpected companies during first setup; refusing to choose or overwrite")
        if matching:
            company = matching[0]
        elif companies or state["companyId"]:
            raise SetupError("The bootstrap company changed; refusing to recreate it")
        else:
            company = self.api("POST", "/companies", {"name": "ACME Co", "description": "Workspace orchestration; initial Codex agent requires deliberate configuration and activation."})
        state["companyId"] = str(uuid.UUID(company["id"]))
        write_json(self.state_path, state)
        company_id, agent_id = state["companyId"], state["setupId"]
        agents = self.api("GET", f"/companies/{company_id}/agents")
        if any(agent.get("id") != agent_id for agent in agents):
            raise SetupError("Unexpected agents during first setup; refusing to modify them")
        agent = self.api("POST", f"/companies/{company_id}/agents", {
            "setupId": agent_id,
            "name": "Codex",
            "role": "engineer",
            "title": "Unconfigured workspace coding agent",
            "adapterType": "codex_local",
            "runner": "legacy",
            "adapterConfig": {
                "command": "codex", "engine": "cli", "cwd": str(self.home),
                "dangerouslyBypassApprovalsAndSandbox": False,
                "env": {"CODEX_HOME": str(self.codex_home)},
            },
            "runtimeConfig": {"heartbeat": {"enabled": False, "intervalSec": 0}},
            "permissions": {"canCreateAgents": False, "canCreateSkills": False},
        })
        if not self.is_setup_draft(agent, company_id, agent_id):
            raise SetupError("Paperclip did not preserve the paused setup hold; refusing to publish the UI")
        adapter = agent.get("adapterConfig", {})
        env = adapter.get("env", {})
        codex_binding = env.get("CODEX_HOME")
        # v2026.1106.0 normalizes env entries to plain bindings and redacts
        # their values even for the board. Do not ask it to reveal credentials.
        # The create payload above supplies the dedicated path; runtime tests
        # independently verify that stored value in an isolated fixture DB.
        binding_matches = codex_binding == str(self.codex_home) or (
            isinstance(codex_binding, dict)
            and codex_binding.get("type") == "plain"
            and codex_binding.get("value") in {str(self.codex_home), "***REDACTED***"}
        )
        if adapter.get("dangerouslyBypassApprovalsAndSandbox") is not False or set(env) != {"CODEX_HOME"} or not binding_matches:
            raise SetupError("Paperclip did not preserve coding-agent isolation; refusing to publish the UI")
        if self.api("GET", f"/companies/{company_id}/heartbeat-runs"):
            raise SetupError("Unexpected agent runs during setup; refusing to publish the UI")

    def start(self) -> None:
        self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.log_path.is_symlink():
            raise SetupError("Paperclip launch log must not be a symlink")
        with self.log_path.open("a", encoding="utf-8") as log:
            self.log_path.chmod(0o600)
            self.child = subprocess.Popen(
                [*self.command, "run", "--config", str(self.config_path), "--no-repair"],
                env=self.environment(), cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )

    def stop(self) -> None:
        if self.child is None:
            return
        if self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=10)
        self.child = None

    def wait_bootstrap(self) -> None:
        for _ in range(180):
            if self.child is None or self.child.poll() is not None:
                raise SetupError(f"Paperclip exited; inspect {self.log_path}")
            try:
                if self.api("GET", "/health").get("status") == "ok":
                    return
            except (OSError, ValueError, SetupError):
                pass
            time.sleep(1)
        raise SetupError(f"Paperclip setup timed out; inspect {self.log_path}")

    def initialize(self) -> dict:
        state = self.prepare()
        if state["phase"] == "complete":
            self.assert_authenticated()
            return state
        self.prepare_secrets()
        write_json(self.config_path, self.fresh_config(bootstrap=True))
        try:
            self.start()
            self.wait_bootstrap()
            self.seed(state)
        finally:
            self.stop()
        write_json(self.config_path, self.fresh_config(bootstrap=False))
        state["phase"] = "complete"
        write_json(self.state_path, state)
        return state

    def assert_authenticated(self) -> None:
        config = read_json(self.config_path)
        server = config.get("server", {})
        if server.get("deploymentMode") != "authenticated" or server.get("exposure") != "private":
            raise SetupError("Managed Paperclip requires authenticated/private mode; refusing to start")

    def health(self) -> None:
        if read_json(self.state_path).get("phase") != "complete":
            raise SetupError("Paperclip first setup is incomplete")
        self.assert_authenticated()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:3100/api/health", timeout=3) as response:
            if json.load(response).get("status") != "ok":
                raise SetupError("Paperclip is not healthy")
        try:
            with opener.open("http://127.0.0.1:3100/api/companies", timeout=3):
                raise SetupError("Paperclip admitted an unauthenticated company request")
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise SetupError(f"Unexpected authentication response: HTTP {error.code}") from None

    def serve(self) -> None:
        if self.root.is_symlink():
            raise SetupError("Paperclip directories must not be symlinks")
        self.root.mkdir(mode=0o700, exist_ok=True)
        lock_path = self.root / "agent-stack.lock"
        if lock_path.is_symlink():
            raise SetupError("Paperclip lock must not be a symlink")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.initialize()
            try:
                self.start()
                print("[paperclip] Authenticated UI starting on port 3100. Initial setup creates ACME Co and an unconfigured paused Codex draft; later user changes are preserved. Private first-owner claim instructions are in ~/.paperclip/instances/default/logs/agent-stack-launch.log.", flush=True)
                self.child.wait()
                raise SetupError("Paperclip exited; Docker may restart this companion")
            finally:
                self.stop()


def main() -> int:
    os.umask(0o077)
    runtime = Runtime(Path.home())
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    if command == "serve":
        if os.geteuid() == 0:
            raise SetupError("Paperclip must run as the workspace user, not root")
        def interrupt(_signum, _frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        runtime.serve()
    elif command == "health":
        runtime.health()
    elif command == "info":
        print(json.dumps(read_json(runtime.state_path), indent=2))
    else:
        raise SetupError("Usage: agent-stack-workspace-paperclip serve|health|info")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SetupError, OSError, ValueError, KeyError) as error:
        print(f"[paperclip] ERROR: {error}", file=sys.stderr)
        sys.exit(1)
