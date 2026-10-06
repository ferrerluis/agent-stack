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
from showcase_policy import PolicyError, require
from showcase_preflight import PreflightError
from showcase_remote import RemoteChecks

CELLS = ("launch_ssh", "openclaw", "hermes", "claude", "codex", "tailscale")
SOURCES = tuple("scripts/" + name + ".py" for name in ("showcase_controller", "showcase_attempt", "showcase_cleanup", "showcase_policy",
    "showcase_preflight", "showcase_models", "showcase_model_spend", "showcase_model_prices", "showcase_model_gateway", "showcase_model_transport", "showcase_model_relay", "showcase_prices", "showcase_aws_prices", "showcase_gcp_prices", "showcase_hetzner_prices", "showcase_terraform", "showcase_remote", "showcase_agents", "showcase_browser", "showcase_peer", "showcase_container_probe", "test_containers"))


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
    def __init__(self, attempt, ledger, remote=None, now=time.time):
        self.attempt, self.ledger = attempt, ledger
        self.run, self.config, self.provider = attempt.run, attempt.config, attempt.provider
        self.remote = RemoteChecks(attempt, ledger) if remote is None else remote
        self.now = now
        self.path = self.run / "showcase-run.json"

    def verdict(self, observed, evidence=None):
        evidence = self.remote.evidence() if evidence is None else evidence
        require(evidence.get("source_revision") == self.attempt.prepared["source_revision"], "acceptance_source_changed")
        required = self.remote.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        checks = evidence["checks"]
        require(all(checks.get(name) == "pass" for name in required | {"launch_ssh", "reboot", "tailscale.acceptance",
                "openclaw.agent_turn", "hermes.agent_turn", "workspace.codex.execution", "workspace.claude.execution"}), "acceptance_incomplete")
        require(observed["apply"] == {"deployment": "applied", "acceptance": "pending", "live_matrix": "not_tested"}
                and observed["doctors"] == {name: "pass" for name in required}, "acceptance_incomplete")
        before, after = evidence.get("baseline"), evidence.get("after_reboot")
        require(isinstance(before, dict) and isinstance(after, dict) and before["boot_id"] != after["boot_id"]
                and all(before[key] == after[key] for key in ("disk_uuid", "data_volume_id", "checksum", "dns")), "persistence_proof_required")
        require(observed["bootstrap"] == before and observed["reboot"] == {"reboot": "pass", "workspace_files": "preserved", "doctors": "pass"},
                "persistence_proof_required")
        for runtime in ("openclaw", "hermes"):
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
        gate = self.attempt.launch_gates(readiness)
        record = {"schema": 1, "run_id": self.run.name, "provider": self.provider, "scope": scope(self.config, self.provider),
                  "source_revision": prepared["source_revision"], "stage": "reserving", "started_at": self.now(), "steps": [],
                  "container_tests_gate": gate, "cells": {key: "unverified" for key in CELLS}, "cleanup": "not_started"}
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
            row.update(status="pass", proof_sha256=proof_hash(value))
            private_write(self.path, record)
            return value
        try:
            self.ledger.reserve(self.provider, self.run.name, quote, now=self.now())
            record["stage"] = "reserved"
            private_write(self.path, record)
            observed["apply"] = step("apply", lambda: self.attempt.apply(self.ledger, readiness))
            observed["bootstrap"] = step("bootstrap", self.remote.bootstrap)
            if gate == "deferred_owned_vm":
                observed["containers"] = step("containers", self.remote.container_tests)
            observed["doctors"] = step("doctors", self.remote.doctors)
            for runtime in ("openclaw", "hermes"):
                observed[runtime] = step(runtime, lambda runtime=runtime: self.remote.runtime_task(runtime))
            for cli in ("claude", "codex"):
                observed[cli] = step(cli, lambda cli=cli: self.remote.workspace_task(cli))
            observed["before_reboot"] = step("before_reboot", lambda: self.remote.network_checks("before_reboot"))
            observed["reboot"] = step("reboot", self.remote.reboot)
            observed["after_reboot"] = step("after_reboot", lambda: self.remote.network_checks("after_reboot"))
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
            record.update(stage="finished", finished_at=self.now(), status="pass" if acceptance and record["cleanup"] == "verified" else "fail")
            if record["status"] != "pass":
                record["cells"] = {key: "unverified" for key in CELLS}
                record["reason"] = failure or "showcase_acceptance_incomplete"
            private_write(self.path, record)
        return public_report(record)

    def recover(self):
        with controller_lock(self.run):
            prepared = verify_prepared(self.run, self.config, self.provider)
            record = private_read(self.path)
            require(record.get("schema") == 1 and record.get("run_id") == self.run.name and record.get("provider") == self.provider
                    and record.get("scope") == scope(self.config, self.provider)
                    and record.get("source_revision") == prepared["source_revision"], "showcase_recovery_scope_changed")
            # Cleanup is always available, even if acceptance is incomplete or
            # damaged. Do not replay any original model/apply/reboot step.
            record.update(stage="cleanup", cleanup="started", status="fail", cells={key: "unverified" for key in CELLS})
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
                    record["status"] = "pass"
                    record.pop("reason", None)
                    record.pop("failure_stage", None)
                except BaseException:
                    record.update(reason="showcase_acceptance_incomplete", cells={key: "unverified" for key in CELLS})
            record.update(stage="finished", finished_at=self.now())
            private_write(self.path, record)
            return public_report(record)
