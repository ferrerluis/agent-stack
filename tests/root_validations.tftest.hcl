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

run "rejects_unknown_cloud_provider" {
  command = plan

  variables {
    cloud_provider = "azure"
  }

  expect_failures = [
    var.cloud_provider,
  ]
}

run "rejects_openclaw_host_network_without_native_tailnet" {
  command = plan
  variables {
    openclaw_host_network_enabled = true
    tailscale_enabled             = false
  }
  expect_failures = [var.openclaw_host_network_enabled]
}

run "rejects_openclaw_host_network_with_sidecar_tailnet" {
  command = plan
  variables {
    openclaw_host_network_enabled = true
    tailscale_enabled             = true
    tailscale_mode                = "sidecar"
    tailscale_auth_key            = "tskey-auth-agent-stack-tests"
  }
  expect_failures = [var.openclaw_host_network_enabled]
}

run "accepts_openclaw_host_network_with_loopback_native_tailnet" {
  command = plan
  variables {
    openclaw_host_network_enabled = true
    tailscale_enabled             = true
    tailscale_mode                = "host"
    tailscale_auth_key            = "tskey-auth-agent-stack-tests"
    public_domain_enabled         = false
    hermes_dashboard_password     = "hermes-password-agent-stack-tests"
    hermes_dashboard_secret       = "hermes-session-secret-agent-stack-tests"
  }
  assert {
    condition     = strcontains(nonsensitive(local.runtime_env), "OPENCLAW_GATEWAY_BIND=loopback") && strcontains(split("\n  hermes:", local.runtime_docker_compose)[0], "network_mode: \"host\"") && !strcontains(split("\n  hermes:", local.runtime_docker_compose)[0], "ports:")
    error_message = "Opt-in OpenClaw host networking must bind loopback without Docker port publication."
  }
}

run "rejects_root_admin_user" {
  command = plan

  variables {
    admin_username = "root"
  }

  expect_failures = [
    var.admin_username,
  ]
}

run "rejects_tailscale_without_auth_key" {
  command = plan

  variables {
    tailscale_enabled  = true
    tailscale_auth_key = ""
  }

  expect_failures = [
    var.tailscale_auth_key,
  ]
}

run "rejects_vpn_without_config_url" {
  command = plan

  variables {
    vpn_enabled      = true
    vpn_username     = "nord-service-user"
    vpn_password     = "nord-service-password"
    vpn_bypass_cidrs = ["203.0.113.5/32"]
  }

  expect_failures = [
    var.vpn_openvpn_config_url,
  ]
}

run "rejects_vpn_without_credentials" {
  command = plan

  variables {
    vpn_enabled            = true
    vpn_openvpn_config_url = "https://downloads.nordcdn.com/configs/files/ovpn_udp/servers/us0000.nordvpn.com.udp.ovpn"
    vpn_bypass_cidrs       = ["203.0.113.5/32"]
  }

  expect_failures = [
    var.vpn_username,
    var.vpn_password,
  ]
}

run "rejects_vpn_without_bypass_cidrs" {
  command = plan

  variables {
    vpn_enabled            = true
    vpn_openvpn_config_url = "https://downloads.nordcdn.com/configs/files/ovpn_udp/servers/us0000.nordvpn.com.udp.ovpn"
    vpn_username           = "nord-service-user"
    vpn_password           = "nord-service-password"
  }

  expect_failures = [
    var.vpn_bypass_cidrs,
  ]
}

run "accepts_nordlynx_with_token_and_public_bypass" {
  command = plan

  variables {
    vpn_enabled                = true
    vpn_provider               = "nordvpn_nordlynx"
    vpn_nordvpn_token          = "nord-access-token-agent-stack-tests"
    vpn_nordvpn_connect_target = "United_States"
    vpn_bypass_cidrs           = ["203.0.113.5/32"]
  }
}

run "rejects_nordlynx_without_token" {
  command = plan

  variables {
    vpn_enabled      = true
    vpn_provider     = "nordvpn_nordlynx"
    vpn_bypass_cidrs = ["203.0.113.5/32"]
  }

  expect_failures = [
    var.vpn_nordvpn_token,
  ]
}

run "rejects_tailscale_vpn_bypass" {
  command = plan

  variables {
    vpn_bypass_cidrs = ["100.64.0.0/10"]
  }

  expect_failures = [
    var.vpn_bypass_cidrs,
  ]
}

