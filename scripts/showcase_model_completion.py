#!/usr/bin/env python3
"""Selected, nonsecret provider completion evidence from bounded response bytes.

HTTP delivery is independent of this proof. Unknown, failed or incomplete
protocols yield no proof; they never release a hold or authorize a retry.
This module makes no request and stores no prompt, answer or provider ID.
"""
import hashlib
import json
import re

LIMIT = 4 * 1024 * 1024
APIS = {"/v1/responses": "responses", "/v1/chat/completions": "chat", "/v1/messages": "messages"}
FIELDS = {"schema", "provider", "model", "api", "finish", "kind", "response_id_sha256", "body_sha256", "text_sha256", "usage"}
COUNTS = {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "context_tokens"}
FINISHES = {"responses": {"completed"}, "chat": {"stop", "tool_calls"}, "messages": {"end_turn", "stop_sequence", "tool_use"}}


def check(value):
    if not value:
        raise ValueError("model_completion_unverified")


def integer(value, maximum=2000000):
    check(type(value) is int and 0 <= value <= maximum)
    return value


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def decoded(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            check(key not in value)
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: check(False))
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        check(depth <= 64)
        if isinstance(item, dict): pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list): pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, float):
            # Even unused numeric fields must be finite.
            check(float("-inf") < item < float("inf"))
    return value


def identity(value, model):
    check(isinstance(value, dict) and value.get("model") == model and not value.get("error"))
    check(isinstance(value.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value["id"]))
    return value["id"]


def identifier(value):
    check(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value))
    return value


def usage(value, api):
    check(isinstance(value, dict))
    if api == "messages":
        inp, out = integer(value.get("input_tokens")), integer(value.get("output_tokens"))
        read = integer(value.get("cache_read_input_tokens", 0))
        write = integer(value.get("cache_creation_input_tokens", 0))
        context = inp + read + write
        check(value.get("service_tier") in (None, "standard"))
        server = value.get("server_tool_use")
        check(server is None or isinstance(server, dict) and all(integer(v) == 0 for v in server.values()))
        cache = value.get("cache_creation")
        check(cache is None or isinstance(cache, dict) and sum(integer(v) for v in cache.values()) == write)
    else:
        names = ("input_tokens", "output_tokens") if api == "responses" else ("prompt_tokens", "completion_tokens")
        inp, out = (integer(value.get(key)) for key in names)
        check(integer(value.get("total_tokens"), 4000000) == inp + out)
        detail_key = "input_tokens_details" if api == "responses" else "prompt_tokens_details"
        details = value.get(detail_key) or {}
        check(isinstance(details, dict))
        read, write = integer(details.get("cached_tokens", 0)), integer(details.get("cache_write_tokens", 0))
        check(read <= inp and write <= inp)
        context = inp
        check(all(integer(details.get(key, 0)) == 0 for key in ("audio_tokens", "image_tokens")))
        output_details = value.get("output_tokens_details" if api == "responses" else "completion_tokens_details") or {}
        check(isinstance(output_details, dict) and all(integer(v) <= out for v in output_details.values()))
        check(integer(output_details.get("audio_tokens", 0)) == 0)
    check(out > 0)
    return dict(zip(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "context_tokens"),
                    (inp, out, read, write, context)))


