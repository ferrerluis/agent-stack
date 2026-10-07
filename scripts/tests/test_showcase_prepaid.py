import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from showcase_policy import PolicyError, Reservations, validate_inputs, prepaid_charge
from showcase_model_spend import ModelSpend, prepare_request
from test_showcase_policy import config, quote, NOW
from test_showcase_model_spend import policy


def funded():
    c = config()
    c['models'] = {'openclaw':'openai/gpt-6-luna', 'hermes':'anthropic/claude-haiku-4-5-20251001',
                   'codex':'gpt-6-luna', 'claude':'claude-haiku-4-5-20251001'}
    c['funding'] = {'purchased_usd':'10.30','peer_reserved_usd':'1.00',
                    'prepaid_caps_usd':{'openai':'1.00','anthropic':'1.00'}}
    return c


class PrepaidTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = funded(); self.ledger = Reservations(Path(self.temp.name)/'batch', self.config)

    def test_purchases_and_peer_count_once_and_prepaid_consumption_is_not_cash(self):
        q = quote(); q['model_prepaid'] = True
        q['model_usd'] = '2.00'
        self.assertEqual(self.ledger.reserve('aws', 'as7-fixture01', q, NOW)['reserved_usd'], '1.00')
        state = json.loads((self.ledger.directory/'reservations.json').read_text())
        self.assertEqual(state['funding'], self.config['funding'])
        with self.assertRaisesRegex(PolicyError, 'prepaid_funding_required'):
            self.ledger.reserve('aws', 'as7-fixture02', quote(), NOW)

    def test_cash_boundary_funding_and_model_changes_refused(self):
        q=quote(hourly='4.00'); q['model_prepaid']=True
        with self.assertRaisesRegex(PolicyError, 'insufficient_budget'):
            self.ledger.reserve('aws','as7-fixture01',q,NOW)
        self.config['funding']['prepaid_caps_usd']['openai']='2.00'
        with self.assertRaisesRegex(PolicyError, 'funding_exceeds_approval'):
            validate_inputs(self.config,'aws')
        self.config=funded(); self.config['models']['claude']='unapproved'
        with self.assertRaisesRegex(PolicyError, 'approved_models_required'):
            validate_inputs(self.config,'aws')

    def peer_quote(self, **changes):
        return {'source':'https://aws.amazon.com/ec2/pricing/on-demand/','verified_at':NOW,
                'hourly_usd':'0.18','fixed_usd':'0.40','cleanup_usd':'0.10'} | changes

    def test_replacement_peer_counts_alongside_original_peer_and_matrix_history(self):
        peer=self.ledger.reserve_peer('as7-peer-fixture01',self.peer_quote(),14400,NOW)
        self.assertEqual(peer['reserved_usd'],'1.22')
        q=quote();q['model_prepaid']=True;q['model_usd']='2.00'
        self.ledger.reserve('aws','as7-fixture01',q,NOW)
        state=json.loads((self.ledger.directory/'reservations.json').read_text())
        self.assertEqual(str(self.ledger.committed_cash(state)),'13.52')
        self.assertEqual(state['funding']['peer_reserved_usd'],'1.00')
        self.assertEqual(len(state['attempts']),1)
        with self.assertRaisesRegex(PolicyError,'cleanup_required_before_launch'):
            self.ledger.reserve_peer('as7-peer-fixture02',self.peer_quote(),14400,NOW)

    def test_replacement_peer_cannot_reset_budget_or_duplicate_its_charge(self):
        self.ledger.reserve_peer('as7-peer-fixture01',self.peer_quote(),14400,NOW)
        with self.assertRaisesRegex(PolicyError,'peer_name_already_reserved'):
            self.ledger.reserve_peer('as7-peer-fixture01',self.peer_quote(),14400,NOW)
        self.config['budget_usd']='12.00'
        with self.assertRaisesRegex(PolicyError,'insufficient_budget'):
            self.ledger.reserve('gcp','as7-fixture01',quote('gcp')|{'model_prepaid':True},NOW)
        state=json.loads((self.ledger.directory/'reservations.json').read_text())
        self.assertEqual(len(state['peer_reservations']),1)
        self.assertEqual(state['attempts'],[])

    def test_unverified_peer_quotes_and_invalid_quota_never_reduce_cash_checks(self):
        for changes in ({'source':'https://example.test/'},{'verified_at':NOW-301},
                        {'hourly_usd':'0'},{'fixed_usd':'0.39'},{'cleanup_usd':'0.09'}):
            with self.subTest(changes=changes), self.assertRaises(PolicyError):
                self.ledger.reserve_peer('as7-peer-fixture01',self.peer_quote(**changes),14400,NOW)
        for cap in (True,0,1073741825,'536870912'):
            self.config['trial_egress_cap_bytes']=cap
            with self.subTest(cap=cap),self.assertRaisesRegex(PolicyError,'trial_egress_cap_invalid'):
                validate_inputs(self.config,'aws')
        self.config['trial_egress_cap_bytes']=536870912
        validate_inputs(self.config,'aws')

    def test_changed_peer_amount_cannot_reduce_its_committed_cost(self):
        self.ledger.reserve_peer('as7-peer-fixture01',self.peer_quote(),14400,NOW)
        path=self.ledger.directory/'reservations.json'
        state=json.loads(path.read_text());state['peer_reservations'][0]['reserved_usd']='0.01'
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(PolicyError,'ledger_unreadable'):
            self.ledger.operate(lambda state: None)

    def test_global_provider_cap_retains_uncertain_holds_and_covers_all_roles(self):
        q=quote(); q['model_prepaid']=True; q['model_usd']='2.00'
        self.ledger.reserve('aws','as7-fixture01',q,NOW)
        p=policy(self.config)
        for row in p['roles'].values():
            row['context_tokens_upper_bound']=1000000
            row['max_requests']=32
        s=ModelSpend(self.ledger,'as7-fixture01','aws','a'*40,p,now=lambda:NOW)
        s.reserve('claude','a'*64,60,500000)
        with self.assertRaisesRegex(PolicyError, 'prepaid_model_budget_exhausted'):
            s.reserve('hermes','b'*64,60,500000)
        s.reserve('codex','c'*64,60,1000)
        row=json.loads((self.ledger.directory/'reservations.json').read_text())['attempts'][0]
        self.assertEqual(len(row['model_requests']),2)
        self.assertEqual(row['model_requests'][0]['status'],'uncertain')
        self.ledger.operate(lambda state: None)

    def test_exact_models_force_low_cost_native_options(self):
        p=policy(self.config)
        for role,path in [('openclaw','/v1/chat/completions'),('codex','/v1/responses'),('hermes','/v1/messages')]:
            value={'model':p['roles'][role]['model'],'messages':[{'role':'user','content':'fixture'}]}
            if path=='/v1/responses': value={'model':value['model'],'input':'fixture','reasoning':{'effort':'high'}}
            if role=='hermes':value['thinking']={'type':'enabled','budget_tokens':10000}
            _,raw=prepare_request(p,role,path,json.dumps(value).encode());actual=json.loads(raw)
            if role=='hermes':self.assertEqual(actual['thinking'],{'type':'disabled'})
            elif role=='codex':self.assertEqual(actual['reasoning'],{'effort':'none'})
            else:self.assertEqual(actual['reasoning_effort'],'none')

    def test_cheaper_haiku_is_allowed_with_unchanged_cash_and_prepaid_limits(self):
        self.config['models'].update(hermes='anthropic/claude-haiku-5-5', claude='claude-haiku-5-5')
        validate_inputs(self.config, 'aws')
        p=policy(self.config)
        for role in ('hermes', 'claude'):
            value={'model':'claude-haiku-5-5', 'messages':[{'role':'user','content':'fixture'}],
                   'thinking':{'type':'adaptive'}, 'output_config':{'effort':'max'}, 'max_tokens':1000}
            _,raw=prepare_request(p,role,'/v1/messages',json.dumps(value).encode());actual=json.loads(raw)
            self.assertEqual(actual['thinking'], {'type':'disabled'})
            self.assertEqual(actual['output_config'], {'effort':'low'})
            self.assertEqual(actual['max_tokens'], 128)
            self.assertEqual(actual['service_tier'], 'standard_only')
        self.config['models']['claude']='claude-haiku-4-5-20251001'
        with self.assertRaisesRegex(PolicyError, 'approved_models_required'): validate_inputs(self.config, 'aws')

    def test_only_verified_usage_tightens_prepaid_bound_original_hold_is_preserved(self):
        terms=policy(self.config)['roles']['claude']
        hold={'held_usd':'0.50','status':'response_received'}
        self.assertEqual(prepaid_charge(hold,terms),0.50)
        hold['completion']={'schema':1,'provider':'anthropic','model':terms['model'],'api':'messages',
            'finish':'end_turn','kind':'text','response_id_sha256':'a'*64,'body_sha256':'b'*64,'text_sha256':'c'*64,
            'usage':{'input_tokens':100,'output_tokens':50,'cache_read_input_tokens':0,'cache_creation_input_tokens':0,'context_tokens':100}}
        self.assertEqual(str(prepaid_charge(hold,terms)),'0.000350000')
        self.assertEqual(hold['held_usd'],'0.50')
        hold['completion']['model']='different'
        with self.assertRaisesRegex(PolicyError,'invalid_model_completion_account'):
            prepaid_charge(hold,terms)