run "rejects_vpn_bypass_supernet_overlapping_tailscale" {
  command = plan

  variables {
    vpn_bypass_cidrs = ["100.0.0.0/8"]
  }

  expect_failures = [
    var.vpn_bypass_cidrs,
  ]
}

run "rejects_invalid_nordlynx_target" {
  command = plan

  variables {
    vpn_nordvpn_connect_target = "United States"
  }

  expect_failures = [
    var.vpn_nordvpn_connect_target,
  ]
}

run "rejects_vpn_non_cidr_bypass" {
  command = plan

  variables {
    vpn_bypass_cidrs = ["203.0.113.5"]
  }

  expect_failures = [
    var.vpn_bypass_cidrs,
  ]
}

run "rejects_admin_password_without_scope" {
  command = plan

  variables {
    admin_password = "agent-stack-admin-password-tests"
  }

  expect_failures = [
    var.admin_password_ssh_scope,
  ]
}

run "rejects_weak_admin_password" {
  command = plan

  variables {
    admin_password           = "short"
    admin_password_ssh_scope = "public"
    allowed_ssh_cidr         = "203.0.113.5/32"
  }

  expect_failures = [
    var.admin_password,
  ]
}

run "rejects_tailnet_admin_password_without_host_tailscale" {
  command = plan

  variables {
    admin_password           = "agent-stack-admin-password-tests"
    admin_password_ssh_scope = "tailnet"
    tailscale_enabled        = true
    tailscale_mode           = "sidecar"
    tailscale_auth_key       = "tskey-auth-agent-stack-tests"
  }

  expect_failures = [
    var.admin_password_ssh_scope,
  ]
}

run "rejects_public_admin_password_with_open_ssh_cidr" {
  command = plan

  variables {
    admin_password           = "agent-stack-admin-password-tests"
    admin_password_ssh_scope = "public"
    allowed_ssh_cidr         = "0.0.0.0/0"
  }

  expect_failures = [
    var.admin_password_ssh_scope,
  ]
}

run "rejects_public_domain_without_domains" {
  command = plan

  variables {
    public_domain_enabled = true
  }

  expect_failures = [
    terraform_data.input_validation,
  ]
}

run "rejects_external_postgres_without_connection_values" {
  command = plan

  variables {
    n8n_database_mode = "external_postgres"
  }

  expect_failures = [
    terraform_data.input_validation,
  ]
}

run "rejects_digitalocean_existing_volume_without_name" {
  command = plan

  variables {
    do_existing_volume_id = "do-volume-without-name"
  }

  expect_failures = [
    var.do_existing_volume_id,
  ]
}

run "rejects_empty_model_providers" {
  command = plan

  variables {
    model_providers_enabled = []
  }

  expect_failures = [
    var.model_providers_enabled,
  ]
}

run "rejects_empty_default_model" {
  command = plan

  variables {
    default_model = ""
  }

  expect_failures = [
    var.default_model,
  ]
}

run "rejects_invalid_enabled_service" {
  command = plan

  variables {
    enabled_services = ["openclaw", "redis"]
  }

  expect_failures = [
    var.enabled_services,
  ]
}

run "rejects_workspace_without_password" {
  command = plan

  variables {
    enabled_services   = ["openclaw", "workspace"]
    workspace_password = ""
  }

  expect_failures = [
    var.workspace_password,
  ]
}

run "rejects_workspace_private_key_path_in_public_keys" {
  command = plan

  variables {
    workspace_ssh_public_keys = ["/Users/alice/.ssh/id_ed25519"]
  }

  expect_failures = [
    var.workspace_ssh_public_keys,
  ]
}

