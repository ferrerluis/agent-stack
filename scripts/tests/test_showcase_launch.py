import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import showcase_launch as launch
import showcase_network_prices as network
from showcase_policy import PolicyError
from showcase_cleanup import private_write
from showcase_policy import Reservations
from test_showcase_prepaid import funded


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.run=Path(self.temp.name)
        self.a=SimpleNamespace(run=self.run,checkout=self.run,provider='aws',prefix='as7-fixture01',
            prepared={'source_revision':'a'*40},record=lambda:{'plan_sha256':'b'*64},now=lambda:1000,
            config={'attempt_seconds':10800})

    def test_quote_uses_actual_catalog_rate_and_counts_traffic_adjustments_cleanup(self):
        r={'status':'resource_unit_prices_measured','provider':'aws','source_revision':'a'*40,
           'plan_sha256':'b'*64,'run_id':'as7-fixture01','currency':'USD',
           'vm_cpu_disks_ipv4_hourly_usd_upper_bound':'0.20'}
        with patch.object(network,'measure_network',return_value={'usd_per_gib_upper_bound':'0.12'}):
            q=launch.complete_quote(self.a,r)
        self.assertEqual(Decimal(q['hourly_usd']),Decimal('0.40'))
        self.assertEqual(Decimal(q['fixed_usd']),Decimal('0.96'))
        self.assertEqual(q['cleanup_usd'],'0.40');self.assertTrue(q['model_prepaid'])
        for changes in [{'plan_sha256':'c'*64},{'currency':'EUR'},{'run_id':'as7-other001'}]:
            with self.assertRaises(PolicyError):launch.complete_quote(self.a,r|changes)

    def test_guest_lifetime_and_quota_cannot_be_extended_or_omitted(self):
        settings={'showcase_trial_limits':{'deadline_utc':'1970-01-01 02:46:40 UTC','egress_cap_bytes':1073741824}}
        private_write(self.run/'terraform.tfvars.json',settings)
        self.assertEqual(launch.trial(self.a),10000)
        for value in [None,{'deadline_utc':'1970-01-02 00:00:00 UTC','egress_cap_bytes':1073741824},
                      {'deadline_utc':'1970-01-01 02:46:40 UTC','egress_cap_bytes':0}]:
            private_write(self.run/'terraform.tfvars.json',{'showcase_trial_limits':value})
            with self.assertRaises(PolicyError):launch.trial(self.a)

    def test_lower_egress_quota_prices_two_boots_and_refuses_an_unmatched_guest(self):
        r={'status':'resource_unit_prices_measured','provider':'aws','source_revision':'a'*40,
           'plan_sha256':'b'*64,'run_id':'as7-fixture01','currency':'USD',
           'vm_cpu_disks_ipv4_hourly_usd_upper_bound':'0.20'}
        for cap, fixed in ((134217728, '0.23'), (268435456, '0.46'), (536870912, '0.92')):
            with self.subTest(cap=cap):
                self.a.config['trial_egress_cap_bytes'] = cap
                with patch.object(network,'measure_network',return_value={'usd_per_gib_upper_bound':'0.23'}):
                    q=launch.complete_quote(self.a,r)
                self.assertEqual(Decimal(q['fixed_usd']),Decimal(fixed))
                settings={'showcase_trial_limits':{'deadline_utc':'1970-01-01 02:46:40 UTC','egress_cap_bytes':cap}}
                private_write(self.run/'terraform.tfvars.json',settings)
                self.assertEqual(launch.trial(self.a),10000)
                settings['showcase_trial_limits']['egress_cap_bytes']=1073741824
                private_write(self.run/'terraform.tfvars.json',settings)
                with self.assertRaisesRegex(PolicyError,'trial_cost_controls_required'):launch.trial(self.a)

    def test_native_network_catalog_tiers_mixed_units_and_missing_prices(self):
        row={'product':{'attributes':{'fromLocation':'US East (N. Virginia)','toLocation':'External','transferType':'AWS Outbound'}},
             'terms':{'OnDemand':{'term':{'priceDimensions':{'one':{'unit':'GB','pricePerUnit':{'USD':'0.09'}},
                 'two':{'unit':'GB','pricePerUnit':{'USD':'0.05'}}}}}}}
        self.a.session=SimpleNamespace(credentials={})
        with patch('showcase_aws_prices.AWSPriceQuery') as query:
            query.return_value.products.return_value=[row]
            self.assertEqual(Decimal(network.measure_network(self.a)['usd_per_gib_upper_bound']),Decimal('0.09663676416'))
            row['terms']['OnDemand']['term']['priceDimensions']['one']['unit']='Hrs'
            with self.assertRaisesRegex(PolicyError,'network_price_unit_unsupported'):network.measure_network(self.a)
            query.return_value.products.return_value=[]
            with self.assertRaisesRegex(PolicyError,'network_price_missing'):network.measure_network(self.a)

    def test_small_transfer_bounds_keep_real_minimums_and_cleanup_without_flat_fee(self):
        self.a.config['trial_egress_cap_bytes'] = 536870912
        base={'status':'resource_unit_prices_measured','source_revision':'a'*40,
              'plan_sha256':'b'*64,'run_id':'as7-fixture01','currency':'USD'}
        cases=(('aws', {'vm_cpu_disks_ipv4_hourly_usd_upper_bound':'0.20'}, '0.04', '0.40'),
               ('gcp', {'vm_disks_ipv4_hourly_usd_upper_bound':'0.20'}, '0.04', '0.40'),
               ('digitalocean', {'vm_and_disk_hourly_usd':'0.20','outbound_gib_usd':'0.01','vm_minimum_usd':'0.03'}, '0.10', '0.40'),
               ('hetzner', {'vm_disk_ips_hourly_upper_bound':'0.20','outbound_tb_upper_bound':'1.20'}, '0.0051539607552', '0.80'))
        for provider, units, fixed, hourly in cases:
            with self.subTest(provider=provider), patch.object(network,'measure_network',return_value={'usd_per_gib_upper_bound':'0.01'}):
                self.a.provider=provider
                quote=launch.complete_quote(self.a,base|units|{'provider':provider})
                self.assertEqual(Decimal(quote['fixed_usd']),Decimal(fixed))
                self.assertEqual(Decimal(quote['hourly_usd']),Decimal(hourly))
                self.assertEqual(Decimal(quote['cleanup_usd']),Decimal(hourly))
                self.assertTrue(quote['model_prepaid'])

    def test_cleanup_hold_covers_an_extra_margin_hour_rounds_up_and_keeps_a_floor(self):
        base={'status':'resource_unit_prices_measured','provider':'aws','source_revision':'a'*40,
              'plan_sha256':'b'*64,'run_id':'as7-fixture01','currency':'USD'}
        for unit, expected in (('0.00333','0.10'),('0.2001','0.41'),('2.345','4.69')):
            with self.subTest(unit=unit), patch.object(network,'measure_network',return_value={'usd_per_gib_upper_bound':'0.01'}):
                quote=launch.complete_quote(self.a,base|{'vm_cpu_disks_ipv4_hourly_usd_upper_bound':unit})
                self.assertEqual(quote['cleanup_usd'],expected)
                self.assertGreaterEqual(Decimal(quote['cleanup_usd']),Decimal(quote['hourly_usd']))

    def test_replacement_peer_requires_its_original_cash_and_deadline_binding(self):
        self.a.config=funded();self.a.config['attempt_seconds']=10800
        batch=self.run/'batch';self.a.run=batch/'attempts/as7-fixture01'
        quote={'source':'https://aws.amazon.com/ec2/pricing/on-demand/','verified_at':1000,
               'hourly_usd':'0.18','fixed_usd':'0.40','cleanup_usd':'0.10'}
        reserved=Reservations(batch,self.a.config).reserve_peer('as7-peer-fixture01',quote,14400,1000)
        auth=self.run/'peer/auth';auth.mkdir(mode=0o700,parents=True)
        handoff=auth.parent/'handoff';handoff.mkdir(mode=0o700)
        peer={'name':reserved['name'],'reserve_usd':reserved['reserved_usd'],'compute_created':True,
              'deadline_utc':datetime.fromtimestamp(reserved['deadline'],timezone.utc).isoformat()}
        path=handoff/'browser-peer-aws.json'
        with patch('showcase_credentials.AUTH_ROOT',auth):
            private_write(path,peer)
            self.assertEqual(launch.check_peer_lifetime(self.a),15400)
            for changes in ({'name':'as7-peer-unknown01'},{'reserve_usd':'0.01'},
                            {'deadline_utc':datetime.fromtimestamp(15401,timezone.utc).isoformat()}):
                private_write(path,peer|changes)
                with self.subTest(changes=changes),self.assertRaisesRegex(PolicyError,'peer_funding_unverified'):
                    launch.check_peer_lifetime(self.a)
            private_write(path,peer|{'compute_created':False})
            with self.assertRaisesRegex(PolicyError,'peer_lifetime_insufficient'):launch.check_peer_lifetime(self.a)
            private_write(path,peer);self.a.now=lambda:5000
            with self.assertRaisesRegex(PolicyError,'peer_lifetime_insufficient'):launch.check_peer_lifetime(self.a)
