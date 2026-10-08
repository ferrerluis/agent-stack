"""VM/container acceptance only; no doctors, model requests, peer or reboot."""
import math
import re

from showcase_policy import require

SERVICES = ("openclaw", "hermes", "workspace")
STEADY_SECONDS = 120
REMOTE = r'''import json,os,pathlib,re,stat,subprocess,sys
def run(*args):
    return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=10).stdout
try:
    assert os.geteuid()==0
    p=pathlib.Path('/opt/agent-stack/.last-apply.json'); s=p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and s.st_size<65536
    applied=json.loads(p.read_bytes())
    assert applied.get('status')=='success' and sorted(applied.get('services',[]))==['hermes','openclaw','workspace']
    assert applied.get('checksum')==sys.argv[1]
    roles={}
    for role in ('openclaw','hermes','workspace'):
        ids=run('docker','compose','-f','/opt/agent-stack/docker-compose.yml','ps','-a','-q',role).split()
        assert len(ids)==1
        v=json.loads(run('docker','inspect',ids[0].decode()))[0]; labels=v['Config']['Labels']; state=v['State']
        assert labels.get('com.docker.compose.project')=='agent-stack' and labels.get('com.docker.compose.service')==role
        roles[role]={'container_id':v['Id'],'image_id':v['Image'],'image_ref':v['Config']['Image'],
            'started_at':state['StartedAt'],'restarts':v['RestartCount'],'running':state['Running'],
            'restarting':state['Restarting'],'dead':state['Dead'],'oom':state['OOMKilled'],'exit_code':state['ExitCode']}
    report={'schema':1,'status':'pass','checksum':applied['checksum'],'roles':roles}
except Exception:
    report={'schema':1,'status':'fail'}
print(json.dumps(report))
sys.exit(0 if report['status']=='pass' else 1)
'''


def snapshot(report, checksum, pins):
    require(set(report) == {"schema", "status", "checksum", "roles"} and type(report["schema"]) is int
            and report["schema"] == 1 and report["status"] == "pass" and report["checksum"] == checksum
            and isinstance(report["roles"], dict) and set(report["roles"]) == set(SERVICES), "light_snapshot_invalid")
    for role, row in report["roles"].items():
        require(isinstance(row, dict) and set(row) == {"container_id", "image_id", "image_ref", "started_at", "restarts",
                "running", "restarting", "dead", "oom", "exit_code"}
                and isinstance(row["container_id"], str) and re.fullmatch(r"[a-f0-9]{64}", row["container_id"])
                and isinstance(row["image_id"], str) and re.fullmatch(r"sha256:[a-f0-9]{64}", row["image_id"])
                and isinstance(row["started_at"], str) and re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", row["started_at"])
                and type(row["restarts"]) is int and row["restarts"] >= 0
                and row["running"] is True and all(row[key] is False for key in ("restarting", "dead", "oom"))
                and type(row["exit_code"]) is int and row["exit_code"] == 0, "light_snapshot_invalid")
        expected = {"openclaw": "ghcr.io/openclaw/openclaw@" + pins["openclaw_digest"],
                    "hermes": pins["hermes_image"], "workspace": "agent-stack-workspace:local"}
        require(row["image_ref"] == expected[role], "light_snapshot_invalid")
    return report


def validate(proof, run_id, revision, scope, pins):
    require(isinstance(proof, dict) and set(proof) == {"schema", "status", "run_id", "source_revision", "scope", "target", "samples"}
            and type(proof["schema"]) is int and proof["schema"] == 1 and proof["status"] == "vm_and_containers_verified"
            and proof["run_id"] == run_id and proof["source_revision"] == revision and proof["scope"] == scope,
            "light_proof_invalid")
    target = proof["target"]
    require(isinstance(target, dict) and set(target) == {"vm_id", "checksum"}
            and isinstance(target["vm_id"], str) and bool(target["vm_id"])
            and isinstance(target["checksum"], str) and re.fullmatch(r"[a-f0-9]{64}", target["checksum"]), "light_proof_invalid")
    samples = proof["samples"]
    require(isinstance(samples, list) and 3 <= len(samples) <= 64, "light_stability_unverified")
    previous = None
    for row in samples:
        require(isinstance(row, dict) and set(row) == {"elapsed_seconds", "snapshot"}
                and type(row["elapsed_seconds"]) in (int, float) and math.isfinite(row["elapsed_seconds"]), "light_proof_invalid")
        snapshot(row["snapshot"], target["checksum"], pins)
        if previous is None:
            require(row["elapsed_seconds"] == 0, "light_stability_unverified")
        else:
            require(0 < row["elapsed_seconds"] - previous["elapsed_seconds"] <= 60
                    and row["snapshot"] == previous["snapshot"], "light_stability_unverified")
        previous = row
    require(previous["elapsed_seconds"] >= STEADY_SECONDS, "light_stability_unverified")
    return proof
