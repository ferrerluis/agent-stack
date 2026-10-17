#!/usr/bin/env python3
"""Inspect the Terraform variable schema and render canonical terraform.tfvars."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PLACEHOLDER_VALUES = {
    "YOUR_AWS_ACCESS_KEY_ID",
    "YOUR_AWS_SECRET_ACCESS_KEY",
    "YOUR_DIGITALOCEAN_API_TOKEN",
    "YOUR_HETZNER_CLOUD_API_TOKEN",
    "sk-ant-api03-...",
    "sk-ant-oat01-...",
    "sk-...",
    "dop_v1_...",
    "hcloud_...",
    "AIzaSy...",
    "tskey-auth-...",
}


@dataclass
class VariableSpec:
    name: str
    section: str
    type_name: str
    description: str = ""
    has_default: bool = False
    default: Any = None
    sensitive: bool = False
    validations: list[str] = field(default_factory=list)
    allowed_values: list[str] = field(default_factory=list)
    has_example_suggestion: bool = False
    example_suggestion: Any = None
    example_is_placeholder: bool = False


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def is_section_heading(line: str) -> bool:
    if not line.startswith("# "):
        return False
    title = line[2:].strip()
    return bool(title) and any(char not in "─" for char in title)


def parse_hcl_literal(raw: str) -> Any:
    value = raw.strip()
    if value == "null":
        return None
    if value == "[]":
        return []
    if value.startswith("["):
        return [json.loads(match.group(0)) for match in re.finditer(r'"(?:[^"\\]|\\.)*"', value)]
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value == "true":
        return True
    if value == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    fail(f"unsupported HCL literal: {raw}")


def strip_inline_comment(line: str) -> str:
    result: list[str] = []
    in_quote = False
    escaped = False
    for char in line:
        if char == '"' and not escaped:
            in_quote = not in_quote
        if char == "#" and not in_quote:
            break
        result.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return "".join(result).rstrip()


def collect_assignment(lines: list[str], start_index: int, initial_value: str) -> tuple[str, int]:
    value_lines = [initial_value]
    depth = initial_value.count("[") - initial_value.count("]")
    index = start_index
    while depth > 0:
        index += 1
        if index >= len(lines):
            fail("unterminated list value in terraform.tfvars.example")
        next_line = strip_inline_comment(lines[index]).strip()
        if not next_line:
            continue
        value_lines.append(next_line)
        depth += next_line.count("[") - next_line.count("]")
    return "\n".join(value_lines).strip(), index


def parse_example_assignments(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    assignments: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        current = strip_inline_comment(lines[index]).strip()
        if not current:
            index += 1
            continue
        match = re.match(r"^([A-Za-z0-9_]+)\s*=\s*(.+)$", current)
        if not match:
            index += 1
            continue
        name = match.group(1)
        raw_value, index = collect_assignment(lines, index, match.group(2).strip())
        assignments[name] = parse_hcl_literal(raw_value)
        index += 1
    return assignments


def extract_validation_blocks(block_lines: list[str]) -> list[str]:
    blocks: list[str] = []
    index = 0
    while index < len(block_lines):
        stripped = block_lines[index].strip()
        if not stripped.startswith("validation"):
            index += 1
            continue
        start = index
        depth = block_lines[index].count("{") - block_lines[index].count("}")
        index += 1
        while index < len(block_lines) and depth > 0:
            depth += block_lines[index].count("{") - block_lines[index].count("}")
            index += 1
        blocks.append("\n".join(line.strip() for line in block_lines[start:index]))
    return blocks


def extract_variable_specs(path: Path) -> list[VariableSpec]:
    lines = path.read_text(encoding="utf-8").splitlines()
    specs: list[VariableSpec] = []
    current_section = "Ungrouped"
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if is_section_heading(stripped):
            current_section = stripped[2:].strip()
            index += 1
            continue
        match = re.match(r'^variable "([^"]+)" \{$', stripped)
        if not match:
            index += 1
            continue
        name = match.group(1)
        block_lines = [lines[index]]
        depth = lines[index].count("{") - lines[index].count("}")
        index += 1
        while index < len(lines) and depth > 0:
            block_lines.append(lines[index])
            depth += lines[index].count("{") - lines[index].count("}")
            index += 1
        block_text = "\n".join(block_lines)

        description_match = re.search(r'description\s*=\s*("(?:[^"\\]|\\.)*")', block_text)
        type_match = re.search(r"type\s*=\s*([^\n]+)", block_text)
        default_match = re.search(r"default\s*=\s*([^\n]+)", block_text)
        sensitive_match = re.search(r"sensitive\s*=\s*(true|false)", block_text)
        allowed_values_match = re.search(r"contains\(\[([^\]]+)\],", block_text)

        allowed_values: list[str] = []
        if allowed_values_match:
            allowed_values = re.findall(r'"([^"]+)"', allowed_values_match.group(1))

        spec = VariableSpec(
            name=name,
            section=current_section,
            type_name=type_match.group(1).strip() if type_match else "string",
            description=json.loads(description_match.group(1)) if description_match else "",
            has_default=default_match is not None,
            default=parse_hcl_literal(default_match.group(1).strip()) if default_match else None,
            sensitive=sensitive_match is not None and sensitive_match.group(1) == "true",
            validations=extract_validation_blocks(block_lines),
            allowed_values=allowed_values,
        )
        specs.append(spec)
    return specs


def is_placeholder_string(value: str) -> bool:
    return value in PLACEHOLDER_VALUES or value.startswith("YOUR_") or value.endswith("...")


def load_schema(repo_root: Path) -> list[VariableSpec]:
    specs = extract_variable_specs(repo_root / "variables.tf")
    example_assignments = parse_example_assignments(repo_root / "terraform.tfvars.example")
    for spec in specs:
        if spec.has_default:
            continue
        if spec.name not in example_assignments:
            continue
        spec.has_example_suggestion = True
        spec.example_suggestion = example_assignments[spec.name]
        if isinstance(spec.example_suggestion, str):
            spec.example_is_placeholder = is_placeholder_string(spec.example_suggestion)
    return specs


def value_to_hcl(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        rendered = ["["]
        for item in value:
            rendered.append(f"  {json.dumps(item)},")
        rendered.append("]")
        return "\n".join(rendered)
    fail(f"unsupported value type: {type(value).__name__}")


def validate_type(spec: VariableSpec, value: Any) -> None:
    if value is None and spec.has_default and spec.default is None:
        return
    if spec.type_name == "string":
        if not isinstance(value, str):
            fail(f"{spec.name} must be a string")
        return
    if spec.type_name == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fail(f"{spec.name} must be a number")
        return
    if spec.type_name == "bool":
        if not isinstance(value, bool):
            fail(f"{spec.name} must be a boolean")
        return
    if spec.type_name.startswith("list("):
        if not isinstance(value, list):
            fail(f"{spec.name} must be a list")
        if not all(isinstance(item, str) for item in value):
            fail(f"{spec.name} must be a list of strings")
        return
    fail(f"{spec.name} uses unsupported type {spec.type_name}")


def validate_value(spec: VariableSpec, value: Any) -> None:
    validate_type(spec, value)

    if isinstance(value, str):
        if spec.example_is_placeholder and value == spec.example_suggestion:
            fail(f"{spec.name} still uses the example placeholder value")
        if spec.sensitive and value and is_placeholder_string(value):
            fail(f"{spec.name} still looks like a placeholder value")
        if not spec.has_default and not value.strip():
            fail(f"{spec.name} is required and cannot be blank")

    if spec.allowed_values:
        if isinstance(value, list):
            invalid = [item for item in value if item not in spec.allowed_values]
            if invalid:
                fail(f"{spec.name} includes unsupported values: {', '.join(invalid)}")
        elif value not in spec.allowed_values:
            fail(f"{spec.name} must be one of: {', '.join(spec.allowed_values)}")

    if spec.name == "admin_username":
        if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value) or value == "root":
            fail("admin_username must be a valid Linux username and cannot be root")

    if spec.name == "model_providers_enabled" and not value:
        fail("model_providers_enabled must include at least one provider")

    if spec.name == "workspace_password_hash" and value and not re.fullmatch(
        r"\$6\$(?:rounds=[1-9][0-9]{3,8}\$)?[./A-Za-z0-9]{8,16}\$[./A-Za-z0-9]{86}", value
    ):
        fail("workspace_password_hash must be blank or a salted SHA-512 crypt hash")


def model_refs(values: dict[str, Any]) -> list[str]:
    refs = [str(values["default_model"])]
    refs.extend(str(item) for item in values["fallback_models"])
    if "hermes" in values["enabled_services"] and values["hermes_config_mode"] != "preserve" and str(values["hermes_model"]).strip():
        refs.append(str(values["hermes_model"]).strip())
    return refs


def uses_model_route(values: dict[str, Any], prefix: str) -> bool:
    return any(ref.startswith(f"{prefix}/") for ref in model_refs(values))


def provider_from_model_ref(model_ref: str) -> str:
    route = model_ref.split("/", 1)[0]
    if route == "openai-codex":
        return "openai"
    return route


def is_ipv4_cidr(value: str) -> bool:
    try:
        ipaddress.IPv4Network(value, strict=False)
    except ValueError:
        return False
    return "/" in value


def build_values(specs: list[VariableSpec], answers: dict[str, Any]) -> dict[str, Any]:
    known_names = {spec.name for spec in specs}
    unknown_names = sorted(name for name in answers if name not in known_names)
    if unknown_names:
        fail(f"unknown variables in answers file: {', '.join(unknown_names)}")

    resolved: dict[str, Any] = {}
    for spec in specs:
        if spec.name in answers and answers[spec.name] is not None:
            value = answers[spec.name]
        elif spec.has_default:
            value = spec.default
        else:
            fail(f"missing required value for {spec.name}")
        validate_value(spec, value)
        resolved[spec.name] = value

    if resolved["tailscale_enabled"] and not str(resolved["tailscale_auth_key"]).strip():
        fail("tailscale_auth_key is required when tailscale_enabled is true")
    if str(resolved["do_existing_volume_id"]).strip() and not str(resolved["do_existing_volume_name"]).strip():
        fail("do_existing_volume_name is required when do_existing_volume_id is set")

    if resolved["public_domain_enabled"] and not (
        str(resolved["base_domain"]).strip()
        or str(resolved["openclaw_domain"]).strip()
        or str(resolved["hermes_domain"]).strip()
        or str(resolved["n8n_domain"]).strip()
    ):
        fail("public_domain_enabled requires base_domain or at least one explicit service domain")
    if resolved["public_domain_enabled"] and resolved["ui_auth_mode"] != "basic":
        fail('public_domain_enabled supports ui_auth_mode = "basic" only')
    if resolved["n8n_database_mode"] == "external_postgres" and not (
        str(resolved["external_postgres_host"]).strip()
        and str(resolved["external_postgres_database"]).strip()
        and str(resolved["external_postgres_user"]).strip()
        and str(resolved["external_postgres_password"]).strip()
    ):
        fail(
            "n8n_database_mode = external_postgres requires external_postgres_host, "
            "external_postgres_database, external_postgres_user, and external_postgres_password"
        )

    harnesses = resolved["workspace_harnesses"]
    if "codex" not in harnesses or len(set(harnesses)) != len(harnesses):
        fail("workspace_harnesses must include codex and contain no duplicates")
    if len(harnesses) > 1 and "workspace" not in resolved["enabled_services"]:
        fail("optional workspace_harnesses require the workspace service")
    if resolved["workspace_claude_enabled"] or "claude_code" in harnesses:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", resolved["workspace_claude_release"]):
            fail("workspace_claude_release must be an exact x.y.z version when Claude Code is selected")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-R[0-9]+(?:\.[0-9]+)?", resolved["workspace_muse_release"]):
        fail("workspace_muse_release must be an exact Muse release")
    if ("meta" in resolved["model_providers_enabled"] or "muse_code" in harnesses) and not str(resolved["meta_api_key"]).strip():
        fail("meta_api_key is required when the Meta provider or Muse Code is selected")
    if any(ch in resolved["meta_api_key"] for ch in "\r\n'"):
        fail("meta_api_key must be a single-line API key without quotes")

    hf_token = resolved["huggingface_api_key"]
    if hf_token and (not re.fullmatch(r"hf_[A-Za-z0-9]+", hf_token) or "..." in hf_token):
        fail("huggingface_api_key must be an existing single-line hf_ token, not a placeholder")
    if "huggingface" in resolved["model_providers_enabled"] and not hf_token:
        fail("huggingface_api_key is required for Hugging Face; use an existing token with Inference Providers permission")
    catalog = json.loads((Path(__file__).resolve().parents[3] / "modules/common/runtime/huggingface-models.json").read_text())
    supported_hf = {"huggingface/" + model["id"] for model in catalog}
    if any(ref.startswith("huggingface/") and ref not in supported_hf for ref in model_refs(resolved)):
        fail("Use a reviewed Hugging Face route: huggingface/openai/gpt-oss-20b:groq or huggingface/openai/gpt-oss-120b:groq; arbitrary Hub models are not validated for agent tools")

    workspace_services = resolved["enabled_services"]
    workspace_auto_update = resolved["workspace_codex_auto_update_enabled"]
    workspace_recovery = resolved["workspace_codex_auto_recover_interrupted_turns"]
    workspace_release = str(resolved["workspace_codex_release"]).strip()
    if workspace_auto_update:
        if "workspace" not in workspace_services:
            fail("workspace_codex_auto_update_enabled requires enabled_services to include workspace")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", workspace_release):
            fail("workspace_codex_auto_update_enabled requires workspace_codex_release to be a stable x.y.z fallback")
    if workspace_recovery and not workspace_auto_update:
        fail("workspace_codex_auto_recover_interrupted_turns requires workspace_codex_auto_update_enabled")
    if workspace_recovery and "workspace" not in workspace_services:
        fail("workspace_codex_auto_recover_interrupted_turns requires enabled_services to include workspace")

    if resolved["workspace_password"] and resolved["workspace_password_hash"]:
        fail("Supply only one of workspace_password and workspace_password_hash")
    if "workspace" in workspace_services and not (
        str(resolved["workspace_password"]).strip() or resolved["workspace_password_hash"]
    ):
        fail("workspace_password or workspace_password_hash is required when workspace is enabled")

    if resolved["vpn_enabled"]:
        if resolved["vpn_provider"] == "nordvpn_openvpn":
            if not str(resolved["vpn_openvpn_config_url"]).strip().startswith("https://"):
                fail("vpn_openvpn_config_url must be an https:// URL for nordvpn_openvpn")
            if not str(resolved["vpn_username"]).strip():
                fail("vpn_username is required for nordvpn_openvpn")
            if not str(resolved["vpn_password"]).strip():
                fail("vpn_password is required for nordvpn_openvpn")
        elif resolved["vpn_provider"] == "nordvpn_nordlynx":
            if not str(resolved["vpn_nordvpn_token"]).strip():
                fail("vpn_nordvpn_token is required for nordvpn_nordlynx")
        if not resolved["vpn_bypass_cidrs"]:
            fail("vpn_bypass_cidrs must include at least one non-Tailscale access CIDR when vpn_enabled is true")
        invalid_vpn_bypass_cidrs = [cidr for cidr in resolved["vpn_bypass_cidrs"] if not is_ipv4_cidr(str(cidr))]
        if invalid_vpn_bypass_cidrs:
            fail(f"vpn_bypass_cidrs must contain IPv4 CIDRs: {', '.join(invalid_vpn_bypass_cidrs)}")
        tailscale_network = ipaddress.IPv4Network("100.64.0.0/10")
        tailscale_bypass_cidrs = [
            cidr
            for cidr in resolved["vpn_bypass_cidrs"]
            if ipaddress.IPv4Network(str(cidr), strict=False).overlaps(tailscale_network)
        ]
        if tailscale_bypass_cidrs:
            fail(
                "vpn_bypass_cidrs must not contain or overlap Tailscale 100.64.0.0/10; "
                "AgentStack preserves Tailscale separately"
            )

    if not re.fullmatch(r"[A-Za-z0-9_-]*", str(resolved["vpn_nordvpn_connect_target"]).strip()):
        fail("vpn_nordvpn_connect_target may contain only letters, numbers, underscores, and hyphens")

    configured_providers = set(resolved["model_providers_enabled"])
    missing_providers = sorted(
        {provider_from_model_ref(ref) for ref in model_refs(resolved)} - configured_providers
    )
    if missing_providers:
        fail(
            "model_providers_enabled must include every provider referenced by default_model "
            f"or fallback_models: {', '.join(missing_providers)}"
        )

    if uses_model_route(resolved, "anthropic") and not (
        str(resolved["anthropic_api_key"]).strip() or str(resolved["anthropic_auth_key"]).strip()
    ):
        fail("anthropic_api_key is required when any configured model uses anthropic/*")
    if uses_model_route(resolved, "openai"):
        if resolved["openai_auth_mode"] == "codex":
            if not str(resolved["openai_codex_auth_json_base64"]).strip():
                fail(
                    "openai_codex_auth_json_base64 is required when openai_auth_mode = codex "
                    "and any configured model uses openai/*; run `codex login` and import ~/.codex/auth.json first"
                )
        elif not str(resolved["openai_api_key"]).strip():
            fail("openai_api_key is required when openai_auth_mode = api_key and any configured model uses openai/*")
    if uses_model_route(resolved, "openai-codex") and not str(resolved["openai_codex_auth_json_base64"]).strip():
        fail(
            "openai_codex_auth_json_base64 is required when any configured model uses "
            "openai-codex/*; run `codex login` and import ~/.codex/auth.json first"
        )
    if uses_model_route(resolved, "google") and not str(resolved["gemini_api_key"]).strip():
        fail("gemini_api_key is required when any configured model uses google/*")
    if uses_model_route(resolved, "groq") and not str(resolved["groq_api_key"]).strip():
        fail("groq_api_key is required when any configured model uses groq/*")

    return resolved


def render_tfvars(specs: list[VariableSpec], values: dict[str, Any]) -> str:
    lines = [
        "# Generated by skills/agent-stack-setup/scripts/render_tfvars.py",
        "# Source of truth: variables.tf",
    ]
    current_section = None
    for spec in specs:
        if spec.section != current_section:
            current_section = spec.section
            lines.extend(["", f"# {current_section}", ""])
        rendered = value_to_hcl(values[spec.name])
        if "\n" in rendered:
            lines.append(f"{spec.name} = {rendered}")
        else:
            lines.append(f"{spec.name} = {rendered}")
    return "\n".join(lines).rstrip() + "\n"


def command_inspect(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    schema = load_schema(repo_root)
    sections: list[dict[str, Any]] = []
    current_section = None
    current_payload: dict[str, Any] | None = None

    for spec in schema:
        if spec.section != current_section:
            current_section = spec.section
            current_payload = {"name": current_section, "variables": []}
            sections.append(current_payload)
        assert current_payload is not None
        current_payload["variables"].append(
            {
                "name": spec.name,
                "type": spec.type_name,
                "description": spec.description,
                "has_default": spec.has_default,
                "default": spec.default,
                "sensitive": spec.sensitive,
                "allowed_values": spec.allowed_values,
                "validations": spec.validations,
                "has_example_suggestion": spec.has_example_suggestion,
                "example_suggestion": spec.example_suggestion,
                "example_is_placeholder": spec.example_is_placeholder,
            }
        )

    json.dump({"repo_root": str(repo_root), "sections": sections}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def read_answers(path: str) -> dict[str, Any]:
    if path == "-":
        payload = json.load(sys.stdin)
    else:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        fail("answers payload must be a JSON object")
    return payload


def read_base_values(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return parse_example_assignments(Path(path))


def command_render(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    specs = load_schema(repo_root)
    answers = read_answers(args.answers)
    base_values = read_base_values(args.base)
    merged_answers = dict(base_values)
    merged_answers.update(answers)
    values = build_values(specs, merged_answers)
    output = render_tfvars(specs, values)
    Path(args.output).write_text(output, encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Print the current variable schema as JSON")
    inspect_parser.add_argument("--repo-root", default=".", help="Path to the repository root")
    inspect_parser.set_defaults(func=command_inspect)

    render_parser = subparsers.add_parser("render", help="Render canonical terraform.tfvars")
    render_parser.add_argument("--repo-root", default=".", help="Path to the repository root")
    render_parser.add_argument("--answers", required=True, help="Path to a JSON object with variable answers, or - for stdin")
    render_parser.add_argument(
        "--base",
        help="Optional existing terraform.tfvars file to merge with before applying answer overrides",
    )
    render_parser.add_argument("--output", required=True, help="Path to the terraform.tfvars file to write")
    render_parser.set_defaults(func=command_render)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