def validate_proof(value, terms):
    """Also used when reopening the original ledger; old HTTP holds stay old."""
    check(isinstance(value, dict) and set(value) == FIELDS and type(value["schema"]) is int and value["schema"] == 1)
    check(value["provider"] == terms["provider"] and value["model"] == terms["model"])
    api = value["api"]
    check(isinstance(api, str) and api in APIS.values()
          and (api == "messages") == (value["provider"] == "anthropic"))
    check(isinstance(value["finish"], str) and value["finish"] in FINISHES[api] and value["kind"] in ("text", "tool_call"))
    if api != "responses": check((value["kind"] == "tool_call") == (value["finish"] in ("tool_calls", "tool_use")))
    check(all(isinstance(value[key], str) and re.fullmatch(r"[a-f0-9]{64}", value[key])
              for key in ("response_id_sha256", "body_sha256", "text_sha256")))
    counts = value["usage"]
    check(isinstance(counts, dict) and set(counts) == COUNTS)
    for count in counts.values(): integer(count)
    check(0 < counts["output_tokens"] <= terms["output_tokens_upper_bound"]
          and counts["context_tokens"] <= terms["context_tokens_upper_bound"])
    if api == "messages":
        check(counts["context_tokens"] == counts["input_tokens"] + counts["cache_read_input_tokens"] + counts["cache_creation_input_tokens"])
    else:
        check(counts["context_tokens"] == counts["input_tokens"] and counts["cache_read_input_tokens"] <= counts["input_tokens"]
              and counts["cache_creation_input_tokens"] <= counts["input_tokens"])
    check(value["kind"] != "text" or value["text_sha256"] != hashed(""))


def tool(name, arguments, allowed):
    check(isinstance(name, str) and name in allowed)
    check(isinstance(arguments, dict))


def responses(value, model, allowed):
    identity(value, model)
    check(value.get("object") == "response" and value.get("status") == "completed"
          and not value.get("incomplete_details") and value.get("service_tier") in (None, "default"))
    output = value.get("output")
    check(isinstance(output, list) and 0 < len(output) <= 256)
    text, calls, ids, call_ids = [], 0, set(), set()
    for row in output:
        check(isinstance(row, dict) and row.get("status") in (None, "completed"))
        item_id = row.get("id")
        check(isinstance(item_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", item_id) and item_id not in ids)
        ids.add(item_id)
        if row.get("type") == "message":
            check(row.get("role") == "assistant" and row.get("status") == "completed" and isinstance(row.get("content"), list))
            for part in row["content"]:
                check(isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str))
                text.append(part["text"])
        elif row.get("type") == "function_call":
            check(isinstance(row.get("arguments"), str))
            call_id = identifier(row.get("call_id")); check(call_id not in call_ids); call_ids.add(call_id)
            tool(row.get("name"), decoded(row["arguments"]), allowed); calls += 1
        else:
            check(row.get("type") == "reasoning")
    check(calls or "".join(text))
    return "completed", "tool_call" if calls else "text", "".join(text), usage(value.get("usage"), "responses")


def chat(value, model, allowed):
    identity(value, model)
    check(value.get("object") == "chat.completion" and value.get("service_tier") in (None, "default"))
    choices = value.get("choices")
    check(isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict))
    choice = choices[0]
    check(type(choice.get("index")) is int and choice["index"] == 0 and choice.get("finish_reason") in ("stop", "tool_calls"))
    message = choice.get("message")
    check(isinstance(message, dict) and message.get("role") == "assistant" and not message.get("refusal")
          and not message.get("function_call") and not message.get("audio"))
    text = message.get("content") or ""
    check(isinstance(text, str))
    calls = message.get("tool_calls") or []
    check(isinstance(calls, list) and len(calls) <= 128)
    ids = set()
    for row in calls:
        check(isinstance(row, dict) and row.get("type") == "function" and identifier(row.get("id")) not in ids)
        ids.add(row["id"]); function = row.get("function")
        check(isinstance(function, dict) and isinstance(function.get("arguments"), str))
        tool(function.get("name"), decoded(function["arguments"]), allowed)
    check(bool(calls) == (choice["finish_reason"] == "tool_calls") and (calls or text))
    return choice["finish_reason"], "tool_call" if calls else "text", text, usage(value.get("usage"), "chat")


def messages(value, model, allowed):
    identity(value, model)
    check(value.get("type") == "message" and value.get("role") == "assistant"
          and value.get("stop_reason") in ("end_turn", "stop_sequence", "tool_use"))
    parts = value.get("content")
    check(isinstance(parts, list) and 0 < len(parts) <= 256)
    text, calls, ids = [], 0, set()
    for row in parts:
        check(isinstance(row, dict))
        if row.get("type") == "text":
            check(isinstance(row.get("text"), str)); text.append(row["text"])
        elif row.get("type") == "tool_use":
            check(identifier(row.get("id")) not in ids); ids.add(row["id"])
            tool(row.get("name"), row.get("input"), allowed); calls += 1
        else: check(row.get("type") in ("thinking", "redacted_thinking"))
    check(bool(calls) == (value["stop_reason"] == "tool_use") and (calls or "".join(text)))
    return value["stop_reason"], "tool_call" if calls else "text", "".join(text), usage(value.get("usage"), "messages")


