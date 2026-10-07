"""Produce bounded launch inputs from this attempt's actual reads and tests.

These checks admit an owned acceptance attempt. Live generation, conditional
permissions, capacity and application compatibility are proved by its result.
There is no externally supplied readiness or price override.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from showcase_attempt import ROOT, verify_prepared
from showcase_cleanup import private_read, private_write, scope
from showcase_controller import RunController, prepare_container_deferral, proof_hash
from showcase_model_session import ModelSession
from showcase_policy import PolicyError, require, money
from showcase_terraform import LAUNCH_CHECKS

RATES = {"aws": "vm_cpu_disks_ipv4_hourly_usd_upper_bound", "digitalocean": "vm_and_disk_hourly_usd",
         "gcp": "vm_disks_ipv4_hourly_usd_upper_bound", "hetzner": "vm_disk_ips_hourly_upper_bound"}


def trial(attempt):
    settings = private_read(attempt.checkout / "terraform.tfvars.json")
    value = settings.get("showcase_trial_limits")
    require(isinstance(value, dict) and value.get("egress_cap_bytes") == 1073741824,
            "trial_cost_controls_required")
    try:
        deadline = datetime.strptime(value["deadline_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        raise PolicyError("trial_cost_controls_required") from None
    require(attempt.now() + 3600 < deadline <= attempt.now() + attempt.config["attempt_seconds"],
            "trial_lifetime_insufficient")
    return deadline


def local_checks(attempt):
    """Run the actual local suite; cache only this exact immutable source tree."""
    prepared = verify_prepared(attempt.run, attempt.config, attempt.provider)
    binding = proof_hash(prepared["source_files"])
    receipt_path = attempt.run.parent.parent / "local-checks.json"
    if receipt_path.exists():
        old = private_read(receipt_path)
        log = attempt.run.parent.parent / "local-checks.log"
        if (old.get("source_revision") == prepared["source_revision"] and old.get("source_files_sha256") == binding
                and old.get("status") == "pass" and log.is_file()
                and hashlib.sha256(log.read_bytes()).hexdigest() == old.get("output_sha256")):
            return old
    # The wrapper tests a private copy and uses fixtures, never operator inputs.
    # Its own cleanup and process-group handling are retained.
    env = dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE="1", AGENT_STACK_TEST_ROOT=str(ROOT / ".tmp/agent-stack-tests"),
               TMPDIR=str(ROOT / ".tmp/agent-stack-tests"))
    mirror_config = ROOT / ".tmp/agent-stack-tests/terraform-mirror.tfrc"
    if mirror_config.is_file():
        env["TF_CLI_CONFIG_FILE"] = str(mirror_config)
    env["PATH"] = str(Path(attempt.terraform).absolute().parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    result = subprocess.run([str(ROOT / "scripts/test-local.sh")], env=env, capture_output=True, timeout=900)
    output = result.stdout + result.stderr
    log = attempt.run.parent.parent / "local-checks.log"
    log.write_bytes(output); log.chmod(0o600)
    require(result.returncode == 0, "local_tests_failed")
    # Refuse any mismatch between the tested checkout and committed attempt.
    require(all((ROOT / name).is_file() and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
                for name, sha in prepared["source_files"].items()), "local_test_source_changed")
    receipt = {"status":"pass", "source_revision":prepared["source_revision"],
               "source_files_sha256":binding, "output_sha256":hashlib.sha256(output).hexdigest()}
    private_write(receipt_path, receipt)
    return receipt


def check_peer_lifetime(attempt):
    from showcase_credentials import AUTH_ROOT
    peer = private_read(AUTH_ROOT.parent / "handoff/browser-peer-aws.json")
    try:
        deadline = datetime.fromisoformat(peer["deadline_utc"]).timestamp()
    except (ValueError, KeyError, TypeError):
        raise PolicyError("peer_lifetime_unverified") from None
    require(peer.get("compute_created") is True and peer.get("reserve_usd") == "1.00"
            and attempt.now() + attempt.config["attempt_seconds"] < deadline,
            "peer_lifetime_insufficient")
    return deadline


def complete_quote(attempt, resource):
    record = attempt.record()
    require(resource.get("status") == "resource_unit_prices_measured" and resource.get("provider") == attempt.provider
            and resource.get("source_revision") == attempt.prepared["source_revision"]
            and resource.get("plan_sha256") == record["plan_sha256"] and resource.get("run_id") == attempt.prefix,
            "resource_price_scope_changed")
    currency = resource.get("currency")
    require(currency == "USD", "usd_account_prices_required")
    # Actual unit reads plus a deliberately generous 100% account adjustment.
    # A 4-GiB allowance conservatively covers the shared 1-GiB routing counters
    # across exactly two boots, including framing/measurement differences.
    # No transfer pool or CPU-credit allowance is subtracted.
    hourly = money(resource[RATES[attempt.provider]]) * 2
    fixed = Decimal("0.50")
    if attempt.provider == "digitalocean":
        fixed = max(fixed, money(resource["outbound_gib_usd"]) * 4 * 2 + money(resource["vm_minimum_usd"]) * 2)
    elif attempt.provider == "hetzner":
        fixed = max(fixed, money(resource["outbound_tb_upper_bound"]) * Decimal(4 * 1073741824) / 1000000000000 * 2)
        hourly *= 2  # whole-hour server rounding within a noninteger lifetime
    else:
        # $0.50 reserves up to $0.0625/GiB after account adjustment. A fresh
        # unit-price check below replaces this with the actual higher bound.
        from showcase_network_prices import measure_network
        network = measure_network(attempt)
        private_write(attempt.run / "network-prices.json", network)
        fixed = max(fixed, money(network["usd_per_gib_upper_bound"]) * 4 * 2)
    source = {"aws":"https://aws.amazon.com/ec2/pricing/on-demand/",
              "digitalocean":"https://www.digitalocean.com/pricing/droplets",
              "gcp":"https://cloud.google.com/vpc/pricing",
              "hetzner":"https://api.hetzner.cloud/v1/pricing"}[attempt.provider]
    return {"provider":attempt.provider,"currency":"USD", "verified_at":datetime.now(timezone.utc).isoformat(),
            "source":source,"hourly_usd":str(hourly),"fixed_usd":str(fixed),
            "model_usd":"2.00","model_prepaid":True,"cleanup_usd":"0.50"}


def start_watchdog(attempt, deadline):
    path = attempt.run / "watchdog.log"
    with path.open("ab") as log:
        path.chmod(0o600)
        child = subprocess.Popen([os.sys.executable, "-B", str(ROOT / "scripts/showcase_watchdog.py"),
                    "--run",str(attempt.run),"--terraform",attempt.terraform,"--deadline",str(deadline)],
                    stdout=log,stderr=log,start_new_session=True,umask=0o077)
    private_write(attempt.run / "watchdog.json", {"pid":child.pid,"deadline":deadline})


def launch(attempt, ledger):
    require("funding" in attempt.config, "approved_test_funding_required")
    local_checks(attempt)
    check_peer_lifetime(attempt)
    deadline = trial(attempt)
    resource = attempt.resource_unit_prices()
    terms = attempt.model_unit_prices()
    quote = complete_quote(attempt, resource)
    remote = RunController(attempt, ledger)
    limits = {role:{"max_requests":32,"output_tokens_upper_bound":1024} for role in attempt.config["models"]}
    remote.models = ModelSession.from_measured_terms(remote.remote, terms, limits)
    readiness = {"source_revision":attempt.prepared["source_revision"], "scope":scope(attempt.config,attempt.provider),
                 "checks":{key:"pass" for key in LAUNCH_CHECKS}}
    if not __import__('shutil').which('docker'):
        readiness['checks']['container_tests']='deferred_owned_vm'
        readiness['container_deferral']=prepare_container_deferral(attempt)
    else:
        raise PolicyError("local_container_receipt_required")
    private_write(attempt.run / "launch-measurements.json", {"local":private_read(attempt.run.parent.parent/'local-checks.json'),
        "preflight":private_read(attempt.run/'preflight.json'),"resource_prices_sha256":proof_hash(resource),
        "model_prices_sha256":proof_hash(terms),"quote":quote,"peer_deadline":check_peer_lifetime(attempt),
        "trial_deadline":deadline,"purpose":"bounded acceptance attempt; live checks remain pending"})
    start_watchdog(attempt, deadline)
    return remote.run_attempt(readiness, quote)
