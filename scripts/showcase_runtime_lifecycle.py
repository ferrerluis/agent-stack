#!/usr/bin/env python3
"""Owned-VM runtime reload, native route and nonbillable container checks.

No paid task, secret retrieval, image pull, replacement container or service-wide
restart is allowed. A private receipt and captured-ID watchdog precede stopping
one pinned container. Failure stops that container and preserves recovery data.
"""
import builtins
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import zlib

import showcase_agents as agents
import showcase_runtime_routes as routes
import showcase_runtime_environment as environment
import showcase_runtime_probe as probe

COMPOSE = ["/usr/bin/docker", "compose", "-f", "/opt/agent-stack/docker-compose.yml"]
INSPECT = '{"id":{{json .Id}},"image_id":{{json .Image}},"image_ref":{{json .Config.Image}},"state":{{json .State}},"restarts":{{json .RestartCount}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}'
SOURCES = ("showcase_runtime_routes", "showcase_agents", "showcase_runtime_environment", "showcase_runtime_probe", "showcase_strict_health", "showcase_runtime_lifecycle")


def need(value):
    if not value: raise ValueError("runtime_lifecycle_unverified")


def inline_source(payloads):
    code = "import builtins,types\nmods={}\ndef owned_import(name,*args,**kwargs):\n return mods[name] if name in mods else builtins.__import__(name,*args,**kwargs)\n"
    for name, (path, content) in payloads.items():
        code += ("m=types.ModuleType(" + repr(name) + ");m.__file__=" + repr(path) + "\n"
                 "m.__dict__['__builtins__']={**vars(builtins),'__import__':owned_import}\n"
                 "exec(compile(" + repr(content) + ",m.__file__,'exec'),m.__dict__);mods[" + repr(name) + "]=m\n")
    return code


def wrapped_source(code):
    # Public checked source only. Compressing avoids SSH/POSIX argument limits;
    # the packet and its temporary credential remain exclusively on stdin.
    compressed = base64.b64encode(zlib.compress(code.encode(), 9)).decode()
    return "import base64,zlib;exec(compile(zlib.decompress(base64.b64decode(" + repr(compressed) + ")), '<owned-showcase-runtime>', 'exec'))"


def source_command(checkout):
    """Inline imports resolve only to the prepared source; no VM files or cache."""
    root = Path(__file__).resolve().parents[1]
    payloads = {}
    for name in SOURCES:
        relative = "skills/agent-stack-doctor/scripts/strict_health.py" if name == "showcase_strict_health" else "scripts/" + name + ".py"
        path = Path(checkout) / relative
        need(path.is_file() and not path.is_symlink())
        content = path.read_bytes(); need(content == (root / relative).read_bytes())
        payloads[name] = (str(path), content.decode())
    native = inline_source({name: payloads[name] for name in
        ("showcase_runtime_routes", "showcase_runtime_environment", "showcase_runtime_probe")})
    native += "raise SystemExit(mods['showcase_runtime_probe'].main())\n"
    code = inline_source(payloads)
    code += "mods['showcase_runtime_lifecycle'].PROBE_SOURCE=" + repr(wrapped_source(native)) + "\n"
    code += "mods['showcase_runtime_lifecycle'].DOCTOR_MODULE=mods['showcase_strict_health']\n"
    code += "raise SystemExit(mods['showcase_runtime_lifecycle'].main())\n"
    return ["sudo", "-n", "python3", "-B", "-I", "-c", wrapped_source(code)]


def image_valid(role, value):
    prefix = r"ghcr\.io/openclaw/openclaw" if role == "openclaw" else r"(?:ghcr\.io/)?nousresearch/hermes-agent"
    need(role in routes.OWNERS and isinstance(value, str) and re.fullmatch(prefix + r"@sha256:[a-f0-9]{64}", value))