def events(raw):
    text = raw.decode("utf-8").replace("\r\n", "\n")
    check("\r" not in text and "\x00" not in text and text.endswith("\n\n"))
    result = []
    for block in text.split("\n\n"):
        data, name = [], None
        for line in block.split("\n"):
            if not line or line.startswith(":"): continue
            key, sep, value = line.partition(":")
            check(sep and key in ("data", "event", "id", "retry"))
            value = value[1:] if value.startswith(" ") else value
            if key == "data": data.append(value)
            elif key == "event": check(name is None); name = value
        if not data: check(name is None); continue
        value = "\n".join(data)
        if value == "[DONE]": check(name is None); result.append(None)
        else:
            row = decoded(value)
            check(isinstance(row, dict) and not row.get("error"))
            check(name is None or name == row.get("type"))
            result.append(row)
    check(0 < len(result) <= 32768)
    return result


def responses_stream(rows, model):
    check(all(isinstance(row, dict) for row in rows))
    check(rows[0].get("type") == "response.created" and rows[-1].get("type") == "response.completed")
    original = rows[0].get("response"); response_id = identity(original, model)
    check(original.get("status") == "in_progress")
    texts, arguments, done_items, added_items, done_texts, done_arguments, done_parts, previous = {}, {}, {}, {}, {}, {}, {}, -1
    for offset, row in enumerate(rows):
        seq = row.get("sequence_number")
        check(type(seq) is int and seq == previous + 1); previous = seq
        kind = row.get("type")
        check(isinstance(kind, str) and kind in {"response.created", "response.in_progress", "response.completed",
              "response.output_item.added", "response.output_item.done", "response.content_part.added", "response.content_part.done",
              "response.output_text.delta", "response.output_text.done", "response.function_call_arguments.delta",
              "response.function_call_arguments.done", "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
              "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"})
        check(kind != "response.created" or offset == 0)
        check(kind != "response.completed" or offset == len(rows) - 1)
        if "response" in row:
            check(identity(row["response"], model) == response_id)
            check(row["response"].get("status") == ("completed" if kind == "response.completed" else "in_progress"))
        if kind in ("response.output_text.delta", "response.function_call_arguments.delta"):
            check(isinstance(row.get("delta"), str) and isinstance(row.get("item_id"), str))
            index = integer(row.get("output_index"), 255)
            check(index not in done_items)
            if kind == "response.output_text.delta":
                key = (index, integer(row.get("content_index"), 255), row["item_id"])
                check(key not in done_texts and key not in done_parts)
                texts[key] = texts.get(key, "") + row["delta"]
            else:
                key = (index, row["item_id"]); check(key not in done_arguments); arguments[key] = arguments.get(key, "") + row["delta"]
        elif kind == "response.output_item.done":
            index = integer(row.get("output_index"), 255); check(index not in done_items); done_items[index] = row.get("item")
        elif kind == "response.output_item.added":
            index = integer(row.get("output_index"), 255)
            check(index not in added_items and index not in done_items
                  and all(key[0] != index for key in texts.keys() | arguments.keys()))
            added_items[index] = row.get("item")
        elif kind == "response.output_text.done":
            key = (integer(row.get("output_index"), 255), integer(row.get("content_index"), 255), identifier(row.get("item_id")))
            check(key[0] not in done_items and key not in done_parts and key not in done_texts and isinstance(row.get("text"), str)); done_texts[key] = row["text"]
        elif kind == "response.function_call_arguments.done":
            key = (integer(row.get("output_index"), 255), identifier(row.get("item_id")))
            check(key[0] not in done_items and key not in done_arguments and isinstance(row.get("arguments"), str)); done_arguments[key] = row["arguments"]
        elif kind == "response.content_part.done":
            key = (integer(row.get("output_index"), 255), integer(row.get("content_index"), 255), identifier(row.get("item_id")))
            check(key[0] not in done_items and key not in done_parts); done_parts[key] = row.get("part")
    final = rows[-1]["response"]
    output = final.get("output"); check(isinstance(output, list))
    for key, text in done_texts.items(): check(key not in texts or texts[key] == text)
    for key, text in done_arguments.items(): check(key not in arguments or arguments[key] == text)
    for (index, part, item), text in (texts | done_texts).items():
        check(index < len(output) and output[index].get("id") == item)
        content = output[index].get("content"); check(isinstance(content, list) and part < len(content))
        check(content[part].get("type") == "output_text" and content[part].get("text") == text)
    for (index, item), text in (arguments | done_arguments).items():
        check(index < len(output) and output[index].get("id") == item and output[index].get("arguments") == text)
    for index, item in done_items.items(): check(index < len(output) and output[index] == item)
    for index, item in added_items.items():
        check(index < len(output) and isinstance(item, dict) and item.get("id") == output[index].get("id")
              and item.get("type") == output[index].get("type"))
    for (index, part, item), value in done_parts.items():
        check(index < len(output) and output[index].get("id") == item)
        content = output[index].get("content"); check(isinstance(content, list) and part < len(content) and content[part] == value)
    return final


