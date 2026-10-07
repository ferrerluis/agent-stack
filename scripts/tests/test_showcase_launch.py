import copy
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
        self.assertEqual(q['cleanup_usd'],'0.50');self.assertTrue(q['model_prepaid'])
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
