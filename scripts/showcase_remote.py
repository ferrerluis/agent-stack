#!/usr/bin/env python3
"""Measured SSH, doctor, runtime/workspace tasks and reboot checks for an owned attempt.

This is a controller component, not a launch/readiness override. Authenticated
second-peer browser checks are a separate requirement.
Raw command output and secrets never enter the evidence record.
"""
import builtins
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import stat
import time
import types
import uuid

from showcase_attempt import KEY_PATH, command, isolated_environment, remaining_timeout, verify_prepared
from showcase_cleanup import capture, private_read, private_write, scope
from showcase_policy import PolicyError, require, acceptance_mode
from showcase_preflight import PreflightError, check_tailnet, field, objects, segment
from showcase_terraform import exclusive, guard_state
import showcase_task_evidence as task_evidence

VM_TYPES = {"aws": "aws_instance", "digitalocean": "digitalocean_droplet", "gcp": "google_compute_instance", "hetzner": "hcloud_server"}
DISK_TYPES = {"aws": "aws_ebs_volume", "digitalocean": "digitalocean_volume", "gcp": "google_compute_disk", "hetzner": "hcloud_volume"}
DOCTOR_PATH = "skills/agent-stack-doctor/scripts/strict_health.py"
AGENT_PATH = "scripts/showcase_agents.py"
PEER_PATH = "scripts/showcase_peer.py"
BROWSER_PATH = "scripts/showcase_browser.py"
HOME = "/home/user"
COMPOSE = ["sudo", "-n", "docker", "compose", "-f", "/opt/agent-stack/docker-compose.yml"]
WORKSPACE = COMPOSE + ["exec", "-T", "--user", "1000", "-e", "HOME=" + HOME,
                       "-e", "PATH=" + HOME + "/.local/bin:/usr/local/bin:/usr/bin:/bin", "workspace"]
HEX = r"[a-f0-9]{64}"
UUID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
BOOTSTRAP = """import json,pathlib,subprocess,sys
def run(*args):
    return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=30).stdout
try:
    run('cloud-init','status','--wait')
    assert run('systemctl','is-active','agent-stack').strip()==b'active'
    loader=json.loads(pathlib.Path('/opt/agent-stack/.loader-ready.json').read_text())
    applied=json.loads(pathlib.Path('/opt/agent-stack/.last-apply.json').read_text())
    assert loader.get('status')=='ready' and loader.get('provider')==sys.argv[1] and loader.get('data_root')=='/opt/agent-stack/data'
    assert applied.get('status')=='success' and applied.get('checksum')==sys.argv[2] and sorted(applied.get('services',[]))==['hermes','openclaw','workspace']
    mount=json.loads(run('findmnt','-J','-o','TARGET,UUID,FSTYPE,SOURCE','--target','/opt/agent-stack/data'))['filesystems']
    assert len(mount)==1 and mount[0]['target']=='/opt/agent-stack/data' and mount[0]['fstype']=='ext4'
    mounted=pathlib.Path(mount[0]['source']).resolve(strict=True)
    if sys.argv[1]=='aws':
        serial=json.loads(run('nvme','id-ctrl',str(mounted),'-o','json'))['sn'].strip().lower()
        assert serial==sys.argv[3].replace('-','').lower()
    else:
        device={'digitalocean':'/dev/disk/by-id/scsi-0DO_Volume_'+sys.argv[4],
                'hetzner':'/dev/disk/by-id/scsi-0HC_Volume_'+sys.argv[3],
                'gcp':'/dev/disk/by-id/google-agent-stack-data'}[sys.argv[1]]
        assert pathlib.Path(device).resolve(strict=True)==mounted
    ts=json.loads(run('tailscale','status','--json'))
    assert ts.get('BackendState')=='Running' and ts['Self'].get('Online') is True and not ts.get('Health')
    report={'schema':1,'status':'pass','checksum':applied['checksum'],'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'disk_uuid':mount[0]['uuid'],'data_volume_id':sys.argv[3],'dns':ts['Self']['DNSName'].rstrip('.')}
except Exception:
    report={'schema':1,'status':'fail'}
print(json.dumps(report))
sys.exit(0 if report['status']=='pass' else 1)
"""
VERIFY_FILES = """import hashlib,json,os,pathlib,re,sys
def unique(pairs):
    value={}
    for key,item in pairs:
        assert key not in value
        value[key]=item
    return value
def constant(_):
    raise ValueError('invalid_json')
def decoded(raw):
    return json.loads(raw,object_pairs_hook=unique,parse_constant=constant)
try:
    assert os.geteuid()==1000 and os.environ['HOME']=='/home/user'
    home=pathlib.Path('/home/user')
    assert re.fullmatch(r'\\.agent-stack/acceptance/(?:codex|claude)-[a-z0-9_]{1,64}',sys.argv[1])
    directory=home/sys.argv[1]
    assert directory.is_dir() and directory.resolve().is_relative_to(home.resolve())
    assert all(not p.is_symlink() for p in (directory,*directory.parents))
    paths=[directory/'input.json',directory/'result.json']
    assert all(p.is_file() and not p.is_symlink() and p.stat().st_uid==1000 and p.stat().st_nlink==1 and p.stat().st_size<=4096 for p in paths)
    before=paths[0].read_bytes(); result=paths[1].read_bytes()
    assert hashlib.sha256(before).hexdigest()==sys.argv[2]
    challenge=decoded(before); actual=decoded(result)
    assert set(challenge)=={'lhs','rhs','nonce'} and set(actual)=={'sum','nonce'}
    assert all(type(challenge[k]) is int and 0<=challenge[k]<1000000 for k in ('lhs','rhs'))
    assert re.fullmatch(r'[a-f0-9]{32}',challenge['nonce'])
    assert type(actual['sum']) is int and actual['sum']==challenge['lhs']+challenge['rhs'] and actual['nonce']==challenge['nonce']
    report={'schema':1,'status':'pass','uid':1000,'input_sha256':hashlib.sha256(before).hexdigest(),'result_sha256':hashlib.sha256(result).hexdigest(),
            'answer_sha256':hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest()}
except Exception:
    report={'schema':1,'status':'fail'}
print(json.dumps(report))
sys.exit(0 if report['status']=='pass' else 1)
"""


