#!/usr/bin/env python3
"""Match a fresh controller task and native usage to original provider holds.

No network, billing refund, raw provider answer or client-selected usage basis.
Legacy completion proofs remain readable but cannot satisfy a fresh task.
"""
import hashlib
import json
import re

from showcase_model_completion import answer_hash, check, validate_proof

ROLES = {"openclaw", "hermes", "codex", "claude"}
COUNTS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}


def challenge_valid(value):
    check(isinstance(value, dict) and set(value) == {"lhs", "rhs", "nonce"})
    check(all(type(value[key]) is int and 0 <= value[key] < 1000000 for key in ("lhs", "rhs")))
    check(isinstance(value["nonce"], str) and re.fullmatch(r"[a-f0-9]{32}", value["nonce"]))


def input_hash(challenge):
    challenge_valid(challenge)
    return hashed(challenge)


def expected_answer(challenge):
    challenge_valid(challenge)
    return answer_hash({"sum": challenge["lhs"] + challenge["rhs"], "nonce": challenge["nonce"]})


def hashed(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def native_usage(value):
    check(isinstance(value, dict) and set(value) == COUNTS)
    check(all(type(count) is int and 0 <= count <= 1000000000 for count in value.values()))
    check(value["output_tokens"] > 0 and value["input_tokens"] + value["cache_read_tokens"] + value["cache_write_tokens"] > 0)
    return dict(value)


def correlate(role, challenge, usage, holds, terms):
    """All calls count; the final successful answer must match the fresh nonce."""
    check(role in ROLES)
    check(role not in ("codex", "claude") or terms["provider"] == ("openai" if role == "codex" else "anthropic"))
    expected = expected_answer(challenge)
    native = native_usage(usage)
    check(isinstance(holds, list) and bool(holds))
    total = dict.fromkeys(COUNTS, 0)
    identities = set()
    for hold in holds:
        check(isinstance(hold, dict) and hold.get("role") == role and hold.get("status") == "response_received")
        proof = hold.get("completion")
        validate_proof(proof, terms)
        identity = proof["response_id_sha256"]
        check(identity not in identities); identities.add(identity)
        check(role not in ("openclaw", "hermes") or proof["kind"] == "text")
        counts = proof["usage"]
        inp, out = counts["input_tokens"], counts["output_tokens"]
        read, write = counts["cache_read_input_tokens"], counts["cache_creation_input_tokens"]
        # Codex reports OpenAI's inclusive input total. The runtime agents
        # report separate uncached/cache buckets; Claude uses Anthropic's
        # uncached input count. These contracts are fixed by role/provider.
        if proof["provider"] == "openai" and role != "codex":
            inp -= read + write
        check(inp >= 0)
        for key, count in zip(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), (inp, out, read, write)):
            total[key] += count
    check(holds[-1]["completion"]["kind"] == "text" and holds[-1]["completion"].get("answer_sha256") == expected)
    check(total == native)
    return {"schema": 1, "status": "task_provider_correlated", "role": role, "answer_sha256": expected,
            "model_usage": native, "requests_sha256": hashed(holds)}
