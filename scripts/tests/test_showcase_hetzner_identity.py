import copy
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import showcase_hetzner_identity as binding
import showcase_cleanup as cleanup
from showcase_preflight import Hetzner, PreflightError
from showcase_terraform import CloudSession
from showcase_policy import PolicyError, validate_inputs
from test_showcase_policy import config, REF
from test_showcase_preflight import FakeHTTP, hcloud_routes, PREFIX

TOKEN='fixture-token-not-a-secret'


def payload(cloud):
    nonce='a'*24; name='as7-hz-map-fw-'+nonce[:12]; identifier='12345'
    native={'schema':1,'status':'cleanup_verified','account_id':cloud['account_id'],'project_id':cloud['project_id'],
        'native_firewall_id':identifier,'name':name,'token_sha256':hashlib.sha256(TOKEN.encode()).hexdigest(),
        'labels':{'as7-scope-proof':nonce},'rules':[],'applied_to':[],'remaining_ids':[],
        'server_created':False,'authentication_entry_created':False,'started_at':1000,'verified_at':1010,
        'cleanup_verified_at':1030,'cleanup_deadline':1300}
    console={'schema':1,'source':'browser_native_ui','account_id':cloud['account_id'],'project_id':cloud['project_id'],
        'native_firewall_id':identifier,'name':name,'rules':0,'applied_resources':0,'status':'Not applied',
        'observed_at':1020,'url':'https://console.hetzner.com/projects/'+cloud['project_id']+'/firewalls/'+identifier+'/rules'}
    return {'schema':1,'kind':'hetzner_project_binding','account_id':cloud['account_id'],'project_id':cloud['project_id'],
        'token_sha256':native['token_sha256'],'native':native,'console':console,'verified_at':1040}


def signed(value):
    value=copy.deepcopy(value)
    for field in ('native','console'):
        value[field+'_receipt_sha256']=hashlib.sha256(json.dumps(value[field],sort_keys=True).encode()).hexdigest()
    return json.dumps({'payload':value,'hmac_sha256':hmac.new(TOKEN.encode(),binding.encoded(value),hashlib.sha256).hexdigest()})


class BindingTest(unittest.TestCase):
    def setUp(self):
        self.config=config();self.cloud=self.config['providers']['hetzner']
        self.cloud['identity_proof_ref']=REF
        self.payload=payload(self.cloud);self.raw=signed(self.payload)

    def test_scope_proof_binds_native_creation_console_exact_identity_and_cleanup(self):
        proof=binding.verify_proof(self.raw,self.cloud,TOKEN,lambda:1100)
        self.assertEqual(proof['identity'],'verified');self.assertEqual(len(proof['proof_sha256']),64)
        self.assertNotIn(TOKEN,json.dumps(proof))

    def test_changed_token_scope_receipt_and_duplicate_or_nonfinite_json_fail(self):
        for cloud,token,raw in ((self.cloud,TOKEN+'changed',self.raw),
            (self.cloud|{'account_id':'other'},TOKEN,self.raw),(self.cloud|{'project_id':'5678'},TOKEN,self.raw),
            (self.cloud,TOKEN,self.raw.replace('Not applied','Applied')),
            (self.cloud,TOKEN,'{"payload":{},"payload":{},"hmac_sha256":"bad"}'),
            (self.cloud,TOKEN,'{"payload":NaN,"hmac_sha256":"bad"}')):
            with self.assertRaisesRegex(ValueError,'hetzner_project_binding_invalid'):
                binding.verify_proof(raw,cloud,token,lambda:1100)

    def test_even_resigned_incomplete_or_attached_or_wrong_ui_receipts_fail(self):
        changes=(lambda p:p['native'].update(status='native_creation_verified'),
            lambda p:p['native'].update(remaining_ids=['12345']),lambda p:p['native'].update(server_created=True),
            lambda p:p['native'].update(authentication_entry_created=True),lambda p:p['native'].update(rules=[{}]),
            lambda p:p['native'].update(applied_to=[{'server':1}]),lambda p:p['console'].update(native_firewall_id='99999'),
            lambda p:p['console'].update(url='https://other.test/'),lambda p:p['console'].update(source='caller_says_ready'),
            lambda p:p['console'].update(rules=True),lambda p:p['console'].update(schema=True),
            lambda p:p['console'].update(observed_at=999),lambda p:p.update(verified_at=1101),
            lambda p:p['native'].update(cleanup_verified_at=1301),lambda p:p['native'].update(cleanup_deadline=1400))
        for change in changes:
            value=copy.deepcopy(self.payload);change(value)
            with self.assertRaises(ValueError):binding.verify_proof(signed(value),self.cloud,TOKEN,lambda:1100)

    def test_missing_proof_retains_declared_only_and_invalid_proof_refuses_reads(self):
        adapter=Hetzner({k:v for k,v in self.cloud.items() if k!='identity_proof_ref'},TOKEN,Mock())
        self.assertEqual(cleanup.identity(adapter,'hetzner'),'declared_only')
        for raw in (None,'caller approved',self.raw.replace('12345','54321')):
            http=Mock();adapter=Hetzner(self.cloud,TOKEN,http,raw)
            with self.assertRaisesRegex(PreflightError,'hetzner_project_binding_invalid'):adapter.preflight(PREFIX)
            http.request.assert_not_called()

    def test_selected_reader_reads_token_and_proof_once_without_provider_env_leak(self):
        self.cloud['identity_proof_ref']='op://fixture/fixture/proof'
        reader=Mock(side_effect=lambda reference:self.raw if reference==self.cloud['identity_proof_ref'] else TOKEN)
        session=CloudSession(self.config,'hetzner',secret_reader=reader,http=FakeHTTP(hcloud_routes()))
        self.assertEqual(cleanup.identity(session.adapter,'hetzner'),'verified')
        report=session.adapter.preflight(PREFIX)
        self.assertEqual(report['identity'],'verified');self.assertNotIn('account_project_token_mapping',report['not_verified'])
        self.assertIn('live_capacity',report['not_verified']);self.assertIn('create_delete_permissions',report['not_verified'])
        self.assertEqual(reader.call_count,2);self.assertEqual(set(session.credentials),{'token'})

    def test_plaintext_readiness_flags_and_other_provider_proof_fields_fail_schema(self):
        self.assertEqual(validate_inputs(self.config,'hetzner')['status'],'inputs_complete')
        self.cloud['identity_proof_ref']='caller says ready'
        with self.assertRaises(PolicyError):validate_inputs(self.config,'hetzner')
        value=config();value['providers']['gcp']['identity_proof_ref']=REF
        with self.assertRaises(PolicyError):validate_inputs(value,'gcp')

    def test_optimized_python_cannot_disable_identity_checks(self):
        code='from showcase_hetzner_identity import verify_proof\ntry: verify_proof("{}",{},"fixture")\nexcept ValueError: pass\nelse: raise RuntimeError("verification_disabled")'
        result=subprocess.run([sys.executable,'-B','-O','-c',code],cwd=Path(binding.__file__).parent,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0)


if __name__=='__main__':unittest.main()
