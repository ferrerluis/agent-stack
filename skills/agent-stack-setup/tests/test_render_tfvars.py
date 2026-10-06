#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "skills/agent-stack-setup/scripts/render_tfvars.py"
VARIABLES_TF = REPO_ROOT / "variables.tf"


def variable_default(name: str) -> str:
    source = VARIABLES_TF.read_text(encoding="utf-8")
    marker = f'variable "{name}"'
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for index in range(brace_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = source[brace_start + 1 : index]
                break
    else:
        raise AssertionError(f"unterminated variable block for {name}")

    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("default"):
            return stripped.split("=", 1)[1].strip()
    raise AssertionError(f"variable {name} has no default")


def run_render(
    answers: dict[str, object],
    expect_success: bool = True,
    base_tfvars: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        answers_path = temp_path / "answers.json"
        output_path = temp_path / "terraform.tfvars"
        answers_path.write_text(json.dumps(answers), encoding="utf-8")

        command = [
            sys.executable,
            str(SCRIPT),
            "render",
            "--repo-root",
            str(REPO_ROOT),
            "--answers",
            str(answers_path),
            "--output",
            str(output_path),
        ]
        if base_tfvars is not None:
            base_path = temp_path / "existing.tfvars"
            base_path.write_text(base_tfvars, encoding="utf-8")
            command.extend(["--base", str(base_path)])

        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )

        if expect_success and result.returncode != 0:
            raise AssertionError(result.stderr)
        if not expect_success and result.returncode == 0:
            raise AssertionError("render unexpectedly succeeded")

        output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return result, output


class RenderTfvarsTest(unittest.TestCase):
    def test_renders_fresh_aws_fixture(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": ["google/gemini-3-pro-preview"],
                "tailscale_auth_key": "tskey-auth-real-value",
            }
        )
        self.assertIn('cloud_provider = "aws"', rendered)
        self.assertIn('project_name = "agent-stack"', rendered)
        self.assertIn('repo_ssh_host_alias = "agent-stack"', rendered)
        self.assertIn('repo_ssh_private_key_path = ".ssh/id_ed25519_agent_stack"', rendered)
        self.assertIn('aws_ami_id = ""', rendered)
        self.assertIn("openclaw_swap_size_mb = 0", rendered)
        self.assertIn("openclaw_health_start_period_seconds = 120", rendered)
        self.assertIn("openclaw_health_retries = 8", rendered)
        self.assertIn('workspace_codex_release = "0.145.0"', rendered)
        self.assertIn("workspace_codex_auto_update_enabled = false", rendered)
        self.assertIn('workspace_codex_auto_update_timezone = "America/New_York"', rendered)
        self.assertIn('workspace_codex_auto_update_time = "04:00"', rendered)
        self.assertIn("workspace_codex_auto_recover_interrupted_turns = false", rendered)

    def test_renders_workspace_codex_hard_cutover_overrides(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
                "enabled_services": ["openclaw", "workspace"],
                "workspace_password": "workspace-password",
                "workspace_codex_auto_update_enabled": True,
                "workspace_codex_auto_update_timezone": "Europe/London",
                "workspace_codex_auto_update_time": "03:30",
                "workspace_codex_auto_recover_interrupted_turns": True,
            }
        )
        self.assertIn("workspace_codex_auto_update_enabled = true", rendered)
        self.assertIn('workspace_codex_auto_update_timezone = "Europe/London"', rendered)
        self.assertIn('workspace_codex_auto_update_time = "03:30"', rendered)
        self.assertIn("workspace_codex_auto_recover_interrupted_turns = true", rendered)

    def test_rejects_workspace_codex_auto_update_without_workspace_or_stable_fallback(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
                "workspace_codex_auto_update_enabled": True,
                "workspace_codex_release": "0.145.0-beta.1",
            },
            expect_success=False,
        )
        self.assertIn("requires enabled_services to include workspace", result.stderr)

        result, _ = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
                "enabled_services": ["openclaw", "workspace"],
                "workspace_password": "workspace-password",
                "workspace_codex_auto_update_enabled": True,
                "workspace_codex_release": "0.145.0-beta.1",
            },
            expect_success=False,
        )
        self.assertIn("stable x.y.z fallback", result.stderr)

    def test_rejects_workspace_codex_recovery_without_the_updater(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
                "enabled_services": ["openclaw", "workspace"],
                "workspace_password": "workspace-password",
                "workspace_codex_auto_recover_interrupted_turns": True,
            },
            expect_success=False,
        )
        self.assertIn("requires workspace_codex_auto_update_enabled", result.stderr)

    def test_renders_fresh_digitalocean_fixture(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "openai_api_key": "real-openai-key",
                "model_providers_enabled": ["openai"],
                "default_model": "openai/gpt-5.3",
                "fallback_models": ["openai/gpt-5.3-mini"],
                "tailscale_enabled": False,
            }
        )
        self.assertIn('cloud_provider = "digitalocean"', rendered)
        self.assertIn('do_token = "do-real-token"', rendered)
        self.assertIn("tailscale_enabled = false", rendered)
        self.assertIn('openai_auth_mode = "api_key"', rendered)

    def test_workspace_hash_render_and_conflicting_or_plaintext_inputs(self) -> None:
        hashed = "$6$fixturesalt12345$" + "a" * 86
        inputs = {"cloud_provider": "digitalocean", "do_token": "do-test-token", "openai_api_key": "test-openai-key",
                  "model_providers_enabled": ["openai"], "default_model": "openai/test-model", "fallback_models": [],
                  "tailscale_enabled": False, "enabled_services": ["workspace"], "workspace_password_hash": hashed}
        _, rendered = run_render(inputs)
        self.assertIn('workspace_password_hash = "' + hashed + '"', rendered)
        self.assertIn('workspace_password = ""', rendered)
        result, _ = run_render({**inputs, "workspace_password": "test-password"}, expect_success=False)
        self.assertIn("Supply only one", result.stderr)
        result, _ = run_render({**inputs, "workspace_password_hash": "test-plaintext-value"}, expect_success=False)
        self.assertIn("salted SHA-512", result.stderr)
        self.assertNotIn("test-plaintext-value", result.stderr)

    def test_renders_openai_codex_auth_mode_with_canonical_models(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "model_providers_enabled": ["openai"],
                "openai_auth_mode": "codex",
                "default_model": "openai/gpt-5.5",
                "fallback_models": ["openai/gpt-5.4-mini"],
                "openai_codex_auth_json_base64": "eyJmb28iOiAiYmFyIn0=",
                "tailscale_enabled": False,
            }
        )
        self.assertIn('openai_auth_mode = "codex"', rendered)
        self.assertIn('default_model = "openai/gpt-5.5"', rendered)
        self.assertIn('fallback_models = [\n  "openai/gpt-5.4-mini",\n]', rendered)
        self.assertIn('openai_api_key = ""', rendered)
        self.assertIn('openai_codex_auth_json_base64 = "eyJmb28iOiAiYmFyIn0="', rendered)

    def test_renders_fresh_hetzner_fixture(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "hetzner",
                "hcloud_token": "real-hcloud-token",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_enabled": False,
            }
        )
        self.assertIn('cloud_provider = "hetzner"', rendered)
        self.assertIn('hcloud_token = "real-hcloud-token"', rendered)
        self.assertIn('hcloud_server_type = "cpx21"', rendered)

    def test_renders_host_vpn_fixture(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "hetzner",
                "hcloud_token": "real-hcloud-token",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_enabled": False,
                "vpn_enabled": True,
                "vpn_openvpn_config_url": "https://downloads.nordcdn.com/configs/files/ovpn_udp/servers/us0000.nordvpn.com.udp.ovpn",
                "vpn_username": "nord-service-user",
                "vpn_password": "nord-service-password",
                "vpn_bypass_cidrs": ["203.0.113.5/32"],
            }
        )
        self.assertIn("vpn_enabled = true", rendered)
        self.assertIn('vpn_provider = "nordvpn_openvpn"', rendered)
        self.assertIn('vpn_openvpn_config_url = "https://downloads.nordcdn.com/configs/files/ovpn_udp/servers/us0000.nordvpn.com.udp.ovpn"', rendered)
        self.assertIn('vpn_username = "nord-service-user"', rendered)
        self.assertIn('vpn_password = "nord-service-password"', rendered)
        self.assertIn('vpn_bypass_cidrs = [\n  "203.0.113.5/32",\n]', rendered)

    def test_rejects_host_vpn_without_bypass_cidr(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "hetzner",
                "hcloud_token": "real-hcloud-token",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_enabled": False,
                "vpn_enabled": True,
                "vpn_openvpn_config_url": "https://downloads.nordcdn.com/configs/files/ovpn_udp/servers/us0000.nordvpn.com.udp.ovpn",
                "vpn_username": "nord-service-user",
                "vpn_password": "nord-service-password",
            },
            expect_success=False,
        )
        self.assertIn("vpn_bypass_cidrs", result.stderr)

    def test_renders_nordlynx_host_vpn_fixture(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "hetzner",
                "hcloud_token": "real-hcloud-token",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_enabled": False,
                "vpn_enabled": True,
                "vpn_provider": "nordvpn_nordlynx",
                "vpn_nordvpn_token": "real-nord-access-token",
                "vpn_nordvpn_connect_target": "United_States",
                "vpn_bypass_cidrs": ["203.0.113.5/32"],
            }
        )
        self.assertIn('vpn_provider = "nordvpn_nordlynx"', rendered)
        self.assertIn('vpn_nordvpn_token = "real-nord-access-token"', rendered)
        self.assertIn('vpn_nordvpn_connect_target = "United_States"', rendered)
        self.assertIn('vpn_openvpn_config_url = ""', rendered)
        self.assertIn('vpn_username = ""', rendered)
        self.assertIn('vpn_password = ""', rendered)

    def test_rejects_nordlynx_without_token(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "hetzner",
                "hcloud_token": "real-hcloud-token",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_enabled": False,
                "vpn_enabled": True,
                "vpn_provider": "nordvpn_nordlynx",
                "vpn_bypass_cidrs": ["203.0.113.5/32"],
            },
            expect_success=False,
        )
        self.assertIn("vpn_nordvpn_token is required", result.stderr)

    def test_rejects_tailscale_vpn_bypass_cidr(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "hetzner",
                "hcloud_token": "real-hcloud-token",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_enabled": False,
                "vpn_enabled": True,
                "vpn_provider": "nordvpn_nordlynx",
                "vpn_nordvpn_token": "real-nord-access-token",
                "vpn_bypass_cidrs": ["100.64.0.0/10"],
            },
            expect_success=False,
        )
        self.assertIn("must not contain or overlap Tailscale 100.64.0.0/10", result.stderr)

    def test_renders_existing_volume_fixture(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "aws_existing_volume_id": "vol-0123456789abcdef0",
                "openclaw_config_mode": "auto",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
            }
        )
        self.assertIn('aws_existing_volume_id = "vol-0123456789abcdef0"', rendered)
        self.assertIn('openclaw_config_mode = "auto"', rendered)

    def test_renders_all_services_defaults(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
            }
        )
        self.assertIn('enabled_services = [\n  "openclaw",\n  "hermes",\n  "n8n",\n]', rendered)
        self.assertIn('n8n_database_mode = "local_postgres"', rendered)
        self.assertIn('postgres_image = "postgres:17-alpine"', rendered)

    def test_rendered_defaults_match_variables_tf_authority(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
            }
        )
        for name in [
            "project_name",
            "host_codex_cli_enabled",
            "repo_ssh_host_alias",
            "repo_ssh_private_key_path",
            "openclaw_health_retries",
            "openai_auth_mode",
            "postgres_image",
            "workspace_codex_release",
            "workspace_codex_auto_update_enabled",
            "workspace_codex_auto_update_timezone",
            "workspace_codex_auto_update_time",
            "workspace_codex_auto_recover_interrupted_turns",
            "vpn_enabled",
            "vpn_provider",
            "vpn_disable_ipv6",
        ]:
            self.assertIn(f"{name} = {variable_default(name)}", rendered)

    def test_renders_openclaw_only_service_selection(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "enabled_services": ["openclaw"],
                "tailscale_auth_key": "tskey-auth-real-value",
            }
        )
        self.assertIn('enabled_services = [\n  "openclaw",\n]', rendered)

    def test_preserves_explicit_custom_ssh_values(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "gemini_api_key": "real-gemini-key",
                "model_providers_enabled": ["google"],
                "default_model": "google/gemini-3-flash-preview",
                "fallback_models": [],
                "project_name": "openclaw",
                "repo_ssh_host_alias": "custom-host",
                "repo_ssh_identity_file": "./.ssh/custom_identity",
                "repo_ssh_private_key_path": ".ssh/custom_identity",
                "tailscale_auth_key": "tskey-auth-real-value",
            }
        )
        self.assertIn('project_name = "openclaw"', rendered)
        self.assertIn('repo_ssh_host_alias = "custom-host"', rendered)
        self.assertIn('repo_ssh_private_key_path = ".ssh/custom_identity"', rendered)

    def test_allows_anthropic_api_key_without_legacy_auth_key(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "anthropic_api_key": "real-anthropic-api-key",
                "model_providers_enabled": ["anthropic"],
                "default_model": "anthropic/claude-haiku-4-5",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
            }
        )
        self.assertIn('anthropic_api_key = "real-anthropic-api-key"', rendered)
        self.assertIn('anthropic_auth_key = ""', rendered)

    def test_rejects_openai_codex_without_imported_auth(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "model_providers_enabled": ["openai"],
                "default_model": "openai-codex/gpt-5.4",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
            },
            expect_success=False,
        )
        self.assertIn("openai_codex_auth_json_base64", result.stderr)

    def test_rejects_openai_codex_auth_mode_without_imported_auth(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "model_providers_enabled": ["openai"],
                "openai_auth_mode": "codex",
                "default_model": "openai/gpt-5.5",
                "fallback_models": ["openai/gpt-5.4-mini"],
                "tailscale_enabled": False,
            },
            expect_success=False,
        )
        self.assertIn("openai_codex_auth_json_base64", result.stderr)

    def test_rejects_openai_api_key_auth_mode_without_api_key(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "model_providers_enabled": ["openai"],
                "openai_auth_mode": "api_key",
                "default_model": "openai/gpt-5.5",
                "fallback_models": ["openai/gpt-5.4-mini"],
                "tailscale_enabled": False,
            },
            expect_success=False,
        )
        self.assertIn("openai_api_key", result.stderr)

    def test_rejects_external_postgres_without_connection_values(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "openai_api_key": "real-openai-key",
                "model_providers_enabled": ["openai"],
                "default_model": "openai/gpt-5.3",
                "fallback_models": [],
                "n8n_database_mode": "external_postgres",
                "tailscale_enabled": False,
            },
            expect_success=False,
        )
        self.assertIn("external_postgres_host", result.stderr)

    def test_rejects_public_domain_without_domain_values(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "openai_api_key": "real-openai-key",
                "model_providers_enabled": ["openai"],
                "default_model": "openai/gpt-5.3",
                "fallback_models": [],
                "public_domain_enabled": True,
                "tailscale_enabled": False,
            },
            expect_success=False,
        )
        self.assertIn("public_domain_enabled", result.stderr)

    def test_renders_public_domain_with_base_domain(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "openai_api_key": "real-openai-key",
                "model_providers_enabled": ["openai"],
                "default_model": "openai/gpt-5.3",
                "fallback_models": [],
                "public_domain_enabled": True,
                "base_domain": "example.com",
                "tailscale_enabled": False,
            }
        )
        self.assertIn("public_domain_enabled = true", rendered)
        self.assertIn('base_domain = "example.com"', rendered)

    def test_renders_openai_codex_import(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "digitalocean",
                "do_token": "do-real-token",
                "model_providers_enabled": ["openai"],
                "default_model": "openai-codex/gpt-5.4",
                "fallback_models": ["openai-codex/gpt-5.3-codex"],
                "openai_codex_auth_json_base64": "eyJmb28iOiAiYmFyIn0=",
                "tailscale_enabled": False,
            }
        )
        self.assertIn('default_model = "openai-codex/gpt-5.4"', rendered)
        self.assertIn('openai_codex_auth_json_base64 = "eyJmb28iOiAiYmFyIn0="', rendered)

    def test_merge_base_preserves_existing_values(self) -> None:
        _, rendered = run_render(
            {
                "cloud_provider": "digitalocean",
                "default_model": "openai/gpt-5.3",
            },
            base_tfvars=(
                'cloud_provider = "digitalocean"\n'
                'do_token = "do-real-token"\n'
                'model_providers_enabled = ["openai"]\n'
                'default_model = "openai/gpt-5.4"\n'
                'fallback_models = ["openai/gpt-5.4-mini"]\n'
                'tailscale_enabled = false\n'
                'openai_api_key = "real-openai-key"\n'
            ),
        )
        self.assertIn('do_token = "do-real-token"', rendered)
        self.assertIn('openai_api_key = "real-openai-key"', rendered)
        self.assertIn('default_model = "openai/gpt-5.3"', rendered)
        self.assertIn('fallback_models = [\n  "openai/gpt-5.4-mini",\n]', rendered)

    def test_rejects_model_route_without_enabled_provider(self) -> None:
        result, _ = run_render(
            {
                "cloud_provider": "aws",
                "aws_access_key": "AKIAREALKEY123456",
                "aws_secret_key": "super-secret",
                "openai_codex_auth_json_base64": "eyJmb28iOiAiYmFyIn0=",
                "model_providers_enabled": ["google"],
                "default_model": "openai-codex/gpt-5.4",
                "fallback_models": [],
                "tailscale_auth_key": "tskey-auth-real-value",
            },
            expect_success=False,
        )
        self.assertIn("model_providers_enabled", result.stderr)

    def test_render_is_idempotent(self) -> None:
        answers = {
            "cloud_provider": "aws",
            "aws_access_key": "AKIAREALKEY123456",
            "aws_secret_key": "super-secret",
            "gemini_api_key": "real-gemini-key",
            "model_providers_enabled": ["google"],
            "default_model": "google/gemini-3-flash-preview",
            "fallback_models": ["google/gemini-3-pro-preview"],
            "tailscale_auth_key": "tskey-auth-real-value",
        }
        _, first = run_render(answers)
        _, second = run_render(answers)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