def chat_stream(rows, model):
    check(rows[-1] is None and all(isinstance(row, dict) for row in rows[:-1]))
    chunks = rows[:-1]; check(chunks)
    response_id = identity(chunks[0], model)
    text, calls, finish, counts, assistant = "", {}, None, None, False
    for row in chunks:
        check(identity(row, model) == response_id and row.get("object") == "chat.completion.chunk"
              and row.get("service_tier") in (None, "default"))
        choices = row.get("choices"); check(isinstance(choices, list) and len(choices) <= 1)
        if row.get("usage") is not None:
            check(not choices and finish is not None and counts is None); counts = row["usage"]
        if not choices: continue
        check(finish is None and counts is None)
        choice = choices[0]; check(isinstance(choice, dict) and type(choice.get("index")) is int and choice["index"] == 0)
        delta = choice.get("delta"); check(isinstance(delta, dict) and not delta.get("refusal") and not delta.get("function_call"))
        if delta.get("role") is not None:
            check(delta["role"] == "assistant" and not assistant); assistant = True
        if delta.get("content") is not None: check(isinstance(delta["content"], str)); text += delta["content"]
        tools = delta.get("tool_calls") or []; check(isinstance(tools, list))
        for tool_delta in tools:
            check(isinstance(tool_delta, dict)); index = integer(tool_delta.get("index"), 127)
            target = calls.setdefault(index, {"type": "function", "id": None, "function": {"name": "", "arguments": ""}})
            if tool_delta.get("id") is not None: check(target["id"] is None); target["id"] = tool_delta["id"]
            check(tool_delta.get("type") in (None, "function"))
            function = tool_delta.get("function") or {}; check(isinstance(function, dict))
            for key in ("name", "arguments"):
                if function.get(key) is not None: check(isinstance(function[key], str)); target["function"][key] += function[key]
        if choice.get("finish_reason") is not None: finish = choice["finish_reason"]
    check(assistant and finish is not None and counts is not None and set(calls) == set(range(len(calls))))
    return {"id": response_id, "model": model, "object": "chat.completion", "usage": counts,
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text,
                         "tool_calls": [calls[index] for index in range(len(calls))]}}]}


