#!/usr/bin/env python3
"""Bounded runtime-agent probe; only the owned-VM controller may dispatch it.

This stdlib-only source is sent to the verified VM over SSH, not installed in
operator deployments. API responses stay in memory; stdout contains selected
proof fields only. Unsupported pinned contracts fail instead of guessing.
"""
import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time

COMPOSE = ["/usr/bin/docker", "compose", "-f", "/opt/agent-stack/docker-compose.yml"]
MODEL = r"(openai|anthropic)/([A-Za-z0-9_.:/-]{1,180})"
REQUEST = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
LIMIT = 1048576


class ProbeError(Exception):
    """Fixed, non-sensitive failure codes only."""


def need(condition, reason="agent_proof_failed"):
    if not condition:
        raise ProbeError(reason)


def strict_json(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result, "invalid_json")
            result[key] = value
        return result
    def constant(_):
        raise ProbeError("invalid_json")
    try:
        need(isinstance(data, (str, bytes)) and len(data) <= LIMIT, "invalid_json")
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError):
        raise ProbeError("invalid_json") from None


def challenge_valid(challenge):
    need(isinstance(challenge, dict) and set(challenge) == {"lhs", "rhs", "nonce"}, "invalid_challenge")
    need(all(type(challenge[key]) is int and 0 <= challenge[key] < 1000000 for key in ("lhs", "rhs")), "invalid_challenge")
    need(isinstance(challenge["nonce"], str) and re.fullmatch(r"[a-f0-9]{32}", challenge["nonce"]), "invalid_challenge")


def prompt(challenge):
    challenge_valid(challenge)
    return ('Compute lhs + rhs from this JSON. Reply with only a JSON object containing the integer sum as "sum" '
            'and the unchanged "nonce". Do not use tools, delegate, send messages, or schedule work. Input: '
            + json.dumps(challenge, sort_keys=True, separators=(",", ":")))


def answer_valid(answer, challenge):
    challenge_valid(challenge)
    need(isinstance(answer, dict) and set(answer) == {"sum", "nonce"} and type(answer["sum"]) is int
         and answer["sum"] == challenge["lhs"] + challenge["rhs"] and answer["nonce"] == challenge["nonce"], "answer_not_verified")


def usage_counts(raw, names):
    need(isinstance(raw, dict), "usage_not_verified")
    result = {}
    for key, name in names.items():
        # OpenClaw omits zero input/cache buckets from its aggregate report.
        # Positive context and exact provider totals are still required.
        value = raw.get(name) if key == "output_tokens" else raw.get(name, 0)
        need(type(value) is int and 0 <= value <= 1000000000, "usage_not_verified")
        result[key] = value
    need(result["input_tokens"] + result["cache_read_tokens"] + result["cache_write_tokens"] > 0
         and result["output_tokens"] > 0, "usage_not_verified")
    return result