class RuntimeLifecycle:
    def __init__(self, binding, execute=agents.invoke, transaction=None, now=time.monotonic, sleep=time.sleep):
        need(os.geteuid() == 0); routes.binding_valid(binding)
        self.binding, self.execute, self.now, self.sleep = copy.deepcopy(binding), execute, now, sleep
        self.tx = routes.ConfigurationTransaction() if transaction is None else transaction
        self.environment = environment.EnvironmentTransaction(self.tx)

    def capture(self, role, image):
        image_valid(role, image)
        identifier = self.execute(COMPOSE + ["ps", "-a", "-q", role], timeout=10).decode().strip()
        need(re.fullmatch(r"[a-f0-9]{64}", identifier))
        row = agents.strict_json(self.execute(["/usr/bin/docker", "inspect", "--format", INSPECT, identifier], timeout=10))
        need(isinstance(row, dict) and set(row) == {"id", "image_id", "image_ref", "state", "restarts", "labels", "mounts"}
             and row["id"] == identifier and row["image_ref"] == image
             and isinstance(row["image_id"], str) and re.fullmatch(r"sha256:[a-f0-9]{64}", row["image_id"])
             and type(row["restarts"]) is int and row["restarts"] == 0)
        need(isinstance(row["labels"], dict) and row["labels"].get("com.docker.compose.project") == "agent-stack"
             and row["labels"].get("com.docker.compose.service") == role)
        destination = "/home/node/.openclaw" if role == "openclaw" else "/opt/data"
        mounted = [m for m in row["mounts"] if isinstance(m, dict) and m.get("Destination") == destination]
        need(len(mounted) == 1 and mounted[0].get("Type") == "bind" and mounted[0].get("RW") is True
             and mounted[0].get("Source") == str(self.tx.root / role))
        state = row["state"]
        need(isinstance(state, dict) and type(state.get("Running")) is bool and type(state.get("Restarting")) is bool
             and state.get("Dead") is False)
        return row

    def known(self, context):
        row = self.capture(context["role"], context["image_ref"])
        need(row["id"] == context["container_id"] and row["image_id"] == context["image_id"])
        return row

    @staticmethod
    def healthy(row):
        state = row["state"]
        return state["Running"] and not state["Restarting"] and isinstance(state.get("Health"), dict) and state["Health"].get("Status") == "healthy"

    def path(self, role):
        need(role in routes.OWNERS)
        return self.tx.journal / (role + ".lifecycle.json")

    def save(self, context, exclusive=False):
        routes.atomic_write(self.path(context["role"]), routes.encoded(context), 0, 0, 0o600, exclusive=exclusive)

    def load(self, role):
        path = self.path(role); info = routes.checked_path(path, owner=0)
        need(info.st_mode & 0o777 == 0o600)
        context = routes.decoded(path.read_bytes())
        need(context.get("schema") == 1 and type(context.get("schema")) is int and context.get("binding") == self.binding
             and context.get("role") == role and re.fullmatch(r"[a-f0-9]{64}", context.get("container_id", ""))
             and re.fullmatch(r"sha256:[a-f0-9]{64}", context.get("image_id", ""))
             and re.fullmatch(re.escape("as7-route-" + self.binding["run_id"] + "-" + role + "-") + r"[a-f0-9]{32}", context.get("unit", ""))
             and re.fullmatch(r"[a-f0-9]{64}", context.get("original_sha256", "")))
        image_valid(role, context.get("image_ref"))
        if role == "hermes":
            need(isinstance(context.get("original_environment_sha256"), str)
                 and re.fullmatch(r"[a-f0-9]{64}", context["original_environment_sha256"]))
        return context

    def stop(self, context):
        self.known(context)
        self.execute(["/usr/bin/docker", "stop", "-t", "5", context["container_id"]], timeout=15)
        row = self.known(context)
        need(not row["state"]["Running"] and not row["state"]["Restarting"])

    def start(self, context):
        row = self.known(context); need(not row["state"]["Running"] and not row["state"]["Restarting"])
        self.execute(["/usr/bin/docker", "start", context["container_id"]], timeout=10)
        end = self.now() + 90
        while self.now() < end:
            row = self.known(context)
            if self.healthy(row): return
            need(row["state"]["Running"] and not row["state"]["Restarting"])
            self.sleep(min(1, max(0, end - self.now())))
        raise ValueError("runtime_lifecycle_unverified")

    def native(self, context, route):
        need(self.healthy(self.known(context)))
        if route["role"] == "openclaw":
            raw = self.execute(["/usr/bin/docker", "exec", "--user", "1000", context["container_id"],
                "env", "-i", "HOME=/home/node", "PATH=/usr/local/bin:/usr/bin:/bin", "openclaw", "gateway", "call", "config.get",
                "--expect-url", "ws://127.0.0.1:18789", "--timeout", "5000", "--params", "{}", "--json"], timeout=10)
            return probe.native_openclaw(agents.strict_json(raw), route)
        report = self.container(context, route, "native", "")
        return report["native_route"]

    def container(self, context, route, action, nonce):
        packet = {"action": action, "route": route, "config_sha256": context["config_sha256"],
                  "source_revision": self.binding["source_revision"], "nonce": nonce}
        if route["role"] == "hermes": packet["environment_sha256"] = context["environment_sha256"]
        if route["role"] == "openclaw":
            command = ["node", "-e", probe.NODE_SOURCE]
            environment = ["HOME=/home/node", "PATH=/usr/local/bin:/usr/bin:/bin"]
        else:
            need(isinstance(PROBE_SOURCE, str) and len(PROBE_SOURCE) > 100)
            command = ["/opt/hermes/.venv/bin/python", "-B", "-I", "-c", PROBE_SOURCE]
            environment = ["HOME=/opt/data", "HERMES_HOME=/opt/data", "PATH=/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin"]
        result = agents.strict_json(self.execute(["/usr/bin/docker", "exec", "-i", "--user", str(routes.OWNERS[route["role"]]),
            context["container_id"], "env", "-i", *environment, *command],
            input=json.dumps(packet, separators=(",", ":")).encode(), timeout=10))
        expected = {"schema": 1, "status": "runtime_native_route_verified" if action == "native" else "runtime_container_route_verified",
            "role": route["role"], "provider": route["provider"], "uid": routes.OWNERS[route["role"]],
            "config_sha256": context["config_sha256"], "source_revision": self.binding["source_revision"], "nonce": nonce}
        if route["role"] == "hermes": expected["environment_sha256"] = context["environment_sha256"]
        need(isinstance(result, dict) and type(result.get("schema")) is int and type(result.get("uid")) is int
             and all(result.get(k) == v for k, v in expected.items())
             and set(result) == (set(expected) | ({"native_route"} if route["role"] == "hermes" else set())))
        if route["role"] == "hermes": probe.native_proof_valid(route, result["native_route"])
        return result

    def disarm(self, context):
        self.execute(["systemctl", "stop", context["unit"] + ".timer", context["unit"] + ".service"], timeout=10)
        need(self.execute(["systemctl", "show", "--property=ActiveState", "--value", context["unit"] + ".timer"], timeout=10).strip() == b"inactive")

    def current_files(self, context, route):
        target = self.tx.root / routes.FILES[route["role"]]
        info = routes.checked_path(target, owner=routes.OWNERS[route["role"]])
        need(info.st_mode & 0o777 == 0o600 and routes.sha(target.read_bytes()) == context["config_sha256"])
        if route["role"] == "hermes":
            info = routes.checked_path(self.environment.target, owner=routes.OWNERS["hermes"])
            need(info.st_mode & 0o777 == 0o600 and routes.sha(self.environment.target.read_bytes()) == context["environment_sha256"])
            environment.selected_environment(self.environment.target.read_bytes(), route)

    def doctor(self, route, request_id):
        """Run the native read-only doctor unchanged; never grant model access."""
        routes.route_valid(route); need(isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id))
        need(DOCTOR_MODULE is not None)
        with self.tx.lock():
            context = self.load(route["role"]); need(context["status"] == "route_verified")
            context.update(status="doctor_started", unit="as7-route-" + self.binding["run_id"] + "-" + route["role"] + "-" + request_id)
            self.save(context)
            try:
                self.known(context)
                self.execute(["systemd-run", "--quiet", "--unit=" + context["unit"], "--on-active=180s",
                    "--timer-property=AccuracySec=1s", "--property=RuntimeMaxSec=30s", "/usr/bin/docker", "stop", "-t", "5", context["container_id"]], timeout=10)
                need(self.execute(["systemctl", "is-active", context["unit"] + ".timer"], timeout=10).strip() == b"active")
                self.current_files(context, route); need(self.native(context, route) == context["native_route"])
                if route["role"] == "openclaw":
                    command = ["env", "-i", "HOME=/home/node", "PATH=/usr/local/bin:/usr/bin:/bin",
                        "openclaw", "doctor", "--lint", "--json", "--severity-min", "warning"]
                else:
                    command = ["env", "-i", "HOME=/opt/data", "HERMES_HOME=/opt/data", "NO_COLOR=1", "PYTHONDONTWRITEBYTECODE=1",
                        "PATH=/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin", "/opt/hermes/.venv/bin/hermes", "doctor"]
                raw = self.execute(["/usr/bin/docker", "exec", "--user", str(routes.OWNERS[route["role"]]), context["container_id"], *command], timeout=90)
                need(isinstance(raw, bytes) and 0 < len(raw) <= agents.LIMIT)
                if route["role"] == "openclaw":
                    parsed = agents.strict_json(raw)
                    need(isinstance(parsed, dict) and type(parsed.get("schemaVersion")) is int)
                    passed = DOCTOR_MODULE.doctor_healthy(parsed)
                else: passed = DOCTOR_MODULE.hermes_doctor_healthy(raw.decode("utf-8"))
                need(passed); self.current_files(context, route)
                need(self.native(context, route) == context["native_route"] and self.healthy(self.known(context)))
                self.disarm(context); need(self.healthy(self.known(context))); self.current_files(context, route)
                context.update(status="doctor_verified", doctor_sha256=routes.sha(raw)); self.save(context)
                return {"schema": 1, "status": "runtime_doctor_verified", "binding": self.binding,
                    **{key: context[key] for key in ("role", "container_id", "image_ref", "image_id", "config_sha256", "native_route", "doctor_sha256")},
                    **({"environment_sha256": context["environment_sha256"]} if route["role"] == "hermes" else {}),
                    "uid": routes.OWNERS[route["role"]], "health": "healthy", "watchdog": "disarmed"}
            except BaseException:
                try: self.stop(context)
                except BaseException: pass
                context["status"] = "uncertain"; self.save(context)
                raise

    def prepare(self, route, image, request_id):
        routes.route_valid(route); image_valid(route["role"], image)
        need(isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id))
        with self.tx.lock():
            role = route["role"]; path = self.path(role)
            need(not path.exists() and not path.is_symlink())
            row = self.capture(role, image); need(self.healthy(row))
            original = self.tx.root / routes.FILES[role]; routes.checked_path(original, owner=routes.OWNERS[role])
            context = {"schema": 1, "binding": self.binding, "role": role, "container_id": row["id"],
                "image_ref": image, "image_id": row["image_id"], "original_sha256": routes.sha(original.read_bytes()),
                "unit": "as7-route-" + self.binding["run_id"] + "-" + role + "-" + request_id,
                "status": "captured"}
            if role == "hermes":
                routes.checked_path(self.environment.target, owner=routes.OWNERS[role])
                context["original_environment_sha256"] = routes.sha(self.environment.target.read_bytes())
            self.save(context, exclusive=True)
            try:
                self.execute(["systemd-run", "--quiet", "--unit=" + context["unit"], "--on-active=180s",
                    "--timer-property=AccuracySec=1s", "--property=RuntimeMaxSec=30s", "/usr/bin/docker", "stop", "-t", "5", row["id"]], timeout=10)
                need(self.execute(["systemctl", "is-active", context["unit"] + ".timer"], timeout=10).strip() == b"active")
                self.stop(context)
                routes.checked_path(original, owner=routes.OWNERS[role])
                context.update(original_sha256=routes.sha(original.read_bytes()), status="stopped"); self.save(context)
                if role == "hermes":
                    routes.checked_path(self.environment.target, owner=routes.OWNERS[role])
                    context["original_environment_sha256"] = routes.sha(self.environment.target.read_bytes()); self.save(context)
                    context["environment_sha256"] = self.environment._install(self.binding, route); self.save(context)
                installed = self.tx._install(self.binding, route)
                context.update(config_sha256=installed["config_sha256"], status="installed"); self.save(context)
                self.start(context)
                context.update(native_route=self.native(context, route), status="reload_verified"); self.save(context)
                return {"schema": 1, "status": "runtime_reload_verified", "binding": self.binding,
                    **{key: context[key] for key in ("role", "container_id", "image_ref", "image_id", "config_sha256", "native_route")},
                    **({"environment_sha256": context["environment_sha256"]} if role == "hermes" else {}),
                    "watchdog": "armed"}
            except BaseException:
                try: self.stop(context)
                except BaseException: pass
                context["status"] = "uncertain"; self.save(context)
                raise

    def verify(self, route, nonce):
        routes.route_valid(route); need(isinstance(nonce, str) and re.fullmatch(r"[a-f0-9]{64}", nonce))
        with self.tx.lock():
            context = self.load(route["role"]); need(context["status"] == "reload_verified")
            try:
                native = self.native(context, route); need(native == context["native_route"])
                result = self.container(context, route, "transport", nonce)
                need(self.healthy(self.known(context)))
                self.disarm(context)
                need(self.healthy(self.known(context))); self.current_files(context, route)
                context["status"] = "route_verified"; self.save(context)
                return {"schema": 1, "status": "runtime_route_verified", "binding": self.binding,
                    **{key: context[key] for key in ("role", "container_id", "image_ref", "image_id", "config_sha256", "native_route")},
                    **({"environment_sha256": context["environment_sha256"]} if route["role"] == "hermes" else {}),
                    "container_probe": result, "watchdog": "disarmed"}
            except BaseException:
                try: self.stop(context)
                except BaseException: pass
                context["status"] = "uncertain"; self.save(context)
                raise

    def restore(self, role):
        with self.tx.lock():
            context = self.load(role)
            self.stop(context)
            record = self.tx.journal / (role + ".json")
            if record.exists() or record.is_symlink(): restored = self.tx._restore(self.binding, role)
            else:
                original = self.tx.root / routes.FILES[role]; routes.checked_path(original, owner=routes.OWNERS[role])
                need(routes.sha(original.read_bytes()) == context["original_sha256"])
                restored = {"original_sha256": context["original_sha256"]}
            if role == "hermes":
                if self.environment.record.exists() or self.environment.record.is_symlink(): self.environment._restore(self.binding)
                else:
                    routes.checked_path(self.environment.target, owner=routes.OWNERS[role])
                    need(routes.sha(self.environment.target.read_bytes()) == context["original_environment_sha256"])
            self.disarm(context)
            self.path(role).unlink(); routes.fsync_dir(self.tx.journal)
            return {"schema": 1, "status": "runtime_config_restored", "role": role, "binding": self.binding,
                    "original_sha256": restored["original_sha256"]}


