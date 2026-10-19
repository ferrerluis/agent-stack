mock_provider "google" {}

override_module {
  target = module.aws
  outputs = {
    instance_public_ip = "203.0.113.10"
    instance_id        = "i-validation-aws"
    ebs_volume_id      = "vol-validation-aws"
    ssh_command        = "ssh admin@203.0.113.10"
  }
}

override_module {
  target = module.digitalocean
  outputs = {
    instance_public_ip = "203.0.113.20"
    droplet_id         = 4242
    volume_name        = "agent-stack-data"
    volume_id          = "do-volume-validation"
    ssh_command        = "ssh admin@203.0.113.20"
  }
}

override_module {
  target = module.hetzner
  outputs = {
    instance_public_ip = "203.0.113.30"
    server_id          = 31337
    volume_id          = "31338"
    ssh_command        = "ssh admin@203.0.113.30"
  }
}

override_module {
  target = module.gcp
  outputs = {
    instance_public_ip = "203.0.113.40"
    instance_id        = "projects/agent-stack-tests/zones/us-east1-b/instances/agent-stack"
    volume_id          = "projects/agent-stack-tests/zones/us-east1-b/disks/agent-stack-data"
    device_name        = "agent-stack-data"
  }
}

variables {
  project_name                                   = "agent-stack"
  admin_ssh_host_override                        = ""
  workspace_ssh_host_override                    = ""
  admin_password                                 = ""
  admin_password_ssh_scope                       = "disabled"
  ssh_public_key                                 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAgentStackTerraformPlanTestsOnly agent-stack-tests"
  repo_ssh_private_key_path                      = "tests/fixtures/fake_ssh_private_key.txt"
  generate_repo_ssh_config                       = false
  aws_access_key                                 = "AKIAAGENTSTACKTESTS"
  aws_secret_key                                 = "agent-stack-tests"
  do_token                                       = "do-agent-stack-tests"
  hcloud_token                                   = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  model_providers_enabled                        = ["google"]
  default_model                                  = "google/gemini-3-flash-preview"
  fallback_models                                = []
  gemini_api_key                                 = "gemini-agent-stack-tests"
  gateway_token                                  = "gateway-token-agent-stack-tests"
  hermes_api_server_key                          = "hermes-key-agent-stack-tests"
  n8n_encryption_key                             = "n8n-key-agent-stack-tests"
  postgres_password                              = "postgres-password-agent-stack-tests"
  ui_auth_password                               = "ui-password-agent-stack-tests"
  enabled_services                               = ["openclaw", "hermes", "n8n"]
  workspace_codex_release                        = "0.145.0"
  workspace_codex_auto_update_enabled            = false
  workspace_codex_auto_update_timezone           = "America/New_York"
  workspace_codex_auto_update_time               = "04:00"
  workspace_codex_auto_recover_interrupted_turns = false
  vpn_enabled                                    = false
  vpn_provider                                   = "nordvpn_openvpn"
  vpn_nordvpn_token                              = ""
  vpn_nordvpn_connect_target                     = ""
  vpn_openvpn_config_url                         = ""
  vpn_username                                   = ""
  vpn_password                                   = ""
  vpn_bypass_cidrs                               = []
  tailscale_enabled                              = false
  tailscale_auth_key                             = ""
  openai_codex_auth_json_base64                  = ""
}

run "codex_only_has_no_opencode" {
  command = plan
  assert {
    condition     = !strcontains(local.runtime_workspace_dockerfile, "ARG OPENCODE_RELEASE") && !strcontains(local.runtime_workspace_entrypoint, "agent-stack-opencode.py --seed")
    error_message = "OpenCode must be entirely opt in."
  }
}

