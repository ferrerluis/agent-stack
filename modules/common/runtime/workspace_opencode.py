#!/usr/bin/env python3
"""Managed OpenCode HF routing. Operator files and native auth remain untouched.

This is a configuration contract, not a sandbox against the workspace owner.
The native executable remains available for diagnostics in /usr/local/libexec.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

BINARY = "/usr/local/libexec/opencode"
ENDPOINT = "https://router.huggingface.co/v1"
# Public HF model IDs, never Baseten's internal providerId. No auto routing.
ROUTES = {
    "moonshotai/Kimi-K3:baseten": {"name": "Kimi", "efforts": ["low", "high", "max"]},
    "deepseek-ai/DeepSeek-V4.1-Flash:baseten": {"name": "DeepSeek Flash", "efforts": ["high", "max"]},
    "meta-models/Muse-Spark-Open-816B-A42B:baseten": {"name": "Muse Spark Open", "efforts": ["low", "medium", "high", "xhigh"]},
}


def references():
    return ["huggingface/" + route for route in ROUTES]


def provider_config():
    return {"npm": "@ai-sdk/openai-compatible", "api": ENDPOINT, "env": ["HF_TOKEN"],
            "name": "Hugging Face (reviewed Baseten routes)", "whitelist": list(ROUTES),
            "options": {"baseURL": ENDPOINT},
            "models": {route: {"id": route, "name": contract["name"], "reasoning": True,
                               "tool_call": True, "interleaved": "reasoning_content",
                               "reasoning_options": [{"type": "effort", "values": contract["efforts"]}],
                               "options": {"reasoningEffort": "high"},
                               "modalities": {"input": ["text"], "output": ["text"]},
                               # Conservative client output cap, not a hosted capacity claim.
                               "limit": {"context": 1048576, "output": 8192}}
                       for route, contract in ROUTES.items()}}


def initial_config(model):
    if model not in references():
        raise ValueError("select a reviewed huggingface/<Hub ID>:baseten model")
    return {"$schema": "https://opencode.ai/config.json", "model": model, "small_model": model,
            "enabled_providers": ["huggingface"], "autoupdate": False,
            "permission": {"*": "ask"}, "provider": {"huggingface": provider_config()}}


def seed(home, model):
    config = initial_config(model)
    directory = Path(home) / ".config/opencode"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((directory / name).exists() for name in ("opencode.json", "opencode.jsonc", "config.json")):
        return
    try:
        fd = os.open(directory / "opencode.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")


def check_reference(value):
    if value not in references():
        raise ValueError("unsupported model; choose one of the reviewed Hugging Face routes")
    return value.split("/", 1)[1]


def validate_config(config, args):
    route = check_reference(config.get("model"))
    if config.get("small_model"):
        check_reference(config["small_model"])
    if config.get("enabled_providers", ["huggingface"]) != ["huggingface"]:
        raise ValueError("enabled_providers must contain only huggingface")
    if "huggingface" in config.get("disabled_providers", []):
        raise ValueError("huggingface is disabled in the operator config")
    if config.get("autoupdate", False) is not False:
        raise ValueError("autoupdate must be false for the pinned OpenCode contract")
    for agent in config.get("agent", {}).values():
        if not isinstance(agent, dict):
            continue
        if agent.get("model"):
            check_reference(agent["model"])
        # Agent options override model variants inside native request prep.
        # Keep persistent agent effort at the common supported default; use the
        # model-specific CLI/TUI variant picker for other reviewed efforts.
        if agent.get("variant") not in (None, "high"):
            raise ValueError("agent variant must be high; select other efforts with the model variant picker")
        agent_options = agent.get("options", {})
        if set(agent_options) - {"reasoningEffort"} or agent_options.get("reasoningEffort", "high") != "high":
            raise ValueError("unsupported agent provider options; retain high and use model-specific variants")
    for command in config.get("command", {}).values():
        if isinstance(command, dict) and command.get("model"):
            check_reference(command["model"])
    supplied = config.get("provider", {}).get("huggingface", {})
    expected = provider_config()
    for key in ("npm", "api", "env", "whitelist"):
        if key in supplied and supplied[key] != expected[key]:
            raise ValueError("Hugging Face provider routing conflicts with the reviewed contract")
    if set(supplied) - set(expected):
        raise ValueError("unsupported Hugging Face provider override")
    options = supplied.get("options", {})
    if set(options) - {"baseURL", "apiKey", "timeout", "headerTimeout", "chunkTimeout"}:
        raise ValueError("unsupported Hugging Face provider option")
    if options.get("baseURL", ENDPOINT) != ENDPOINT:
        raise ValueError("Hugging Face baseURL must be the official router endpoint")
    for key in ("timeout", "headerTimeout", "chunkTimeout"):
        if key in options and (type(options[key]) is not int or options[key] <= 0):
            raise ValueError("Hugging Face timeouts must be positive milliseconds")
    for key, model in supplied.get("models", {}).items():
        if key not in ROUTES:
            raise ValueError("unsupported Hugging Face model override")
        reviewed = expected["models"][key]
        if set(model) - set(reviewed):
            raise ValueError("unsupported Hugging Face model fields")
        for field in ("id", "provider", "reasoning", "tool_call", "interleaved", "reasoning_options", "variants"):
            if field in model and model[field] != reviewed.get(field):
                raise ValueError("model routing or reasoning conflicts with the reviewed contract")
        if set(model.get("options", {})) - {"reasoningEffort"}:
            raise ValueError("unsupported model option")
        effort = model.get("options", {}).get("reasoningEffort", "high")
        if effort not in ROUTES[key]["efforts"]:
            raise ValueError("unsupported model-specific reasoning effort")
    # Reject CLI overrides before starting the model, including --model=value.
    variant = None
    for index, arg in enumerate(args):
        if arg in ("-m", "--model", "--variant"):
            if index + 1 == len(args):
                raise ValueError("missing model or variant argument")
            value = args[index + 1]
        elif arg.startswith(("--model=", "--variant=")):
            value = arg.split("=", 1)[1]
        else:
            continue
        if arg in ("-m", "--model") or arg.startswith("--model="):
            route = check_reference(value)
        else:
            variant = value
    if variant and variant not in ROUTES[route]["efforts"]:
        raise ValueError("unsupported reasoning variant for the selected HF model")
    return route



def has_hf_auth(config, env):
    # Follow native API-key sources without printing or modifying credentials.
    # Called only in the workspace user's process, never by Terraform/setup.
    options = config.get("provider", {}).get("huggingface", {}).get("options", {})
    key = options.get("apiKey")
    if (isinstance(key, str) and key.strip()) or env.get("HF_TOKEN", "").strip():
        return True
    auth = None
    if env.get("OPENCODE_AUTH_CONTENT"):
        try:
            auth = json.loads(env["OPENCODE_AUTH_CONTENT"])
        except ValueError:
            pass  # Native also falls back to the saved file on invalid JSON.
    if auth is None:
        data = Path(env.get("XDG_DATA_HOME") or str(Path(env["HOME"]) / ".local/share"))
        try:
            auth = json.loads((data / "opencode/auth.json").read_text())
        except (OSError, ValueError):
            return False
    item = auth.get("huggingface", {}) if isinstance(auth, dict) else {}
    return isinstance(item, dict) and item.get("type") == "api" and isinstance(item.get("key"), str) and bool(item["key"].strip())


def launch(args):
    # A remote/long-running server could use a different config or bypass this
    # per-launch validation. These modes require a separate reviewed integration.
    tokens = iter(args)
    command = None
    for token in tokens:
        if token in ("--log-level", "--port", "--hostname", "--cors", "--model", "-m", "--agent", "--prompt"):
            next(tokens, None)
        elif not token.startswith("-"):
            command = token
            break
    if command in ("serve", "web", "acp", "upgrade") or any(
        arg in ("--attach", "--auto") or arg.startswith(("--attach=", "--auto=")) for arg in args
    ):
        raise ValueError("remote servers, attachment, auto approval and upgrades are outside this managed HF contract")
    env = dict(os.environ, OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_MODELS_FETCH="1")
    if args in (["--version"], ["--help"], ["-h"]):
        os.execve(BINARY, [BINARY, *args], env)
    # debug config resolves JSON/JSONC, project and inline precedence. Never echo
    # captured output: native configuration can contain interpolated credentials.
    result = subprocess.run([BINARY, "debug", "config"], env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("cannot resolve native config; repair OpenCode JSON/JSONC before launch")
    try:
        config = json.loads(result.stdout)
        validate_config(config, args)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, json.JSONDecodeError):
            raise ValueError("native configuration readback was not valid JSON") from None
        if isinstance(error, ValueError):
            raise
        raise ValueError("unsupported native configuration structure") from None
    if command not in ("auth", "models", "debug", "export", "import", "session", "stats", "completion") and not any(
        arg in ("--help", "-h") for arg in args
    ) and not has_hf_auth(config, env):
        raise ValueError("HF authentication is missing; when authorized, set HF_TOKEN or run opencode auth login for Hugging Face")
    # Overlay only the reviewed routing contract in memory, leaving operator
    # files intact. Preserve validated native auth/timeouts and effort choice.
    managed = provider_config()
    supplied = config.get("provider", {}).get("huggingface", {})
    managed["options"].update(supplied.get("options", {}))
    for route, model in supplied.get("models", {}).items():
        managed["models"][route]["options"].update(model.get("options", {}))
    inline = copy.deepcopy(config)
    inline.update({"enabled_providers": ["huggingface"], "small_model": config.get("small_model") or config["model"],
                   "autoupdate": False, "provider": {"huggingface": managed}})
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
    os.execve(BINARY, [BINARY, *args], env)


def main():
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--seed":
            seed(sys.argv[2], sys.argv[3])
        else:
            launch(sys.argv[1:])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Values in validation errors are deliberately never reflected.
        print("OpenCode HF configuration: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
