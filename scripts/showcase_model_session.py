#!/usr/bin/env python3
"""Internal original-budget model channel and independently checked coding routes.

Production still needs pinned verification and measured launch wiring. No public
paid command, price override, provider selection or readiness bypass is added.
"""
import copy
import hashlib
import json
from pathlib import Path
import secrets
import showcase_runtime_evidence as runtime_evidence

from showcase_attempt import verify_prepared
from showcase_cleanup import private_write
from showcase_model_gateway import BudgetGateway
from showcase_model_prices import policy_from_terms
from showcase_model_spend import ModelSpend, ROLES, digest
from showcase_model_transport import SSHModelTransport
from showcase_policy import PolicyError, require

PROBE = "scripts/showcase_model_container.py"
RUNTIME_CONFIG = "scripts/showcase_runtime_routes.py"


class ModelSession:
    def __init__(self, checks, policy, gateway_factory=BudgetGateway, transport_factory=SSHModelTransport):
        self.checks, self.policy = checks, copy.deepcopy(policy)
        self.revision = checks.attempt.prepared["source_revision"]
        self.gateway_factory, self.transport_factory = gateway_factory, transport_factory
        self.gateway = self.transport = None
        self.stage, self.state, self.routes = None, "new", {}
        self.tasks = {}
        self.runtime_configs = {}
        self.spend = ModelSpend(checks.ledger, checks.run.name, checks.provider,
                                checks.attempt.prepared["source_revision"], self.policy, now=checks.now)

    @classmethod
    def from_measured_terms(cls, checks, receipt, limits):
        record = checks.attempt.record()
        policy = policy_from_terms(receipt, checks.config, checks.provider, checks.attempt.prepared["source_revision"],
                                   record["plan_sha256"], limits, now=checks.now)
        return cls(checks, policy)

    def active(self):
        require(self.state == "verified" and self.transport is not None and self.transport.healthy()
                and self.gateway is not None and self.gateway.healthy(), "model_session_required")
        require(self.policy == self.spend.policy, "model_session_scope_changed")
        prepared, _ = self.checks.ensure()
        require(prepared["source_revision"] == self.policy["source_revision"], "model_session_scope_changed")
        self.spend.activate(1)
        evidence = self.checks.evidence()
        require((self.stage == "before_reboot" and "reboot" not in evidence["checks"]) or
                (self.stage == "after_reboot" and evidence["checks"].get("reboot") in ("pass", "pending_doctors")), "model_session_boot_changed")
        if self.stage == "after_reboot" and evidence["checks"].get("reboot") == "pending_doctors":
            before, after = evidence.get("baseline"), evidence.get("after_reboot")
            require(evidence["checks"].get("reboot_boot") == "pass" and isinstance(before, dict) and isinstance(after, dict)
                    and before.get("boot_id") != after.get("boot_id") and all(before.get(k) == after.get(k) for k in
                    ("checksum", "disk_uuid", "data_volume_id", "dns")), "model_session_boot_changed")
        baseline = evidence.get("after_reboot") if self.stage == "after_reboot" else evidence.get("baseline")
        require(isinstance(baseline, dict) and baseline.get("boot_id") == self.transport.proof["boot_id"]
                and baseline.get("checksum") == self.transport.proof["checksum"], "model_session_boot_changed")
        return evidence

    def start(self, stage):
        require(stage in ("before_reboot", "after_reboot") and
                ((self.state == "new" and stage == "before_reboot") or
                 (self.state == "closed" and self.stage == "before_reboot" and stage == "after_reboot")), "model_session_already_invoked")
        evidence = self.checks.evidence(); self.checks.require_container_gate(evidence)
        require(stage not in evidence.get("model_channels", {}), "model_session_already_invoked")
        require(stage != "after_reboot" or evidence["checks"].get("reboot") in ("pass", "pending_doctors"), "model_session_boot_changed")
        if stage == "after_reboot" and evidence["checks"].get("reboot") == "pending_doctors":
            before, after = evidence.get("baseline"), evidence.get("after_reboot")
            require(evidence["checks"].get("reboot_boot") == "pass" and isinstance(before, dict) and isinstance(after, dict)
                    and before["boot_id"] != after["boot_id"] and all(before[k] == after[k] for k in
                    ("checksum", "disk_uuid", "data_volume_id", "dns")), "model_session_boot_changed")
        # Reopening after reboot shares this same spend object and original
        # policy/holds; it never reserves a new allocation or replays a task.
        self.spend.activate(60)
        self.stage, self.state, self.routes, self.tasks = stage, "starting", {}, {}
        evidence.setdefault("model_channels", {})[stage] = {"status": "started"}
        private_write(self.checks.path, evidence)
        try:
            self.gateway = self.gateway_factory(self.spend, self.checks.attempt.session.secret)
            self.gateway.gate_task_starts()
            self.gateway.__enter__()
            self.transport = self.transport_factory(self.checks, self.gateway)
            self.transport.__enter__()
            self.state = "verified"
            self.checks.attach_models(self)
            evidence["model_channels"][stage] = copy.deepcopy(self.transport.proof)
            private_write(self.checks.path, evidence)
            return copy.deepcopy(self.transport.proof)
        except BaseException:
            evidence["model_channels"][stage]["status"] = "uncertain"
            private_write(self.checks.path, evidence)
            self.close()
            raise

    def workspace_client(self, role):
        require(role in ("codex", "claude"), "invalid_workspace_cli")
        evidence = self.active()
        require(role not in self.routes, "workspace_route_already_invoked")
        checkout = self.checks.attempt.checkout
        source = checkout / PROBE
        require(source.is_file() and not source.is_symlink() and source.read_bytes() ==
                (Path(__file__).resolve().parents[1] / PROBE).read_bytes(), "model_container_source_changed")
        verify_prepared(self.checks.run, self.checks.config, self.checks.provider)
        target = self.checks.bind()
        baseline = evidence["baseline"] if self.stage == "before_reboot" else evidence["after_reboot"]
        require(self.checks.probe(target) == baseline, "model_session_boot_changed")
        bundle = self.transport.client(role)
        probe = self.gateway.new_probe(role, kind="workspace")
        packet = {**bundle, "nonce": probe["nonce"], "bridge_ipv4": self.transport.ready["bridge_ipv4"],
                  "source_revision": self.policy["source_revision"]}
        # Mark the route before the nonbillable container request. A lost
        # response cannot reuse a capability/probe within this boot.
        self.routes[role] = {"status": "started"}
        from showcase_remote import WORKSPACE, decoded
        report = decoded(self.checks.ssh(target, WORKSPACE + ["python3", "-B", "-I", "-c", source.read_text()],
                                        input=json.dumps(packet).encode(), timeout=15))
        expected = {"schema": 1, "status": "workspace_route_verified", "role": role, "provider": bundle["provider"],
                    "uid": 1000, "bridge_ipv4": packet["bridge_ipv4"], "nonce": probe["nonce"],
                    "source_revision": self.policy["source_revision"],
                    "base_url_sha256": hashlib.sha256(bundle["base_url"].encode()).hexdigest()}
        require(report == expected and type(report.get("uid")) is int and
                self.gateway.probe_observed(role, probe["nonce"]), "workspace_model_route_unverified")
        require(self.checks.probe(target) == baseline, "model_session_boot_changed")
        self.active(); self.routes[role] = report
        evidence = self.checks.evidence()
        evidence["model_channels"][self.stage].setdefault("workspace_routes", {})[role] = report
        private_write(self.checks.path, evidence)
        return bundle

    def begin_task(self, role):
        self.active()
        require(role in ROLES and role not in self.tasks, "model_role_already_used")
        if role in ("openclaw", "hermes"): self.runtime_client(role)
        def verify(state):
            row = self.spend.account(state)
            require(not any(hold["role"] == role for hold in row["model_requests"]), "model_role_already_used")
        self.checks.ledger.operate(verify)
        start = {"role": role, "started_at": self.checks.now(), "boot_id": self.transport.proof["boot_id"],
                 "policy_sha256": digest(self.policy)}
        self.tasks[role] = start
        self.gateway.authorize_role(role)
        return copy.deepcopy(start)

    def runtime_ready(self, role):
        require(role in ("openclaw", "hermes"), "invalid_runtime")
        evidence = self.active(); config = self.runtime_configs.get(role)
        require(self.stage == "before_reboot" and isinstance(config, dict) and config.get("lifecycle") is True,
                "runtime_model_route_required")
        require(isinstance(config.get("route"), dict), "runtime_model_route_required")
        channel = evidence["model_channels"][self.stage]
        ready, doctor = channel.get("runtime_routes", {}).get(role), channel.get("runtime_doctors", {}).get(role)
        binding = {"run_id": self.checks.run.name, "source_revision": self.revision,
                   "boot_id": self.transport.proof["boot_id"], "checksum": self.transport.proof["checksum"]}
        image = ("ghcr.io/openclaw/openclaw@" + self.checks.config["pins"]["openclaw_digest"] if role == "openclaw"
                 else self.checks.config["pins"]["hermes_image"])
        terms = self.policy["roles"][role]
        require(config.get("binding") == binding and config["route"]["role"] == role
                and config["route"].get("bridge_ipv4") == self.transport.ready["bridge_ipv4"]
                and config["route"].get("base_url") == "http://" + self.transport.ready["bridge_ipv4"] + ":" + str(self.transport.ready["bridge_port"]) + "/" + role
                and all(config["route"].get(k) == terms[v] for k, v in (("provider", "provider"), ("model", "model"),
                    ("context_tokens", "context_tokens_upper_bound"), ("output_tokens", "output_tokens_upper_bound"))), "runtime_model_route_required")
        try:
            runtime_evidence.identity(ready, doctor, config["route"], binding, image)
            require(self.gateway.probe_observed(role, ready["container_probe"]["nonce"]), "runtime_model_route_required")
            self.checks.doctor_receipt()  # Complete diagnostics on this boot.
        except (ValueError, TypeError, KeyError, PolicyError):
            raise PolicyError("runtime_model_route_required") from None
        return copy.deepcopy((config, ready, doctor, image))

    def runtime_client(self, role):
        config, _, _, _ = self.runtime_ready(role)
        require(role not in self.tasks and role not in self.checks.evidence()["model_channels"][self.stage].get("runtime_tasks", {}),
                "model_role_already_used")
        require(config["route"].get("credential") == self.transport.client(role).get("credential"), "runtime_model_route_required")
        return copy.deepcopy(config["route"])

    def run_runtime_task(self, role, challenge, request_id, start):
        """Dispatch once through checked captured-container code; no paid replay."""
        config, ready, doctor, image = self.runtime_ready(role)
        require(isinstance(start, dict) and set(start) == {"role", "started_at", "boot_id", "policy_sha256"}
                and start == self.tasks.get(role) and start.get("role") == role
                and start.get("boot_id") == self.transport.proof["boot_id"] and start.get("policy_sha256") == digest(self.policy),
                "model_task_receipt_unverified")
        require(config["route"].get("credential") == self.transport.client(role).get("credential"), "runtime_model_route_required")
        from showcase_agents import challenge_valid, REQUEST
        import re
        challenge_valid(challenge)
        require(isinstance(request_id, str) and re.fullmatch(REQUEST, request_id), "runtime_task_failed")
        from showcase_attempt import remaining_timeout
        require(remaining_timeout(self.checks.ensure()[1], 300, now=self.checks.now()) == 300, "agent_task_time_required")
        evidence = self.active(); row = evidence["model_channels"][self.stage]
        require(role not in row.get("runtime_tasks", {}), "model_role_already_used")
        baseline = evidence["baseline"]
        require(self.checks.probe(config["target"]) == baseline, "model_session_boot_changed")
        row.setdefault("runtime_tasks", {})[role] = {"status": "started", "request_id": request_id}
        private_write(self.checks.path, evidence)
        try:
            try:
                report = self.runtime_lifecycle_command(config["target"], {"action": "task", "binding": config["binding"],
                    "route": config["route"], "challenge": challenge, "request_id": request_id}, 300)
            finally: self.gateway.seal_role(role)
            checked = runtime_evidence.validate(report, ready, doctor, config["route"], config["binding"], image, challenge, request_id)
            require(self.checks.probe(config["target"]) == baseline, "model_session_boot_changed")
            require(self.runtime_ready(role) == (config, ready, doctor, image), "runtime_task_failed")
            evidence = self.checks.evidence()
            evidence["model_channels"][self.stage]["runtime_tasks"][role] = checked
            private_write(self.checks.path, evidence)
            from showcase_task_evidence import hashed
            return {"turn": copy.deepcopy(report["turn"]), "proof_sha256": hashed(checked)}
        except BaseException:
            from showcase_cleanup import private_read
            try:
                evidence = private_read(self.checks.path)
                require(evidence.get("run_id") == self.checks.run.name and evidence.get("source_revision") == self.revision,
                        "model_session_scope_changed")
                evidence["model_channels"][self.stage]["runtime_tasks"][role]["status"] = "uncertain"
                private_write(self.checks.path, evidence)
            finally: self.close()
            raise PolicyError("runtime_task_failed") from None

    def runtime_config_command(self, target, packet):
        source = self.checks.attempt.checkout / RUNTIME_CONFIG
        require(source.is_file() and not source.is_symlink() and source.read_bytes() ==
                (Path(__file__).resolve().parents[1] / RUNTIME_CONFIG).read_bytes(), "runtime_config_source_changed")
        verify_prepared(self.checks.run, self.checks.config, self.checks.provider)
        from showcase_remote import decoded
        return decoded(self.checks.ssh(target, ["sudo", "-n", "python3", "-B", "-I", "-c", source.read_text()],
                                       input=json.dumps(packet).encode(), timeout=20))

    def runtime_lifecycle_command(self, target, packet, timeout):
        from showcase_runtime_lifecycle import source_command
        verify_prepared(self.checks.run, self.checks.config, self.checks.provider)
        from showcase_remote import decoded
        return decoded(self.checks.ssh(target, source_command(self.checks.attempt.checkout),
                                       input=json.dumps(packet).encode(), timeout=timeout))

    def activate_runtime(self, role):
        """Reload/probe one pinned runtime. Doctor/task authorization is separate."""
        require(role in ("openclaw", "hermes"), "invalid_runtime")
        evidence = self.active(); require(role not in self.runtime_configs, "runtime_config_already_invoked")
        from showcase_attempt import remaining_timeout
        require(remaining_timeout(self.checks.ensure()[1], 220, now=self.checks.now()) == 220, "runtime_reload_time_required")
        target = self.checks.bind()
        baseline = evidence["baseline"] if self.stage == "before_reboot" else evidence["after_reboot"]
        require(self.checks.probe(target) == baseline, "model_session_boot_changed")
        bundle = self.transport.client(role)
        address = self.transport.ready["bridge_ipv4"]
        route = {**bundle, "base_url": "http://" + address + ":" + str(self.transport.ready["bridge_port"]) + "/" + role,
                 "bridge_ipv4": address, "model": self.policy["roles"][role]["model"],
                 "context_tokens": self.policy["roles"][role]["context_tokens_upper_bound"],
                 "output_tokens": self.policy["roles"][role]["output_tokens_upper_bound"]}
        from showcase_runtime_routes import route_valid
        from showcase_runtime_probe import native_proof_valid
        route_valid(route)
        binding = {"run_id": self.checks.run.name, "source_revision": self.revision,
                   "boot_id": baseline["boot_id"], "checksum": baseline["checksum"]}
        image = ("ghcr.io/openclaw/openclaw@" + self.checks.config["pins"]["openclaw_digest"] if role == "openclaw"
                 else self.checks.config["pins"]["hermes_image"])
        self.runtime_configs[role] = {"target": copy.deepcopy(target), "binding": binding, "lifecycle": True, "route": copy.deepcopy(route)}
        for key in ("service." + role, role + ".doctor", role + ".config", role + ".readiness"):
            if key in evidence["checks"]: evidence["checks"][key] = "uncertain"
        evidence["model_channels"][self.stage].setdefault("runtime_routes", {})[role] = {"status": "started"}
        private_write(self.checks.path, evidence)
        try:
            ready = self.runtime_lifecycle_command(target, {"action": "prepare", "binding": binding, "route": route,
                "image_ref": image, "request_id": secrets.token_hex(16)}, 180)
            fields = {"schema", "status", "binding", "role", "container_id", "image_ref", "image_id", "config_sha256", "native_route", "watchdog"}
            if role == "hermes": fields.add("environment_sha256")
            require(set(ready) == fields and type(ready["schema"]) is int and ready["schema"] == 1
                    and ready["status"] == "runtime_reload_verified" and ready["binding"] == binding
                    and ready["role"] == role and ready["image_ref"] == image and ready["watchdog"] == "armed"
                    and isinstance(ready["container_id"], str) and len(ready["container_id"]) == 64
                    and all(c in "0123456789abcdef" for c in ready["container_id"])
                    and isinstance(ready["image_id"], str) and ready["image_id"].startswith("sha256:")
                    and len(ready["image_id"]) == 71 and all(c in "0123456789abcdef" for c in ready["image_id"][7:])
                    and isinstance(ready["config_sha256"], str) and len(ready["config_sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in ready["config_sha256"]), "runtime_reload_unverified")
            require(role != "hermes" or isinstance(ready["environment_sha256"], str) and len(ready["environment_sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in ready["environment_sha256"]), "runtime_reload_unverified")
            native_proof_valid(route, ready["native_route"])
            self.active()
            nonce = self.gateway.new_probe(role, kind="runtime")["nonce"]
            report = self.runtime_lifecycle_command(target, {"action": "verify", "binding": binding, "route": route, "nonce": nonce}, 30)
            require(set(report) == fields | {"container_probe"} and report["status"] == "runtime_route_verified"
                    and report["watchdog"] == "disarmed" and all(report[k] == ready[k] for k in fields - {"status", "watchdog"}), "runtime_route_unverified")
            container = report["container_probe"]
            expected = {"schema": 1, "status": "runtime_container_route_verified", "role": role, "provider": route["provider"],
                "uid": 1000 if role == "openclaw" else 10000, "config_sha256": ready["config_sha256"],
                "source_revision": self.revision, "nonce": nonce}
            if role == "hermes": expected["environment_sha256"] = ready["environment_sha256"]
            require(isinstance(container, dict) and type(container.get("schema")) is int and type(container.get("uid")) is int
                    and all(container.get(k) == v for k, v in expected.items())
                    and set(container) == (set(expected) | ({"native_route"} if role == "hermes" else set()))
                    and (role != "hermes" or container["native_route"] == ready["native_route"])
                    and self.gateway.probe_observed(role, nonce), "runtime_route_unverified")
            require(self.checks.probe(target) == baseline, "model_session_boot_changed")
            self.active(); evidence = self.checks.evidence()
            evidence["model_channels"][self.stage]["runtime_routes"][role] = report
            private_write(self.checks.path, evidence)
            return copy.deepcopy(report)
        except BaseException:
            from showcase_cleanup import private_read
            try:
                evidence = private_read(self.checks.path)
                require(evidence.get("run_id") == self.checks.run.name and evidence.get("source_revision") == self.revision, "model_session_scope_changed")
                evidence["model_channels"][self.stage]["runtime_routes"][role]["status"] = "uncertain"
                private_write(self.checks.path, evidence)
            finally: self.close()
            raise PolicyError("runtime_route_unverified") from None

    def runtime_doctor(self, role):
        """Native diagnostic under denied generation; no task/hold is granted."""
        evidence = self.active(); require(role in ("openclaw", "hermes"), "invalid_runtime")
        config = self.runtime_configs.get(role)
        require(isinstance(config, dict) and config.get("lifecycle") is True, "runtime_route_required")
        row = evidence["model_channels"][self.stage]
        require(role not in row.get("runtime_doctors", {}), "runtime_doctor_already_invoked")
        ready = row.get("runtime_routes", {}).get(role)
        require(isinstance(ready, dict) and ready.get("status") == "runtime_route_verified"
                and ready.get("binding") == config["binding"], "runtime_route_required")
        from showcase_attempt import remaining_timeout
        require(remaining_timeout(self.checks.ensure()[1], 220, now=self.checks.now()) == 220, "runtime_doctor_time_required")
        baseline = evidence["baseline"] if self.stage == "before_reboot" else evidence["after_reboot"]
        require(self.checks.probe(config["target"]) == baseline, "model_session_boot_changed")
        def requests():
            return self.checks.ledger.operate(lambda state: copy.deepcopy(self.spend.account(state)["model_requests"]))
        previous = requests()
        row.setdefault("runtime_doctors", {})[role] = {"status": "started"}
        evidence["checks"][role + ".doctor"] = "started"; private_write(self.checks.path, evidence)
        try:
            with self.gateway.diagnostic_window(role):
                report = self.runtime_lifecycle_command(config["target"], {"action": "doctor", "binding": config["binding"],
                    "route": config["route"], "request_id": secrets.token_hex(16)}, 180)
                identity = {key: ready[key] for key in ("binding", "role", "container_id", "image_ref", "image_id", "config_sha256", "native_route")}
                if role == "hermes": identity["environment_sha256"] = ready["environment_sha256"]
                checks = {"service." + role: "pass", role + ".doctor": "pass"}
                if role == "hermes": checks.update({"hermes.config": "pass", "hermes.readiness": "pass"})
                require(isinstance(report, dict) and set(report) == set(identity) | {"schema", "status", "uid", "health", "watchdog", "doctor_sha256", "checks"}
                        | ({"readiness_sha256"} if role == "hermes" else set())
                        and type(report["schema"]) is int and report["schema"] == 1 and report["status"] == "runtime_doctor_verified"
                        and all(report.get(k) == v for k, v in identity.items()) and type(report["uid"]) is int
                        and report["uid"] == (1000 if role == "openclaw" else 10000) and report["health"] == "healthy"
                        and report["watchdog"] == "disarmed" and isinstance(report["doctor_sha256"], str)
                        and len(report["doctor_sha256"]) == 64 and all(c in "0123456789abcdef" for c in report["doctor_sha256"])
                        and report["checks"] == checks
                        and (role != "hermes" or isinstance(report["readiness_sha256"], str) and len(report["readiness_sha256"]) == 64
                             and all(c in "0123456789abcdef" for c in report["readiness_sha256"]))
                        and requests() == previous, "runtime_doctor_unverified")
                require(self.checks.probe(config["target"]) == baseline, "model_session_boot_changed")
                self.active()
            evidence = self.checks.evidence()
            evidence["model_channels"][self.stage]["runtime_doctors"][role] = report
            evidence["checks"].update(checks)
            private_write(self.checks.path, evidence)
            return copy.deepcopy(report)
        except BaseException:
            from showcase_cleanup import private_read
            try:
                evidence = private_read(self.checks.path)
                require(evidence.get("run_id") == self.checks.run.name and evidence.get("source_revision") == self.revision, "model_session_scope_changed")
                evidence["model_channels"][self.stage]["runtime_doctors"][role]["status"] = "uncertain"
                evidence["checks"].update({role + ".doctor": "uncertain", "service." + role: "uncertain"})
                private_write(self.checks.path, evidence)
            finally: self.close()
            raise PolicyError("runtime_doctor_unverified") from None

    def stage_runtime_config(self, role):
        """Stage a stopped runtime's file; does not restart it or authorize a task."""
        require(role in ("openclaw", "hermes"), "invalid_runtime")
        evidence = self.active()
        require(role not in self.runtime_configs, "runtime_config_already_invoked")
        target = self.checks.bind()
        baseline = evidence["baseline"] if self.stage == "before_reboot" else evidence["after_reboot"]
        require(self.checks.probe(target) == baseline, "model_session_boot_changed")
        bundle = self.transport.client(role)
        address = self.transport.ready["bridge_ipv4"]
        route = {**bundle, "base_url": "http://" + address + ":" + str(self.transport.ready["bridge_port"]) + "/" + role,
                 "bridge_ipv4": address, "model": self.policy["roles"][role]["model"],
                 "context_tokens": self.policy["roles"][role]["context_tokens_upper_bound"],
                 "output_tokens": self.policy["roles"][role]["output_tokens_upper_bound"]}
        from showcase_runtime_routes import route_valid
        route_valid(route)
        binding = {"run_id": self.checks.run.name, "source_revision": self.revision,
                   "boot_id": baseline["boot_id"], "checksum": baseline["checksum"]}
        # Restoration authority precedes SSH, covering a lost install reply.
        self.runtime_configs[role] = {"target": copy.deepcopy(target), "binding": binding}
        evidence["model_channels"][self.stage].setdefault("runtime_configs", {})[role] = {"status": "started"}
        private_write(self.checks.path, evidence)
        try:
            report = self.runtime_config_command(target, {"action": "install", "binding": binding, "route": route})
            require(set(report) == {"schema", "status", "role", "binding", "config_sha256", "base_url_sha256", "native_contract"}
                    and report["status"] == "runtime_config_installed" and report["role"] == role
                    and report["binding"] == binding and report["native_contract"] == "unverified"
                    and report["base_url_sha256"] == hashlib.sha256(route["base_url"].encode()).hexdigest()
                    and isinstance(report["config_sha256"], str) and len(report["config_sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in report["config_sha256"]), "runtime_config_route_unverified")
            require(self.checks.probe(target) == baseline, "model_session_boot_changed")
            self.active(); evidence = self.checks.evidence()
            evidence["model_channels"][self.stage]["runtime_configs"][role] = report
            private_write(self.checks.path, evidence)
            return copy.deepcopy(report)
        except BaseException:
            # An expired work window or lost ledger access must not prevent
            # capability revocation. This read is for failure recording only.
            from showcase_cleanup import private_read
            try:
                evidence = private_read(self.checks.path)
                require(evidence.get("run_id") == self.checks.run.name and evidence.get("source_revision") == self.revision,
                        "model_session_scope_changed")
                evidence["model_channels"][self.stage]["runtime_configs"][role]["status"] = "uncertain"
                private_write(self.checks.path, evidence)
            finally: self.close()
            raise PolicyError("runtime_config_route_unverified") from None

    def task_receipt(self, role, start):
        self.active()
        require(isinstance(start, dict) and set(start) == {"role", "started_at", "boot_id", "policy_sha256"}
                and role in self.tasks and start == self.tasks[role]
                and start["role"] == role and start["boot_id"] == self.transport.proof["boot_id"]
                and start["policy_sha256"] == digest(self.policy), "model_task_receipt_unverified")
        self.gateway.seal_role(role)
        self.active()
        def verify(state):
            row = self.spend.account(state)
            holds = [copy.deepcopy(hold) for hold in row["model_requests"] if hold["role"] == role]
            require(holds and all(hold["status"] == "response_received" and start["started_at"] <= hold["started_at"] <=
                                 self.checks.now() for hold in holds), "model_task_receipt_unverified")
            require(all("completion" in hold for hold in holds) and holds[-1]["completion"]["kind"] == "text",
                    "model_task_completion_unverified")
            return {"schema": 1, "status": "provider_completions_verified", "run_id": self.checks.run.name,
                    "source_revision": self.policy["source_revision"], "scope": copy.deepcopy(self.policy["scope"]),
                    "role": role, "provider": self.policy["roles"][role]["provider"], "model": self.policy["roles"][role]["model"],
                    "boot_id": start["boot_id"], "policy_sha256": digest(self.policy), "quote_sha256": row["quote_sha256"],
                    "requests": holds}
        receipt = self.checks.ledger.operate(verify)
        del self.tasks[role]
        return receipt

    def close(self):
        if self.state in ("new", "closed"): return {"status": "closed"}
        require(self.state != "closed_unverified", "model_session_shutdown_unverified")
        self.state = "stopping"
        failed = False
        for component in (self.transport, self.gateway):
            if component is not None:
                try: component.close()
                except BaseException: failed = True
        # Revoke the channel before restoring files. Even a failed restore
        # cannot leave the staged capability able to buy another response.
        restored = {}
        for role, config in self.runtime_configs.items():
            try:
                packet = {"action": "restore", "binding": config["binding"], "role": role}
                report = (self.runtime_lifecycle_command(config["target"], packet, 120) if config.get("lifecycle")
                          else self.runtime_config_command(config["target"], packet))
                require(set(report) == {"schema", "status", "role", "binding", "original_sha256"}
                        and report["status"] == "runtime_config_restored" and report["role"] == role
                        and report["binding"] == config["binding"] and isinstance(report["original_sha256"], str)
                        and len(report["original_sha256"]) == 64 and
                        all(c in "0123456789abcdef" for c in report["original_sha256"]), "runtime_config_restore_unverified")
                restored[role] = report
            except BaseException:
                failed = True; restored[role] = {"status": "uncertain"}
        self.state = "closed_unverified" if failed else "closed"
        self.routes.clear()
        self.tasks.clear()
        from showcase_cleanup import private_read
        if self.checks.path.exists():
            evidence = private_read(self.checks.path)
            require(evidence.get("run_id") == self.checks.run.name and evidence.get("source_revision") ==
                    self.revision, "model_session_scope_changed")
            row = evidence.get("model_channels", {}).get(self.stage)
            if row is not None:
                row["shutdown"] = "uncertain" if failed else "verified"
                if restored: row["runtime_config_restores"] = restored
                for role, config in self.runtime_configs.items():
                    if config.get("lifecycle"):
                        for key in ("service." + role, role + ".doctor", role + ".config", role + ".readiness"):
                            if key in evidence["checks"]: evidence["checks"][key] = "uncertain"
                private_write(self.checks.path, evidence)
        if not failed: self.runtime_configs.clear()
        require(not failed, "model_session_shutdown_unverified")
        return {"status": "closed"}
