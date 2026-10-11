#!/usr/bin/env python3
"""Opt-in live planning and recovery for isolated showcase attempts.

The run phase produces measured inputs and executes an approved bounded test.
This CLI never accepts a self-declared readiness file or price override.
Cleanup can delete only a recorded, reserved, ownership-checked applied attempt.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys

from showcase_attempt import prepare, private_directory, verify_prepared
from showcase_cleanup import private_read, scope
from showcase_controller import RunController
from showcase_policy import PROVIDERS, PolicyError, Reservations, require, secret_ref, validate_inputs
from showcase_preflight import PreflightError
from showcase_terraform import CloudSession, TerraformAttempt


def configure_provider_cache(run):
    """Use the same verified disk mirror as the local tests, when available."""
    from showcase_attempt import ROOT
    mirror = ROOT / ".tmp/agent-stack-tests/provider-mirror"
    if mirror.exists():
        require(mirror.is_dir() and not mirror.is_symlink()
                and mirror.stat().st_uid == os.geteuid(), "provider_mirror_refused")
        content = ('provider_installation {\n  filesystem_mirror {\n    path = '
                   + json.dumps(str(mirror))
                   + '\n    include = ["registry.terraform.io/*/*"]\n  }\n}\n')
        target = run / "terraform.rc"
        require(target.is_file() and not target.is_symlink(), "private_directory_required")
        target.write_text(content)
        target.chmod(0o600)


def recovery_inputs(original, current, provider):
    # Repaired pins and late inputs for other clouds cannot change the source
    # or scope of cleanup. Permit only explicitly supplied credential-reference
    # repairs; keep the original attempt settings and spending limits.
    require(current.get("owner") == original["owner"] and scope(current, provider) == scope(original, provider)
            and current.get("tailnet", {}).get("name") == original["tailnet"]["name"]
            and current["tailnet"].get("runner_dns") == original["tailnet"]["runner_dns"], "cleanup_input_scope_changed")
    refs = current["providers"][provider]["credential_refs"]
    original_refs = original["providers"][provider]["credential_refs"]
    require(isinstance(refs, dict) and set(refs) == set(original_refs) and all(secret_ref(value) for value in refs.values()), "secret_reference_required")
    cleanup_ref = current["tailnet"].get("cleanup_key_ref")
    require(secret_ref(cleanup_ref), "secret_reference_required")
    value = copy.deepcopy(original)
    value["providers"][provider]["credential_refs"] = refs
    value["tailnet"]["cleanup_key_ref"] = cleanup_ref
    return value


def interrupted(signum, frame):
    raise PolicyError("command_interrupted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--profile", choices=("showcase",), default="showcase")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--terraform", default="terraform")
    parser.add_argument("--phase", choices=("plan", "run", "cleanup"), required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        require(os.environ.get("AGENT_STACK_LIVE_TESTS") == "1", "live_opt_in_required")
        require(re.fullmatch(r"as7-[a-z0-9-]{8,30}", args.run_id) is not None, "invalid_run_id")
        require(all(shutil.which(tool) for tool in (args.terraform, "ssh-keygen", "ssh", "openssl")), "terraform_ssh_openssl_required")
        current = private_read(args.config)
        batch = Path(args.batch).absolute()
        run = batch / "attempts" / args.run_id
        flow = None
        if args.phase in ("plan", "run"):
            validate_inputs(current, args.provider)
            if not run.exists() and not run.is_symlink():
                prepare(current, args.provider, args.run_id, batch, args.revision)
            verify_prepared(run, current, args.provider)
            configure_provider_cache(run)
            original = current
            credentials = current
            require(not (run / "terraform.json").exists() and not (run / "repo/terraform.tfstate").exists(), "fresh_attempt_required")
        else:
            private_directory(batch)
            private_directory(run)
            original = private_read(run / "inputs.json")
            validate_inputs(original, args.provider)
            prepared = verify_prepared(run, original, args.provider)
            configure_provider_cache(run)
            credentials = recovery_inputs(original, current, args.provider)
            if (run / "showcase-run.json").exists():
                flow = private_read(run / "showcase-run.json")
                require(flow.get("schema") == 1 and flow.get("run_id") == args.run_id and flow.get("provider") == args.provider
                        and flow.get("scope") == scope(original, args.provider)
                        and flow.get("source_revision") == prepared["source_revision"], "showcase_recovery_scope_changed")
            # Reject unreserved/unapplied recovery before touching op/API auth.
            record = private_read(run / "terraform.json")
            stages = ("planned", "apply_started", "apply_uncertain", "applied", "destroy_started", "cleanup_failed") + (("cleaned",) if flow is not None else ())
            require(record.get("stage") in stages, "applied_attempt_required")
            ledger = Reservations(batch, original)
            def active(state):
                require(any(row["run_id"] == args.run_id and row["provider"] == args.provider and row["status"] in
                            (("active", "cleaned") if flow is not None else ("active",))
                            for row in state["attempts"]), "active_attempt_scope_required")
            ledger.operate(active)
        session = CloudSession(credentials, args.provider)
        attempt = TerraformAttempt(run, original, args.provider, session, terraform=args.terraform)
        if args.phase in ("plan", "run"):
            report = {**attempt.plan(), "status": "planned", "spend_reserved": False}
            if args.phase == "plan":
                report["resource_unit_prices"] = attempt.resource_unit_prices()
            if args.phase == "run":
                # Run the paid controller in a dedicated process group so the
                # independently started watchdog cannot signal the app or shell.
                if os.getpgrp() != os.getpid():
                    os.setsid()
                fields = Path('/proc/self/stat').read_text().split()
                from showcase_cleanup import private_write
                private_write(run/'controller-owner.json', {'pid':os.getpid(),'pgid':os.getpgrp(),'start_ticks':fields[21]})
                from showcase_launch import launch
                report = launch(attempt, Reservations(batch, original))
        else:
            ledger = Reservations(batch, original)
            if flow is None:
                report = {**attempt.cleanup(ledger), "status": "cleanup_verified"}
            else:
                result = RunController(attempt, ledger).recover()
                report = {**result, "acceptance": result["status"],
                          "status": "cleanup_verified" if result["cleanup"] == "verified" else "fail"}
    except (PolicyError, PreflightError) as error:
        report = {"status": "fail", "reason": str(error), "launch_ready": False, "live_matrix": "not_tested"}
        if args.phase == "cleanup":
            report["cleanup"] = "unverified"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        report = {"status": "fail", "reason": "invalid_live_input_or_response", "launch_ready": False, "live_matrix": "not_tested"}
        if args.phase == "cleanup":
            report["cleanup"] = "unverified"
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in ("planned", "pass", "cleanup_verified") else 1


if __name__ == "__main__":
    sys.exit(main())