run "accepts_workspace_password_hash_without_plaintext" {
  command = plan

  variables {
    enabled_services        = ["openclaw", "workspace"]
    workspace_password      = ""
    workspace_password_hash = "$6$fixturesalt12345$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }

  assert {
    condition     = strcontains(local.runtime_workspace_env, "WORKSPACE_PASSWORD_HASH='$6$fixturesalt12345$") && strcontains(local.runtime_workspace_env, "WORKSPACE_PASSWORD=\n")
    error_message = "The hash must retain literal dollar signs and the plaintext workspace password must be empty."
  }
}

run "rejects_workspace_password_and_hash_together" {
  command = plan

  variables {
    workspace_password      = "tests-only-password"
    workspace_password_hash = "$6$fixturesalt12345$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }

  expect_failures = [var.workspace_password]
}

run "rejects_plaintext_in_workspace_password_hash" {
  command = plan

  variables {
    workspace_password_hash = "tests-only-plaintext"
  }

  expect_failures = [var.workspace_password_hash]
}

run "rejects_workspace_codex_auto_update_without_workspace_service" {
  command = plan

  variables {
    workspace_codex_auto_update_enabled = true
  }

  expect_failures = [
    var.workspace_codex_auto_update_enabled,
  ]
}

run "accepts_workspace_codex_auto_update_with_workspace_service" {
  command = plan

  variables {
    enabled_services                    = ["openclaw", "workspace"]
    workspace_password                  = "workspace-password-agent-stack-tests"
    workspace_codex_auto_update_enabled = true
  }
}

run "rejects_workspace_codex_auto_update_with_prerelease_fallback" {
  command = plan

  variables {
    enabled_services                    = ["openclaw", "workspace"]
    workspace_password                  = "workspace-password-agent-stack-tests"
    workspace_codex_auto_update_enabled = true
    workspace_codex_release             = "0.145.0-beta.1"
  }

  expect_failures = [
    var.workspace_codex_auto_update_enabled,
  ]
}

run "rejects_invalid_workspace_codex_auto_update_timezone" {
  command = plan

  variables {
    workspace_codex_auto_update_timezone = "America New_York"
  }

  expect_failures = [
    var.workspace_codex_auto_update_timezone,
  ]
}

run "rejects_invalid_workspace_codex_auto_update_time" {
  command = plan

  variables {
    workspace_codex_auto_update_time = "4:00"
  }

  expect_failures = [
    var.workspace_codex_auto_update_time,
  ]
}

run "rejects_workspace_codex_auto_recovery_without_auto_update" {
  command = plan

  variables {
    workspace_codex_auto_recover_interrupted_turns = true
  }

  expect_failures = [
    var.workspace_codex_auto_recover_interrupted_turns,
  ]
}

run "rejects_workspace_codex_auto_update_and_recovery_without_workspace_service" {
  command = plan

  variables {
    enabled_services                               = ["openclaw"]
    workspace_codex_auto_update_enabled            = true
    workspace_codex_auto_recover_interrupted_turns = true
  }

  expect_failures = [
    var.workspace_codex_auto_update_enabled,
  ]
}

run "accepts_workspace_codex_auto_recovery_with_workspace_service" {
  command = plan

  variables {
    enabled_services                               = ["openclaw", "workspace"]
    workspace_password                             = "workspace-password-agent-stack-tests"
    workspace_codex_auto_update_enabled            = true
    workspace_codex_auto_update_timezone           = "Europe/London"
    workspace_codex_auto_update_time               = "03:30"
    workspace_codex_auto_recover_interrupted_turns = true
  }
}

run "rejects_floating_workspace_codex_release" {
  command = plan

  variables {
    workspace_codex_release = "latest"
  }

  expect_failures = [
    var.workspace_codex_release,
  ]
}

run "rejects_invalid_tailscale_mode" {
  command = plan

  variables {
    tailscale_mode = "container"
  }

  expect_failures = [
    var.tailscale_mode,
  ]
}

run "rejects_missing_gcp_project" {
  command = plan
  variables { cloud_provider = "gcp" }
  expect_failures = [var.gcp_project_id]
}
run "rejects_wrong_gcp_zone" {
  command = plan
  variables {
    cloud_provider = "gcp"
    gcp_project_id = "agent-stack-tests"
    gcp_zone       = "us-west1-a"
  }
  expect_failures = [var.gcp_zone]
}
run "rejects_pinned_nonstandard_gcp_image" {
  command = plan
  variables {
    gcp_pin_boot_image = true
    gcp_image          = "foreign-project/image"
  }
  expect_failures = [var.gcp_pin_boot_image]
}
run "rejects_pinned_nonstandard_hetzner_image" {
  command = plan
  variables {
    hcloud_pin_boot_image = true
    hcloud_image          = "debian-12"
  }
  expect_failures = [var.hcloud_pin_boot_image]
}
run "rejects_unpinned_claude" {
  command = plan
  variables {
    enabled_services         = ["workspace"]
    workspace_password       = "tests-only-long-password"
    workspace_claude_enabled = true
    workspace_claude_release = "latest"
  }
  expect_failures = [var.workspace_claude_release]
}
