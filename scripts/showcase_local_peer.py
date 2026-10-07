#!/usr/bin/env python3
"""Free second peer in owned Linux user/network/mount namespaces.

Uses a real TUN, private DNS and the unchanged SSH/browser probe. No host
daemon, routes, DNS, inbound port or cloud machine is installed. A fixed
deadline watcher removes only the recorded namespace and ephemeral node/key.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
from urllib.request import Request

from showcase_agents import invoke, strict_json
from showcase_cleanup import private_read, private_write
from showcase_policy import PolicyError, require, secret_ref
from showcase_preflight import JsonHTTP, read_secret, segment

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / '.tmp/agent-stack-tests'
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC'}
KEEPER = '''import json,os,pathlib,subprocess,sys,time
root=pathlib.Path(sys.argv[1]); deadline=float(sys.argv[2])
assert os.getuid()==0 and time.time()<deadline<=time.time()+14400
subprocess.run(['/usr/bin/mount','--make-rprivate','/'],check=True)
for source,target in [('run','/run'),('resolv.conf','/etc/resolv.conf'),('bin','/usr/local/bin')]:
    subprocess.run(['/usr/bin/mount','--bind',str(root/source),target],check=True)
(root/'keeper-ready').write_text(str(os.getpid()))
while time.time()<deadline: time.sleep(min(2,max(.01,deadline-time.time())))
'''


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_path(access):
    require(isinstance(access, dict) and set(access) == {'record_path'}
            and isinstance(access['record_path'], str), 'local_peer_reference_invalid')
    path = Path(access['record_path'])
    require(path.is_absolute() and path.parent.parent == STORAGE and path.name == 'state.json'
            and re.fullmatch(r'local-peer-[a-f0-9]{12}', path.parent.name), 'local_peer_reference_invalid')
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'local_peer_reference_invalid')
    require(path.parent.is_dir() and path.parent.stat().st_uid == os.getuid()
            and stat.S_IMODE(path.parent.stat().st_mode) == 0o700, 'local_peer_reference_invalid')
    return path


def process(pid):
    require(type(pid) is int and pid > 1, 'local_peer_process_invalid')
    base = Path('/proc') / str(pid)
    try:
        require(base.stat().st_uid == os.getuid(), 'local_peer_process_invalid')
        fields = (base / 'stat').read_text().rpartition(')')[2].split()
        require(fields[0] != 'Z', 'local_peer_process_missing')
        return {'pid': pid, 'start_ticks': int(fields[19]),
                'command_sha256': hashlib.sha256((base / 'cmdline').read_bytes()).hexdigest(),
                'net': os.readlink(base / 'ns/net'), 'user': os.readlink(base / 'ns/user'),
                'mount': os.readlink(base / 'ns/mnt')}
    except (OSError, ValueError, IndexError):
        raise PolicyError('local_peer_process_missing') from None


def live(row):
    require(isinstance(row, dict) and set(row) == {'pid', 'start_ticks', 'command_sha256', 'net', 'user', 'mount'},
            'local_peer_process_invalid')
    require(process(row['pid']) == row, 'local_peer_process_changed')


def command_hash(args):
    return hashlib.sha256(b'\0'.join(value.encode() for value in args) + b'\0').hexdigest()


def prefix(state):
    pid = str(state['processes']['keeper']['pid'])
    return ['/usr/bin/nsenter', '--target', pid, '--user', '--mount', '--net', '--preserve-credentials', '--']


def checked(access, runner_dns, now=time.time, execute=invoke):
    path = record_path(access)
    try:
        state = private_read(path)
        require(state['schema'] == 1 and type(state['schema']) is int and state['kind'] == 'rootless_native'
                and state['status'] == 'ready' and state['runner_dns'] == runner_dns
                and state['boot_id'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                and state['source_sha256'] == digest(__file__)
                and type(state['deadline']) in (int, float) and math.isfinite(state['deadline'])
                and state['created_at'] < now() < state['deadline'] <= state['created_at'] + 14400
                and state['cloud_compute_created'] is False and state['cash_reserved_usd'] == '0.00',
                'local_peer_binding_changed')
        require(set(state['processes']) == {'keeper', 'slirp', 'daemon', 'watchdog'}, 'local_peer_watchdog_required')
        for row in state['processes'].values():
            live(row)
        keeper = state['processes']['keeper']
        require(keeper['net'] != os.readlink('/proc/self/ns/net')
                and keeper['mount'] != os.readlink('/proc/self/ns/mnt')
                and keeper['user'] != os.readlink('/proc/self/ns/user'), 'local_peer_not_isolated')
        require(state['processes']['daemon']['net'] == keeper['net'], 'local_peer_binding_changed')
        expected = [state['tools']['python']['path'], '-B', str(Path(__file__).resolve()), 'watch', str(path), str(state['deadline'])]
        require(state['processes']['watchdog']['command_sha256'] == command_hash(expected), 'local_peer_watchdog_changed')
        expected = ['/usr/bin/unshare', '--user', '--map-root-user', '--net', '--mount',
                    '/usr/bin/python3', '-I', '-c', KEEPER, str(path.parent), str(state['deadline'])]
        # unshare execs Python in place, retaining the PID and start ticks.
        require(keeper['command_sha256'] == command_hash(expected[5:]), 'local_peer_namespace_changed')
        for row in state['tools'].values():
            require(digest(row['path']) == row['sha256'], 'local_peer_tool_changed')
        native = strict_json(execute(prefix(state) + ['tailscale', 'status', '--json'], env=ENV, timeout=15))
        own = native.get('Self', {})
        require(native.get('BackendState') == 'Running' and not native.get('Health') and own.get('Online') is True
                and own.get('DNSName', '').rstrip('.') == runner_dns and str(own.get('ID')) == state['node_id']
                and state['tag'] in own.get('Tags', []) and own.get('TailscaleIPs') == state['addresses'],
                'local_peer_native_identity_changed')
    except (KeyError, TypeError, ValueError, OSError):
        raise PolicyError('local_peer_binding_changed') from None
    return state


def lifetime(access, runner_dns, attempt_seconds, now=time.time):
    state = checked(access, runner_dns, now)
    require(now() + attempt_seconds < state['deadline'], 'peer_lifetime_insufficient')
    return state['deadline']


def execute_probe(access, runner_dns, command, payload, now=time.time, execute=invoke):
    # Source arrives from the controller's checked checkout, and private input
    # stays on stdin. No shell, local proxy or ambient user configuration.
    from showcase_peer import LOADER, payload_valid
    payload_valid(payload)
    words = shlex.split(command)
    require(len(words) == 5 and words[:4] == ['python3', '-I', '-c', LOADER], 'local_peer_source_invalid')
    state = checked(access, runner_dns, now, execute)
    require(now() + 250 < state['deadline'], 'peer_lifetime_insufficient')
    words[0] = state['tools']['python']['path']
    env = {**ENV, 'HOME': state['home']}
    output = execute(prefix(state) + words, input=json.dumps(payload, separators=(',', ':')).encode(), env=env, timeout=250)
    checked(access, runner_dns, now, execute)
    return output


def api(token, path, method='GET', payload=None):
    http = JsonHTTP()
    url = 'https://api.tailscale.com/api/v2/' + path
    request = Request(url, method=method, headers={'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json', 'Accept': 'application/json'},
        data=None if payload is None else json.dumps(payload).encode())
    try:
        with http.opener.open(request, timeout=20) as response:
            require(response.status in (200, 201, 204), 'local_peer_api_failed')
            raw = response.read(1048577)
            require(len(raw) <= 1048576, 'local_peer_api_failed')
            return strict_json(raw) if raw else {}
    except Exception:
        raise PolicyError('local_peer_api_failed') from None


def devices(token, state):
    value = api(token, 'tailnet/' + segment(state['tailnet']) + '/devices')
    require(isinstance(value.get('devices'), list), 'local_peer_api_failed')
    rows = [row for row in value['devices'] if row.get('hostname') == state['name']
            or row.get('name', '').split('.', 1)[0] == state['name']
            or str(row.get('nodeId')) == state.get('node_id')]
    require(len(rows) <= 1 and all(state['tag'] in row.get('tags', [])
            and row.get('name', '').split('.', 1)[0] == state['name']
            and ('node_id' not in state or str(row.get('nodeId')) == state['node_id'])
            and ('native_device_id' not in state or str(row.get('id')) == state['native_device_id']) for row in rows),
            'local_peer_ownership_changed')
    return rows


def keys(token, state):
    value = api(token, 'tailnet/' + segment(state['tailnet']) + '/keys')
    require(isinstance(value.get('keys'), list), 'local_peer_api_failed')
    rows = [row for row in value['keys'] if row.get('description') == state['description']
            or row.get('id') == state.get('key_id')]
    require(len(rows) <= 1 and all(row.get('description') == state['description']
            and ('key_id' not in state or row.get('id') == state['key_id']) for row in rows),
            'local_peer_ownership_changed')
    return rows


def stop_process(row):
    """Pin the actual process before signaling; a reused PID is refused."""
    try:
        fd = os.pidfd_open(row['pid'])
    except ProcessLookupError:
        return
    try:
        live(row)
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        require(bool(select.select([fd], [], [], 3)[0]), 'local_peer_cleanup_unverified')
    finally:
        os.close(fd)


def cleanup(access):
    path = record_path(access)
    fd = os.open(path.parent / 'cleanup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return cleanup_locked(path)
    finally:
        os.close(fd)


def cleanup_locked(path):
    state = private_read(path)
    # Cleanup must still work after implementation changes. Live acceptance
    # requires the source hash; deletion binds native process/node identities.
    require(state['kind'] == 'rootless_native'
            and state['boot_id'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'local_peer_binding_changed')
    state['status'] = 'cleanup_started'; private_write(path, state)
    # The selected namespace must still be held by a recorded process. If all
    # holders disappeared, never use an old namespace number to find victims.
    rows = state.get('processes', {})
    namespace = None; namespace_fd = None
    for role in ('keeper', 'daemon'):
        row = rows.get(role)
        if row:
            try:
                live(row)
                namespace_fd = os.open('/proc/' + str(row['pid']) + '/ns/net', os.O_RDONLY | os.O_CLOEXEC)
                require(os.readlink('/proc/self/fd/' + str(namespace_fd)) == row['net'], 'local_peer_process_changed')
                live(row); namespace = row['net']; break
            except PolicyError as error:
                if namespace_fd is not None: os.close(namespace_fd); namespace_fd = None
                require(str(error) == 'local_peer_process_missing', 'local_peer_process_changed')
    if namespace:
        try:
            # Pin the kernel namespace until all descendants are gone. Its
            # number cannot be reused while this descriptor remains open.
            require(namespace != os.readlink('/proc/self/ns/net'), 'local_peer_not_isolated')
            for _ in range(4):
                victims = []
                for item in Path('/proc').iterdir():
                    if item.name.isdigit():
                        try:
                            row = process(int(item.name))
                            if row['net'] == namespace: victims.append(row)
                        except PolicyError:
                            continue
                if not victims: break
                for row in victims:
                    try: stop_process(row)
                    except PolicyError as error:
                        require(str(error) == 'local_peer_process_missing', 'local_peer_process_changed')
            else:
                raise PolicyError('local_peer_cleanup_unverified')
        finally:
            os.close(namespace_fd)
    if 'slirp' in rows:
        try: stop_process(rows['slirp'])
        except PolicyError as error:
            require(str(error) == 'local_peer_process_missing', 'local_peer_process_changed')
    if state.get('key_create_started'):
        token = read_secret(state['cleanup_key_ref'])
        for row in devices(token, state):
            api(token, 'device/' + segment(str(row['id'])), 'DELETE')
        for row in keys(token, state):
            api(token, 'tailnet/' + segment(state['tailnet']) + '/keys/' + segment(row['id']), 'DELETE')
        require(not devices(token, state) and not keys(token, state), 'local_peer_cleanup_unverified')
    for role, row in rows.items():
        if role == 'watchdog': continue
        try:
            current = process(row['pid'])
        except PolicyError as error:
            require(str(error) == 'local_peer_process_missing' or not Path('/proc', str(row['pid'])).exists(),
                    'local_peer_cleanup_unverified')
        else:
            require(current != row, 'local_peer_cleanup_unverified')
    state.update(status='cleanup_verified', cleanup_verified_at=time.time(), remaining_devices=[], remaining_keys=[])
    private_write(path, state)
    return {'status': state['status'], 'cloud_compute_created': False, 'cash_reserved_usd': '0.00'}


def start(config, tools, lifetime_seconds=14400):
    require(type(lifetime_seconds) is int and 600 <= lifetime_seconds <= 14400, 'peer_lifetime_insufficient')
    require(set(tools) == {'slirp', 'slirp_library', 'tailscale', 'tailscaled', 'python'}, 'local_peer_tools_required')
    require(secret_ref(config['tailnet']['cleanup_key_ref']), 'secret_reference_required')
    # Explicit paths; inspect/hash binaries without installing system packages.
    tools = {name: {'path': str(Path(value).absolute()), 'sha256': digest(value)} for name, value in tools.items()}
    created = time.time(); deadline = created + lifetime_seconds
    name = 'as7-local-' + os.urandom(6).hex()
    root = STORAGE / ('local-peer-' + name.removeprefix('as7-local-'))
    root.mkdir(mode=0o700); path = root / 'state.json'
    state = {'schema': 1, 'kind': 'rootless_native', 'status': 'starting', 'name': name,
        'description': 'AgentStack TODO7 free peer ' + name, 'created_at': created, 'deadline': deadline,
        'tailnet': config['tailnet']['name'], 'tag': config['tailnet']['required_tag'],
        'cleanup_key_ref': config['tailnet']['cleanup_key_ref'], 'tools': tools,
        'source_sha256': digest(__file__), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'home': str(Path.home()), 'processes': {}, 'cloud_compute_created': False, 'cash_reserved_usd': '0.00'}
    private_write(path, state)
    for directory in ('run/tailscale', 'bin'):
        (root / directory).mkdir(mode=0o700, parents=True)
    # Some hosts point resolv.conf into /run. Prepare its private target before
    # hiding host /run, then mount DNS last so that the symlink remains valid.
    target = Path('/etc/resolv.conf').resolve()
    if target.is_relative_to('/run'):
        private_target = root / 'run' / target.relative_to('/run')
        private_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_target.touch(mode=0o600)
    (root / 'bin/tailscale').symlink_to(tools['tailscale']['path'])
    (root / 'resolv.conf').write_text('nameserver 10.0.2.3\n'); (root / 'resolv.conf').chmod(0o600)
    log = (root / 'process.log').open('ab'); (root / 'process.log').chmod(0o600)
    def spawn(role, args, env=None, fds=()):
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env=env, pass_fds=fds, start_new_session=True, umask=0o077)
        state['processes'][role] = process(child.pid); private_write(path, state)
        return child
    try:
        watcher = spawn('watchdog', [tools['python']['path'], '-B', str(Path(__file__).resolve()), 'watch', str(path), str(deadline)])
        keeper = spawn('keeper', ['/usr/bin/unshare', '--user', '--map-root-user', '--net', '--mount',
            '/usr/bin/python3', '-I', '-c', KEEPER, str(root), str(deadline)])
        limit = time.monotonic() + 12
        while not (root / 'keeper-ready').exists():
            require(keeper.poll() is None and time.monotonic() < limit, 'local_peer_namespace_failed'); time.sleep(.05)
        state['processes']['keeper'] = process(keeper.pid); private_write(path, state)
        ready_r, ready_w = os.pipe()
        try:
            env = {**ENV, 'LD_LIBRARY_PATH': str(Path(tools['slirp_library']['path']).parent)}
            slirp = spawn('slirp', [tools['slirp']['path'], '--configure', '--disable-host-loopback',
                '--ready-fd=' + str(ready_w), str(keeper.pid), 'tap0'], env, (ready_w,))
            os.close(ready_w); ready_w = None
            require(bool(select.select([ready_r], [], [], 12)[0]) and bool(os.read(ready_r, 1)), 'local_peer_network_failed')
        finally:
            os.close(ready_r)
            if ready_w is not None: os.close(ready_w)
        daemon = spawn('daemon', prefix(state) + [tools['tailscaled']['path'], '--tun=tailscale0',
            '--socket=/run/tailscale/tailscaled.sock', '--state=' + str(root / 'tailscaled.state')], ENV)
        limit = time.monotonic() + 12
        while not (root / 'run/tailscale/tailscaled.sock').exists():
            require(daemon.poll() is None and time.monotonic() < limit, 'local_peer_daemon_failed'); time.sleep(.05)
        for role, child in (('daemon', daemon), ('slirp', slirp), ('watchdog', watcher)):
            state['processes'][role] = process(child.pid)
        private_write(path, state)
        token = read_secret(state['cleanup_key_ref'])
        require(not devices(token, state) and not keys(token, state), 'local_peer_name_exists')
        state['key_create_started'] = True; private_write(path, state)
        result = api(token, 'tailnet/' + segment(state['tailnet']) + '/keys', 'POST', {
            'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': True,
                'preauthorized': True, 'tags': [state['tag']]}}},
            'expirySeconds': lifetime_seconds, 'description': state['description']})
        require(isinstance(result.get('key'), str) and result['key'].startswith('tskey-auth-') and result.get('id'),
                'local_peer_api_failed')
        state['key_id'] = result['id']; private_write(path, state)
        from showcase_peer import memory_file
        with memory_file(result['key'].encode()) as key:
            fd = int(key.rsplit('/', 1)[-1])
            # The child user namespace cannot read its parent's /proc memory
            # path. Inherit only this sealed FD and open it through self/fd.
            invoke(prefix(state) + ['tailscale', 'up', '--auth-key=file:/proc/self/fd/' + str(fd), '--hostname=' + name,
                '--accept-dns=false', '--accept-routes=false', '--shields-up=true', '--timeout=40s'],
                env=ENV, timeout=45, pass_fds=(fd,))
        del result
        # Route libc/browser DNS to the native daemon in this namespace only.
        # A bind-mounted file cannot be atomically replaced by an OS DNS
        # manager; accepting host DNS changes is unnecessary here.
        (root / 'resolv.conf').write_text('nameserver 100.100.100.100\n')
        limit = time.monotonic() + 40
        while True:
            native = strict_json(invoke(prefix(state) + ['tailscale', 'status', '--json'], env=ENV, timeout=15))
            if native.get('BackendState') == 'Running' and not native.get('Health') and native.get('Self', {}).get('Online') is True:
                break
            require(time.monotonic() < limit, 'local_peer_native_identity_changed'); time.sleep(1)
        own = native['Self']; state.update(runner_dns=own['DNSName'].rstrip('.'), node_id=str(own['ID']), addresses=own['TailscaleIPs'])
        matches = devices(token, state)
        require(len(matches) == 1 and matches[0].get('authorized') is True
                and str(matches[0].get('nodeId')) == state['node_id'], 'local_peer_native_identity_changed')
        state['native_device_id'] = str(matches[0]['id']); state['status'] = 'ready'; private_write(path, state)
        checked({'record_path': str(path)}, state['runner_dns'])
        return {'status': 'ready', 'local_peer': {'record_path': str(path)}, 'runner_dns': state['runner_dns'],
                'deadline': deadline, 'cloud_compute_created': False, 'cash_reserved_usd': '0.00'}
    except BaseException:
        try: cleanup({'record_path': str(path)})
        except Exception: pass  # Fixed watchdog independently retries cleanup.
        raise
    finally:
        log.close()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('start', 'check', 'cleanup', 'watch'))
    parser.add_argument('path'); parser.add_argument('deadline', nargs='?', type=float)
    parser.add_argument('--tools'); args = parser.parse_args()
    access = {'record_path': args.path}
    try:
        if args.action == 'start':
            print(json.dumps(start(private_read(args.path), private_read(args.tools))))
        elif args.action == 'check':
            state = private_read(record_path(access)); checked(access, state['runner_dns'])
            print(json.dumps({'status': 'ready', 'deadline': state['deadline'], 'cash_reserved_usd': '0.00'}))
        elif args.action == 'cleanup': print(json.dumps(cleanup(access)))
        else:
            require(args.deadline is not None and math.isfinite(args.deadline), 'local_peer_watchdog_changed')
            while time.time() < args.deadline:
                state = private_read(record_path(access))
                require(state['deadline'] == args.deadline, 'local_peer_watchdog_changed')
                if state['status'] == 'cleanup_verified': return 0
                if state['status'] == 'cleanup_started': break
                time.sleep(min(5, max(.01, args.deadline - time.time())))
            for _ in range(3):
                try:
                    cleanup(access); return 0
                except Exception: time.sleep(5)
            raise PolicyError('local_peer_cleanup_unverified')
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"status":"refused_or_failed"}'); return 1


if __name__ == '__main__':
    sys.exit(main())
