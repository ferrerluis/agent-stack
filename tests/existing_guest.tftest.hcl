mock_provider "aws" {}
mock_provider "google" {}
mock_provider "digitalocean" {}
mock_provider "hcloud" {}

override_data {
  target = data.external.existing_guest_preflight[0]
  values = { result = { status = "ready", architecture = "arm64", data_state = "empty" } }
}

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

variables {
  cloud_provider                                 = "existing"
  existing_guest_host_key                        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOnly"
  project_name                                   = "agent-stack"
  admin_ssh_host_override                        = "192.168.231.10"
  workspace_ssh_host_override                    = ""
  admin_password                                 = ""
  admin_password_ssh_scope                       = "disabled"
  ssh_public_key                                 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAgentStackTerraformPlanTestsOnly agent-stack-tests"
  repo_ssh_private_key_path                      = "tests/fixtures/fake_ssh_private_key.txt"
  generate_repo_ssh_config                       = false
  aws_access_key                                 = ""
  aws_secret_key                                 = ""
  do_token                                       = ""
  hcloud_token                                   = ""
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
  tailscale_enabled                              = true
  tailscale_auth_key                             = "tskey-auth-agent-stack-tests"
  openai_codex_auth_json_base64                  = ""
}

run "existing_vm_runtime_without_cloud_resources" {
  command = plan
  assert {
    condition     = length(module.aws) == 0 && length(module.digitalocean) == 0 && length(module.hetzner) == 0
    error_message = "Existing guest runtime must not create cloud resources."
  }
  assert {
    condition     = startswith(terraform_data.runtime_apply.triggers_replace.instance_id, "existing:192.168.231.10:22:") && terraform_data.runtime_apply.triggers_replace.volume_id == "existing-directory:/opt/agent-stack/data"
    error_message = "Runtime must track the supplied guest identity and ordinary data directory without requiring a VM module."
  }
  assert {
    condition     = output.ssh_command == "ssh admin@192.168.231.10"
    error_message = "Runtime SSH target must be the guest, not the physical host."
  }
}
run "reject_missing_guest_address" {
  command = plan
  variables { admin_ssh_host_override = "" }
  expect_failures = [terraform_data.existing_guest_validation]
}
run "reject_missing_verified_guest_host_key" {
  command = plan
  variables { existing_guest_host_key = "" }
  expect_failures = [terraform_data.existing_guest_validation]
}

run "existing_guest_never_generates_login_keys" {
  command = plan
  variables { ssh_public_key = "" }
  expect_failures = [terraform_data.existing_guest_validation]
  assert {
    condition     = length(data.external.repo_ssh_public_key) == 0
    error_message = "An existing-guest plan cannot invoke the automatic SSH key generator."
  }
}

run "user_prepared_endpoint_uses_pinned_config" {
  command = plan
  variables {
    generate_repo_ssh_config = true
    admin_ssh_host_override  = "127.0.0.1"
    existing_guest_ssh_port  = 2200
  }
  assert {
    condition     = output.ssh_command == "ssh -F ./.ssh/config agent-stack" && strcontains(local_file.repo_ssh_config[0].content, "Port 2200") && strcontains(local_file.repo_ssh_config[0].content, "StrictHostKeyChecking yes") && strcontains(local_file.repo_ssh_config[0].content, "GlobalKnownHostsFile /dev/null") && !strcontains(local_file.repo_ssh_config[0].content, "ProxyJump")
    error_message = "Use the supplied reachable endpoint and pinned guest identity without creating a proxy/tunnel."
  }
  assert {
    condition     = !strcontains(local_file.existing_guest_known_hosts[0].content, "hypervisor") && output.existing_guest_ssh_alias == "agent-stack"
    error_message = "Guest access must not require or configure a physical host identity."
  }
}

run "reject_guest_architecture_mismatch" {
  command = plan
  variables { existing_guest_architecture = "amd64" }
  expect_failures = [terraform_data.existing_guest_validation]
}

run "prepared_guest_needs_no_libvirt_loader_or_disk_setup" {
  command = plan
  assert {
    condition     = !strcontains(local.runtime_mount_volume, "loader-ready") && !strcontains(local.runtime_mount_volume, "mkfs") && !strcontains(local.runtime_mount_volume, "/etc/fstab") && !strcontains(local.runtime_mount_volume, "UUID=")
    error_message = "A normal prepared guest must not require a removed VM loader or receive disk formatting/mount lifecycle."
  }
}

run "workspace_prepared_guest_address_is_not_a_tailnet_promise" {
  command = plan
  variables {
    enabled_services   = ["workspace"]
    workspace_password = "fixture-workspace-password"
    tailscale_enabled  = false
  }
  assert {
    condition     = output.workspace_ssh_command == "ssh -p 2222 user@192.168.231.10" && strcontains(output.workspace_note, "user-prepared guest")
    error_message = "Prepared guest workspace must use the supplied address and explain user-managed access."
  }
}

run "explicit_amd64_guest" {
  command = plan
  variables { existing_guest_architecture = "amd64" }
  override_data {
    target = data.external.existing_guest_preflight[0]
    values = { result = { status = "ready", architecture = "amd64", data_state = "managed" } }
  }
  assert {
    condition     = output.existing_guest_architecture == "amd64"
    error_message = "A supported native amd64 guest must use the same runtime target."
  }
}