def decoded(data):
    require(isinstance(data, bytes) and len(data) <= 65536, "remote_report_invalid")
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise PolicyError("remote_report_invalid") from None
    require(isinstance(result, dict) and type(result.get("schema")) is int and result["schema"] == 1, "remote_report_invalid")
    return result


def matching_address(adapter, provider, values):
    """Re-query the selected VM so edited state cannot select an SSH target."""
    identifier, name = str(values["id"]), values.get("name") or (values.get("tags") or {}).get("Name")
    if provider == "aws":
        result = adapter.call(adapter.ec2, "describe_instances", InstanceIds=[identifier])
        instances = [row for reservation in objects(result, "Reservations") for row in objects(reservation, "Instances")]
        require(len(instances) == 1 and instances[0].get("InstanceId") == identifier
                and field(instances[0], "State").get("Name") == "running", "owned_vm_not_running")
        api = instances[0].get("PublicIpAddress")
        saved = values.get("public_ip")
    elif provider == "digitalocean":
        vm = field(adapter.get("/droplets/" + segment(identifier)), "droplet")
        require(str(vm.get("id")) == identifier and vm.get("name") == name and vm.get("status") == "active", "owned_vm_not_running")
        public = [row.get("ip_address") for row in objects(field(vm, "networks"), "v4") if row.get("type") == "public"]
        require(len(public) == 1, "owned_vm_address_unverified")
        api, saved = public[0], values.get("ipv4_address")
    elif provider == "hetzner":
        vm = field(adapter.get("/servers/" + segment(identifier)), "server")
        require(str(vm.get("id")) == identifier and vm.get("name") == name and vm.get("status") == "running", "owned_vm_not_running")
        api, saved = field(field(vm, "public_net"), "ipv4").get("ip"), values.get("ipv4_address")
    else:
        project, zone = adapter.cloud["project_id"], adapter.cloud["zone"]
        require(identifier == "projects/" + project + "/zones/" + zone + "/instances/" + name, "owned_vm_scope_mismatch")
        # Google.get already uses the independently verified project as its base.
        vm = adapter.get("/zones/" + segment(zone) + "/instances/" + segment(name))
        require(vm.get("name") == name and vm.get("status") == "RUNNING", "owned_vm_not_running")
        interfaces = objects(vm, "networkInterfaces")
        public = [row.get("natIP") for interface in interfaces for row in objects(interface, "accessConfigs")]
        require(len(public) == 1, "owned_vm_address_unverified")
        api = public[0]
        try:
            saved = values["network_interface"][0]["access_config"][0]["nat_ip"]
        except (KeyError, IndexError, TypeError):
            raise PolicyError("owned_vm_address_unverified") from None
    require(isinstance(api, str) and api == saved, "owned_vm_address_unverified")
    try:
        ip = ipaddress.ip_address(api)
    except ValueError:
        raise PolicyError("owned_vm_address_unverified") from None
    require(ip.version == 4 and ip.is_global and not ip.is_multicast, "owned_vm_address_unverified")
    return api


