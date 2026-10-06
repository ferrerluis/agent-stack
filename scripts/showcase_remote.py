#!/usr/bin/env python3
"""Measured SSH, doctor, workspace-task and reboot checks for an owned attempt.

This is a controller component, not a launch/readiness override. Runtime agent
turns and authenticated second-peer browser checks are separate requirements.
Raw command output and secrets never enter the evidence record.
"""
import importlib.util
import ipaddress
import json
import math
import os
import re
import shlex
import stat
import time

from showcase_attempt import KEY_PATH, command, isolated_environment, remaining_timeout, verify_prepared
from showcase_cleanup import capture, private_read, private_write
from showcase_policy import PolicyError, require
from showcase_preflight import field, objects, segment
from showcase_terraform import exclusive, guard_state

VM_TYPES = {"aws": "aws_instance", "digitalocean": "digitalocean_droplet", "gcp": "google_compute_instance", "hetzner": "hcloud_server"}
DISK_TYPES = {"aws": "aws_ebs_volume", "digitalocean": "digitalocean_volume", "gcp": "google_compute_disk", "hetzner": "hcloud_volume"}
DOCTOR_PATH = "skills/agent-stack-doctor/scripts/strict_health.py"
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
    challenge=json.loads(before); actual=json.loads(result)
    assert set(challenge)=={'lhs','rhs','nonce'} and set(actual)=={'sum','nonce'}
    assert all(type(challenge[k]) is int and 0<=challenge[k]<1000000 for k in ('lhs','rhs'))
    assert re.fullmatch(r'[a-f0-9]{32}',challenge['nonce'])
    assert type(actual['sum']) is int and actual['sum']==challenge['lhs']+challenge['rhs'] and actual['nonce']==challenge['nonce']
    report={'schema':1,'status':'pass','uid':1000,'input_sha256':hashlib.sha256(before).hexdigest(),'result_sha256':hashlib.sha256(result).hexdigest()}
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
        vm = adapter.get("/projects/" + segment(project) + "/zones/" + segment(zone) + "/instances/" + segment(name))
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

    def ensure(self):
        prepared = verify_prepared(self.run, self.config, self.provider)
        require(self.attempt.record().get("stage") == "applied", "applied_attempt_required")
        active = self.attempt.active(self.ledger)
        remaining_timeout(active, 1, now=self.now())
        return prepared, active

    def evidence(self):
        prepared, _ = self.ensure()
        result = private_read(self.path) if self.path.exists() else {"schema": 1, "run_id": self.run.name,
            "source_revision": prepared["source_revision"], "checks": {}, "tasks": {}, "baseline": None}
        require(result.get("schema") == 1 and result.get("run_id") == self.run.name and result.get("source_revision") == prepared["source_revision"]
                and isinstance(result.get("checks"), dict) and isinstance(result.get("tasks"), dict), "remote_evidence_scope_mismatch")
        return result

    def bind(self):
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
        require(len(nodes) == 1 and re.fullmatch(re.escape(self.run.name) + r"\.[a-z0-9.-]+\.ts\.net", nodes[0]["name"]), "one_owned_tailnet_node_required")
        return {"host": "admin@" + matching_address(self.attempt.session.adapter, self.provider, vms[0]),
                "checksum": checksum, "dns": nodes[0]["name"], "data_volume_id": str(disks[0]["id"]),
                "data_volume_name": disks[0].get("name") or (disks[0].get("tags") or {}).get("Name")}

    def ssh(self, target, args, input=None, timeout=60):
        _, active = self.ensure()
        hosts = self.attempt.checkout / ".ssh/known_hosts"
        require(hosts.is_file() and not hosts.is_symlink() and hosts.stat().st_uid == os.geteuid()
                and stat.S_IMODE(hosts.stat().st_mode) == 0o600, "private_known_hosts_required")
        wrapper = self.attempt.checkout / "bin/agent-stack-ssh"
        require(wrapper.is_file() and wrapper.stat().st_mode & 0o100, "verified_ssh_wrapper_required")
        return self.execute([str(wrapper), "--batch", "--known-hosts", str(hosts), "--key", str(self.attempt.checkout / KEY_PATH),
                             "--host", target["host"], "--", shlex.join(args)], env=isolated_environment(self.run),
                            input=input, timeout=remaining_timeout(active, timeout, now=self.now()))

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
        report = decoded(self.ssh(target, ["sudo", "-n", "python3", "-", "--strict", "--remote", "--json",
            "--services", "openclaw,hermes,workspace", "--require-claude", "--check-tailscale", "--tailscale-mode", "host"],
            input=source.read_bytes(), timeout=300))
        required = doctor.required_checks(["openclaw", "hermes", "workspace"], True, True)
        require(report.get("status") == "pass" and doctor.accepted(report, required), "runtime_doctor_failed")
        return {row["name"]: "pass" for row in report["checks"] if row["name"] in required}

    @exclusive
    def doctors(self):
        evidence = self.evidence()
        required = self.doctor_module().required_checks(["openclaw", "hermes", "workspace"], True, True)
        evidence["checks"].update({key: "started" for key in required})
        private_write(self.path, evidence)
        try:
            checks = self.check_doctors(self.bind())
        except BaseException:
            evidence["checks"].update({key: "uncertain" for key in required})
            private_write(self.path, evidence)
            raise
        evidence["checks"].update(checks)
        private_write(self.path, evidence)
        return checks

    def verify_files(self, target, task):
        report = decoded(self.ssh(target, WORKSPACE + ["python3", "-c", VERIFY_FILES, task["fixture"], task["input_sha256"]]))
        require(report.get("status") == "pass" and type(report.get("uid")) is int and report["uid"] == 1000
                and report.get("input_sha256") == task["input_sha256"] and isinstance(report.get("result_sha256"), str)
                and re.fullmatch(HEX, report["result_sha256"]), "workspace_file_verification_failed")
        return report["result_sha256"]

    @exclusive
    def workspace_task(self, cli):
        require(cli in ("codex", "claude"), "invalid_workspace_cli")
        evidence = self.evidence()
        require(evidence["checks"].get("launch_ssh") == "pass", "bootstrap_proof_required")
        require(cli not in evidence["tasks"], "workspace_task_already_invoked")
        target = self.bind()
        model = self.config["models"][cli]
        require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", model), "model_ids_required")
        # Persist before secret retrieval/SSH: timeout or process loss cannot
        # silently repeat paid work inside the same attempt.
        evidence["tasks"][cli] = {"status": "started", "model": model}
        private_write(self.path, evidence)
        try:
            key = self.attempt.session.secret(self.config["secret_refs"]["openai_api_key" if cli == "codex" else "anthropic_api_key"])
            require(isinstance(key, str) and 0 < len(key.encode()) <= 65536 and not any(char.isspace() for char in key), "invalid_secret_field")
            report = decoded(self.ssh(target, WORKSPACE + ["python3", "/usr/local/libexec/agent-stack-workspace-cli-task",
                "--cli", cli, "--model", model, "--timeout", "180"], input=key.encode(), timeout=195))
            usage, fixture, digest = report.get("model_usage"), report.get("fixture"), report.get("input_sha256")
            duration = report.get("duration_seconds")
            require(report.get("cli") == cli and report.get("status") == "pass" and report.get("verified") is True
                    and isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens"))
                    and usage["output_tokens"] > 0 and isinstance(fixture, str)
                    and re.fullmatch(r"\.agent-stack/acceptance/" + cli + r"-[a-z0-9_]{1,64}", fixture)
                    and isinstance(digest, str) and re.fullmatch(HEX, digest)
                    and type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration <= 190, "workspace_task_failed")
            task = {"status": "pass", "model": model, "fixture": fixture, "input_sha256": digest,
                    "model_usage": {k: usage[k] for k in ("input_tokens", "output_tokens")}, "duration_seconds": duration}
            task["result_sha256"] = self.verify_files(target, task)
            evidence["tasks"][cli] = task
            evidence["checks"]["workspace." + cli + ".execution"] = "pass"
        except BaseException:
            evidence["tasks"][cli]["status"] = "uncertain"
            private_write(self.path, evidence)
            raise
        private_write(self.path, evidence)
        return task

    @exclusive
    def reboot(self, resume=False):
        evidence = self.evidence()
        baseline = evidence["baseline"]
        require(isinstance(baseline, dict) and all(cli in evidence["tasks"] and evidence["tasks"][cli].get("status") == "pass"
                for cli in ("codex", "claude")), "persistence_baseline_required")
        require(type(resume) is bool and ((resume and evidence["checks"].get("reboot") in ("started", "uncertain"))
                or (not resume and "reboot" not in evidence["checks"])), "reboot_already_invoked")
        target = self.bind()
        if not resume:
            before = self.probe(target)
            require(before == baseline, "persistence_baseline_changed")
        evidence["checks"]["reboot"] = "started"
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
            self.check_doctors(target)
            for task in evidence["tasks"].values():
                require(self.verify_files(target, task) == task["result_sha256"], "persisted_workspace_result_changed")
            evidence["after_reboot"] = after
            evidence["checks"]["reboot"] = "pass"
        except BaseException:
            evidence["checks"]["reboot"] = "uncertain"
            private_write(self.path, evidence)
            raise
        private_write(self.path, evidence)
        return {"reboot": "pass", "workspace_files": "preserved", "doctors": "pass"}
