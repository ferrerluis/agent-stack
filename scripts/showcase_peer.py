#!/usr/bin/env python3
"""Bounded second-peer SSH/browser acceptance; no enrollment or paid work.

Controller bindings and exact-source checks are mandatory. Keys live in sealed
Linux memfd objects for a single SSH use; keys/pins have no on-disk files and
browser auth state is not exported. Source is delivered in memory.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import time

import showcase_browser as browser
from showcase_agents import ProbeError, invoke, need, strict_json

PATHS = {"showcase_agents": "scripts/showcase_agents.py", "showcase_browser": "scripts/showcase_browser.py",
         "showcase_peer": "scripts/showcase_peer.py"}
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC"}
SSH_PROOF = """import json,os,pathlib,pwd,re,sys
try:
    role,nonce=sys.argv[1:]
    assert role in ('host','workspace') and re.fullmatch(r'[a-f0-9]{32}',nonce)
    uid=os.geteuid(); user=pwd.getpwuid(uid).pw_name
    assert user==('admin' if role=='host' else 'user') and uid>0
    if role=='workspace':
        assert uid==1000 and os.environ['HOME']=='/home/user'
        assert pathlib.Path('/home/user').is_dir() and pathlib.Path('/home/user').stat().st_uid==1000
    connection=os.environ['SSH_CONNECTION'].split()
    assert len(connection)==4 and connection[1].isdigit() and connection[3]=='22'
    boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',boot)
    report={'schema':1,'status':'pass','role':role,'nonce':nonce,'uid':uid,'user':user,
        'boot_id':boot,'client_address':connection[0]}
except Exception:
    report={'schema':1,'status':'fail'}
print(json.dumps(report))
sys.exit(0 if report['status']=='pass' else 1)
"""
VM_SOURCE = """import json,os,pathlib,re,signal,subprocess,sys
def deadline(_signum,_frame): raise RuntimeError('collector_deadline')
signal.signal(signal.SIGALRM,deadline)
signal.alarm(110)
def pairs(rows):
    result={}
    for key,value in rows:
        assert key not in result
        result[key]=value
    return result
def run(*args):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=35).stdout
    assert len(result)<=65536
    return result
