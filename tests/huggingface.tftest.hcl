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

run "hf_exact_routes_and_runtime_exports" {
  command = plan
  variables {
    model_providers_enabled   = ["huggingface"]
    huggingface_api_key       = "hf_OfflineFixtureOnly"
    hermes_dashboard_password = "hermes-dashboard-test-fixture"
    hermes_dashboard_secret   = "hermes-dashboard-test-secret"
    default_model             = "huggingface/openai/gpt-oss-20b:groq"
    fallback_models           = ["huggingface/openai/gpt-oss-120b:groq"]
  }
  assert {
    condition     = strcontains(nonsensitive(local.runtime_env), "HF_TOKEN='hf_OfflineFixtureOnly'") && !strcontains(nonsensitive(local.runtime_workspace_env), "hf_OfflineFixtureOnly") && !strcontains(nonsensitive(local.runtime_workspace_dockerfile), "hf_OfflineFixtureOnly")
    error_message = "HF credentials must reach agent runtime without entering workspace coding harnesses or build layers."
  }
  assert {
    condition     = jsondecode(local.runtime_hermes_seed).model_ref == var.default_model && tolist(jsondecode(local.runtime_hermes_seed).fallback_models) == var.fallback_models
    error_message = "Nested HF IDs and provider suffixes must survive Hermes seeding."
  }
  assert {
    condition     = strcontains(nonsensitive(local.runtime_installer), "openai/gpt-oss-120b:groq") && strcontains(nonsensitive(local.runtime_installer), "verify_huggingface_model_config")
    error_message = "Bootstrap must include reviewed provider metadata and exact model readback."
  }
}

run "hf_requires_existing_token" {
  command = plan
  variables {
    model_providers_enabled = ["huggingface"]
    default_model           = "huggingface/openai/gpt-oss-20b:groq"
  }
  expect_failures = [terraform_data.input_validation]
}

run "hf_rejects_token_injection" {
  command = plan
  variables { huggingface_api_key = "hf_secret\nINJECTED=true" }
  expect_failures = [var.huggingface_api_key]
}

run "hf_requires_selected_provider" {
  command = plan
  variables { fallback_models = ["huggingface/openai/gpt-oss-20b:groq"] }
  expect_failures = [terraform_data.input_validation]
}

run "hf_rejects_arbitrary_hub_weights" {
  command = plan
  variables {
    model_providers_enabled = ["huggingface"]
    huggingface_api_key     = "hf_OfflineFixtureOnly"
    default_model           = "huggingface/organization/arbitrary-weights:groq"
  }
  expect_failures = [terraform_data.input_validation]
}

run "hf_rejects_automatic_provider_policy" {
  command = plan
  variables {
    model_providers_enabled = ["huggingface"]
    huggingface_api_key     = "hf_OfflineFixtureOnly"
    default_model           = "huggingface/openai/gpt-oss-120b:cheapest"
  }
  expect_failures = [terraform_data.input_validation]
}

run "hf_hermes_override_requires_provider_and_token" {
  command = plan
  variables { hermes_model = "huggingface/openai/gpt-oss-20b:groq" }
  expect_failures = [terraform_data.input_validation]
}

run "hf_disabled_hermes_override_is_unused" {
  command = plan
  variables {
    enabled_services = ["openclaw"]
    hermes_model     = "huggingface/org/unused:provider"
  }
}

run "hf_preserved_hermes_override_is_unused" {
  command = plan
  variables {
    hermes_config_mode = "preserve"
    hermes_model       = "huggingface/org/unused:provider"
  }
}
