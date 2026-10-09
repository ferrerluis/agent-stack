#!/usr/bin/env python3
"""One measured, reserved showcase attempt with acceptance and final cleanup.

This component takes gate results from the future production measurer; it is
not exposed as a readiness-file override or a paid public CLI phase. Recovery
never replays apply, model tasks or reboot after an interrupted controller.
"""
import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from showcase_attempt import private_directory, verify_prepared
from showcase_cleanup import private_read, private_write, scope
from showcase_policy import PolicyError, require, acceptance_mode
from showcase_preflight import PreflightError
from showcase_remote import RemoteChecks
import showcase_task_evidence as task_evidence
import showcase_runtime_evidence as runtime_evidence

CELLS = ("launch_ssh", "openclaw", "hermes", "claude", "codex", "tailscale")
FULL_GROUPS = ("runtime_doctors", "workspace_api_key_tasks", "authenticated_tailscale_urls", "reboot_persistence",
               "documentation_and_linked_plans", "verified_cleanup")


def cell_names(mode):
    return ("vm_and_containers",) if mode == "light" else FULL_GROUPS if mode == "full" else CELLS


SOURCES = ("modules/common/runtime/hermes_health.py", "modules/common/runtime/workspace_cli_task.py", "modules/common/runtime/workspace_sandbox.py", "modules/common/runtime/workspace_sandbox_profiles.json") + tuple("scripts/" + name + ".py" for name in ("showcase_fingerprints", "showcase_contents", "showcase_workspace_sandbox", "showcase_light", "showcase_launch", "showcase_network_prices", "showcase_watchdog", "showcase_credentials", "showcase_controller", "showcase_attempt", "showcase_cleanup", "showcase_policy",
    "showcase_preflight", "showcase_models", "showcase_model_completion", "showcase_task_evidence", "showcase_model_spend", "showcase_model_prices", "showcase_model_gateway", "showcase_model_transport", "showcase_model_relay", "showcase_model_session", "showcase_model_container", "showcase_runtime_routes", "showcase_runtime_environment", "showcase_runtime_probe", "showcase_runtime_task", "showcase_runtime_evidence", "showcase_runtime_lifecycle", "showcase_prices", "showcase_aws_prices", "showcase_gcp_prices", "showcase_hetzner_prices", "showcase_terraform", "showcase_remote", "showcase_agents", "showcase_browser", "showcase_peer", "showcase_container_probe", "test_containers"))


def proof_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def checked_controller_source(attempt):
    root = Path(__file__).resolve().parents[1]
    for name in SOURCES:
        path = attempt.checkout / name
        require(path.is_file() and not path.is_symlink() and path.read_bytes() == (root / name).read_bytes(), "controller_source_revision_changed")


