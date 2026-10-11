"""Produce bounded launch inputs from this attempt's actual reads and tests.

These checks admit an owned acceptance attempt. Live generation, conditional
permissions, capacity and application compatibility are proved by its result.
There is no externally supplied readiness or price override.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import time

from showcase_attempt import ROOT, verify_prepared
from showcase_cleanup import private_read, private_write, scope
from showcase_controller import RunController, prepare_container_deferral, proof_hash
from showcase_model_session import ModelSession
from showcase_policy import PolicyError, require, money, trial_egress_cap, Reservations, acceptance_mode
from showcase_terraform import LAUNCH_CHECKS, LIGHT_LAUNCH_CHECKS

RATES = {"aws": "vm_cpu_disks_ipv4_hourly_usd_upper_bound", "digitalocean": "vm_and_disk_hourly_usd",
         "gcp": "vm_disks_ipv4_hourly_usd_upper_bound", "hetzner": "vm_disk_ips_hourly_upper_bound"}


def trial(attempt):
    settings = private_read(attempt.checkout / "terraform.tfvars.json")
    value = settings.get("showcase_trial_limits")
    require(isinstance(value, dict) and type(value.get("egress_cap_bytes")) is int
            and value.get("egress_cap_bytes") == trial_egress_cap(attempt.config),
            "trial_cost_controls_required")
    try:
        deadline = datetime.strptime(value["deadline_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        raise PolicyError("trial_cost_controls_required") from None
    require(attempt.now() + 3600 < deadline <= attempt.now() + attempt.config["attempt_seconds"],
            "trial_lifetime_insufficient")
    return deadline


def local_test_environment(terraform):
    """Bind the selected local executables and installed dependency versions."""
    tools = {}
    for name in ("terraform", "python3", "bash", "git", "ssh", "ssh-keygen", "sshd"):
        effective_path = str(Path(terraform).absolute().parent) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")
        selected = terraform if name == "terraform" else shutil.which(name, path=effective_path)
        if name == "sshd" and selected is None and Path("/usr/sbin/sshd").is_file():
            selected = "/usr/sbin/sshd"
        require(selected is not None and Path(selected).is_file(), "local_test_tool_missing")
        path = Path(selected).absolute(); digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1048576), b""):
                digest.update(chunk)
        tools[name] = {"path": str(path), "sha256": digest.hexdigest(), "mode": stat.S_IMODE(path.stat().st_mode)}
    packages = sorted((distribution.metadata["Name"], distribution.version)
                      for distribution in importlib.metadata.distributions())
    return {"tools": tools, "python": sys.version, "prefix": sys.prefix,
            "platform": [platform.system(), platform.machine()], "uid": os.geteuid(), "packages": packages}


def prose_documentation(name):
    # Skills and other Markdown may be shipped or consumed as runtime inputs.
    # Only these known planning/prose locations are eligible for cheap reuse.
    path = Path(name)
    return name == "README.md" or path.suffix.lower() == ".md" and path.parts[:2] == ("docs", "plans")


def local_execution_fingerprint(prepared, terraform):
    # Known prose documentation has changed links checked separately on reuse.
    # Everything else, including tests, fixtures, locks and examples, remains an
    # input. Unknown new file types are included conservatively.
    files = {}
    for name, expected in prepared["source_files"].items():
        path = ROOT / name
        require(path.is_file() and not path.is_symlink()
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected, "local_test_source_changed")
        if not prose_documentation(name):
            files[name] = {"sha256": expected, "mode": stat.S_IMODE(path.stat().st_mode)}
    require(files, "local_test_execution_inputs_missing")
    return proof_hash({"schema": 2, "files": files, "environment": local_test_environment(terraform),
                       "command": ["scripts/test-local.sh"], "mirror": hashlib.sha256(
                           (ROOT / ".tmp/agent-stack-tests/terraform-mirror.tfrc").read_bytes()).hexdigest()
                       if (ROOT / ".tmp/agent-stack-tests/terraform-mirror.tfrc").is_file() else None})


def changed_documentation_links(prepared, previous):
    current = {name: value for name, value in prepared["source_files"].items() if Path(name).suffix.lower() == ".md"}
    for name, value in current.items():
        if previous.get(name) == value:
            continue
        source = ROOT / name
        for target in re.findall(r"\]\(([^)]+)\)", source.read_text()):
            target = target.strip().strip("<>").split("#", 1)[0]
            if not target or target.startswith("/") or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue
            # Titles in Markdown links are optional, and external workspace
            # references are not repository files or execution inputs.
            target = re.split(r'\s+[\"\']', target, maxsplit=1)[0]
            path = (source.parent / target).resolve()
            if path.is_relative_to(ROOT.resolve()):
                require(path.exists(), "local_documentation_link_missing")
    return current


def local_checks(attempt):
    """Qualify execution inputs once; commits are provenance, not a cache key."""
    prepared = verify_prepared(attempt.run, attempt.config, attempt.provider)
    binding = proof_hash(prepared["source_files"])
    execution = local_execution_fingerprint(prepared, attempt.terraform)
    receipt_path = attempt.run.parent.parent / "local-checks.json"
    if receipt_path.exists():
        old = private_read(receipt_path)
        log = attempt.run.parent.parent / "local-checks.log"
        if (old.get("schema") == 2 and old.get("execution_input_fingerprint") == execution
                and old.get("receipt_sha256") == proof_hash({k: v for k, v in old.items() if k != "receipt_sha256"})
                and old.get("status") == "pass" and log.is_file()
                and hashlib.sha256(log.read_bytes()).hexdigest() == old.get("output_sha256")):
            documentation = changed_documentation_links(prepared, old.get("documentation_files", {}))
            require(local_execution_fingerprint(prepared, attempt.terraform) == execution, "local_test_source_changed")
            receipt = {**old, "source_revision": prepared["source_revision"], "source_files_sha256": binding,
                       "documentation_files": documentation}
            receipt["receipt_sha256"] = proof_hash({k: v for k, v in receipt.items() if k != "receipt_sha256"})
            if receipt != old:
                private_write(receipt_path, receipt)
            return receipt
    documentation = changed_documentation_links(prepared, {})
    # The wrapper tests a private copy and uses fixtures, never operator inputs.
    # Its own cleanup and process-group handling are retained.
    env = dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE="1", AGENT_STACK_TEST_ROOT=str(ROOT / ".tmp/agent-stack-tests"),
               TMPDIR=str(ROOT / ".tmp/agent-stack-tests"))
    mirror_config = ROOT / ".tmp/agent-stack-tests/terraform-mirror.tfrc"
    if mirror_config.is_file():
        env["TF_CLI_CONFIG_FILE"] = str(mirror_config)
    env["PATH"] = str(Path(attempt.terraform).absolute().parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    log = attempt.run.parent.parent / "local-checks.log"
    try:
        result = subprocess.run([str(ROOT / "scripts/test-local.sh")], env=env, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired as error:
        # Keep completed phases available for diagnosis, but never issue a pass
        # receipt for a suite that did not finish. This is local fixture time;
        # the separately bounded cloud lifetime is unchanged.
        log.write_bytes((error.stdout or b"") + (error.stderr or b"")); log.chmod(0o600)
        raise PolicyError("local_tests_timed_out") from None
    output = result.stdout + result.stderr
    log.write_bytes(output); log.chmod(0o600)
    require(result.returncode == 0, "local_tests_failed")
    # Refuse any mismatch between the tested checkout and committed attempt.
    require(all((ROOT / name).is_file() and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
                for name, sha in prepared["source_files"].items()), "local_test_source_changed")
    require(local_execution_fingerprint(prepared, attempt.terraform) == execution, "local_test_source_changed")
    receipt = {"schema":2, "status":"pass", "source_revision":prepared["source_revision"],
               "qualified_source_revision":prepared["source_revision"], "qualified_source_files_sha256":binding,
               "source_files_sha256":binding, "execution_input_fingerprint":execution,
               "documentation_files":documentation,
               "output_sha256":hashlib.sha256(output).hexdigest()}
    receipt["receipt_sha256"] = proof_hash(receipt)
    private_write(receipt_path, receipt)
    return receipt


def check_peer_lifetime(attempt):
    if 'local_peer' in attempt.config['runner']:
        from showcase_local_peer import lifetime
        return lifetime(attempt.config['runner']['local_peer'], attempt.config['tailnet']['runner_dns'],
                        attempt.config['attempt_seconds'], attempt.now)
    from showcase_credentials import AUTH_ROOT
    peer = private_read(AUTH_ROOT.parent / "handoff/browser-peer-aws.json")
    try:
        deadline = datetime.fromisoformat(peer["deadline_utc"]).timestamp()
    except (ValueError, KeyError, TypeError):
        raise PolicyError("peer_lifetime_unverified") from None
    funded = peer.get("name") == "as7-peer-3bc66e" and peer.get("reserve_usd") == "1.00"
    if not funded:
        def binding(state):
            matches = [row for row in state.get("peer_reservations", []) if row["name"] == peer.get("name")]
            require(len(matches) == 1 and matches[0]["reserved_usd"] == peer.get("reserve_usd")
                    and abs(matches[0]["deadline"] - deadline) <= 0.00001, "peer_funding_unverified")
        Reservations(attempt.run.parent.parent, attempt.config).operate(binding)
        funded = True
    require(peer.get("compute_created") is True and funded
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
    # Two boots and a further 2x framing/measurement margin cover the actual
    # per-boot quota. Retain the separate account adjustment below.
    # No transfer pool or CPU-credit allowance is subtracted.
    hourly = money(resource[RATES[attempt.provider]]) * 2
    traffic_gib = Decimal(trial_egress_cap(attempt.config) * 4) / 1073741824
    if attempt.provider == "digitalocean":
        fixed = money(resource["outbound_gib_usd"]) * traffic_gib * 2 + money(resource["vm_minimum_usd"]) * 2
    elif attempt.provider == "hetzner":
        fixed = money(resource["outbound_tb_upper_bound"]) * traffic_gib * 1073741824 / 1000000000000 * 2
        hourly *= 2  # whole-hour server rounding within a noninteger lifetime
    else:
        # Price the bounded traffic rather than adding an unrelated flat fee.
        # Keep all framing/boot/account margins and the separate cleanup hold.
        from showcase_network_prices import measure_network
        network = measure_network(attempt)
        private_write(attempt.run / "network-prices.json", network)
        fixed = money(network["usd_per_gib_upper_bound"]) * traffic_gib * 2
    source = {"aws":"https://aws.amazon.com/ec2/pricing/on-demand/",
              "digitalocean":"https://www.digitalocean.com/pricing/droplets",
              "gcp":"https://cloud.google.com/vpc/pricing",
              "hetzner":"https://api.hetzner.cloud/v1/pricing"}[attempt.provider]
    # The full guest lifetime already includes its cleanup window. Retain a
    # further whole hour at the measured, margin-adjusted rate for recovery,
    # with a ten-cent floor; expensive resources receive a larger hold.
    cleanup = max(Decimal("0.10"), hourly).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return {"provider":attempt.provider,"currency":"USD", "verified_at":datetime.now(timezone.utc).isoformat(),
            "source":source,"hourly_usd":str(hourly),"fixed_usd":str(fixed),
            "model_usd":"2.00","model_prepaid":True,"cleanup_usd":str(cleanup)}


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
    mode = acceptance_mode(attempt.config, attempt.provider)
    peer_deadline = None if mode == "light" else check_peer_lifetime(attempt)
    deadline = trial(attempt)
    resource = attempt.resource_unit_prices()
    terms = None if mode == "light" else attempt.model_unit_prices()
    quote = complete_quote(attempt, resource)
    remote = RunController(attempt, ledger)
    if mode != "light":
        limits = {role:{"max_requests":32,"output_tokens_upper_bound":1024} for role in attempt.config["models"]}
        remote.models = ModelSession.from_measured_terms(remote.remote, terms, limits)
    readiness = {"source_revision":attempt.prepared["source_revision"], "scope":scope(attempt.config,attempt.provider),
                 "checks":{key:"pass" for key in LAUNCH_CHECKS}}
    if mode == "light":
        readiness.update(acceptance_mode="light", checks={key:"pass" for key in LIGHT_LAUNCH_CHECKS})
    elif not __import__('shutil').which('docker'):
        readiness['checks']['container_tests']='deferred_owned_vm'
        readiness['container_deferral']=prepare_container_deferral(attempt)
    else:
        raise PolicyError("local_container_receipt_required")
    private_write(attempt.run / "launch-measurements.json", {"local":private_read(attempt.run.parent.parent/'local-checks.json'),
        "preflight":private_read(attempt.run/'preflight.json'),"resource_prices_sha256":proof_hash(resource),
        "acceptance_mode":mode,"model_prices_sha256":None if terms is None else proof_hash(terms),
        "quote":quote,"peer_deadline":peer_deadline,
        "trial_deadline":deadline,"purpose":"bounded acceptance attempt; live checks remain pending"})
    start_watchdog(attempt, deadline)
    return remote.run_attempt(readiness, quote)
