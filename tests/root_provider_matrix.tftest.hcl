mock_provider "google" {}

override_module {
  target = module.aws
  outputs = {
    instance_public_ip = "203.0.113.10"
    instance_id        = "i-provider-matrix-aws"
    ebs_volume_id      = "vol-provider-matrix-aws"
    ssh_command        = "ssh admin@203.0.113.10"
  }
}

override_module {
  target = module.digitalocean
  outputs = {
    instance_public_ip = "203.0.113.20"
    droplet_id         = 4242
    volume_name        = "agent-stack-data"
    volume_id          = "do-volume-provider-matrix"
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
  hermes_dashboard_password                      = "hermes-password-agent-stack-tests"
  hermes_dashboard_secret                        = "hermes-session-secret-agent-stack-tests"
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
  tailscale_enabled                              = true
  tailscale_auth_key                             = "tskey-auth-agent-stack-tests"
  openai_codex_auth_json_base64                  = ""
}

run "aws_provider_matrix" {
  command   = plan
  state_key = "aws-provider-matrix"

  variables {
    cloud_provider = "aws"
  }

  assert {
    condition     = output.provider_used == "aws"
    error_message = "Root module should report AWS as the selected provider."
  }

  assert {
    condition     = output.instance_public_ip == "203.0.113.10"
    error_message = "AWS selection should read the AWS module public IP."
  }

  assert {
    condition     = terraform_data.runtime_apply.triggers_replace.instance_id == "i-provider-matrix-aws"
    error_message = "Runtime replacement triggers should include the selected AWS instance ID."
  }

  assert {
    condition     = terraform_data.runtime_apply.triggers_replace.volume_id == "vol-provider-matrix-aws"
    error_message = "Runtime replacement triggers should include the selected AWS data volume ID."
  }
}

run "digitalocean_provider_matrix" {
  command   = plan
  state_key = "digitalocean-provider-matrix"

  variables {
    cloud_provider = "digitalocean"
  }

  assert {
    condition     = output.provider_used == "digitalocean"
    error_message = "Root module should report DigitalOcean as the selected provider."
  }

  assert {
    condition     = output.instance_public_ip == "203.0.113.20"
    error_message = "DigitalOcean selection should read the DigitalOcean module public IP."
  }

  assert {
    condition     = terraform_data.runtime_apply.triggers_replace.instance_id == 4242
    error_message = "Runtime replacement triggers should include the selected Droplet ID."
  }

  assert {
    condition     = terraform_data.runtime_apply.triggers_replace.volume_id == "do-volume-provider-matrix"
    error_message = "Runtime replacement triggers should include the selected DigitalOcean volume ID."
  }
}

run "hetzner_provider_matrix" {
  command   = plan
  state_key = "hetzner-provider-matrix"

  variables {
    cloud_provider = "hetzner"
  }

  assert {
    condition     = output.provider_used == "hetzner"
    error_message = "Root module should report Hetzner as the selected provider."
  }

  assert {
    condition     = output.instance_public_ip == "203.0.113.30"
    error_message = "Hetzner selection should read the Hetzner module public IP."
  }

  assert {
    condition     = terraform_data.runtime_apply.triggers_replace.instance_id == 31337
    error_message = "Runtime replacement triggers should include the selected Hetzner server ID."
  }

  assert {
    condition     = terraform_data.runtime_apply.triggers_replace.volume_id == "31338"
    error_message = "Runtime replacement triggers should include the selected Hetzner volume ID."
  }
}

run "gcp_provider_matrix" {
  command   = plan
  state_key = "gcp-provider-matrix"
  variables {
    cloud_provider           = "gcp"
    gcp_project_id           = "agent-stack-tests"
    enabled_services         = ["openclaw", "hermes", "workspace"]
    workspace_password       = "tests-only-long-password"
    workspace_claude_enabled = true
    workspace_claude_release = "2.1.211"
  }
  assert {
    condition     = output.instance_public_ip == "203.0.113.40" && output.provider_used == "gcp"
    error_message = "GCP selection must use the Google endpoint."
  }
  assert {
    condition     = terraform_data.runtime_apply.triggers_replace.instance_id == "projects/agent-stack-tests/zones/us-east1-b/instances/agent-stack"
    error_message = "Runtime deployment must track the Google instance."
  }
  assert {
    condition     = strcontains(local.runtime_mount_volume, "/dev/disk/by-id/google-agent-stack-data")
    error_message = "GCP runtime must mount the attached data disk by stable device name."
  }
  assert {
    condition     = strcontains(local.runtime_workspace_dockerfile, "ARG CLAUDE_RELEASE=2.1.211") && strcontains(local.runtime_workspace_dockerfile, "/usr/local/bin/claude")
    error_message = "The combined workspace must render a pinned, user-accessible Claude installation."
  }
}

run "host_tailscale_gateway_reachable_through_private_publish" {
  command   = plan
  state_key = "host-tailscale-bind"
  variables {
    cloud_provider = "aws"
    tailscale_mode = "host"
  }
  assert {
    condition     = strcontains(nonsensitive(local.runtime_env), "OPENCLAW_GATEWAY_BIND=lan") && strcontains(local.runtime_docker_compose, "127.0.0.1:18789:18789")
    error_message = "Host Tailscale must reach the container while Docker keeps the gateway published only on host loopback."
  }
  assert {
    condition     = jsondecode(nonsensitive(local.runtime_openclaw_seed)).gateway_bind == "lan"
    error_message = "Fresh gateway configuration must agree with the host-mode listener."
  }
}

run "sidecar_gateway_keeps_shared_namespace_loopback" {
  command   = plan
  state_key = "sidecar-tailscale-bind"
  variables {
    cloud_provider = "aws"
    tailscale_mode = "sidecar"
  }
  assert {
    condition     = strcontains(nonsensitive(local.runtime_env), "OPENCLAW_GATEWAY_BIND=loopback") && jsondecode(nonsensitive(local.runtime_openclaw_seed)).gateway_bind == "loopback"
    error_message = "A sidecar sharing the OpenClaw namespace must use loopback in both environment and first-start configuration."
  }
}
