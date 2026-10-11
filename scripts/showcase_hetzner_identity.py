"""Validate the selected native/console project-binding receipt; no API writes."""
import hashlib
import hmac
import json
import math
import re
import time


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def need(condition):
    if not condition:
        raise ValueError('hetzner_project_binding_invalid')


def verify_proof(raw, cloud, token, now=time.time):
    """Bind a signed private receipt to the actual project-scoped token.

    Records one free unattached firewall's native creation, exact ID/name in
    the signed-in console, and verified deletion. Tokens cannot change their
    project scope. Changing a token, scope or receipt invalidates the binding.
    """
    try:
        need(isinstance(raw, str) and len(raw.encode()) <= 32768)
        def pairs(rows):
            result = {}
            for key, value in rows:
                need(key not in result); result[key] = value
            return result
        proof = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        need(isinstance(proof, dict) and set(proof) == {'payload', 'hmac_sha256'})
        payload = proof['payload']
        need(hmac.compare_digest(proof['hmac_sha256'], hmac.new(token.encode(), encoded(payload), hashlib.sha256).hexdigest()))
        need(set(payload) == {'schema','kind','account_id','project_id','token_sha256','native','console',
                             'native_receipt_sha256','console_receipt_sha256','verified_at'})
        need(type(payload['schema']) is int and payload['schema'] == 1 and payload['kind'] == 'hetzner_project_binding')
        need(payload['account_id'] == cloud['account_id'] and payload['project_id'] == cloud['project_id'])
        need(payload['token_sha256'] == hashlib.sha256(token.encode()).hexdigest())
        native, console = payload['native'], payload['console']
        for value, key in ((native,'native_receipt_sha256'),(console,'console_receipt_sha256')):
            need(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() == payload[key])
        need(type(native['schema']) is int and native['schema'] == 1 and type(console['schema']) is int and console['schema'] == 1)
        need(native['status'] == 'cleanup_verified' and native['remaining_ids'] == [])
        need(native['token_sha256'] == payload['token_sha256'])
        need(native['server_created'] is False and native['authentication_entry_created'] is False)
        need(native['rules'] == [] and native['applied_to'] == [])
        need(set(native['labels']) == {'as7-scope-proof'})
        nonce = native['labels']['as7-scope-proof']
        need(re.fullmatch(r'[a-f0-9]{24}',nonce) and native['name'] == 'as7-hz-map-fw-' + nonce[:12])
        need(re.fullmatch(r'[1-9][0-9]{0,19}',native['native_firewall_id']))
        for key in ('account_id','project_id','native_firewall_id','name'):
            need(console[key] == native[key])
        need(native['account_id'] == payload['account_id'] and native['project_id'] == payload['project_id'])
        need(type(console['rules']) is int and console['rules'] == 0)
        need(type(console['applied_resources']) is int and console['applied_resources'] == 0)
        need(console['status'] == 'Not applied' and console['source'] == 'browser_native_ui')
        need(console['url'] == ('https://console.hetzner.com/projects/' + cloud['project_id']
            + '/firewalls/' + native['native_firewall_id'] + '/rules'))
        times = [native['started_at'],native['verified_at'],console['observed_at'],native['cleanup_verified_at'],payload['verified_at']]
        need(all(type(value) in (int,float) and math.isfinite(value) for value in times))
        need(0 < times[0] <= times[1] <= times[2] <= times[3] <= times[4] <= now())
        need(native['cleanup_deadline'] == native['started_at'] + 300 and times[3] <= native['cleanup_deadline'])
        return {'identity':'verified','method':'native_console_firewall','account_id':cloud['account_id'],
                'project_id':cloud['project_id'],'proof_sha256':hashlib.sha256(raw.encode()).hexdigest()}
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError):
        raise ValueError('hetzner_project_binding_invalid') from None