run "opencode_independent_of_chief_model_and_optional_tools" {
  command = plan
  variables {
    enabled_services         = ["workspace"]
    workspace_password       = "fixture-workspace-password"
    workspace_harnesses      = ["codex", "opencode"]
    workspace_opencode_model = "huggingface/moonshotai/Kimi-K3:baseten"
  }
  assert {
    condition     = strcontains(local.runtime_workspace_dockerfile, "ARG OPENCODE_RELEASE=1.19.3") && strcontains(local.runtime_workspace_dockerfile, "sha256sum -c -") && strcontains(local.runtime_workspace_dockerfile, "ARG CODEX_RELEASE") && !strcontains(local.runtime_workspace_dockerfile, "ARG CLAUDE_RELEASE") && !strcontains(local.runtime_workspace_dockerfile, "ARG MUSE_RELEASE")
    error_message = "Codex must remain mandatory and optional CLIs independent."
  }
  assert {
    condition     = strcontains(local.runtime_workspace_entrypoint, "'huggingface/moonshotai/Kimi-K3:baseten'") && local.runtime_template_vars.workspace_opencode_model == local.proof_runtime_template_vars.workspace_opencode_model && local.proof_runtime_template_vars.workspace_opencode_enabled
    error_message = "Model config and export/proof recipes must reflect OpenCode selection."
  }
  assert {
    condition     = var.default_model == "google/gemini-3-flash-preview" && !strcontains(nonsensitive(local.runtime_workspace_env), "OPENAI_API_KEY") && !strcontains(local.runtime_workspace_dockerfile, "gemini-agent-stack-tests")
    error_message = "OpenCode must not inherit or bake personal-agent credentials or change the chief model."
  }
}

run "all_harnesses_coexist" {
  command = plan
  variables {
    enabled_services         = ["workspace"]
    workspace_password       = "fixture-workspace-password"
    workspace_harnesses      = ["codex", "claude_code", "muse_code", "opencode"]
    workspace_claude_release = "2.1.301"
    workspace_opencode_model = "huggingface/meta-models/Muse-Spark-Open-816B-A42B:baseten"
    meta_api_key             = "muse-fixture-key"
  }
  assert {
    condition     = strcontains(local.runtime_workspace_dockerfile, "ARG OPENCODE_RELEASE") && strcontains(local.runtime_workspace_dockerfile, "ARG CLAUDE_RELEASE") && strcontains(local.runtime_workspace_dockerfile, "ARG MUSE_RELEASE")
    error_message = "All optional harnesses must coexist."
  }
}

run "requires_explicit_opencode_model" {
  command = plan
  variables {
    enabled_services    = ["workspace"]
    workspace_password  = "fixture-workspace-password"
    workspace_harnesses = ["codex", "opencode"]
  }
  expect_failures = [var.workspace_opencode_model]
}

run "requires_workspace_for_opencode" {
  command = plan
  variables {
    workspace_harnesses      = ["codex", "opencode"]
    workspace_opencode_model = "huggingface/moonshotai/Kimi-K3:baseten"
  }
  expect_failures = [var.workspace_harnesses]
}

run "rejects_v2_release" {
  command = plan
  variables { workspace_opencode_release = "2.0.0" }
  expect_failures = [var.workspace_opencode_release]
}

run "rejects_mutable_release" {
  command = plan
  variables { workspace_opencode_release = "latest" }
  expect_failures = [var.workspace_opencode_release]
}

run "rejects_unsafe_model" {
  command = plan
  variables { workspace_opencode_model = "openai/model'" }
  expect_failures = [var.workspace_opencode_model]
}

run "codex_mandatory_with_opencode" {
  command = plan
  variables {
    enabled_services         = ["workspace"]
    workspace_password       = "fixture-workspace-password"
    workspace_harnesses      = ["opencode"]
    workspace_opencode_model = "huggingface/moonshotai/Kimi-K3:baseten"
  }
  expect_failures = [var.workspace_harnesses]
}

run "deepseek_exact_hosted_revision" {
  command = plan
  variables {
    enabled_services         = ["workspace"]
    workspace_password       = "fixture-workspace-password"
    workspace_harnesses      = ["codex", "opencode"]
    workspace_opencode_model = "huggingface/deepseek-ai/DeepSeek-V4.1-Flash:baseten"
  }
  assert {
    condition     = strcontains(local.runtime_workspace_entrypoint, "huggingface/deepseek-ai/DeepSeek-V4.1-Flash:baseten")
    error_message = "The exact hosted model revision and provider must survive rendering."
  }
}

run "rejects_non_hf_model" {
  command = plan
  variables { workspace_opencode_model = "openai/gpt-4.1" }
  expect_failures = [var.workspace_opencode_model]
}

run "rejects_automatic_hf_routing" {
  command = plan
  variables { workspace_opencode_model = "huggingface/moonshotai/Kimi-K3" }
  expect_failures = [var.workspace_opencode_model]
}

run "rejects_unhosted_deepseek_revision" {
  command = plan
  variables { workspace_opencode_model = "huggingface/deepseek-ai/DeepSeek-V4.1-Flash-1017:baseten" }
  expect_failures = [var.workspace_opencode_model]
}
