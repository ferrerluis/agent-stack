"""Representative protocol bytes only; never real provider completion proof."""
import copy
import json

MODEL = "fixture-model"


def response(api="responses", text="fixture final answer", response_id="resp_fixture", tool_call=False):
    if api == "responses":
        output = [{"id": "item_fixture", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]}]
        if tool_call: output = [{"id": "item_fixture", "type": "function_call", "call_id": "call_fixture",
                                "name": "write_file", "arguments": '{"path":"result.json"}', "status": "completed"}]
        return {"id": response_id, "object": "response", "model": MODEL, "status": "completed", "error": None,
                "incomplete_details": None, "output": output, "service_tier": "default",
                "usage": {"input_tokens": 30, "output_tokens": 20, "total_tokens": 50,
                          "input_tokens_details": {"cached_tokens": 10}, "output_tokens_details": {"reasoning_tokens": 3}}}
    if api == "chat":
        message = {"role": "assistant", "content": text, "refusal": None}
        if tool_call: message["tool_calls"] = [{"id": "call_fixture", "type": "function",
                                               "function": {"name": "write_file", "arguments": '{"path":"result.json"}'}}]
        return {"id": response_id, "object": "chat.completion", "model": MODEL, "service_tier": "default",
                "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_call else "stop", "message": message}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50,
                          "prompt_tokens_details": {"cached_tokens": 10}}}
    content = [{"type": "text", "text": text}]
    if tool_call: content = [{"type": "tool_use", "id": "tool_fixture", "name": "write_file", "input": {"path": "result.json"}}]
    return {"id": response_id, "type": "message", "role": "assistant", "model": MODEL,
            "content": content, "stop_reason": "tool_use" if tool_call else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 30, "output_tokens": 20, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
                      "service_tier": "standard"}}


def frame(value, named=True):
    if value is None: return b"data: [DONE]\n\n"
    name = "event: " + value["type"] + "\n" if named else ""
    return (name + "data: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode()


def stream_rows(value):
    value = copy.deepcopy(value)
    if value.get("object") == "response":
        initial = {**value, "status": "in_progress", "output": [], "usage": None}
        rows = [{"type": "response.created", "response": initial}]
        item = value["output"][0]
        if item["type"] == "message":
            rows.append({"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                         "item_id": item["id"], "delta": item["content"][0]["text"]})
        else:
            rows.append({"type": "response.function_call_arguments.delta", "output_index": 0,
                         "item_id": item["id"], "delta": item["arguments"]})
        rows += [{"type": "response.output_item.done", "output_index": 0, "item": item},
                 {"type": "response.completed", "response": value}]
        return [{**row, "sequence_number": i} for i, row in enumerate(rows)]
    if value.get("object") == "chat.completion":
        message = value["choices"][0]["message"]
        delta = {**message}; delta.pop("refusal", None)
        if delta.get("tool_calls"): delta["tool_calls"] = [{**row, "index": i} for i, row in enumerate(delta["tool_calls"])]
        common = {"id": value["id"], "model": value["model"], "object": "chat.completion.chunk", "service_tier": "default"}
        return [{**common, "choices": [{"index": 0, "finish_reason": None, "delta": delta}]},
                {**common, "choices": [{"index": 0, "finish_reason": value["choices"][0]["finish_reason"], "delta": {}}]},
                {**common, "choices": [], "usage": value["usage"]}, None]
    start = {**value, "content": [], "stop_reason": None, "usage": {**value["usage"], "output_tokens": 1}}
    rows = [{"type": "message_start", "message": start}, {"type": "ping"}]
    for i, block in enumerate(value["content"]):
        key = "text" if block["type"] == "text" else "input"
        rows.append({"type": "content_block_start", "index": i, "content_block": {**block, key: "" if key == "text" else {}}})
        delta = {"type": "text_delta", "text": block[key]} if key == "text" else {"type": "input_json_delta", "partial_json": json.dumps(block[key])}
        rows += [{"type": "content_block_delta", "index": i, "delta": delta}, {"type": "content_block_stop", "index": i}]
    return rows + [{"type": "message_delta", "delta": {"stop_reason": value["stop_reason"], "stop_sequence": value["stop_sequence"]},
                   "usage": {"output_tokens": value["usage"]["output_tokens"]}}, {"type": "message_stop"}]


def encoded(value, streaming=False):
    if not streaming: return json.dumps(value, separators=(",", ":")).encode()
    named = value.get("object") != "chat.completion"
    return b"".join(frame(row, named) for row in stream_rows(value))