try:
    assert os.geteuid()==0
    compose=['/usr/bin/docker','compose','-f','/opt/agent-stack/docker-compose.yml']
    ids={}
    for service in ('openclaw','hermes','workspace'):
        identifier=run(*compose,'ps','-q',service).decode().strip()
        assert re.fullmatch(r'[a-f0-9]{64}',identifier)
        state=json.loads(run('/usr/bin/docker','inspect','--format','{{json .State}}',identifier))
        assert state.get('Running') is True and state.get('Restarting') is False
        ids[service]=identifier
    handoff=json.loads(run('/usr/bin/docker','exec',ids['openclaw'],'openclaw','dashboard','--json'),object_pairs_hook=pairs)
    assert isinstance(handoff,dict) and isinstance(handoff.get('browserUrl'),str)
    auth=json.loads(run('/usr/bin/docker','exec',ids['hermes'],'/opt/hermes/.venv/bin/python','-c',
        'import json,os; print(json.dumps({k:os.environ[k] for k in ("HERMES_DASHBOARD_BASIC_AUTH_USERNAME","HERMES_DASHBOARD_BASIC_AUTH_PASSWORD")}))'),object_pairs_hook=pairs)
    host=pathlib.Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip()
    workspace=run('/usr/bin/docker','exec',ids['workspace'],'/usr/bin/cat',
        '/var/lib/agent-stack-workspace/ssh-host-keys/ssh_host_ed25519_key.pub').decode().strip()
    for service,identifier in ids.items():
        assert run(*compose,'ps','-q',service).decode().strip()==identifier
        assert json.loads(run('/usr/bin/docker','inspect','--format','{{json .State}}',identifier)).get('Running') is True
    report={'schema':1,'status':'pass','browser_url':handoff['browserUrl'],
        'hermes_username':auth['HERMES_DASHBOARD_BASIC_AUTH_USERNAME'],
        'hermes_password':auth['HERMES_DASHBOARD_BASIC_AUTH_PASSWORD'],
        'host_public_key':host,'workspace_public_key':workspace,
        'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
except Exception:
    report={'schema':1,'status':'fail'}
print(json.dumps(report))
sys.exit(0 if report['status']=='pass' else 1)
"""
LOADER = """import base64,hashlib,json,sys,types
try:
    assert len(sys.argv[1])<=110000
    rows=json.loads(base64.b64decode(sys.argv[1],validate=True))
    assert list(rows)==['showcase_agents','showcase_browser','showcase_peer']
    for name,row in rows.items():
        assert set(row)=={'source','sha256'} and len(row['source'])<=40000
        assert hashlib.sha256(row['source'].encode()).hexdigest()==row['sha256']
        module=types.ModuleType(name); module.__file__='<checked-peer-source>/'+name+'.py'
        sys.modules[name]=module
        exec(compile(row['source'],module.__file__,'exec'),module.__dict__)
    sys.exit(sys.modules['showcase_peer'].main())
except Exception:
    print('{"schema":1,"status":"fail"}')
    sys.exit(1)
"""


def public_key(value):
    need(isinstance(value, str) and len(value) <= 2048 and not re.search(r"[\r\n\x00]", value), "host_key_invalid")
    match = re.fullmatch(r"ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\r\n]*)?", value)
    need(match, "host_key_invalid")
    try:
        raw = base64.b64decode(match[1], validate=True)
    except ValueError:
        raise ProbeError("host_key_invalid") from None
    need(len(raw) == 51 and raw[:19] == b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20', "host_key_invalid")
    return "ssh-ed25519 " + base64.b64encode(raw).decode()


def key_bytes(value):
    need(isinstance(value, (str, bytes)), "private_key_invalid")
    value = value.encode() if isinstance(value, str) else value
    need(64 <= len(value) <= 8192 and value.startswith(b'-----BEGIN OPENSSH PRIVATE KEY-----\n')
         and value.rstrip().endswith(b'-----END OPENSSH PRIVATE KEY-----') and b'\x00' not in value, "private_key_invalid")
    return value


@contextmanager
def memory_file(value):
    need(callable(getattr(os, 'memfd_create', None)), "linux_memory_file_required")
    fd = os.memfd_create('agent-stack-acceptance', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(value):
            offset += os.write(fd, value[offset:])
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        path = '/proc/' + str(os.getpid()) + '/fd/' + str(fd)
        need(Path(path).read_bytes() == value, "memory_file_unverified")
        yield path
    finally:
        os.close(fd)


def ssh_args(host, port, user, key, known, command):
    # Every option is explicit: never discover agents, personal config or proxy hops.
    return ['ssh', '-F', '/dev/null', '-i', key, '-p', str(port), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
        '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + known,
        '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'UpdateHostKeys=no',
        '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no', '-o', 'PreferredAuthentications=publickey',
        '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
        '-o', 'PermitLocalCommand=no', '-o', 'RequestTTY=no', '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2', user + '@' + host, command]


def validate_ssh(raw, role, nonce, boot, runner_addresses):
    fields = {'schema', 'status', 'role', 'nonce', 'uid', 'user', 'boot_id', 'client_address'}
    need(isinstance(raw, dict) and set(raw) == fields and type(raw['schema']) is int and raw['schema'] == 1
         and raw['status'] == 'pass' and raw['role'] == role and raw['nonce'] == nonce and raw['boot_id'] == boot
         and type(raw['uid']) is int and 0 < raw['uid'] < 2147483648
         and raw['user'] == ('admin' if role == 'host' else 'user')
         and (role != 'workspace' or raw['uid'] == 1000), 'peer_ssh_proof_invalid')
    need(browser.ts_ip(raw['client_address']) in runner_addresses, 'second_peer_ssh_source_unverified')
    return raw


def payload_valid(payload):
    need(isinstance(payload, dict) and set(payload) == {'browser', 'ssh'}, 'peer_request_invalid')
    browser.request_valid(payload['browser'])
    ssh = payload['ssh']
    need(isinstance(ssh, dict) and set(ssh) == {'private_key', 'host_public_key', 'workspace_public_key', 'nonce', 'boot_id'}, 'peer_request_invalid')
    key_bytes(ssh['private_key'])
    for role in ('host', 'workspace'):
        public_key(ssh[role + '_public_key'])
    need(isinstance(ssh['nonce'], str) and re.fullmatch(r'[a-f0-9]{32}', ssh['nonce'])
         and isinstance(ssh['boot_id'], str) and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', ssh['boot_id']), 'peer_request_invalid')
    need(len(json.dumps(payload).encode()) <= 4096, 'peer_request_too_large')


def run_probe(payload, execute=invoke, resolve=None, factory=None, now=time.time):
    payload_valid(payload)
    started = now()
    request, credentials = payload['browser'], payload['ssh']
    kwargs = {} if resolve is None else {'resolve': resolve}
    peer = browser.peer_check(request, lambda args, timeout: execute(args, timeout=timeout, env=ENV), **kwargs)
    reports = {}
    for role, port, user in (('host', 22, 'admin'), ('workspace', 2222, 'user')):
        host_key = public_key(credentials[role + '_public_key'])
        names = peer['address'] if port == 22 else '[' + peer['address'] + ']:' + str(port)
        with memory_file(key_bytes(credentials['private_key'])) as key, memory_file((names + ' ' + host_key + '\n').encode()) as known:
            args = ssh_args(peer['address'], port, user, key, known,
                shlex.join(['python3', '-I', '-c', SSH_PROOF, role, credentials['nonce']]))
            raw = strict_json(execute(args, input=None, env=ENV, timeout=30))
        reports[role] = validate_ssh(raw, role, credentials['nonce'], credentials['boot_id'], peer['runner_addresses'])
    if factory is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ProbeError('peer_browser_dependency_missing') from None
        factory = sync_playwright
    browser_started = now()
    with factory() as playwright:
        apps = browser.browser_check(request, peer, playwright, now)
    browser_duration = round(now() - browser_started, 3)
    proof = {'schema': 1, 'status': 'pass', 'request_id': request['request_id'], 'peer': peer, 'apps': apps,
             'duration_seconds': round(now() - started, 3)}
    # The browser verifier bounds the browser portion to 150 seconds. This
    # wrapper additionally proves both SSH paths under its 240-second deadline.
    browser_proof = {**proof, 'duration_seconds': browser_duration}
    browser.validate_proof(browser_proof, request)
    return {**proof, 'ssh': reports, 'browser_duration_seconds': browser_duration}


def validate_proof(proof, payload):
    payload_valid(payload)
    need(isinstance(proof, dict) and set(proof) == {'schema', 'status', 'request_id', 'peer', 'apps', 'ssh', 'duration_seconds', 'browser_duration_seconds'}
         and type(proof['duration_seconds']) in (int, float) and 0 <= proof['duration_seconds'] < 240, 'peer_proof_invalid')
    selected = {k: v for k, v in proof.items() if k not in ('ssh', 'browser_duration_seconds')}
    browser.validate_proof({**selected, 'duration_seconds': proof['browser_duration_seconds']}, payload['browser'])
    need(proof['browser_duration_seconds'] <= proof['duration_seconds'], 'peer_proof_invalid')
    need(isinstance(proof['ssh'], dict) and set(proof['ssh']) == {'host', 'workspace'}, 'peer_proof_invalid')
    for role, raw in proof['ssh'].items():
        validate_ssh(raw, role, payload['ssh']['nonce'], payload['ssh']['boot_id'], proof['peer']['runner_addresses'])
    return {**proof, 'proof_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}


def source_command(checkout):
    rows = {}
    for name, path in PATHS.items():
        source = (Path(checkout) / path).read_text()
        need(len(source) <= 40000, 'peer_source_invalid')
        rows[name] = {'source': source, 'sha256': hashlib.sha256(source.encode()).hexdigest()}
    encoded = base64.b64encode(json.dumps(rows, separators=(',', ':')).encode()).decode()
    need(len(encoded) <= 110000, 'peer_source_too_large')
    return shlex.join(['python3', '-I', '-c', LOADER, encoded])


def access_valid(access):
    need(isinstance(access, dict) and set(access) == {'host', 'port', 'username', 'host_public_key', 'private_key_ref'}, 'existing_peer_access_required')
    try:
        ip = ipaddress.ip_address(access['host'])
        need(not ip.is_loopback and not ip.is_unspecified and not ip.is_multicast, 'peer_access_invalid')
    except (ValueError, TypeError):
        raise ProbeError('peer_access_invalid') from None
    need(type(access['port']) is int and 1 <= access['port'] <= 65535
         and isinstance(access['username'], str) and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', access['username'])
         and isinstance(access['private_key_ref'], str) and re.fullmatch(r'op://[^/\s]+/[^/\s]+/(?:[^/\s]+/)?[^/\s]+', access['private_key_ref']), 'peer_access_invalid')
    public_key(access['host_public_key'])


def existing_peer_execute(access, command, payload, secret_reader, execute=invoke):
    access_valid(access)
    payload_valid(payload)
    # A selected field is retrieved once, immediately before this SSH use.
    private = key_bytes(secret_reader(access['private_key_ref']))
    host = str(ipaddress.ip_address(access['host']))
    name = host if access['port'] == 22 else '[' + host + ']:' + str(access['port'])
    with memory_file(private) as key, memory_file((name + ' ' + public_key(access['host_public_key']) + '\n').encode()) as known:
        return execute(ssh_args(host, access['port'], access['username'], key, known, command),
            input=json.dumps(payload, separators=(',', ':')).encode(), env=ENV, timeout=250)


def main():
    def stop(_signum, _frame):
        raise ProbeError('peer_deadline_exceeded')
    signal.signal(signal.SIGALRM, stop)
    signal.signal(signal.SIGTERM, stop)
    signal.alarm(240)
    try:
        payload = strict_json(sys.stdin.buffer.read(4097))
        proof = run_probe(payload)
        validate_proof(proof, payload)
        print(json.dumps(proof, sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"schema":1,"status":"fail"}')
        return 1
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    sys.exit(main())