PROBE_SOURCE = ""  # The checked inline loader sets this; no dynamic VM imports.
DOCTOR_MODULE = None  # The same loader supplies the checked strict parser.


def main():
    try:
        need(os.geteuid() == 0)
        raw = sys.stdin.buffer.read(4097); need(0 < len(raw) <= 4096); packet = routes.decoded(raw)
        action = packet.get("action"); binding = packet.get("binding"); routes.binding_valid(binding)
        need(Path("/proc/sys/kernel/random/boot_id").read_text().strip() == binding["boot_id"])
        applied = routes.decoded(Path("/opt/agent-stack/.last-apply.json").read_bytes())
        need(applied.get("status") == "success" and applied.get("checksum") == binding["checksum"]
             and sorted(applied.get("services", [])) == ["hermes", "openclaw", "workspace"])
        lifecycle = RuntimeLifecycle(binding)
        if action == "prepare":
            need(set(packet) == {"action", "binding", "route", "image_ref", "request_id"})
            report = lifecycle.prepare(packet["route"], packet["image_ref"], packet["request_id"])
        elif action == "verify":
            need(set(packet) == {"action", "binding", "route", "nonce"})
            report = lifecycle.verify(packet["route"], packet["nonce"])
        elif action == "doctor":
            need(set(packet) == {"action", "binding", "route", "request_id"})
            report = lifecycle.doctor(packet["route"], packet["request_id"])
        else:
            need(action == "restore" and set(packet) == {"action", "binding", "role"})
            report = lifecycle.restore(packet["role"])
    except BaseException:
        report = {"schema": 1, "status": "runtime_lifecycle_unverified"}
    print(json.dumps(report))
    return 1 if report["status"] == "runtime_lifecycle_unverified" else 0


if __name__ == "__main__": sys.exit(main())
