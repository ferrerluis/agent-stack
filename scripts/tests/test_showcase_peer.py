import base64
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_peer as peer
from test_showcase_browser import ADDRESS, RUNNER, TARGET, SECRET, BrowserFixture, request, status

BOOT = '11111111-1111-1111-1111-111111111111'
NONCE = 'c' * 32
KEY = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32).decode()
PRIVATE = '-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZS1rZXktbm90LWF1dGhvcml6ZWQ=\n-----END OPENSSH PRIVATE KEY-----\n'


def payload():
    return {'browser': request(), 'ssh': {'private_key': PRIVATE, 'host_public_key': KEY, 'workspace_public_key': KEY,
        'nonce': NONCE, 'boot_id': BOOT}}


def access():
    return {'host': '203.0.113.44', 'port': 22, 'username': 'fixture', 'host_public_key': KEY,
        'private_key_ref': 'op://fixture-vault/fixture-item/fixture-field'}


def ssh_proof(role):
    return {'schema': 1, 'status': 'pass', 'role': role, 'nonce': NONCE, 'uid': 1000,
        'user': 'admin' if role == 'host' else 'user', 'boot_id': BOOT, 'client_address': '100.100.1.2'}


class PeerTest(unittest.TestCase):
    def probe(self, mutate=None):
        calls = []
        def execute(args, timeout, input=None, env=None):
            calls.append((args, timeout, input, env))
            if args[:2] == ['tailscale', 'status']:
                return json.dumps(status()).encode()
            if args[:2] == ['tailscale', 'ping']:
                return b'pong from as7-fixture (100.100.2.3) via DERP(fixture) in 1ms'
            words = shlex.split(args[-1]); role = words[-2]
            self.assertEqual(words[:4], ['python3', '-I', '-c', peer.SSH_PROOF])
            self.assertEqual(words[-1], NONCE)
            self.assertEqual(args[args.index('-p') + 1], '22' if role == 'host' else '2222')
            self.assertIn(('admin' if role == 'host' else 'user') + '@' + ADDRESS, args)
            key = args[args.index('-i') + 1]
            known = [v.split('=', 1)[1] for v in args if v.startswith('UserKnownHostsFile=')][0]
            self.assertEqual(Path(key).read_text(), PRIVATE)
            self.assertIn(KEY, Path(known).read_text())
            self.assertEqual(env, peer.ENV)
            self.assertNotIn(PRIVATE, repr(args))
            raw = ssh_proof(role)
            if mutate:
                mutate(raw)
            return json.dumps(raw).encode()
        fixture = BrowserFixture()
        result = peer.run_probe(payload(), execute, resolve=Mock(return_value=[(2, 1, 6, '', (ADDRESS, 443))]),
            factory=lambda: contextlib.nullcontext(fixture), now=lambda: 1000)
        return result, calls

    def test_private_peer_ssh_and_real_browser_rules_share_request_and_boot(self):
        report, calls = self.probe()
        checked = peer.validate_proof(report, payload())
        self.assertEqual(len(checked['proof_sha256']), 64)
        self.assertEqual(set(report['ssh']), {'host', 'workspace'})
        self.assertEqual(len(calls), 4)
        for args, _, _, env in calls:
            self.assertEqual(env, peer.ENV)
            if args[0] == 'ssh':
                self.assertIn('StrictHostKeyChecking=yes', args)
                self.assertIn('IdentityAgent=none', args)
                self.assertIn('ProxyCommand=none', args)
                self.assertIn('ClearAllForwardings=yes', args)
                self.assertFalse(Path(args[args.index('-i') + 1]).exists())
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(PRIVATE, json.dumps(report))
        self.assertNotIn('fake-handoff', json.dumps(report))

    def test_stale_wrong_user_root_uid_boot_or_public_client_cannot_pass(self):
        for fields in ({'nonce': 'd' * 32}, {'boot_id': '22222222-2222-2222-2222-222222222222'},
            {'uid': 0}, {'user': 'other'}, {'client_address': '203.0.113.4'}, {'client_address': '100.100.4.5'},
            {'schema': True}, {'status': 'fail'}, {'extra': SECRET}):
            with self.subTest(fields=fields), self.assertRaises(peer.ProbeError):
                self.probe(lambda p: p.update(fields))

    def test_controller_rechecks_both_ssh_paths_and_actual_browser_duration(self):
        report, _ = self.probe()
        for mutate in (lambda p: p['ssh'].pop('workspace'), lambda p: p['ssh']['workspace'].update(uid=1001),
            lambda p: p['ssh']['host'].update(nonce='e' * 32), lambda p: p['ssh']['host'].update(boot_id='stale'),
            lambda p: p.update(duration_seconds=240), lambda p: p.update(browser_duration_seconds=150),
            lambda p: p.update(duration_seconds=1, browser_duration_seconds=2), lambda p: p.update(secret=SECRET),
            lambda p: p['peer'].update(runner_addresses=[ADDRESS])):
            bad = copy.deepcopy(report); mutate(bad)
            with self.assertRaises(peer.ProbeError): peer.validate_proof(bad, payload())

    def test_invalid_extra_or_oversized_private_inputs_fail_before_commands(self):
        for mutate in (lambda p: p.update(extra=SECRET), lambda p: p['ssh'].update(nonce='bad'),
            lambda p: p['ssh'].update(private_key='plaintext'), lambda p: p['ssh'].update(host_public_key='ssh-ed25519 invalid'),
            lambda p: p['browser'].update(hermes_password='x' * 1024, hermes_username='y' * 1024, openclaw_url=p['browser']['openclaw_url'] + '&padding=' + 'z' * 3000)):
            bad = payload(); mutate(bad); execute = Mock()
            with self.assertRaises(peer.ProbeError): peer.run_probe(bad, execute=execute)
            execute.assert_not_called()

    def test_memory_file_is_sealed_private_and_removed_on_exception(self):
        path = None
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with peer.memory_file(b'fake-private-data') as path:
                self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
                self.assertEqual(Path(path).read_bytes(), b'fake-private-data')
                with self.assertRaises(OSError): Path(path).write_bytes(b'changed')
                raise RuntimeError('fixture')
        self.assertFalse(Path(path).exists())

    def test_actual_openssh_can_read_generated_key_from_sealed_parent_memfd(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test-only-key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True, capture_output=True)
            with peer.memory_file(path.read_bytes()) as memory:
                result = subprocess.run(['ssh-keygen', '-y', '-f', memory], check=True, capture_output=True, timeout=10)
                self.assertEqual(peer.public_key(result.stdout.decode().strip()), peer.public_key(path.with_suffix('.pub').read_text().strip()))
                seals = fcntl.fcntl(int(memory.rsplit('/', 1)[1]), fcntl.F_GET_SEALS)
                self.assertTrue(seals & fcntl.F_SEAL_WRITE)
            self.assertFalse(Path(memory).exists())

    def test_existing_peer_credentials_one_read_no_disk_profile_or_agent(self):
        reader = Mock(return_value=PRIVATE)
        def execute(args, input, env, timeout):
            self.assertEqual(timeout, 250); self.assertEqual(env, peer.ENV)
            self.assertIn('fixture@203.0.113.44', args)
            self.assertIn('StrictHostKeyChecking=yes', args)
            self.assertIn('IdentityAgent=none', args)
            self.assertEqual(json.loads(input), payload())
            self.assertEqual(Path(args[args.index('-i') + 1]).read_text(), PRIVATE)
            self.assertNotIn(SECRET, repr(args)); self.assertNotIn(PRIVATE, repr(args))
            return b'fixed fixture response'
        self.assertEqual(peer.existing_peer_execute(access(), 'fixed public source command', payload(), reader, execute), b'fixed fixture response')
        reader.assert_called_once_with(access()['private_key_ref'])

    def test_wrong_endpoint_host_key_plaintext_ref_or_extra_field_refuses_secret(self):
        for changes in ({'host': '127.0.0.1'}, {'host': '0.0.0.0'}, {'host': 'host.test'}, {'host': '224.0.0.1'},
            {'port': True}, {'port': 0}, {'username': 'fixture;command'}, {'private_key_ref': SECRET},
            {'host_public_key': KEY + '\ninjected'}, {'host_public_key': 'ssh-ed25519 invalid'}, {'extra': SECRET}):
            bad = access(); bad.update(changes); reader = Mock(); execute = Mock()
            with self.assertRaises(peer.ProbeError): peer.existing_peer_execute(bad, 'command', payload(), reader, execute)
            reader.assert_not_called(); execute.assert_not_called()

    def test_key_or_command_failure_closes_fds_and_is_not_retried(self):
        paths = []
        def execute(args, **kwargs):
            paths.append(args[args.index('-i') + 1]); raise peer.ProbeError('probe_command_failed')
        reader = Mock(return_value=PRIVATE)
        with self.assertRaises(peer.ProbeError): peer.existing_peer_execute(access(), 'command', payload(), reader, execute)
        reader.assert_called_once(); self.assertEqual(len(paths), 1); self.assertFalse(Path(paths[0]).exists())

    def test_source_bundle_uses_exact_files_no_credential_in_command(self):
        command = peer.source_command(Path(__file__).resolve().parents[2])
        words = shlex.split(command)
        self.assertEqual(words[:4], ['python3', '-I', '-c', peer.LOADER])
        rows = json.loads(base64.b64decode(words[4]))
        self.assertEqual(set(rows), set(peer.PATHS))
        for name, row in rows.items():
            self.assertEqual(row['sha256'], hashlib.sha256(row['source'].encode()).hexdigest())
        self.assertNotIn(SECRET, command); self.assertNotIn(PRIVATE, command)

    def test_actual_loader_accepts_hashes_and_refuses_drift_before_execution(self):
        rows = {}
        for name in peer.PATHS:
            source = 'def main():\n    print(\'{"schema":1,"fixture":true}\')\n    return 0\n'
            rows[name] = {'source': source, 'sha256': hashlib.sha256(source.encode()).hexdigest()}
        def run(value):
            encoded = base64.b64encode(json.dumps(value).encode()).decode()
            return subprocess.run(['python3', '-I', '-c', peer.LOADER, encoded], capture_output=True, timeout=10)
        result = run(rows); self.assertEqual(result.returncode, 0); self.assertTrue(json.loads(result.stdout)['fixture'])
        rows['showcase_browser']['source'] += 'raise RuntimeError("' + SECRET + '")\n'
        result = run(rows); self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SECRET.encode(), result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {'schema': 1, 'status': 'fail'})

    def test_public_key_wire_type_and_length_not_just_a_regex(self):
        self.assertEqual(peer.public_key(KEY + ' comment'), KEY)
        for value in ('ssh-rsa abc', 'ssh-ed25519 abc', 'ssh-ed25519 ' + base64.b64encode(b'x' * 51).decode(), KEY + '\n'):
            with self.assertRaises(peer.ProbeError): peer.public_key(value)

    def test_main_never_emits_private_error_or_url_and_disarms_alarm(self):
        output = io.StringIO(); stdin = Mock(buffer=io.BytesIO(json.dumps(payload()).encode()))
        with patch.object(sys, 'stdin', stdin), contextlib.redirect_stdout(output), patch.object(peer, 'run_probe', side_effect=RuntimeError(SECRET)), patch.object(peer.signal, 'signal'), patch.object(peer.signal, 'alarm') as alarm:
            self.assertEqual(peer.main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {'schema': 1, 'status': 'fail', 'diagnostic': {
            'phase': 'request', 'reason': 'unexpected_error', 'exit_code': None}})
        self.assertEqual(alarm.call_args_list[-1].args, (0,))

    def test_actual_vm_collector_selects_credentials_and_refuses_replaced_container(self):
        for replaced in (False, True):
            counts = {}; calls = []; printed = io.StringIO()
            def run(args, **kwargs):
                args = list(args)
                calls.append(args)
                if args[1:4] == ['compose', '-f', '/opt/agent-stack/docker-compose.yml']:
                    role = args[-1]; counts[role] = counts.get(role, 0) + 1
                    value = 'b' if replaced and role == 'openclaw' and counts[role] > 1 else 'a'
                    return Mock(stdout=(value * 64).encode())
                if args[1] == 'inspect':
                    return Mock(stdout=b'{"Running":true,"Restarting":false}')
                if args[1] == 'exec' and 'dashboard' in args:
                    return Mock(stdout=b'{"browserUrl":"http://127.0.0.1:18789/#bootstrapToken=fake-handoff","ignored":"fake-extra"}')
                if args[1] == 'exec' and '/usr/bin/cat' in args:
                    return Mock(stdout=KEY.encode())
                return Mock(stdout=json.dumps({'HERMES_DASHBOARD_BASIC_AUTH_USERNAME': 'fixture',
                    'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD': SECRET}).encode())
            def path(value):
                if value == '/etc/ssh/ssh_host_ed25519_key.pub': return Mock(read_text=Mock(return_value=KEY))
                if value == '/proc/sys/kernel/random/boot_id': return Mock(read_text=Mock(return_value=BOOT))
                raise AssertionError('unexpected VM path')
            with patch('subprocess.run', side_effect=run), patch('pathlib.Path', side_effect=path), patch('os.geteuid', return_value=0), \
                patch.object(signal, 'signal'), patch.object(signal, 'alarm'), contextlib.redirect_stdout(printed), self.assertRaises(SystemExit) as stopped:
                exec(compile(peer.VM_SOURCE, '<vm-collector-fixture>', 'exec'), {'__name__': 'fixture'})
            result = json.loads(printed.getvalue())
            self.assertEqual(stopped.exception.code, 1 if replaced else 0)
            self.assertEqual(sum('dashboard' in args for args in calls), 1)
            if replaced:
                self.assertEqual(result, {'schema': 1, 'status': 'fail', 'diagnostic': {
                    'phase': 'container_recheck', 'reason': 'collector_invalid', 'exit_code': None}})
                self.assertNotIn(SECRET, printed.getvalue())
            else:
                self.assertEqual(result['hermes_password'], SECRET)
                self.assertEqual(result['boot_id'], BOOT)
                self.assertNotIn('ignored', result)

    def test_vm_failure_identifies_the_command_without_emitting_its_private_output(self):
        printed = io.StringIO()
        def run(args, **kwargs):
            if 'dashboard' in args:
                raise subprocess.CalledProcessError(7, args, output=SECRET.encode(), stderr=PRIVATE.encode())
            if args[1] == 'inspect': return Mock(stdout=b'{"Running":true,"Restarting":false}')
            return Mock(stdout=b'a' * 64)
        with (patch('subprocess.run', side_effect=run), patch('os.geteuid', return_value=0),
              patch.object(signal, 'signal'), patch.object(signal, 'alarm'),
              contextlib.redirect_stdout(printed), self.assertRaises(SystemExit) as stopped):
            exec(compile(peer.VM_SOURCE, '<vm-collector-fixture>', 'exec'), {})
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(peer.failure_details(printed.getvalue()), {
            'phase': 'dashboard_handoff', 'reason': 'child_exit', 'exit_code': 7})
        self.assertNotIn(SECRET, printed.getvalue()); self.assertNotIn(PRIVATE, printed.getvalue())

    def test_peer_failure_and_parser_preserve_only_fixed_diagnostics(self):
        printed = io.StringIO()
        def fail(value, diagnostic):
            diagnostic['phase'] = 'workspace_ssh'
            raise RuntimeError(SECRET + PRIVATE)
        with (patch.object(peer, 'run_probe', side_effect=fail),
              patch.object(sys, 'stdin', Mock(buffer=io.BytesIO(json.dumps(payload()).encode()))),
              patch.object(signal, 'signal'), patch.object(signal, 'alarm'), contextlib.redirect_stdout(printed)):
            self.assertEqual(peer.main(), 1)
        value = json.loads(printed.getvalue())
        self.assertEqual(peer.failure_details(printed.getvalue()), {
            'phase': 'workspace_ssh', 'reason': 'unexpected_error', 'exit_code': None})
        self.assertNotIn(SECRET, printed.getvalue()); self.assertNotIn(PRIVATE, printed.getvalue())
        for change in ({'schema': True}, {'status': 'pass'}, {'extra': SECRET},
                       {'diagnostic': value['diagnostic'] | {'reason': SECRET}},
                       {'diagnostic': value['diagnostic'] | {'phase': PRIVATE}},
                       {'diagnostic': value['diagnostic'] | {'exit_code': True}},
                       {'diagnostic': value['diagnostic'] | {'extra': SECRET}}):
            self.assertIsNone(peer.failure_details(json.dumps(value | change)))


if __name__ == '__main__':
    unittest.main()