def messages_stream(rows, model):
    check(all(isinstance(row, dict) for row in rows))
    meaningful = [row for row in rows if row.get("type") != "ping"]
    check(meaningful and meaningful[0].get("type") == "message_start" and meaningful[-1].get("type") == "message_stop")
    message = meaningful[0].get("message"); identity(message, model)
    check(message.get("content") == [] and message.get("stop_reason") is None and isinstance(message.get("usage"), dict))
    counts = dict(message["usage"]); blocks, active, parts, stopped, finish, stop_sequence, saw_usage = [], None, "", False, None, None, False
    for offset, row in enumerate(meaningful):
        kind = row.get("type")
        if kind == "message_start": check(offset == 0)
        elif kind == "content_block_start":
            check(active is None and finish is None and integer(row.get("index"), 255) == len(blocks))
            block = row.get("content_block"); check(isinstance(block, dict) and block.get("type") in ("text", "tool_use", "thinking", "redacted_thinking"))
            blocks.append(dict(block)); active, parts = len(blocks) - 1, ""
        elif kind == "content_block_delta":
            check(active is not None and type(row.get("index")) is int and row["index"] == active)
            delta = row.get("delta"); check(isinstance(delta, dict)); block = blocks[active]
            if delta.get("type") == "text_delta":
                check(block["type"] == "text" and isinstance(delta.get("text"), str) and isinstance(block.get("text"), str)); block["text"] += delta["text"]
            elif delta.get("type") == "input_json_delta":
                check(block["type"] == "tool_use" and isinstance(delta.get("partial_json"), str)); parts += delta["partial_json"]
            else: check(block["type"] == "thinking" and delta.get("type") in ("thinking_delta", "signature_delta"))
        elif kind == "content_block_stop":
            check(active is not None and type(row.get("index")) is int and row["index"] == active)
            if blocks[active]["type"] == "tool_use" and parts: blocks[active]["input"] = decoded(parts)
            active = None
        elif kind == "message_delta":
            check(active is None and finish is None and isinstance(row.get("delta"), dict) and isinstance(row.get("usage"), dict))
            check(not row["delta"].keys() - {"stop_reason", "stop_sequence"} and "model" not in row)
            for key, value in row["usage"].items():
                if key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    check(integer(value) >= integer(counts.get(key, 0)))
                counts[key] = value
            check("output_tokens" in row["usage"]); saw_usage = True
            finish = row["delta"].get("stop_reason")
            stop_sequence = row["delta"].get("stop_sequence")
        elif kind == "message_stop":
            check(offset == len(meaningful) - 1 and active is None and finish is not None and saw_usage and not stopped); stopped = True
        else: check(False)
    check(stopped)
    return {**message, "content": blocks, "usage": counts, "stop_reason": finish, "stop_sequence": stop_sequence}


def completion(provider, model, path, content_type, raw, request, terms):
    """Return proof or None; a transport receipt alone is never completion."""
    try:
        check(provider in ("openai", "anthropic") and path in APIS and isinstance(raw, bytes) and 0 < len(raw) <= LIMIT)
        api = APIS[path]; check((api == "messages") == (provider == "anthropic"))
        tools = request.get("tools", [])
        allowed = {row.get("name") if api != "chat" else row.get("function", {}).get("name") for row in tools}
        check(all(isinstance(name, str) and name for name in allowed))
        if content_type == "application/json":
            check(request.get("stream", False) is False); value = decoded(raw)
        else:
            check(content_type == "text/event-stream" and request.get("stream", False) is True)
            value = {"responses": responses_stream, "chat": chat_stream, "messages": messages_stream}[api](events(raw), model)
        finish, kind, text, counts = {"responses": responses, "chat": chat, "messages": messages}[api](value, model, allowed)
        if api == "messages" and finish == "stop_sequence":
            check(isinstance(request.get("stop_sequences"), list) and isinstance(value.get("stop_sequence"), str)
                  and value["stop_sequence"] in request["stop_sequences"])
        limit = request[{"responses": "max_output_tokens", "chat": "max_completion_tokens", "messages": "max_tokens"}[api]]
        check(counts["output_tokens"] <= limit)
        proof = {"schema": 1, "provider": provider, "model": model, "api": api, "finish": finish, "kind": kind,
                 "response_id_sha256": hashed(value["id"]), "body_sha256": hashlib.sha256(raw).hexdigest(),
                 "text_sha256": hashed(text), "usage": counts}
        validate_proof(proof, terms)
        return proof
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError, RecursionError):
        return None