class RemoteChecks:
    def __init__(self, attempt, ledger, execute=command, now=time.time, sleep=time.sleep):
        self.attempt, self.ledger = attempt, ledger
        self.run, self.config, self.provider = attempt.run, attempt.config, attempt.provider
        self.execute, self.now, self.sleep = execute, now, sleep
        self.path = self.run / "remote-checks.json"
        self.models = None

    def attach_models(self, models):
        from showcase_model_session import ModelSession
        require(isinstance(models, ModelSession) and models.checks is self and
                models.spend.ledger is self.ledger and (self.models is None or self.models is models), "model_session_scope_changed")
        self.models = models

    def ensure(self):
        prepared = verify_prepared(self.run, self.config, self.provider)
        require(self.attempt.record().get("stage") == "applied", "applied_attempt_required")
        active = self.attempt.active(self.ledger)
        remaining_timeout(active, 1, now=self.now())
        return prepared, active

    def evidence(self):
        prepared, _ = self.ensure()
        result = private_read(self.path) if self.path.exists() else {"schema": 1, "run_id": self.run.name,
            "source_revision": prepared["source_revision"], "checks": {}, "tasks": {}, "agents": {}, "baseline": None}
        require(result.get("schema") == 1 and result.get("run_id") == self.run.name and result.get("source_revision") == prepared["source_revision"]
                and isinstance(result.get("checks"), dict) and isinstance(result.get("tasks"), dict), "remote_evidence_scope_mismatch")
        result.setdefault("agents", {})
        require(isinstance(result["agents"], dict), "remote_evidence_scope_mismatch")
        result.setdefault("network", {})
        require(isinstance(result["network"], dict), "remote_evidence_scope_mismatch")
        return result

    def require_container_gate(self, evidence):
        record = self.attempt.record()
        require(record.get("container_tests_gate") in ("pass", "deferred_owned_vm"), "container_proof_required")
        if record["container_tests_gate"] == "deferred_owned_vm":
            require(evidence["checks"].get("container_tests") == "pass" and evidence.get("containers", {}).get("status") == "pass"
                    and evidence["containers"].get("bundle_sha256") == record.get("container_bundle_sha256"), "container_proof_required")

    def bind(self, require_tailnet=True):
        self.ensure()
        inventory_path = self.run / "inventory.json"
        previous = private_read(inventory_path) if inventory_path.exists() else None
        inventory = capture(self.attempt.session.inventory(self.run.name, previous), self.config, previous)
        private_write(inventory_path, inventory)
        require(inventory.get("identity") == "verified", "account_project_mapping_unverified")
        rows = guard_state(private_read(self.attempt.state_path), private_read(self.run / "deployment-plan.json"),
                           self.config, self.provider, self.run.name, self.attempt.checkout, inventory, self.attempt.session.adapter)
        vms = [row["values"] for row in rows.values() if row["type"] == VM_TYPES[self.provider]]
        disks = [row["values"] for row in rows.values() if row["type"] == DISK_TYPES[self.provider]]
        require(len(vms) == 1, "one_owned_vm_required")
        require(len(disks) == 1, "one_owned_data_disk_required")
        checksum = rows.get("terraform_data.runtime_apply", {}).get("values", {}).get("input")
        if isinstance(checksum, dict) and checksum.get("type") == "string":
            checksum = checksum.get("value")
        require(isinstance(checksum, str) and re.fullmatch(HEX, checksum), "runtime_checksum_required")
        nodes = inventory["tailnet"]["devices"]
        if require_tailnet:
            require(len(nodes) == 1 and re.fullmatch(re.escape(self.run.name) + r"\.[a-z0-9.-]+\.ts\.net", nodes[0]["name"]), "one_owned_tailnet_node_required")
        result = {"host": "admin@" + matching_address(self.attempt.session.adapter, self.provider, vms[0]),
                "checksum": checksum, "dns": nodes[0]["name"] if require_tailnet else None, "data_volume_id": str(disks[0]["id"]),
                "data_volume_name": disks[0].get("name") or (disks[0].get("tags") or {}).get("Name")}
        if not require_tailnet:
            result["vm_id"] = str(vms[0]["id"])
        return result

    def ssh(self, target, args, input=None, timeout=60, capture_failure=False):
        _, active = self.ensure()
        hosts = self.attempt.checkout / ".ssh/known_hosts"
        require(hosts.is_file() and not hosts.is_symlink() and hosts.stat().st_uid == os.geteuid()
                and stat.S_IMODE(hosts.stat().st_mode) == 0o600, "private_known_hosts_required")
        wrapper = self.attempt.checkout / "bin/agent-stack-ssh"
        require(wrapper.is_file() and wrapper.stat().st_mode & 0o100, "verified_ssh_wrapper_required")
        require(type(capture_failure) is bool or capture_failure in ("container-command-error.json", "runtime-command-error.json", "runtime-restore-error.json", "host-doctor-command-error.json", "workspace-command-error.json"),
                "private_failure_report_refused")
        report = "container-command-error.json" if capture_failure is True else capture_failure
        options = {"failure_report": self.run / report} if report and self.execute is command else {}
        pinned = self.models is not None or (acceptance_mode(self.config, self.provider) == "light" and hosts.stat().st_size > 0)
        return self.execute([str(wrapper), "--batch", *( ["--pinned"] if pinned else [] ),
                             "--known-hosts", str(hosts), "--key", str(self.attempt.checkout / KEY_PATH),
                             "--host", target["host"], "--", shlex.join(args)], env=isolated_environment(self.run),
                            input=input, timeout=remaining_timeout(active, timeout, now=self.now()), **options)

    @exclusive
    def light(self):
        import showcase_light as light
        require(acceptance_mode(self.config, self.provider) == "light" and self.models is None
                and self.attempt.record().get("container_tests_gate") == "light_runtime_pending", "light_scope_required")
        evidence = self.evidence()
        require("light" not in evidence, "light_already_invoked")
        evidence["checks"]["vm_and_containers"] = "started"
        private_write(self.path, evidence)
        samples, baseline, first = [], None, None
        until = self.now() + 900
        try:
            while self.now() < until:
                try:
                    target = self.bind(require_tailnet=False)
                    report = light.snapshot(decoded(self.ssh(target, ["sudo", "-n", "python3", "-B", "-I", "-", target["checksum"]],
                        input=light.REMOTE.encode(), timeout=40)), target["checksum"], self.config["pins"])
                except PolicyError as error:
                    if samples or str(error) not in ("command_failed", "command_timed_out", "light_snapshot_invalid", "owned_vm_not_running"):
                        raise
                    self.sleep(10)
                    continue
                instant = time.monotonic()
                if first is None:
                    first, baseline = instant, target
                require(target == baseline, "light_target_changed")
                samples.append({"elapsed_seconds": instant - first, "snapshot": report})
                require(len(samples) == 1 or report == samples[0]["snapshot"], "light_stability_unverified")
                if instant - first >= light.STEADY_SECONDS:
                    proof = {"schema": 1, "status": "vm_and_containers_verified", "run_id": self.run.name,
                             "source_revision": evidence["source_revision"], "scope": scope(self.config, self.provider),
                             "target": {key: target[key] for key in ("vm_id", "checksum")}, "samples": samples}
                    light.validate(proof, self.run.name, evidence["source_revision"], scope(self.config, self.provider), self.config["pins"])
                    evidence["light"] = proof
                    evidence["checks"]["vm_and_containers"] = "pass"
                    private_write(self.path, evidence)
                    return proof
                self.sleep(20)
            raise PolicyError("light_startup_or_stability_timeout")
        except BaseException:
            evidence["checks"]["vm_and_containers"] = "uncertain"
            private_write(self.path, evidence)
            raise

    def boot_image(self, target):
        """Resolve the owned VM's actual boot image through its native API."""
        adapter = self.attempt.session.adapter
        identifier = target["vm_id"]
        if self.provider == "aws":
            result = adapter.call(adapter.ec2, "describe_instances", InstanceIds=[identifier])
            rows = [v for r in objects(result, "Reservations") for v in objects(r, "Instances")]
            require(len(rows) == 1 and rows[0].get("InstanceId") == identifier, "resolved_boot_image_unverified")
            image = rows[0].get("ImageId")
            require(isinstance(image, str) and re.fullmatch(r"ami-[a-f0-9]+", image), "resolved_boot_image_unverified")
        elif self.provider in ("digitalocean", "hetzner"):
            collection, key = ("droplets", "droplet") if self.provider == "digitalocean" else ("servers", "server")
            row = field(adapter.get("/" + collection + "/" + segment(identifier)), key)
            require(str(row.get("id")) == identifier, "resolved_boot_image_unverified")
            image = field(row, "image").get("id")
            require(type(image) is int and image > 0, "resolved_boot_image_unverified")
            image = str(image)
        else:
            cloud = self.config["providers"][self.provider]
            start = "projects/" + cloud["project_id"] + "/zones/" + cloud["zone"] + "/instances/"
            require(identifier.startswith(start), "resolved_boot_image_unverified")
            row = adapter.get("/zones/" + segment(cloud["zone"]) + "/instances/" + segment(identifier[len(start):]))
            disks = [v for v in objects(row, "disks") if v.get("boot") is True]
            require(len(disks) == 1, "resolved_boot_image_unverified")
            source = disks[0].get("source")
            expected = "https://www.googleapis.com/compute/v1/projects/" + cloud["project_id"] + "/zones/" + cloud["zone"] + "/disks/"
            require(isinstance(source, str) and source.startswith(expected), "resolved_boot_image_unverified")
            disk = adapter.get("/zones/" + segment(cloud["zone"]) + "/disks/" + segment(source[len(expected):]))
            image = disk.get("sourceImageId")
            require(isinstance(image, str) and re.fullmatch(r"[0-9]+", image), "resolved_boot_image_unverified")
        return self.provider + ":" + image

    @exclusive
    def resolved_contents(self):
        import base64
        from showcase_fingerprints import validate_contents
        evidence = self.evidence()
        target = self.bind(require_tailnet=False)
        binding = {"run_id": self.run.name, "source_revision": evidence["source_revision"],
                   "checksum": target["checksum"], "vm_id": target["vm_id"]}
        path = self.attempt.checkout / "scripts/showcase_contents.py"
        require(path.is_file() and not path.is_symlink(), "resolved_contents_verifier_required")
        source = path.read_bytes()
        boot_image = self.boot_image(target)
        workspace_sources = {key: hashlib.sha256((self.attempt.checkout / name).read_bytes()).hexdigest()
            for key, name in {"installer": "modules/common/runtime/workspace_sandbox.py",
                              "bundle": "modules/common/runtime/workspace_sandbox_profiles.json"}.items()}
        request = json.dumps({"binding": binding, "boot_image": boot_image, "source": base64.b64encode(source).decode(),
                              "workspace_sources": workspace_sources}).encode()
        until = self.now() + 900
        while True:
            try:
                report = decoded(self.ssh(target, ["sudo", "-n", "python3", "-B", "-I", "-c", source.decode()],
                                          input=request, timeout=420))
                validate_contents(report, binding)
                require(report["boot_image"] == boot_image, "resolved_boot_image_unverified")
                return report
            except PolicyError as error:
                # A light trial may reach two minutes while npm still installs
                # or builds. Wait for resolved contents within the owned trial;
                # never treat a running-but-unresolved image as fingerprinted.
                if str(error) != "command_failed" or self.now() >= until:
                    raise
                self.sleep(10)

    def probe(self, target, timeout=180):
        report = decoded(self.ssh(target, ["sudo", "-n", "python3", "-", self.provider, target["checksum"],
            target["data_volume_id"], target["data_volume_name"]], input=BOOTSTRAP.encode(), timeout=timeout))
        require(report.get("status") == "pass" and report.get("checksum") == target["checksum"] and report.get("dns") == target["dns"]
                and report.get("data_volume_id") == target["data_volume_id"]
                and all(isinstance(report.get(key), str) and re.fullmatch(UUID, report[key]) for key in ("boot_id", "disk_uuid")), "bootstrap_proof_failed")
        return {key: report[key] for key in ("boot_id", "disk_uuid", "data_volume_id", "checksum", "dns")}

    @exclusive
    def bootstrap(self):
        evidence = self.evidence()
        evidence["checks"]["launch_ssh"] = "started"
        private_write(self.path, evidence)
        try:
            proof = self.probe(self.bind())
            if evidence["baseline"] is None:
                evidence["baseline"] = proof
            else:
                require(proof == (evidence.get("after_reboot") if evidence["checks"].get("reboot") == "pass" else evidence["baseline"]), "persistence_baseline_changed")
        except BaseException:
            evidence["checks"]["launch_ssh"] = "uncertain"
            private_write(self.path, evidence)
            raise
        evidence["checks"]["launch_ssh"] = "pass"
        private_write(self.path, evidence)
        return proof

    def doctor_module(self):
        source = self.attempt.checkout / DOCTOR_PATH
        require(source.is_file(), "verified_doctor_required")
        spec = importlib.util.spec_from_file_location("owned_showcase_doctor", source)
        doctor = importlib.util.module_from_spec(spec)
        # Loading checked code must not create an unrecorded __pycache__ in
        # the isolated source tree or make its next integrity check fail.
        exec(compile(source.read_bytes(), str(source), "exec"), doctor.__dict__)
        return doctor

    def check_doctors(self, target):
        doctor = self.doctor_module()
        source = self.attempt.checkout / DOCTOR_PATH
        required = doctor.required_checks(["openclaw", "hermes", "workspace"], True, True)
        if self.models is None:
            report = decoded(self.ssh(target, ["sudo", "-n", "python3", "-", "--strict", "--remote", "--json",
                "--services", "openclaw,hermes,workspace", "--require-claude", "--check-tailscale", "--tailscale-mode", "host"],
                input=source.read_bytes(), timeout=300, capture_failure="host-doctor-command-error.json"))
        else:
            # Never rerun native doctors with the original container's model
            # environment. Captured, isolated diagnostics supply each runtime
            # check; the unchanged strict collector supplies workspace/routes.
            evidence = self.models.active()
            baseline = evidence["baseline"] if self.models.stage == "before_reboot" else evidence["after_reboot"]
            require(self.probe(target) == baseline, "model_session_boot_changed")
            reports = [self.models.runtime_doctor(role) for role in ("openclaw", "hermes")]
            before = self.ledger.operate(lambda state: task_evidence.hashed(self.models.spend.account(state)["model_requests"]))
            with self.models.gateway.diagnostic_window("openclaw"), self.models.gateway.diagnostic_window("hermes"):
                report = decoded(self.ssh(target, ["sudo", "-n", "python3", "-", "--strict", "--remote", "--json",
                    "--services", "workspace", "--require-claude", "--check-tailscale", "--tailscale-mode", "host",
                    "--route-services", "openclaw,hermes"], input=source.read_bytes(), timeout=180, capture_failure="host-doctor-command-error.json"))
                subset = doctor.required_checks(["workspace"], True, True)
                require(report.get("status") == "pass" and doctor.accepted(report, subset), "runtime_doctor_failed")
                require(self.ledger.operate(lambda state: task_evidence.hashed(self.models.spend.account(state)["model_requests"])) == before,
                        "runtime_doctor_failed")
                require(self.probe(target) == baseline, "model_session_boot_changed")
                self.models.active()
            report["checks"] += [{"name": key, "status": value} for proof in reports for key, value in proof["checks"].items()]
        require(report.get("status") == "pass" and doctor.accepted(report, required), "runtime_doctor_failed")
        return {row["name"]: "pass" for row in report["checks"] if row["name"] in required}

    @exclusive
    def doctors(self):
        evidence = self.evidence()
        self.require_container_gate(evidence)
        required = self.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        if self.models is not None:
            self.models.active()
            require(self.models.stage not in evidence.get("doctor_stages", {}), "runtime_doctor_already_invoked")
            evidence.setdefault("doctor_stages", {})[self.models.stage] = {"status": "started"}
        evidence["checks"].update({key: "started" for key in required})
        private_write(self.path, evidence)
        try:
            checks = self.check_doctors(self.bind())
        except BaseException:
            if self.models is not None:
                try: self.models.close()
                except BaseException: pass
                evidence = private_read(self.path)
                evidence.setdefault("doctor_stages", {})[self.models.stage] = {"status": "uncertain"}
            evidence["checks"].update({key: "uncertain" for key in required})
            private_write(self.path, evidence)
            raise
        evidence = self.evidence()
        evidence["checks"].update(checks)
        if self.models is not None:
            stage = self.models.stage
            baseline = evidence["baseline"] if stage == "before_reboot" else evidence["after_reboot"]
            channel = evidence["model_channels"][stage]
            evidence["doctor_stages"][stage] = {"schema": 1, "status": "verified", "run_id": self.run.name,
                "source_revision": evidence["source_revision"], "boot_id": baseline["boot_id"], "checksum": baseline["checksum"],
                "checks": checks, "runtime_reports_sha256": {role: task_evidence.hashed(channel["runtime_doctors"][role]) for role in ("openclaw", "hermes")},
                "runtime_routes_sha256": {role: task_evidence.hashed(channel["runtime_routes"][role]) for role in ("openclaw", "hermes")}}
        private_write(self.path, evidence)
        return checks

    def doctor_receipt(self):
        require(self.models is not None, "model_session_required")
        evidence = self.models.active()
        proof = evidence.get("doctor_stages", {}).get(self.models.stage)
        required = self.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        baseline = evidence["baseline"] if self.models.stage == "before_reboot" else evidence["after_reboot"]
        channel = evidence["model_channels"][self.models.stage]
        require(isinstance(proof, dict) and set(proof) == {"schema", "status", "run_id", "source_revision", "boot_id", "checksum",
                "checks", "runtime_reports_sha256", "runtime_routes_sha256"}
                and type(proof.get("schema")) is int and proof["schema"] == 1 and proof.get("status") == "verified"
                and proof.get("run_id") == self.run.name and proof.get("source_revision") == evidence["source_revision"]
                and proof.get("boot_id") == baseline["boot_id"] and proof.get("checksum") == baseline["checksum"]
                and proof.get("checks") == {name: "pass" for name in required}
                and all(evidence["checks"].get(name) == "pass" for name in required)
                and all(proof.get(key) == {role: task_evidence.hashed(channel.get(source, {}).get(role)) for role in ("openclaw", "hermes")}
                        for key, source in (("runtime_reports_sha256", "runtime_doctors"), ("runtime_routes_sha256", "runtime_routes"))),
                "runtime_doctor_proof_required")
        return json.loads(json.dumps(proof))

    @exclusive
    def container_tests(self):
        evidence = self.evidence()
        require(evidence["checks"].get("launch_ssh") == "pass" and isinstance(evidence["baseline"], dict), "bootstrap_proof_required")
        require("containers" not in evidence, "container_tests_already_invoked")
        bundle = private_read(self.run / "container-bundle.json")
        module = self.attempt.container_module()
        try:
            sha = module.validate_bundle(bundle)
        except Exception:
            raise PolicyError("container_bundle_invalid") from None
        require(bundle["source_revision"] == self.attempt.prepared["source_revision"]
                and bundle["script"] == (self.attempt.checkout / "scripts/test_containers.py").read_text()
                and all(bundle["pins"][name] == self.config["pins"][name] for name in ("codex_release", "claude_release")), "container_bundle_invalid")
        record = self.attempt.record()
        require(record.get("container_tests_gate", "pass") != "deferred_owned_vm" or record.get("container_bundle_sha256") == sha,
                "container_bundle_changed")
        _, active = self.ensure()
        require(remaining_timeout(active, 3400, now=self.now()) == 3400, "container_check_time_required")
        evidence["containers"] = {"status": "started", "request_id": bundle["request_id"], "bundle_sha256": sha}
        evidence["checks"]["container_tests"] = "started"
        private_write(self.path, evidence)
        try:
            target = self.bind()
            require(self.probe(target) == evidence["baseline"], "container_boot_baseline_changed")
            source = (self.attempt.checkout / "scripts/showcase_container_probe.py").read_text()
            require(remaining_timeout(self.ensure()[1], 3100, now=self.now()) == 3100, "container_check_time_required")
            report = decoded(self.ssh(target, ["sudo", "-n", "python3", "-I", "-c", source],
                                      input=json.dumps(bundle).encode(), timeout=3100, capture_failure=True))
            proof = module.validate_proof(report, bundle, evidence["baseline"]["boot_id"])
            require(self.probe(target) == evidence["baseline"], "container_boot_baseline_changed")
            evidence["containers"].update(proof)
            evidence["checks"]["container_tests"] = "pass"
        except BaseException:
            evidence["containers"]["status"] = "uncertain"
            evidence["checks"]["container_tests"] = "uncertain"
            private_write(self.path, evidence)
            raise PolicyError("container_acceptance_failed") from None
        private_write(self.path, evidence)
        return proof

    def verify_files(self, target, task):
        try:
            require(task["input_sha256"] == task_evidence.input_hash(task["challenge"])
                    and task["answer_sha256"] == task_evidence.expected_answer(task["challenge"]), "workspace_file_verification_failed")
        except (ValueError, TypeError, KeyError):
            raise PolicyError("workspace_file_verification_failed") from None
        report = decoded(self.ssh(target, WORKSPACE + ["python3", "-c", VERIFY_FILES, task["fixture"], task["input_sha256"]]))
        require(report.get("status") == "pass" and type(report.get("uid")) is int and report["uid"] == 1000
                and report.get("input_sha256") == task["input_sha256"] and isinstance(report.get("result_sha256"), str)
                and re.fullmatch(HEX, report["result_sha256"]) and report.get("answer_sha256") == task["answer_sha256"], "workspace_file_verification_failed")
        return report["result_sha256"]

    def correlate_task(self, role, challenge, usage, receipt):
        try:
            return task_evidence.correlate(role, challenge, usage, receipt["requests"], self.models.policy["roles"][role])
        except (ValueError, TypeError, KeyError, IndexError):
            raise PolicyError("model_task_provider_mismatch") from None

    def agent_module(self):
        source = self.attempt.checkout / AGENT_PATH
        require(source.is_file(), "verified_agent_probe_required")
        spec = importlib.util.spec_from_file_location("owned_showcase_agents", source)
        module = importlib.util.module_from_spec(spec)
        exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
        return module

    def peer_module(self):
        # Resolve these imports from the checked revision, never a different
        # worktree or personal module path, and create no Python cache files.
        modules = {}
        def local_import(name, *args, **kwargs):
            return modules[name] if name in modules else builtins.__import__(name, *args, **kwargs)
        for name, path in (("showcase_agents", AGENT_PATH), ("showcase_browser", BROWSER_PATH), ("showcase_peer", PEER_PATH)):
            source = self.attempt.checkout / path
            require(source.is_file(), "verified_peer_probe_required")
            module = types.ModuleType(name)
            module.__file__ = str(source)
            module.__dict__["__builtins__"] = {**vars(builtins), "__import__": local_import}
            exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
            modules[name] = module
        return modules["showcase_peer"]

    @exclusive
    def network_checks(self, stage, peer_execute=None):
        require(stage in ("before_reboot", "after_reboot"), "invalid_network_stage")
        evidence = self.evidence()
        self.require_container_gate(evidence)
        required = self.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        require(evidence["checks"].get("launch_ssh") == "pass" and
                all(evidence["checks"].get(key) == "pass" for key in required), "runtime_doctor_proof_required")
        require((stage == "before_reboot" and "reboot" not in evidence["checks"]) or
                (stage == "after_reboot" and evidence["checks"].get("reboot") == "pass"), "network_boot_stage_invalid")
        module = self.peer_module()
        local = self.config['runner'].get('local_peer')
        access = local if local is not None else self.config["runner"].get("peer_ssh")
        if local is not None:
            from showcase_local_peer import checked
            checked(local, self.config['tailnet']['runner_dns'], self.now)
        else:
            try:
                module.access_valid(access)
            except module.ProbeError:
                raise PolicyError("existing_peer_access_required") from None
        _, active = self.ensure()
        require(remaining_timeout(active, 750, now=self.now()) == 750, "network_check_time_required")
        baseline = evidence["baseline"] if stage == "before_reboot" else evidence.get("after_reboot")
        require(isinstance(baseline, dict), "network_boot_baseline_changed")
        request_id, nonce = str(uuid.uuid4()), secrets.token_hex(16)
        evidence["network"][stage] = {"status": "started", "request_id": request_id, "boot_id": baseline["boot_id"], "nonce": nonce}
        evidence["checks"]["tailscale.acceptance"] = "started"
        private_write(self.path, evidence)
        try:
            target = self.bind()
            require(self.probe(target) == baseline, "network_boot_baseline_changed")
            check_tailnet(self.config["tailnet"], self.attempt.session.secret(self.config["tailnet"]["cleanup_key_ref"]),
                          self.attempt.session.http)
            raw = module.strict_json(self.ssh(target, ["sudo", "-n", "python3", "-"], input=module.VM_SOURCE.encode(), timeout=120))
            require(isinstance(raw, dict) and set(raw) == {"schema", "status", "browser_url", "hermes_username", "hermes_password",
                    "host_public_key", "workspace_public_key", "boot_id"} and type(raw["schema"]) is int and raw["schema"] == 1
                    and raw["status"] == "pass" and raw["boot_id"] == baseline["boot_id"], "private_peer_inputs_unverified")
            request = {"schema": 1, "request_id": request_id, "runner_dns": self.config["tailnet"]["runner_dns"],
                "target_dns": target["dns"], "openclaw_url": module.browser.rebase_handoff(raw["browser_url"], target["dns"]),
                "hermes_username": raw["hermes_username"], "hermes_password": raw["hermes_password"]}
            payload = {"browser": request, "ssh": {"nonce": nonce, "boot_id": baseline["boot_id"],
                "host_public_key": module.public_key(raw["host_public_key"]), "workspace_public_key": module.public_key(raw["workspace_public_key"]),
                "private_key": (self.attempt.checkout / KEY_PATH).read_text()}}
            module.payload_valid(payload)
            command = module.source_command(self.attempt.checkout)
            # Leave the full peer deadline after collection; never let a stale
            # preparation window dispatch beyond the attempt's work deadline.
            require(remaining_timeout(self.ensure()[1], 250, now=self.now()) == 250, "network_check_time_required")
            if peer_execute is None:
                if local is not None:
                    from showcase_local_peer import execute_probe
                    peer_execute = lambda access, command, payload: execute_probe(access,
                        self.config['tailnet']['runner_dns'], command, payload, self.now)
                else:
                    reader = self.attempt.session.reader
                    peer_execute = lambda access, command, payload: module.existing_peer_execute(access, command, payload, reader)
            report = module.strict_json(peer_execute(access, command, payload))
            proof = module.validate_proof(report, payload)
            require(self.probe(target) == baseline, "network_boot_baseline_changed")
            evidence["network"][stage].update(proof)
            evidence["checks"]["tailscale.acceptance"] = "pass"
        except BaseException as error:
            evidence["network"][stage]["status"] = "uncertain"
            evidence["checks"]["tailscale.acceptance"] = "uncertain"
            private_write(self.path, evidence)
            # Collector/browser errors may contain credentials or private URLs.
            reason = "onepassword_unavailable_or_locked" if isinstance(error, PreflightError) and str(error) == "onepassword_unavailable_or_locked" else "network_acceptance_failed"
            raise PolicyError(reason) from None
        private_write(self.path, evidence)
        return proof

    @exclusive
    def runtime_task(self, runtime):
        require(runtime in ("openclaw", "hermes"), "invalid_runtime")
        evidence = self.evidence()
        self.require_container_gate(evidence)
        require(evidence["checks"].get("launch_ssh") == "pass", "bootstrap_proof_required")
        required = self.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        require(all(evidence["checks"].get(key) == "pass" for key in required), "runtime_doctor_proof_required")
        require(runtime not in evidence["agents"], "runtime_task_already_invoked")
        require(self.models is not None, "model_session_required")
        self.bind(); module = self.agent_module()
        model = self.config["models"][runtime]
        require(isinstance(model, str) and re.fullmatch(module.MODEL, model), "model_ids_required")
        challenge = {"lhs": secrets.randbelow(1000000), "rhs": secrets.randbelow(1000000), "nonce": secrets.token_hex(16)}
        request_id = str(uuid.uuid4())
        _, active = self.ensure()
        require(remaining_timeout(active, 300, now=self.now()) == 300, "agent_task_time_required")
        evidence["agents"][runtime] = {"status": "started", "model": model, "request_id": request_id, "challenge": challenge}
        private_write(self.path, evidence)
        try:
            self.models.runtime_client(runtime)
            start = self.models.begin_task(runtime)
            report = self.models.run_runtime_task(runtime, challenge, request_id, start)
            try:
                proof = module.validate_proof(report["turn"], runtime, model, challenge, request_id)
            except module.ProbeError:
                raise PolicyError("runtime_task_failed") from None
            proof["runtime_task_sha256"] = report["proof_sha256"]
            proof["gateway_receipt"] = self.models.task_receipt(runtime, start)
            proof["provider_task"] = self.correlate_task(runtime, challenge, proof["model_usage"], proof["gateway_receipt"])
            evidence = self.evidence()
            evidence["agents"][runtime].update(proof)
            evidence["checks"][runtime + ".agent_turn"] = "pass"
        except BaseException:
            try: self.models.close()
            except BaseException: pass
            evidence = private_read(self.path)
            evidence["agents"][runtime]["status"] = "uncertain"
            # A failed task can stop its test container. Prior healthy/doctor
            # evidence cannot remain green after that uncertainty.
            evidence["checks"].update({key: "uncertain" for key in required if key.startswith(runtime + ".") or key == "service." + runtime})
            evidence["checks"][runtime + ".agent_turn"] = "uncertain"
            private_write(self.path, evidence)
            raise
        private_write(self.path, evidence)
        return proof

    @exclusive
    def workspace_sandbox(self, stage):
        from showcase_workspace_sandbox import verify
        require(stage in ("before_reboot", "after_reboot"), "workspace_sandbox_stage_refused")
        evidence = self.evidence()
        self.require_container_gate(evidence)
        require(stage not in evidence.setdefault("workspace_sandbox_checks", {}), "workspace_sandbox_already_invoked")
        target = self.bind()
        baseline = evidence["baseline"] if stage == "before_reboot" else evidence.get("after_reboot")
        require(isinstance(baseline, dict) and baseline.get("checksum") == target["checksum"], "bootstrap_proof_required")
        path = self.attempt.checkout / "scripts/showcase_workspace_sandbox.py"
        require(path.is_file() and not path.is_symlink(), "workspace_sandbox_verifier_required")
        nonce = secrets.token_hex(16)
        report = decoded(self.ssh(target, ["sudo", "-n", "docker", "compose", "-f", "/opt/agent-stack/docker-compose.yml",
            "exec", "-T", "--user", "1000", "-e", "HOME=/home/user", "workspace", "python3", "-B", "-c", path.read_text()],
            input=json.dumps({"nonce": nonce}).encode(), timeout=90, capture_failure=True))
        try:
            verify(report, nonce)
            require(report["boot_id"] == baseline["boot_id"], "workspace_sandbox_boot_changed")
        except (AssertionError, ValueError, TypeError, KeyError):
            raise PolicyError("workspace_sandbox_unverified") from None
        evidence["workspace_sandbox_checks"][stage] = report
        private_write(self.path, evidence)
        return report

    @exclusive
    def workspace_task(self, cli):
        require(cli in ("codex", "claude"), "invalid_workspace_cli")
        evidence = self.evidence()
        self.require_container_gate(evidence)
        require(evidence["checks"].get("launch_ssh") == "pass", "bootstrap_proof_required")
        from showcase_policy import acceptance_mode
        if acceptance_mode(self.config, self.provider) == "full":
            require(evidence.get("workspace_sandbox_checks", {}).get("before_reboot", {}).get("status") == "workspace_sandbox_verified",
                    "workspace_sandbox_proof_required")
        require(cli not in evidence["tasks"], "workspace_task_already_invoked")
        require(self.models is not None, "model_session_required")
        target = self.bind()
        model = self.config["models"][cli]
        require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", model), "model_ids_required")
        # Persist before secret retrieval/SSH: timeout or process loss cannot
        # silently repeat paid work inside the same attempt.
        challenge = {"lhs": secrets.randbelow(1000000), "rhs": secrets.randbelow(1000000), "nonce": secrets.token_hex(16)}
        evidence["tasks"][cli] = {"status": "started", "model": model, "challenge": challenge}
        private_write(self.path, evidence)
        try:
            bundle = self.models.workspace_client(cli)
            start = self.models.begin_task(cli)
            report = decoded(self.ssh(target, WORKSPACE + ["python3", "/usr/local/libexec/agent-stack-workspace-cli-task",
                "--cli", cli, "--model", model, "--timeout", "180", "--model-gateway", "--challenge",
                json.dumps(challenge, sort_keys=True, separators=(",", ":"))], input=json.dumps(bundle).encode(), timeout=195,
                capture_failure="workspace-command-error.json"))
            usage, fixture, digest = report.get("model_usage"), report.get("fixture"), report.get("input_sha256")
            duration = report.get("duration_seconds")
            require(report.get("cli") == cli and report.get("status") == "pass" and report.get("verified") is True
                    and isinstance(usage, dict) and set(usage) == task_evidence.COUNTS
                    and all(type(usage.get(k)) is int and 0 <= usage[k] <= 1000000000 for k in task_evidence.COUNTS)
                    and usage["output_tokens"] > 0 and isinstance(fixture, str)
                    and re.fullmatch(r"\.agent-stack/acceptance/" + cli + r"-[a-z0-9_]{1,64}", fixture)
                    and digest == task_evidence.input_hash(challenge)
                    and type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration <= 190, "workspace_task_failed")
            require(report.get("model_route") == {"kind": "budget_gateway", "role": cli, "provider": bundle["provider"],
                    "base_url_sha256": hashlib.sha256(bundle["base_url"].encode()).hexdigest()}, "workspace_model_route_unverified")
            task = {"status": "pass", "model": model, "fixture": fixture, "input_sha256": digest,
                    "challenge": challenge, "answer_sha256": task_evidence.expected_answer(challenge),
                    "model_usage": dict(usage), "duration_seconds": duration}
            task["result_sha256"] = self.verify_files(target, task)
            task["gateway_receipt"] = self.models.task_receipt(cli, start)
            task["provider_task"] = self.correlate_task(cli, challenge, usage, task["gateway_receipt"])
            evidence = self.evidence()
            evidence["tasks"][cli] = task
            evidence["checks"]["workspace." + cli + ".execution"] = "pass"
        except BaseException:
            # Stop authority immediately, even when the CLI/SSH result is
            # lost. Closing does not refund or replay any admitted request.
            try: self.models.close()
            except BaseException: pass
            evidence = private_read(self.path)
            evidence["tasks"][cli]["status"] = "uncertain"
            evidence["checks"]["workspace." + cli + ".execution"] = "uncertain"
            private_write(self.path, evidence)
            raise
        private_write(self.path, evidence)
        return task

    @exclusive
    def reboot(self, resume=False, defer_doctors=False):
        evidence = self.evidence()
        self.require_container_gate(evidence)
        required = self.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        baseline = evidence["baseline"]
        require(isinstance(baseline, dict) and all(cli in evidence["tasks"] and evidence["tasks"][cli].get("status") == "pass"
                for cli in ("codex", "claude")) and (acceptance_mode(self.config, self.provider) == "full"
                or all(evidence["agents"].get(runtime, {}).get("status") == "pass"
                for runtime in ("openclaw", "hermes"))), "persistence_baseline_required")
        require(evidence["network"].get("before_reboot", {}).get("status") == "pass", "peer_baseline_required")
        require(type(resume) is bool and ((resume and evidence["checks"].get("reboot") in ("started", "uncertain"))
                or (not resume and "reboot" not in evidence["checks"])), "reboot_already_invoked")
        require(self.models is None or self.models.state == "closed", "model_session_must_close_before_reboot")
        require(type(defer_doctors) is bool and (not defer_doctors or self.models is not None), "model_session_required")
        target = self.bind()
        if not resume:
            before = self.probe(target)
            require(before == baseline, "persistence_baseline_changed")
        evidence["checks"]["reboot"] = "started"
        # The previous network proof remains historical, but cannot establish
        # post-reboot access. Only a fresh second-peer check can restore this.
        evidence["checks"]["tailscale.acceptance"] = "uncertain"
        private_write(self.path, evidence)
        try:
            if not resume:
                try:
                    self.ssh(target, ["sudo", "-n", "systemctl", "reboot"], timeout=30)
                except PolicyError as error:
                    if str(error) not in ("command_failed", "command_timed_out"):
                        raise
            until = self.now() + 300
            after = None
            while self.now() < until:
                self.sleep(min(3, until - self.now()))
                if self.now() >= until:
                    break
                try:
                    after = self.probe(target, timeout=min(180, until - self.now()))
                except PolicyError as error:
                    if str(error) not in ("command_failed", "command_timed_out", "bootstrap_proof_failed"):
                        raise
                    continue
                if after["boot_id"] != baseline["boot_id"]:
                    break
            require(isinstance(after, dict) and after["boot_id"] != baseline["boot_id"], "new_boot_not_verified")
            require(all(after[key] == baseline[key] for key in ("disk_uuid", "data_volume_id", "checksum", "dns")), "persistent_disk_or_runtime_changed")
            checks = {} if defer_doctors else self.check_doctors(target)
            for task in evidence["tasks"].values():
                require(self.verify_files(target, task) == task["result_sha256"], "persisted_workspace_result_changed")
            evidence["after_reboot"] = after
            evidence["checks"].update(checks)
            evidence["checks"]["reboot"] = "pending_doctors" if defer_doctors else "pass"
            if defer_doctors:
                evidence["checks"]["reboot_boot"] = "pass"
                evidence["checks"].update({key: "uncertain" for key in required})
        except BaseException:
            evidence["checks"].update({key: "uncertain" for key in required})
            evidence["checks"]["reboot"] = "uncertain"
            private_write(self.path, evidence)
            raise
        private_write(self.path, evidence)
        return {"reboot": "pass", "workspace_files": "preserved", "doctors": "pending" if defer_doctors else "pass"}

    @exclusive
    def complete_reboot(self):
        """Complete persistence only after fresh activation and all diagnostics."""
        evidence = self.evidence()
        require(evidence["checks"].get("reboot") == "pending_doctors" and evidence["checks"].get("reboot_boot") == "pass"
                and self.models is not None and self.models.stage == "after_reboot", "post_reboot_doctors_required")
        self.models.active(); self.require_container_gate(evidence)
        required = self.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        proof, after = self.doctor_receipt(), evidence["after_reboot"]
        require(proof.get("status") == "verified" and proof.get("boot_id") == after["boot_id"]
                and proof.get("checks") == {name: "pass" for name in required}
                and all(evidence["checks"].get(name) == "pass" for name in required), "post_reboot_doctors_required")
        try:
            target = self.bind()
            require(self.probe(target) == after, "persistence_baseline_changed")
            for task in evidence["tasks"].values():
                require(self.verify_files(target, task) == task["result_sha256"], "persisted_workspace_result_changed")
            self.models.active()
        except BaseException:
            evidence["checks"].update({name: "uncertain" for name in required})
            evidence["checks"]["reboot"] = "uncertain"; private_write(self.path, evidence)
            try: self.models.close()
            except BaseException: pass
            raise
        evidence["checks"]["reboot"] = "pass"; private_write(self.path, evidence)
        return {"reboot": "pass", "workspace_files": "preserved", "doctors": "pass"}
