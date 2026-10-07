import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_local_peer as local
import showcase_peer as peer
import showcase_launch as launch
from showcase_cleanup import private_write
from showcase_policy import PolicyError, validate_inputs
from test_showcase_policy import config
from test_showcase_peer import payload


class LocalPeerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name); self.root = self.storage / ('local-peer-' + 'a' * 12)
        self.root.mkdir(mode=0o700); self.path = self.root / 'state.json'
        self.access = {'record_path': str(self.path)}
        self.addCleanup(patch.stopall); patch.object(local, 'STORAGE', self.storage).start()
        self.tool = self.root / 'python'; self.tool.write_text('fixture binary'); self.tool.chmod(0o700)
        keeper = {'pid': 101, 'start_ticks': 1000, 'command_sha256': 'a' * 64,
                  'net': 'net:[2]', 'mount': 'mnt:[2]', 'user': 'user:[2]'}
        self.state = {'schema': 1, 'kind': 'rootless_native', 'status': 'ready', 'runner_dns': 'runner.fixture.ts.net',
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'source_sha256': local.digest(local.__file__),
            'created_at': 1000, 'deadline': 15400, 'cash_reserved_usd': '0.00', 'cloud_compute_created': False,
            'processes': {name: {**keeper, 'pid': 101 + index} for index, name in enumerate(('keeper','slirp','daemon','watchdog'))},
            'node_id': 'nFixture', 'addresses': ['100.100.1.2'], 'tag': 'tag:fixture',
            'tools': {'python': {'path': str(self.tool), 'sha256': local.digest(self.tool)}}, 'home': '/home/fixture'}
        self.state['processes']['keeper']['command_sha256'] = local.command_hash(['/usr/bin/python3','-I','-c',
            local.KEEPER, str(self.root), str(self.state['deadline'])])
        self.state['processes']['watchdog']['command_sha256'] = local.command_hash([str(self.tool),'-B',
            str(Path(local.__file__).resolve()),'watch',str(self.path),str(self.state['deadline'])])
        self.native = {'BackendState': 'Running', 'Health': [], 'Self': {'Online': True,
            'DNSName': 'runner.fixture.ts.net.', 'ID': 'nFixture', 'Tags': ['tag:fixture'], 'TailscaleIPs': ['100.100.1.2']}}
        self.execute = Mock(return_value=json.dumps(self.native).encode())
        self.actual_processes = {row['pid']: copy.deepcopy(row) for row in self.state['processes'].values()}
        patch.object(local, 'process', side_effect=lambda pid: copy.deepcopy(self.actual_processes[pid])).start()
        patch.object(local.os, 'readlink', side_effect=lambda path: {'net':'net:[1]','mnt':'mnt:[1]','user':'user:[1]'}[str(path).rsplit('/',1)[-1]]).start()
        private_write(self.path, self.state)

    def checked(self):
        return local.checked(self.access, 'runner.fixture.ts.net', now=lambda: 1100, execute=self.execute)

    def test_actual_live_namespace_native_identity_and_source_are_required(self):
        self.assertEqual(self.checked()['deadline'], 15400)
        args = self.execute.call_args.args[0]
        self.assertEqual(args[:9], ['/usr/bin/nsenter','--target','101','--user','--mount','--net','--preserve-credentials','--','tailscale'])
        self.assertEqual(args[-2:], ['status','--json']); self.assertNotIn('proxy', repr(args))
        self.assertEqual(self.execute.call_args.kwargs['env'], local.ENV)

    def test_reused_pid_changed_process_and_dead_watchdog_refuse_before_native_command(self):
        for role in ('keeper','daemon','slirp','watchdog'):
            original = copy.deepcopy(self.actual_processes)
            self.actual_processes[self.state['processes'][role]['pid']]['start_ticks'] += 1
            self.execute.reset_mock()
            with self.subTest(role=role), self.assertRaisesRegex(PolicyError, 'local_peer_process_changed'): self.checked()
            self.execute.assert_not_called(); self.actual_processes = original

    def test_record_cannot_substitute_deadline_boot_scope_source_or_paid_peer(self):
        for fields in ({'deadline': float('inf')}, {'deadline': 1100}, {'deadline': 15401},
            {'status':'cleanup_verified'}, {'source_sha256':'b'*64}, {'boot_id':'other'},
            {'runner_dns':'other.fixture.ts.net'}, {'cloud_compute_created':True}, {'cash_reserved_usd':'0.01'}, {'schema':True}):
            private_write(self.path, self.state | fields); self.execute.reset_mock()
            with self.subTest(fields=fields), self.assertRaises(PolicyError): self.checked()
            self.execute.assert_not_called()

    def test_native_wrong_node_address_tag_health_and_online_are_refused(self):
        for field, value in (('ID','nOther'),('Online',False),('DNSName','other.fixture.ts.net'),
                             ('TailscaleIPs',['100.100.1.3']),('Tags',['tag:other'])):
            native = copy.deepcopy(self.native); native['Self'][field] = value
            self.execute.return_value = json.dumps(native).encode()
            with self.subTest(field=field), self.assertRaisesRegex(PolicyError,'local_peer_native_identity_changed'): self.checked()
        self.execute.return_value = json.dumps(self.native | {'Health':['unhealthy']}).encode()
        with self.assertRaises(PolicyError): self.checked()

    def test_reference_symlink_outside_storage_and_public_permissions_are_refused(self):
        for access in ({'record_path':'/tmp/state.json'}, {'record_path':str(self.path),'extra':True},
                       {'record_path':str(self.root/'../state.json')}):
            with self.assertRaises(PolicyError): local.record_path(access)
        self.root.chmod(0o755)
        with self.assertRaises(PolicyError): local.record_path(self.access)
        self.root.chmod(0o700); self.path.unlink(); self.path.symlink_to(self.tool)
        with self.assertRaises(PolicyError): local.record_path(self.access)

    def test_changed_binary_and_fake_watcher_command_refuse(self):
        self.tool.write_text('changed binary')
        with self.assertRaisesRegex(PolicyError,'local_peer_tool_changed'): self.checked()
        self.tool.write_text('fixture binary')
        self.state['processes']['watchdog']['command_sha256']='f'*64
        self.actual_processes[104]['command_sha256']='f'*64
        private_write(self.path,self.state)
        with self.assertRaisesRegex(PolicyError,'local_peer_watchdog_changed'): self.checked()

    def test_free_lifetime_does_not_consume_or_replace_cash_reservation(self):
        with patch.object(local,'checked',return_value=self.state):
            self.assertEqual(local.lifetime(self.access,'runner.fixture.ts.net',4200,lambda:1100),15400)
            with self.assertRaisesRegex(PolicyError,'peer_lifetime_insufficient'):
                local.lifetime(self.access,'runner.fixture.ts.net',14400,lambda:1100)
        attempt = Mock(); attempt.config = config(); attempt.config['runner']['local_peer'] = self.access
        with patch.object(local,'lifetime',return_value=15400) as lifetime:
            self.assertEqual(launch.check_peer_lifetime(attempt),15400)
            lifetime.assert_called_once_with(self.access,attempt.config['tailnet']['runner_dns'],attempt.config['attempt_seconds'],attempt.now)

    def test_only_one_transport_and_no_unselected_command_or_plaintext_input(self):
        value=config(); value['runner']['local_peer']=self.access
        self.assertEqual(validate_inputs(value,'aws')['status'],'inputs_complete')
        value['runner']['peer_ssh']={}
        with self.assertRaisesRegex(PolicyError,'one_peer_transport_required'):validate_inputs(value,'aws')
        for command in ('sh -c echo',peer.source_command(Path(__file__).resolve().parents[2])+'; command'):
            with self.assertRaises(PolicyError): local.execute_probe(self.access,'runner.fixture.ts.net',command,payload(),execute=self.execute)
        self.execute.assert_not_called()

    def test_checked_source_and_stdin_execute_in_owned_namespace_and_recheck_after(self):
        command=peer.source_command(Path(__file__).resolve().parents[2]); request=payload()
        def execute(args,**kwargs):
            if args[-2:]==['status','--json']: return json.dumps(self.native).encode()
            self.assertEqual(args[:8],local.prefix(self.state))
            self.assertEqual(args[8],str(self.tool)); self.assertEqual(json.loads(kwargs['input']),request)
            self.assertNotIn(request['ssh']['private_key'],repr(args))
            self.assertEqual(kwargs['env']['HOME'],'/home/fixture'); self.assertEqual(kwargs['timeout'],250)
            return b'fixture proof'
        fn=Mock(side_effect=execute)
        result=local.execute_probe(self.access,'runner.fixture.ts.net',command,request,now=lambda:1100,execute=fn)
        self.assertEqual(result,b'fixture proof');self.assertEqual(fn.call_count,3)

    def test_pidfd_prevents_signaling_a_reused_pid(self):
        row=self.state['processes']['daemon']; self.actual_processes[row['pid']]['start_ticks']+=1
        with (patch.object(os,'pidfd_open',return_value=55), patch.object(os,'close') as close,
              patch.object(signal_module(),'pidfd_send_signal') as send):
            with self.assertRaises(PolicyError): local.stop_process(row)
            send.assert_not_called();close.assert_called_once_with(55)

    def test_cleanup_refuses_same_name_with_replaced_native_node_or_key(self):
        state={'name':'as7-local-fixture','tag':'tag:fixture','tailnet':'fixture','node_id':'nFixture',
               'native_device_id':'dFixture','description':'fixture owned key','key_id':'kFixture'}
        node={'hostname':state['name'],'name':state['name']+'.fixture.ts.net','tags':[state['tag']],
              'nodeId':'nFixture','id':'dFixture'}
        with patch.object(local,'api',return_value={'devices':[node]}):
            self.assertEqual(local.devices('fake token',state),[node])
        for changes in ({'nodeId':'nOther'},{'id':'dOther'},{'tags':['tag:other']}):
            with patch.object(local,'api',return_value={'devices':[node|changes]}), self.assertRaises(PolicyError):
                local.devices('fake token',state)
        for row in ({'id':'kOther','description':state['description']},
                    {'id':'kFixture','description':'unrelated key'}):
            with patch.object(local,'api',return_value={'keys':[row]}), self.assertRaises(PolicyError):
                local.keys('fake token',state)

    def test_native_absence_query_failure_cannot_claim_cleanup(self):
        self.state.update(key_create_started=True,cleanup_key_ref='op://fixture/fixture/token')
        private_write(self.path,self.state)
        def missing(_pid): raise PolicyError('local_peer_process_missing')
        with (patch.object(local,'process',side_effect=missing), patch.object(local,'stop_process'),
              patch.object(local,'read_secret',return_value='fake token'),
              patch.object(local,'devices',side_effect=PolicyError('local_peer_api_failed'))):
            with self.assertRaises(PolicyError): local.cleanup(self.access)
        self.assertEqual(json.loads(self.path.read_text())['status'],'cleanup_started')

    def test_actual_child_reads_only_explicitly_inherited_sealed_descriptor(self):
        value=b'fixture-not-an-auth-key'; paths=[]
        with peer.memory_file(value) as memory:
            paths.append(memory); fd=int(memory.rsplit('/',1)[-1])
            code="import pathlib,hashlib,sys;print(hashlib.sha256(pathlib.Path('/proc/self/fd/'+sys.argv[1]).read_bytes()).hexdigest())"
            result=local.invoke([sys.executable,'-I','-c',code,str(fd)],timeout=10,env=local.ENV,pass_fds=(fd,))
            self.assertEqual(result.strip().decode(),hashlib.sha256(value).hexdigest())
            with self.assertRaises(peer.ProbeError):
                local.invoke([sys.executable,'-I','-c',code,str(fd)],timeout=10,env=local.ENV)
        self.assertFalse(Path(paths[0]).exists())


def signal_module():
    import signal
    return signal


if __name__=='__main__': unittest.main()
