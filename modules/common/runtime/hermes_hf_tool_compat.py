"""Normalize Muse JSON file content through Hermes' native plugin hooks."""
import json
from collections import OrderedDict

MUSE = "meta-models/Muse-Spark-Open-816B-A42B:baseten"


def register(ctx):
    sessions = OrderedDict()

    def track(session_id="", model="", base_url="", **kwargs):
        if not session_id:
            return
        sessions[session_id] = model == MUSE and base_url.rstrip("/") == "https://router.huggingface.co/v1"
        if len(sessions) > 1024:
            sessions.popitem(last=False)

    def normalize(tool_name="", args=None, session_id="", **kwargs):
        if not sessions.get(session_id) or tool_name != "write_file" or not isinstance(args, dict):
            return
        content = args.get("content")
        if type(content) in (dict, list):
            return {"action": "modify", "args": {"content": json.dumps(content, separators=(",", ":"), ensure_ascii=False)}}

    def forget(session_id="", **kwargs):
        sessions.pop(session_id, None)

    ctx.register_hook("pre_api_request", track)
    ctx.register_hook("pre_tool_call", normalize)
    ctx.register_hook("on_session_end", forget)