def parse_turn(runtime, raw, model_ref, challenge, request_id, run_id=None, native_provider=None):
    """Validate the actual serving pair and terminal response, never model echoes."""
    challenge_valid(challenge)
    match = re.fullmatch(MODEL, model_ref) if isinstance(model_ref, str) else None
    need(match and isinstance(request_id, str) and re.fullmatch(REQUEST, request_id), "invalid_agent_request")
    provider, model = match.groups()
    need(isinstance(raw, dict))
    if runtime == "openclaw":
        need(raw.get("runId") == request_id and raw.get("status") in ("ok", "completed"))
        result = raw.get("result")
        need(isinstance(result, dict))
        meta, payloads = result.get("meta"), result.get("payloads")
        need(isinstance(meta, dict) and meta.get("aborted") is False and not any(meta.get(key) for key in
             ("error", "failureSignal", "pendingToolCalls", "yielded", "replayInvalid")))
        need(meta.get("stopReason") in (None, "completed", "stop"))
        need(meta.get("livenessState") not in ("blocked", "paused", "abandoned")
             and not meta.get("agentHarnessResultClassification"))
        completion, tools = meta.get("completion") or {}, meta.get("toolSummary") or {}
        need(isinstance(completion, dict) and not completion.get("refusal")
             and isinstance(tools, dict) and not tools.get("calls"))
        need(not any(result.get(key) for key in ("didSendViaMessagingTool", "didSendDeterministicApprovalPrompt", "successfulCronAdds")))
        agent = meta.get("agentMeta")
        need(isinstance(agent, dict) and agent.get("sessionId") == request_id)
        need(agent.get("provider") == provider and agent.get("model") == model, "served_model_mismatch")
        need(not agent.get("fallbackAttempts"), "model_fallback_not_allowed")
        trace = meta.get("executionTrace") or {}
        need(isinstance(trace, dict) and not trace.get("fallbackUsed"), "model_fallback_not_allowed")
        need(isinstance(payloads, list) and len(payloads) == 1 and isinstance(payloads[0], dict)
             and not any(payloads[0].get(key) for key in ("isError", "isReasoning", "mediaUrl", "mediaUrls")))
        answer = strict_json(payloads[0].get("text"))
        usage = usage_counts(agent.get("usage"), {"input_tokens": "input", "output_tokens": "output",
            "cache_read_tokens": "cacheRead", "cache_write_tokens": "cacheWrite"})
        transport = "gateway_rpc"
    else:
        need(runtime == "hermes", "invalid_runtime")
        need(isinstance(run_id, str) and re.fullmatch(r"run_[A-Za-z0-9_-]{1,100}", run_id), "invalid_run_id")
        need(raw.get("object") == "hermes.run" and raw.get("run_id") == run_id and raw.get("status") == "completed"
             and not raw.get("error") and not raw.get("shutdown_requested_at"))
        actual = raw.get("runtime")
        expected_provider = ("custom" if provider == "openai" else provider) if native_provider is None else native_provider
        need(isinstance(actual, dict) and actual.get("provider") == expected_provider
             and actual.get("model") == model and actual.get("route_source") == "raw_request", "served_model_mismatch")
        if native_provider is not None:
            need(native_provider == "custom" and actual.get("requested") == {"provider": "as7-budget", "model": model}
                 and raw.get("completed") is True and raw.get("partial") is False and raw.get("interrupted") is False
                 and not any(raw.get(k) for k in ("pending_steer", "turn_exit_reason")), "agent_proof_failed")
        # The API's top-level model is only an echo and is deliberately ignored.
        answer = strict_json(raw.get("output"))
        usage = usage_counts(raw.get("usage"), {key: key for key in
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")})
        transport = "api_runs"
    answer_valid(answer, challenge)
    return {"schema": 1, "runtime": runtime, "status": "pass", "request_id": request_id,
            "served_model": model_ref, "transport": transport, "answer": answer, "model_usage": usage}


def validate_proof(report, runtime, model, challenge, request_id):
    """Second validation on the controller, against its original fresh challenge."""
    need(isinstance(report, dict) and report.get("schema") == 1 and type(report["schema"]) is int
         and report.get("status") == "pass" and report.get("runtime") == runtime
         and report.get("request_id") == request_id and report.get("served_model") == model
         and report.get("transport") == {"openclaw": "gateway_rpc", "hermes": "api_runs"}.get(runtime)
         and report.get("watchdog") == "disarmed")
    answer_valid(report.get("answer"), challenge)
    usage = usage_counts(report.get("model_usage"), {key: key for key in
        ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")})
    duration = report.get("duration_seconds")
    need(type(duration) in (int, float) and 0 <= duration <= 285)
    return {"status": "pass", "model": model, "request_id": request_id, "transport": report["transport"],
            "model_usage": usage, "duration_seconds": duration,
            "answer_sha256": hashlib.sha256(json.dumps(report["answer"], sort_keys=True).encode()).hexdigest()}


def invoke(args, timeout=10, input=None, env=None, pass_fds=()):
    """Bound memory and elapsed time; kill local descendants on any interruption."""
    need(input is None or isinstance(input, bytes) and len(input) <= 4096, "probe_input_limit")
    need(isinstance(pass_fds, tuple) and len(pass_fds) <= 8
         and all(type(fd) is int and 3 <= fd < 2147483648 for fd in pass_fds), 'probe_descriptor_invalid')
    child = subprocess.Popen(args, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=pass_fds)
    output, size = bytearray(), 0
    deadline = time.monotonic() + timeout
    try:
        if input is not None:
            child.stdin.write(input)
            child.stdin.close()
        with selectors.DefaultSelector() as streams:
            for pipe in (child.stdout, child.stderr):
                streams.register(pipe, selectors.EVENT_READ)
            while streams.get_map():
                remaining = deadline - time.monotonic()
                need(remaining > 0, "probe_command_timeout")
                for key, _ in streams.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        streams.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    need(size <= LIMIT, "probe_output_limit")
                    if key.fileobj is child.stdout:
                        output.extend(chunk)
            try:
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise ProbeError("probe_command_timeout") from None
        need(child.returncode == 0, "probe_command_failed")
        return bytes(output)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)
        raise
    finally:
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


# Runs API supports cancellation and reports the executor's real terminal state.
# The outer VM watchdog also covers an unobserved POST result or lost SSH.
HERMES_SOURCE = '''import json,os,pathlib,re,sys,time,urllib.request
def pairs(rows):
    result={}
    for key,value in rows:
        assert key not in result
        result[key]=value
    return result
def constant(_): raise RuntimeError('invalid_json')
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise RuntimeError('redirect_refused')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def request(method,path,data=None):
    assert re.fullmatch(r'/v1/(?:capabilities|runs(?:/run_[A-Za-z0-9_-]{1,100}(?:/stop)?)?)',path)
    body=None if data is None else json.dumps(data).encode()
    req=urllib.request.Request('http://127.0.0.1:8642'+path,data=body,method=method,
        headers={'Authorization':'Bearer '+os.environ['API_SERVER_KEY'],'Content-Type':'application/json',
                 'Idempotency-Key':args['request_id']})
    with opener.open(req,timeout=10) as response:
        data=response.read(1048577)
        assert len(data)<=1048576
        return json.loads(data,object_pairs_hook=pairs,parse_constant=constant)
try:
    args=json.load(sys.stdin)
    config_text=pathlib.Path('/opt/data/config.yaml').read_text()
    try: config=json.loads(config_text)
    except ValueError:
        import yaml
        config=yaml.safe_load(config_text)
    config=config['model']
    provider,model=args['model'].split('/',1)
    provider='custom' if provider=='openai' else provider
    assert config['provider']==provider and config['default']==model
    assert provider!='custom' or config['base_url']=='https://api.openai.com/v1'
    capabilities=request('GET','/v1/capabilities')
    assert capabilities.get('object')=='hermes.api_server.capabilities'
    assert capabilities.get('auth',{}).get('type')=='bearer' and capabilities['auth'].get('required') is True
    assert all(capabilities.get('features',{}).get(key) is True for key in ('run_submission','run_status','run_stop'))
    created=request('POST','/v1/runs',{'input':args['prompt'],'provider':provider,'model':model,'conversation_history':[]})
    run_id=created['run_id']
    assert re.fullmatch(r'run_[A-Za-z0-9_-]{1,100}',run_id) and created['status'] in ('started','running')
    deadline=time.monotonic()+165
    while time.monotonic()<deadline:
        report=request('GET','/v1/runs/'+run_id)
        assert report.get('run_id')==run_id
        if report.get('status') in ('completed','failed','cancelled','interrupted'): break
        time.sleep(1)
    else:
        request('POST','/v1/runs/'+run_id+'/stop',{})
        raise RuntimeError('run_not_completed')
    print(json.dumps({'run_id':run_id,'report':report}))
except Exception:
    print(json.dumps({'status':'probe_failed'}))
    sys.exit(1)
'''


def container_running(container, execute):
    state = strict_json(execute(["/usr/bin/docker", "inspect", "--format", "{{json .State}}", container]))
    need(isinstance(state, dict) and type(state.get("Running")) is bool and type(state.get("Restarting")) is bool, "container_state_unverified")
    return state["Running"] or state["Restarting"]


def run_probe(runtime, model, challenge, request_id, execute=invoke, now=time.monotonic):
    need(runtime in ("openclaw", "hermes") and isinstance(model, str) and re.fullmatch(MODEL, model)
         and isinstance(request_id, str) and re.fullmatch(REQUEST, request_id), "invalid_agent_request")
    challenge_valid(challenge)
    started, container, armed = now(), None, False
    unit = "as7-agent-" + request_id.replace("-", "")
    failed = {"schema": 1, "runtime": runtime, "request_id": request_id, "status": "fail", "containment": "not_needed"}
    try:
        container = execute(COMPOSE + ["ps", "-q", runtime]).decode().strip()
        need(re.fullmatch(r"[a-f0-9]{64}", container), "one_runtime_container_required")
        need(container_running(container, execute), "runtime_container_not_running")
        if runtime == "openclaw":
            help_text = execute(["/usr/bin/docker", "exec", container, "openclaw", "gateway", "call", "--help"]).decode()
            need(all(flag in help_text for flag in ("--expect-url", "--expect-final", "--params", "--timeout", "--json")), "pinned_gateway_contract_unsupported")
        # This root-owned timer survives a killed SSH/probe process. It targets
        # the captured container ID, never a replacement service by name.
        execute(["systemd-run", "--quiet", "--unit=" + unit, "--on-active=210s", "--timer-property=AccuracySec=1s",
                 "--property=RuntimeMaxSec=30s", "/usr/bin/docker", "stop", "-t", "5", container])
        armed = True
        need(execute(["systemctl", "is-active", unit + ".timer"]).strip() == b"active", "watchdog_not_verified")
        if runtime == "openclaw":
            params = {"message": prompt(challenge), "sessionId": request_id, "sessionKey": "agent:main:as7-" + request_id,
                "agentId": "main", "idempotencyKey": request_id, "model": model, "deliver": False, "timeout": 180}
            raw = strict_json(execute(["/usr/bin/docker", "exec", container, "openclaw", "gateway", "call", "agent",
                "--expect-url", "ws://127.0.0.1:18789", "--expect-final", "--timeout", "185000", "--json",
                "--params", json.dumps(params)], timeout=190))
            proof = parse_turn(runtime, raw, model, challenge, request_id)
        else:
            body = json.dumps({"model": model, "request_id": request_id, "prompt": prompt(challenge)}).encode()
            raw = strict_json(execute(["/usr/bin/docker", "exec", "-i", container, "/opt/hermes/.venv/bin/python", "-c", HERMES_SOURCE],
                timeout=190, input=body))
            proof = parse_turn(runtime, raw.get("report"), model, challenge, request_id, raw.get("run_id"))
        need(container_running(container, execute), "runtime_container_not_running")
        execute(["systemctl", "stop", unit + ".timer", unit + ".service"])
        need(execute(["systemctl", "show", "--property=ActiveState", "--value", unit + ".timer"]).strip() == b"inactive",
             "watchdog_disarm_unverified")
        armed = False
        proof.update({"watchdog": "disarmed", "duration_seconds": round(now() - started, 2)})
        validate_proof(proof, runtime, model, challenge, request_id)
        return proof
    except BaseException:
        # Do not restart an uncertain paid turn. Preserve the failed runtime
        # for cleanup or the one authorized fresh retry.
        if container and re.fullmatch(r"[a-f0-9]{64}", container) and armed:
            failed["containment"] = "uncertain"
            try:
                execute(["/usr/bin/docker", "stop", "-t", "5", container], timeout=20)
                need(not container_running(container, execute), "runtime_stop_unverified")
                failed["containment"] = "stopped"
                execute(["systemctl", "stop", unit + ".timer", unit + ".service"])
            except BaseException:
                pass  # Keep the watchdog if direct containment is uncertain.
        return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=("openclaw", "hermes"))
    parser.add_argument("model")
    parser.add_argument("request_id")
    parser.add_argument("challenge")
    args = parser.parse_args()
    try:
        need(os.geteuid() == 0, "owned_vm_root_required")
        result = run_probe(args.runtime, args.model, strict_json(args.challenge), args.request_id)
    except Exception:
        result = {"schema": 1, "runtime": args.runtime, "status": "fail"}
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
