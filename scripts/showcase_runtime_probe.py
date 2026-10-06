#!/usr/bin/env python3
"""Checked, nonbillable runtime route probes. No model-generation endpoint.

Hermes resolves its configured native provider without a model request. Both
containers independently check UID/config bytes and an authenticated nonce.
OpenClaw's active gateway revision is checked by the outer VM lifecycle probe.
"""
import hashlib
from http.client import HTTPConnection
import ipaddress
import json
import os
from pathlib import Path
import re
import sys


def need(value):
    if not value: raise ValueError("runtime_container_route_unverified")


def decoded(raw, limit=4096):
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result); result[key] = value
        return result
    def constant(_): raise ValueError("runtime_container_route_unverified")
    need(isinstance(raw, bytes) and 0 < len(raw) <= limit)
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    need(isinstance(result, dict))
    return result


def route_valid(route):
    need(isinstance(route, dict) and set(route) == {"role", "provider", "model", "base_url", "credential", "bridge_ipv4", "context_tokens", "output_tokens"})
    need(all(isinstance(route[k], str) for k in ("role", "provider", "model", "base_url", "credential", "bridge_ipv4")))
    need(route["role"] in ("openclaw", "hermes") and route["provider"] in ("openai", "anthropic")
         and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,179}", route["model"])
         and re.fullmatch(r"[a-f0-9]{64}", route["credential"]))
    address = ipaddress.ip_address(route["bridge_ipv4"])
    need(address.version == 4 and any(address in ipaddress.ip_network(net) for net in
         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
    match = re.fullmatch(r"http://" + re.escape(str(address)) + r":([1-9][0-9]{0,4})/" + route["role"], route["base_url"])
    need(match and int(match[1]) <= 65535)
    need(type(route["context_tokens"]) is int and 4096 <= route["context_tokens"] <= 10000000
         and type(route["output_tokens"]) is int and 16 <= route["output_tokens"] <= 4096)
    return int(match[1])


def native_hermes(config, route, resolver):
    """Check the real resolver's route, credential and wire, not config echoes."""
    route_valid(route); need(route["role"] == "hermes" and isinstance(config, dict))
    base = route["base_url"] + ("/v1" if route["provider"] == "openai" else "")
    mode = "chat_completions" if route["provider"] == "openai" else "anthropic_messages"
    model = config.get("model"); need(isinstance(model, dict))
    need(model == {"provider": "as7-budget", "default": route["model"], "base_url": base,
                   "api_key": route["credential"], "api_mode": mode})
    need(config.get("providers") == {"as7-budget": {"api": base, "api_key": route["credential"],
         "transport": "openai_chat" if route["provider"] == "openai" else "anthropic_messages",
         "discover_models": False, "models": [route["model"]]}} and config.get("fallback_providers") == []
         and not any(config.get(k) for k in ("custom_providers", "auxiliary", "model_routes", "model_aliases", "secrets")))
    actual = resolver(requested="as7-budget", target_model=route["model"],
                      explicit_api_key=route["credential"], explicit_base_url=base)
    need(isinstance(actual, dict) and actual.get("provider") == "custom" and actual.get("model") == route["model"]
         and actual.get("requested_provider") == "as7-budget" and actual.get("source") == "custom_provider:as7-budget"
         and actual.get("api_mode") == mode and actual.get("base_url") == base
         and actual.get("api_key") == route["credential"]
         and not any(actual.get(k) for k in ("credential_pool", "extra_headers", "request_overrides", "fallback")))
    return {"kind": "hermes_native_resolver", "provider": "custom", "requested_provider": "as7-budget",
            "model": route["model"], "api_mode": mode, "base_url_sha256": hashlib.sha256(base.encode()).hexdigest()}


def native_openclaw(snapshot, route):
    route_valid(route); need(route["role"] == "openclaw" and isinstance(snapshot, dict))
    need(snapshot.get("valid") is True and not any(snapshot.get(k) for k in ("issues", "warnings", "legacyIssues")))
    revision = snapshot.get("configRevisionHash")
    need(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{64}", revision)
         and snapshot.get("appliedConfigHash") == revision)
    config = snapshot.get("config"); need(isinstance(config, dict))
    models = config.get("models"); need(isinstance(models, dict) and models.get("mode") == "replace")
    providers = models.get("providers"); need(isinstance(providers, dict) and set(providers) == {route["provider"]})
    actual = providers[route["provider"]]
    base = route["base_url"] + ("/v1" if route["provider"] == "openai" else "")
    api = "openai-completions" if route["provider"] == "openai" else "anthropic-messages"
    need(isinstance(actual, dict) and actual.get("baseUrl") == base and actual.get("api") == api
         and actual.get("auth") == "api-key" and not any(actual.get(k) for k in ("request", "headers")))
    rows = actual.get("models")
    need(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("id") == route["model"]
         and rows[0].get("input") == ["text"] and rows[0].get("contextWindow") == route["context_tokens"]
         and rows[0].get("maxTokens") == route["output_tokens"])
    agents = config.get("agents"); need(isinstance(agents, dict) and not agents.get("list"))
    defaults = agents.get("defaults"); reference = route["provider"] + "/" + route["model"]
    need(isinstance(defaults, dict) and defaults.get("model") == {"primary": reference, "fallbacks": []}
         and defaults.get("models") == {reference: {}}
         and not any(defaults.get(k) for k in ("agentRuntime", "cliBackends", "compaction", "subagents")))
    return {"kind": "openclaw_applied_gateway_config", "provider": route["provider"], "model": route["model"],
            "api": api, "base_url_sha256": hashlib.sha256(base.encode()).hexdigest(), "applied_config_hash": revision}


def native_proof_valid(route, report):
    route_valid(route)
    base = route["base_url"] + ("/v1" if route["provider"] == "openai" else "")
    if route["role"] == "hermes":
        expected = {"kind": "hermes_native_resolver", "provider": "custom", "requested_provider": "as7-budget",
                    "model": route["model"], "api_mode": "chat_completions" if route["provider"] == "openai" else "anthropic_messages",
                    "base_url_sha256": hashlib.sha256(base.encode()).hexdigest()}
        need(report == expected)
    else:
        expected = {"kind": "openclaw_applied_gateway_config", "provider": route["provider"], "model": route["model"],
                    "api": "openai-completions" if route["provider"] == "openai" else "anthropic-messages",
                    "base_url_sha256": hashlib.sha256(base.encode()).hexdigest()}
        need(isinstance(report, dict) and set(report) == set(expected) | {"applied_config_hash"}
             and all(report.get(k) == v for k, v in expected.items())
             and isinstance(report["applied_config_hash"], str) and re.fullmatch(r"[a-f0-9]{64}", report["applied_config_hash"]))


def hermes_probe(packet, resolver=None):
    need(isinstance(packet, dict) and set(packet) == {"action", "route", "config_sha256", "environment_sha256", "source_revision", "nonce"})
    port = route_valid(packet["route"]); route = packet["route"]
    need(route["role"] == "hermes" and os.geteuid() == 10000 and packet["action"] in ("native", "transport", "readiness")
         and isinstance(packet["config_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", packet["config_sha256"])
         and isinstance(packet["source_revision"], str) and re.fullmatch(r"[a-f0-9]{40}", packet["source_revision"])
         and isinstance(packet["environment_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", packet["environment_sha256"])
         and isinstance(packet["nonce"], str) and (packet["nonce"] == "" if packet["action"] in ("native", "readiness") else re.fullmatch(r"[a-f0-9]{64}", packet["nonce"])))
    path = Path("/opt/data/config.yaml")
    need(not any(parent.is_symlink() for parent in (path, *path.parents)))
    info = path.stat(); need(path.is_file() and info.st_uid == 10000 and info.st_gid == 10000 and info.st_nlink == 1
                            and info.st_mode & 0o777 == 0o600 and 0 < info.st_size <= 262144)
    raw = path.read_bytes(); need(hashlib.sha256(raw).hexdigest() == packet["config_sha256"])
    config = decoded(raw, 262144); need(not config.get("secrets"))
    env_path = Path("/opt/data/.env")
    need(not any(parent.is_symlink() for parent in (env_path, *env_path.parents)))
    env_info = env_path.stat(); need(env_path.is_file() and env_info.st_uid == 10000 and env_info.st_gid == 10000
        and env_info.st_nlink == 1 and env_info.st_mode & 0o777 == 0o600 and 0 < env_info.st_size <= 262144)
    env_raw = env_path.read_bytes(); need(hashlib.sha256(env_raw).hexdigest() == packet["environment_sha256"])
    from showcase_runtime_environment import selected_environment
    selected_environment(env_raw, route)
    if resolver is None:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        resolver = resolve_runtime_provider
    native = native_hermes(config, route, resolver)
    readiness_sha256 = None
    if packet["action"] == "readiness":
        from showcase_runtime_environment import assignments
        values, _ = assignments(env_raw)
        key = values.get("API_SERVER_KEY")
        need(isinstance(key, str) and 0 < len(key) <= 4096 and not any(c.isspace() for c in key) and HEALTH_MODULE is not None)
        # Fixed numeric origin/path, no inherited proxy, redirects or retry.
        # The retained application key never leaves this short-lived process.
        connection = HTTPConnection("127.0.0.1", 8642, timeout=5)
        try:
            connection.request("GET", "/health/detailed", headers={"Authorization": "Bearer " + key})
            response = connection.getresponse()
            need(response.status == 200 and response.getheader("Content-Type").split(";", 1)[0].strip() == "application/json")
            raw = response.read(65537); need(0 < len(raw) <= 65536)
            need(HEALTH_MODULE.healthy(decoded(raw, 65536)))
            readiness_sha256 = hashlib.sha256(raw).hexdigest()
        finally: connection.close()
    if packet["action"] == "transport":
        connection = HTTPConnection(route["bridge_ipv4"], port, timeout=3)
        try:
            connection.request("POST", "/hermes/_transport", json.dumps({"nonce": packet["nonce"]}, separators=(",", ":")),
                               {"Authorization": "Bearer " + route["credential"], "Content-Type": "application/json"})
            response = connection.getresponse()
            need(response.status == 200 and response.getheader("Content-Type") == "application/json")
            raw = response.read(1025)
            need(raw == json.dumps({"schema": 1, "role": "hermes", "nonce": packet["nonce"],
                 "source_revision": packet["source_revision"]}, separators=(",", ":")).encode())
        finally: connection.close()
    need(hashlib.sha256(path.read_bytes()).hexdigest() == packet["config_sha256"])
    need(hashlib.sha256(env_path.read_bytes()).hexdigest() == packet["environment_sha256"])
    result = {"schema": 1, "status": {"native": "runtime_native_route_verified", "transport": "runtime_container_route_verified", "readiness": "runtime_readiness_verified"}[packet["action"]],
            "role": "hermes", "provider": route["provider"], "uid": 10000, "config_sha256": packet["config_sha256"],
            "environment_sha256": packet["environment_sha256"],
            "source_revision": packet["source_revision"], "nonce": packet["nonce"], "native_route": native}
    if readiness_sha256 is not None: result["readiness_sha256"] = readiness_sha256
    return result


HEALTH_MODULE = None  # The checked source loader supplies the repo's parser.


NODE_SOURCE = r'''const fs=require('node:fs'),crypto=require('node:crypto'),http=require('node:http');
const need=(v)=>{if(!v)throw Error('runtime_container_route_unverified')};
const hash=(v)=>crypto.createHash('sha256').update(v).digest('hex');
let raw='';process.stdin.setEncoding('utf8');
process.stdin.on('data',(s)=>{raw+=s;if(Buffer.byteLength(raw)>4096){process.stdin.destroy();fail()}});
function fail(){if(process.exitCode)return;process.exitCode=1;console.log(JSON.stringify({schema:1,status:'runtime_container_route_unverified'}))}
process.stdin.on('end',async()=>{try{
 const p=JSON.parse(raw);need(JSON.stringify(p)===raw&&Object.keys(p).sort().join(',')==='action,config_sha256,nonce,route,source_revision');
 const r=p.route;need(r&&Object.keys(r).sort().join(',')==='base_url,bridge_ipv4,context_tokens,credential,model,output_tokens,provider,role');
 need(process.getuid()===1000&&p.action==='transport'&&r.role==='openclaw'&&['openai','anthropic'].includes(r.provider));
 need(['base_url','bridge_ipv4','credential','model','provider','role'].every(k=>typeof r[k]==='string'));
 need(/^[a-f0-9]{64}$/.test(r.credential)&&/^[a-f0-9]{64}$/.test(p.nonce)&&/^[a-f0-9]{40}$/.test(p.source_revision)&&/^[a-f0-9]{64}$/.test(p.config_sha256));
 need(/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,179}$/.test(r.model)&&Number.isInteger(r.context_tokens)&&r.context_tokens>=4096&&r.context_tokens<=10000000&&Number.isInteger(r.output_tokens)&&r.output_tokens>=16&&r.output_tokens<=4096);
 const a=r.bridge_ipv4.split('.').map(Number);need(a.length===4&&a.every(x=>Number.isInteger(x)&&x>=0&&x<=255)&&a.join('.')===r.bridge_ipv4&&(a[0]===10||(a[0]===172&&a[1]>=16&&a[1]<=31)||(a[0]===192&&a[1]===168)));
 const u=new URL(r.base_url);need(u.protocol==='http:'&&u.hostname===r.bridge_ipv4&&u.port&&Number(u.port)>0&&Number(u.port)<=65535&&u.pathname==='/openclaw'&&!u.search&&!u.hash&&!u.username&&!u.password&&u.href===r.base_url);
 const path='/home/node/.openclaw/openclaw.json';for(const dir of ['/home','/home/node','/home/node/.openclaw',path])need(!fs.lstatSync(dir).isSymbolicLink());
 const info=fs.statSync(path);need(info.isFile()&&info.uid===1000&&info.gid===1000&&info.nlink===1&&(info.mode&511)===384&&info.size>0&&info.size<=262144&&hash(fs.readFileSync(path))===p.config_sha256);
 const body=JSON.stringify({nonce:p.nonce});const expected=JSON.stringify({schema:1,role:'openclaw',nonce:p.nonce,source_revision:p.source_revision});
 await new Promise((resolve,reject)=>{const q=http.request({hostname:r.bridge_ipv4,port:Number(u.port),path:'/openclaw/_transport',method:'POST',headers:{Authorization:'Bearer '+r.credential,'Content-Type':'application/json','Content-Length':Buffer.byteLength(body)}},res=>{
  let value='',size=0;res.setEncoding('utf8');res.on('data',s=>{size+=Buffer.byteLength(s);if(size>1024){q.destroy();reject(Error())}else value+=s});res.on('error',reject);res.on('end',()=>{try{need(res.statusCode===200&&res.headers['content-type']==='application/json'&&value===expected);resolve()}catch(e){reject(e)}})
 });q.setTimeout(3000,()=>q.destroy(Error()));q.on('error',reject);q.end(body)});
 need(hash(fs.readFileSync(path))===p.config_sha256);console.log(JSON.stringify({schema:1,status:'runtime_container_route_verified',role:'openclaw',provider:r.provider,uid:1000,config_sha256:p.config_sha256,source_revision:p.source_revision,nonce:p.nonce}));
}catch(e){fail()}});process.stdin.on('error',fail);
'''


def main():
    try: report = hermes_probe(decoded(sys.stdin.buffer.read(4097)))
    except Exception: report = {"schema": 1, "status": "runtime_container_route_unverified"}
    print(json.dumps(report))
    return 1 if report["status"] == "runtime_container_route_unverified" else 0


if __name__ == "__main__": sys.exit(main())