@contextmanager
def controller_lock(run):
    private_directory(run)
    fd = os.open(run / "showcase-controller.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0, "private_lock_required")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PolicyError("showcase_run_busy") from None
        yield


def public_report(record):
    report = {key: record[key] for key in ("status", "run_id", "provider", "source_revision", "cells", "cleanup")}
    if "acceptance_mode" in record:
        report["acceptance_mode"] = record["acceptance_mode"]
    if record["status"] != "pass":
        report["reason"] = record["reason"]
        report["failed_step"] = record.get("failure_stage", "acceptance")
    return report


def prepare_container_deferral(attempt):
    import shutil
    require(shutil.which("docker") is None, "local_docker_available")
    verify_prepared(attempt.run, attempt.config, attempt.provider)
    path = attempt.run / "container-bundle.json"
    require(not path.exists() and not path.is_symlink(), "container_bundle_already_exists")
    module = attempt.container_module()
    bundle = module.render_bundle(attempt.checkout, attempt.prepared["source_revision"], attempt.config["pins"], attempt.terraform)
    sha = module.validate_bundle(bundle)
    private_write(path, bundle)
    return {"mode": "first_owned_vm", "run_id": attempt.run.name, "source_revision": attempt.prepared["source_revision"],
            "bundle_sha256": sha, "local_docker": "absent"}


class RunController:
    def __init__(self, attempt, ledger, remote=None, now=time.time, models=None):
        self.attempt, self.ledger = attempt, ledger
        self.run, self.config, self.provider = attempt.run, attempt.config, attempt.provider
        self.remote = RemoteChecks(attempt, ledger) if remote is None else remote
        self.now = now
        self.models = models
        self.mode = acceptance_mode(self.config, self.provider)
        self.path = self.run / "showcase-run.json"

    def verdict(self, observed, evidence=None):
        evidence = self.remote.evidence() if evidence is None else evidence
        require(evidence.get("source_revision") == self.attempt.prepared["source_revision"], "acceptance_source_changed")
        if self.mode == "light":
            from showcase_light import validate
            require(set(observed) == {"apply", "light", "resolved_contents"} and observed["apply"] == {
                    "deployment": "applied", "acceptance": "pending", "live_matrix": "not_tested"}
                    and evidence["checks"] == {"vm_and_containers": "pass"}
                    and not evidence.get("tasks") and not evidence.get("agents") and not evidence.get("model_channels")
                    and not evidence.get("network") and evidence.get("baseline") is None
                    and observed["light"] == evidence.get("light"), "light_proof_invalid")
            validate(observed["light"], self.run.name, evidence["source_revision"], scope(self.config, self.provider), self.config["pins"])
            row = self.reservation()
            require(row is not None and not row.get("model_requests") and "model_policy" not in row, "light_model_authority_forbidden")
            return {"vm_and_containers": "pass"}
        runtime_tasks = self.mode == "legacy_full"
        required = self.remote.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        runtime_required = self.remote.doctor_module().required_checks(["openclaw", "hermes"], False, False)
        checks = evidence["checks"]
        task_checks = {"openclaw.agent_turn", "hermes.agent_turn"} if runtime_tasks else set()
        require(all(checks.get(name) == "pass" for name in (required - runtime_required) | task_checks | {"launch_ssh", "reboot", "reboot_boot", "tailscale.acceptance",
                "workspace.codex.execution", "workspace.claude.execution"}), "acceptance_incomplete")
        # Verified close restores files while stopped. Historical diagnostics
        # prove the tested boots; current runtime service status must be uncertain.
        require(all(checks.get(name) == "uncertain" for name in runtime_required), "runtime_shutdown_proof_changed")
        require(observed["apply"] == {"deployment": "applied", "acceptance": "pending", "live_matrix": "not_tested"}
                and all(observed[name] == {name: "pass" for name in required} for name in ("doctors", "doctors_after_reboot")), "acceptance_incomplete")
        before, after = evidence.get("baseline"), evidence.get("after_reboot")
        require(isinstance(before, dict) and isinstance(after, dict) and before["boot_id"] != after["boot_id"]
                and all(before[key] == after[key] for key in ("disk_uuid", "data_volume_id", "checksum", "dns")), "persistence_proof_required")
        require(observed["bootstrap"] == before and observed["reboot"] == {"reboot": "pass", "workspace_files": "preserved", "doctors": "pending"}
                and observed["reboot_verified"] == {"reboot": "pass", "workspace_files": "preserved", "doctors": "pass"},
                "persistence_proof_required")
        for runtime in (("openclaw", "hermes") if runtime_tasks else ()):
            row = evidence.get("agents", {}).get(runtime, {})
            expected = observed[runtime]
            require(row.get("status") == "pass" and row.get("model") == self.config["models"][runtime]
                    and all(row.get(key) == value for key, value in expected.items()), "runtime_proof_changed")
            challenge = row.get("challenge")
            require(isinstance(challenge, dict) and set(challenge) == {"lhs", "rhs", "nonce"}
                    and all(type(challenge[k]) is int for k in ("lhs", "rhs")) and isinstance(challenge["nonce"], str)
                    and re.fullmatch(r"[a-f0-9]{32}", challenge["nonce"]), "runtime_proof_changed")
            answer = {"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]}
            require(row.get("answer_sha256") == hashlib.sha256(json.dumps(answer, sort_keys=True).encode()).hexdigest(), "runtime_proof_changed")
        for cli in ("codex", "claude"):
            row = evidence.get("tasks", {}).get(cli, {})
            require(row == observed[cli] and row.get("status") == "pass" and row.get("model") == self.config["models"][cli], "coding_proof_changed")
        # Client reports cannot create billing proof. Match every request to
        # the original durable account, including recovery after deletion.
        def account(state):
            rows = [row for row in state["attempts"] if row["provider"] == self.provider and row["run_id"] == self.run.name]
            require(len(rows) == 1 and "model_policy" in rows[0], "model_task_receipt_unverified")
            return rows[0]
        model_account = self.ledger.operate(account)
        nonces = []
        for stage, baseline, suffix, doctor_name, receipt_name in (
                ("before_reboot", before, "", "doctors", "doctor_evidence"),
                ("after_reboot", after, "_after_reboot", "doctors_after_reboot", "doctor_evidence_after_reboot")):
            channel = evidence.get("model_channels", {}).get(stage, {})
            binding = {"run_id": self.run.name, "source_revision": evidence["source_revision"],
                       "boot_id": baseline["boot_id"], "checksum": baseline["checksum"]}
            stage_proof = evidence.get("doctor_stages", {}).get(stage, {})
            require(set(channel.get("runtime_tasks", {})) == ({"openclaw", "hermes"} if runtime_tasks and stage == "before_reboot" else set()),
                    "runtime_task_proof_changed")
            require(stage_proof == observed[receipt_name] and set(stage_proof) == {"schema", "status", "run_id", "source_revision",
                    "boot_id", "checksum", "checks", "runtime_reports_sha256", "runtime_routes_sha256"}
                    and type(stage_proof.get("schema")) is int and stage_proof["schema"] == 1 and stage_proof["status"] == "verified"
                    and all(stage_proof.get(k) == v for k, v in binding.items()) and stage_proof["checks"] == observed[doctor_name]
                    and isinstance(stage_proof["runtime_reports_sha256"], dict) and isinstance(stage_proof["runtime_routes_sha256"], dict)
                    and set(stage_proof["runtime_reports_sha256"]) == set(stage_proof["runtime_routes_sha256"]) == {"openclaw", "hermes"}
                    and set(channel.get("runtime_config_restores", {})) == {"openclaw", "hermes"}, "runtime_doctor_proof_changed")
            for role in ("openclaw", "hermes"):
                route = channel.get("runtime_routes", {}).get(role, {})
                doctor = channel.get("runtime_doctors", {}).get(role, {})
                restored = channel["runtime_config_restores"][role]
                role_required = self.remote.doctor_module().required_checks([role])
                image = ("ghcr.io/openclaw/openclaw@" + self.config["pins"]["openclaw_digest"] if role == "openclaw" else self.config["pins"]["hermes_image"])
                require(route == observed[role + "_activation" + suffix] and route.get("binding") == binding
                        and type(route.get("schema")) is int and route["schema"] == 1 and route.get("status") == "runtime_route_verified"
                        and route.get("role") == role and route.get("watchdog") == "disarmed" and route.get("image_ref") == image
                        and isinstance(route.get("container_id"), str) and re.fullmatch(r"[a-f0-9]{64}", route["container_id"])
                        and isinstance(route.get("image_id"), str) and re.fullmatch(r"sha256:[a-f0-9]{64}", route["image_id"])
                        and stage_proof["runtime_routes_sha256"][role] == proof_hash(route)
                        and stage_proof["runtime_reports_sha256"][role] == proof_hash(doctor), "runtime_doctor_proof_changed")
                identity = {k: route.get(k) for k in ("binding", "role", "container_id", "image_ref", "image_id", "config_sha256", "native_route")}
                if role == "hermes": identity["environment_sha256"] = route.get("environment_sha256")
                require(set(doctor) == set(identity) | {"schema", "status", "uid", "health", "watchdog", "doctor_sha256", "checks"}
                        | ({"readiness_sha256"} if role == "hermes" else set()) and all(doctor.get(k) == v for k, v in identity.items())
                        and type(doctor.get("schema")) is int and doctor["schema"] == 1 and doctor.get("status") == "runtime_doctor_verified"
                        and type(doctor.get("uid")) is int and doctor["uid"] == (1000 if role == "openclaw" else 10000)
                        and doctor.get("health") == "healthy" and doctor.get("watchdog") == "disarmed"
                        and doctor.get("checks") == {name: "pass" for name in role_required}, "runtime_doctor_proof_changed")
                for key in ("config_sha256", "doctor_sha256") + (("environment_sha256", "readiness_sha256") if role == "hermes" else ()):
                    require(isinstance(doctor.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", doctor[key]), "runtime_doctor_proof_changed")
                terms = model_account["model_policy"]["roles"][role]
                public_route = {"role": role, "provider": terms["provider"], "model": terms["model"], "credential": "0" * 64,
                    "base_url": "http://" + channel["bridge_ipv4"] + ":" + str(channel["bridge_port"]) + "/" + role,
                    "bridge_ipv4": channel["bridge_ipv4"], "context_tokens": terms["context_tokens_upper_bound"], "output_tokens": terms["output_tokens_upper_bound"]}
                from showcase_runtime_probe import native_proof_valid
                try: native_proof_valid(public_route, doctor["native_route"])
                except (ValueError, TypeError, KeyError): raise PolicyError("runtime_doctor_proof_changed") from None
                if runtime_tasks and stage == "before_reboot":
                    row = evidence["agents"][role]; task = channel["runtime_tasks"][role]
                    try:
                        checked = runtime_evidence.validate(task, route, doctor, public_route, binding, image,
                            row["challenge"], row["request_id"], stored=True)
                        require(checked == task and proof_hash(task) == row.get("runtime_task_sha256") == observed[role].get("runtime_task_sha256")
                                and all(row.get(k) == v for k, v in checked["turn"].items()), "runtime_task_proof_changed")
                    except (ValueError, TypeError, KeyError, runtime_evidence.agents.ProbeError):
                        raise PolicyError("runtime_task_proof_changed") from None
                container = route.get("container_probe", {})
                require(container.get("status") == "runtime_container_route_verified" and type(container.get("schema")) is int and container["schema"] == 1
                        and container.get("role") == role and container.get("provider") == terms["provider"] and type(container.get("uid")) is int
                        and container["uid"] == doctor["uid"] and container.get("source_revision") == evidence["source_revision"]
                        and container.get("config_sha256") == doctor["config_sha256"] and isinstance(container.get("nonce"), str)
                        and re.fullmatch(r"[a-f0-9]{64}", container["nonce"])
                        and (role != "hermes" or container.get("environment_sha256") == doctor["environment_sha256"]
                             and container.get("native_route") == doctor["native_route"]), "runtime_doctor_proof_changed")
                nonces.append(container["nonce"])
                require(set(restored) == {"schema", "status", "role", "binding", "original_sha256"}
                        and type(restored.get("schema")) is int and restored["schema"] == 1 and restored.get("role") == role
                        and restored.get("binding") == binding and restored.get("status") == "runtime_config_restored"
                        and isinstance(restored.get("original_sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", restored["original_sha256"]), "runtime_shutdown_proof_changed")
        require(len(set(nonces)) == 4, "runtime_doctor_proof_changed")
        for role in (("openclaw", "hermes", "codex", "claude") if runtime_tasks else ("codex", "claude")):
            receipt = observed[role].get("gateway_receipt")
            expected = [hold for hold in model_account["model_requests"] if hold["role"] == role]
            require(isinstance(receipt, dict) and set(receipt) == {"schema", "status", "run_id", "source_revision", "scope", "role",
                    "provider", "model", "boot_id", "policy_sha256", "quote_sha256", "requests"}
                    and type(receipt.get("schema")) is int and receipt["schema"] == 1
                    and receipt.get("status") == "provider_completions_verified"
                    and receipt.get("run_id") == self.run.name and receipt.get("source_revision") == evidence["source_revision"]
                    and receipt.get("scope") == model_account["model_policy"]["scope"] and receipt.get("role") == role
                    and receipt.get("provider") == model_account["model_policy"]["roles"][role]["provider"]
                    and receipt.get("model") == model_account["model_policy"]["roles"][role]["model"]
                    and receipt.get("boot_id") == before["boot_id"]
                    and receipt.get("policy_sha256") == proof_hash(model_account["model_policy"])
                    and receipt.get("quote_sha256") == model_account["quote_sha256"] and expected and
                    receipt.get("requests") == expected and all(hold["status"] == "response_received" and "completion" in hold for hold in expected)
                    and expected[-1]["completion"]["kind"] == "text",
                    "model_task_receipt_unverified")
            row = evidence["agents" if role in ("openclaw", "hermes") else "tasks"][role]
            try:
                proof = task_evidence.correlate(role, row["challenge"], row["model_usage"], expected, model_account["model_policy"]["roles"][role])
                require(row.get("provider_task") == proof and observed[role].get("provider_task") == proof
                        and row.get("answer_sha256") == proof["answer_sha256"], "model_task_provider_mismatch")
                if role in ("codex", "claude"):
                    require(row.get("input_sha256") == task_evidence.input_hash(row["challenge"]), "coding_proof_changed")
            except (ValueError, TypeError, KeyError, IndexError):
                raise PolicyError("model_task_provider_mismatch") from None
        for stage, name, closed_name, baseline in (("before_reboot", "model_channel", "model_channel_closed", before),
                ("after_reboot", "model_channel_after_reboot", "model_channel_after_reboot_closed", after)):
            channel = evidence.get("model_channels", {}).get(stage, {})
            require(channel.get("status") == "private_channel_verified" and channel.get("shutdown") == "verified"
                    and channel.get("boot_id") == baseline["boot_id"] and channel.get("checksum") == baseline["checksum"]
                    and channel.get("source_revision") == evidence["source_revision"] and channel.get("run_id") == self.run.name
                    and all(channel.get(key) == value for key, value in observed[name].items())
                    and observed[closed_name] == {"status": "closed"}, "model_channel_proof_changed")
        routes = evidence["model_channels"]["before_reboot"].get("workspace_routes", {})
        require(set(routes) == {"codex", "claude"} and all(routes[cli].get("status") == "workspace_route_verified"
                and type(routes[cli].get("uid")) is int and routes[cli]["uid"] == 1000 for cli in routes), "workspace_model_route_unverified")
        requests = []
        for stage, boot in (("before_reboot", before["boot_id"]), ("after_reboot", after["boot_id"])):
            row = evidence.get("network", {}).get(stage, {})
            expected = observed[stage]
            require(row.get("status") == "pass" and row.get("boot_id") == boot
                    and all(row.get(key) == value for key, value in expected.items()), "peer_proof_changed")
            requests.append(row.get("request_id"))
        require(all(isinstance(value, str) for value in requests) and requests[0] != requests[1], "fresh_peer_proof_required")
        self.remote.require_container_gate(evidence)
        if "containers" in observed:
            require(evidence.get("containers") == observed["containers"] and checks.get("container_tests") == "pass", "container_proof_changed")
        if self.mode == "full":
            from showcase_workspace_sandbox import verify
            for stage, name, baseline in (("before_reboot", "workspace_sandbox", before), ("after_reboot", "workspace_sandbox_after_reboot", after)):
                row = evidence.get("workspace_sandbox_checks", {}).get(stage)
                require(isinstance(row, dict) and row == observed[name] and row.get("boot_id") == baseline["boot_id"], "workspace_sandbox_proof_changed")
                try: verify(row, row["nonce"])
                except (AssertionError, ValueError, TypeError, KeyError): raise PolicyError("workspace_sandbox_unverified") from None
            require(not evidence.get("agents") and not any(hold["role"] in ("openclaw", "hermes")
                    for hold in model_account["model_requests"]), "unexpected_runtime_task")
            return {key: "pass" if key in FULL_GROUPS[:4] else "unverified" for key in FULL_GROUPS}
        return {key: "pass" for key in CELLS}

    def reservation(self):
        def select(state):
            matches = [row for row in state["attempts"] if row["run_id"] == self.run.name and row["provider"] == self.provider]
            require(len(matches) <= 1, "attempt_scope_mismatch")
            return dict(matches[0]) if matches else None
        return self.ledger.operate(select)

    def run_attempt(self, readiness, quote):
        with controller_lock(self.run):
            return self._run(readiness, quote)

    def _run(self, readiness, quote):
        prepared = verify_prepared(self.run, self.config, self.provider)
        checked_controller_source(self.attempt)
        require(not self.path.exists() and not self.path.is_symlink(), "showcase_recovery_required")
        require(self.attempt.record().get("stage") == "planned", "fresh_planned_attempt_required")
        from showcase_model_session import ModelSession
        require((self.models is None if self.mode == "light" else isinstance(self.models, ModelSession)
                and self.models.checks is self.remote), "measured_model_session_required")
        gate = self.attempt.launch_gates(readiness)
        record = {"schema": 1, "run_id": self.run.name, "provider": self.provider, "scope": scope(self.config, self.provider),
                  "source_revision": prepared["source_revision"], "stage": "reserving", "started_at": self.now(), "steps": [],
                  "acceptance_mode": self.mode, "container_tests_gate": gate,
                  "cells": {key: "unverified" for key in cell_names(self.mode)}, "cleanup": "not_started"}
        private_write(self.path, record)
        observed = {}
        acceptance = False
        failure = None
        def step(name, action):
            record["stage"] = name
            row = {"name": name, "status": "started"}
            record["steps"].append(row)
            private_write(self.path, record)
            value = action()
            observed[name] = value
            row.update(status="pass", proof_sha256=proof_hash(value))
            private_write(self.path, record)
            return value
        try:
            self.ledger.reserve(self.provider, self.run.name, quote, now=self.now())
            record["stage"] = "reserved"
            private_write(self.path, record)
            observed["apply"] = step("apply", lambda: self.attempt.apply(self.ledger, readiness))
            if self.mode == "light":
                observed["light"] = step("light", self.remote.light)
                observed["resolved_contents"] = step("resolved_contents", self.remote.resolved_contents)
            else:
                self.full_steps(step, observed, gate)
            evidence = self.remote.evidence()
            record["cells"] = self.verdict(observed, evidence)
            record["acceptance"] = {"status": "verified", "observed": observed,
                "remote_evidence_sha256": proof_hash(evidence)}
            acceptance = True
        except BaseException as error:
            known = {"onepassword_unavailable_or_locked", "insufficient_budget", "attempt_limit_reached", "cleanup_required_before_launch"}
            failure = str(error) if isinstance(error, (PolicyError, PreflightError)) and str(error) in known else "showcase_step_failed"
            if record["steps"] and record["steps"][-1]["status"] == "started":
                record["steps"][-1]["status"] = "uncertain"
            record["failure_stage"] = record["stage"]
        finally:
            if self.models is not None:
                try: self.models.close()
                except BaseException:
                    acceptance = False
                    failure = "model_session_shutdown_unverified"
                    record.setdefault("failure_stage", "model_channel_close")
            # Even a failed/uncertain reserve can have committed a ledger row.
            # Inspect only this run, never clean another active provider/run.
            try:
                reservation = self.reservation()
                if reservation is not None and reservation["status"] == "active":
                    record.update(stage="cleanup", cleanup="started")
                    private_write(self.path, record)
                    result = self.attempt.cleanup(self.ledger)
                    require(result.get("cleanup") == "verified" and self.reservation()["status"] == "cleaned", "cleanup_unverified")
                    record["cleanup"] = "verified"
                elif reservation is not None:
                    require(reservation["status"] == "cleaned" and self.attempt.record()["stage"] == "cleaned"
                            and reservation.get("cleanup_receipt") == private_read(self.run / "cleanup-receipt.json"), "cleanup_unverified")
                    record["cleanup"] = "verified"
                else:
                    record["cleanup"] = "not_reserved"
            except BaseException:
                record["cleanup"] = "uncertain"
                failure = failure or "cleanup_unverified"
                record.setdefault("failure_stage", "cleanup")
            if acceptance and record["cleanup"] == "verified":
                try:
                    require(proof_hash(private_read(self.remote.path)) == record["acceptance"]["remote_evidence_sha256"], "acceptance_evidence_changed")
                except BaseException:
                    acceptance = False
                    failure = "acceptance_evidence_changed"
                    record["failure_stage"] = "acceptance"
            if self.mode in ("full", "light"):
                try:
                    from showcase_fingerprints import recipe_from_plan, record_checks, GROUPS, proof, context
                    recipe = recipe_from_plan(private_read(self.run / "deployment-plan.json"), self.provider)
                    record["fingerprints"] = record_checks(self.attempt.checkout, record, self.config, recipe, observed)
                    if record["cleanup"] == "verified":
                        origin = {key: record[key] for key in ("run_id", "source_revision", "provider", "scope")}
                        components = context(self.attempt.checkout, self.config, self.provider, recipe)
                        record["fingerprints"]["checks"]["verified_cleanup"] = proof(self.attempt.checkout,
                            "verified_cleanup", components, private_read(self.run / "cleanup-receipt.json"), origin, {})
                    for group, status in record["cells"].items():
                        if status == "pass":
                            require(all(record["fingerprints"]["checks"].get(name, {}).get("status") == "pass"
                                        for name in GROUPS[group]), "fingerprint_proof_unverified")
                except BaseException:
                    acceptance = False
                    failure = failure or "fingerprint_proof_unverified"
                    record.setdefault("failure_stage", "fingerprints")
            record["check_observations"] = observed
            record.update(stage="finished", finished_at=self.now(), status="pass" if acceptance and record["cleanup"] == "verified" else "fail")
            if record["status"] != "pass":
                record["cells"] = {key: "unverified" for key in cell_names(self.mode)}
                record["reason"] = failure or "showcase_acceptance_incomplete"
            elif self.mode == "full":
                record["cells"]["verified_cleanup"] = "pass"
            private_write(self.path, record)
        return public_report(record)

    def full_steps(self, step, observed, gate):
        observed["bootstrap"] = step("bootstrap", self.remote.bootstrap)
        if gate == "deferred_owned_vm":
            observed["containers"] = step("containers", self.remote.container_tests)
        if self.mode == "full":
            observed["resolved_contents"] = step("resolved_contents", self.remote.resolved_contents)
            observed["workspace_sandbox"] = step("workspace_sandbox", lambda: self.remote.workspace_sandbox("before_reboot"))
        observed["model_channel"] = step("model_channel", lambda: self.models.start("before_reboot"))
        for runtime in ("openclaw", "hermes"):
            observed[runtime + "_activation"] = step(runtime + "_activation", lambda runtime=runtime: self.models.activate_runtime(runtime))
        observed["doctors"] = step("doctors", self.remote.doctors)
        observed["doctor_evidence"] = step("doctor_evidence", self.remote.doctor_receipt)
        for runtime in (("openclaw", "hermes") if self.mode == "legacy_full" else ()):
            observed[runtime] = step(runtime, lambda runtime=runtime: self.remote.runtime_task(runtime))
        # A Codex startup failure must not cause an unnecessary Claude call.
        # Both named checks and their independent evidence are required.
        for cli in ("codex", "claude"):
            observed[cli] = step(cli, lambda cli=cli: self.remote.workspace_task(cli))
        observed["before_reboot"] = step("before_reboot", lambda: self.remote.network_checks("before_reboot"))
        observed["model_channel_closed"] = step("model_channel_closed", self.models.close)
        observed["reboot"] = step("reboot", lambda: self.remote.reboot(defer_doctors=True))
        if self.mode == "full":
            observed["resolved_contents_after_reboot"] = step("resolved_contents_after_reboot", self.remote.resolved_contents)
            observed["workspace_sandbox_after_reboot"] = step("workspace_sandbox_after_reboot", lambda: self.remote.workspace_sandbox("after_reboot"))
        observed["model_channel_after_reboot"] = step("model_channel_after_reboot", lambda: self.models.start("after_reboot"))
        for runtime in ("openclaw", "hermes"):
            observed[runtime + "_activation_after_reboot"] = step(runtime + "_activation_after_reboot", lambda runtime=runtime: self.models.activate_runtime(runtime))
        observed["doctors_after_reboot"] = step("doctors_after_reboot", self.remote.doctors)
        observed["doctor_evidence_after_reboot"] = step("doctor_evidence_after_reboot", self.remote.doctor_receipt)
        observed["reboot_verified"] = step("reboot_verified", self.remote.complete_reboot)
        observed["after_reboot"] = step("after_reboot", lambda: self.remote.network_checks("after_reboot"))
        observed["model_channel_after_reboot_closed"] = step("model_channel_after_reboot_closed", self.models.close)

    def recover(self):
        with controller_lock(self.run):
            prepared = verify_prepared(self.run, self.config, self.provider)
            record = private_read(self.path)
            require(record.get("schema") == 1 and record.get("run_id") == self.run.name and record.get("provider") == self.provider
                    and record.get("scope") == scope(self.config, self.provider)
                    and record.get("source_revision") == prepared["source_revision"]
                    and record.get("acceptance_mode", "legacy_full") == self.mode, "showcase_recovery_scope_changed")
            # Cleanup is always available, even if acceptance is incomplete or
            # damaged. Do not replay any original model/apply/reboot step.
            record.update(stage="cleanup", cleanup="started", status="fail", cells={key: "unverified" for key in cell_names(self.mode)})
            private_write(self.path, record)
            try:
                reservation = self.reservation()
                require(reservation is not None, "active_attempt_required")
                if reservation["status"] == "active":
                    result = self.attempt.cleanup(self.ledger)
                    require(result.get("cleanup") == "verified", "cleanup_unverified")
                    reservation = self.reservation()
                require(reservation["status"] == "cleaned" and self.attempt.record()["stage"] == "cleaned"
                        and reservation.get("cleanup_receipt") == private_read(self.run / "cleanup-receipt.json"), "cleanup_unverified")
                record["cleanup"] = "verified"
            except BaseException:
                record.update(cleanup="uncertain", reason="cleanup_unverified", failure_stage="cleanup")
            if record["cleanup"] == "verified":
                try:
                    proof = record.get("acceptance")
                    require(isinstance(proof, dict) and proof.get("status") == "verified"
                            and proof_hash(private_read(self.remote.path)) == proof.get("remote_evidence_sha256"), "acceptance_incomplete")
                    # Bind the saved observations to the durable step receipts.
                    for name, value in proof["observed"].items():
                        rows = [row for row in record["steps"] if row["name"] == name and row["status"] == "pass"]
                        require(len(rows) == 1 and rows[0]["proof_sha256"] == proof_hash(value), "acceptance_incomplete")
                    record["cells"] = self.verdict(proof["observed"], private_read(self.remote.path))
                    if self.mode in ("full", "light"):
                        from showcase_fingerprints import recipe_from_plan, record_checks, GROUPS
                        recipe = recipe_from_plan(private_read(self.run / "deployment-plan.json"), self.provider)
                        recalculated = record_checks(self.attempt.checkout, record, self.config, recipe, proof["observed"])
                        saved = record.get("fingerprints", {}).get("checks", {})
                        require(all(recalculated["checks"][name] == saved.get(name) for name in recalculated["checks"]),
                                "fingerprint_evidence_changed")
                        require(saved.get("verified_cleanup", {}).get("evidence") == private_read(self.run / "cleanup-receipt.json"),
                                "fingerprint_cleanup_changed")
                        for group, status in record["cells"].items():
                            if status == "pass":
                                require(all(saved.get(name, {}).get("status") == "pass" for name in GROUPS[group]),
                                        "fingerprint_proof_unverified")
                    if self.mode == "full":
                        record["cells"]["verified_cleanup"] = "pass"
                    record["status"] = "pass"
                    record.pop("reason", None)
                    record.pop("failure_stage", None)
                except BaseException:
                    record.update(reason="showcase_acceptance_incomplete", cells={key: "unverified" for key in cell_names(self.mode)})
            record.update(stage="finished", finished_at=self.now())
            private_write(self.path, record)
            return public_report(record)
