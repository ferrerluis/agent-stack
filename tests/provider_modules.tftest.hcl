provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
}

provider "digitalocean" {
  token = "test"
}

provider "hcloud" {
  token = "test"
}

mock_provider "aws" {
  alias           = "fake"
  source          = "./tests/mocks/aws"
  override_during = plan
}

mock_provider "digitalocean" {
  alias           = "fake"
  source          = "./tests/mocks/digitalocean"
  override_during = plan
}

mock_provider "hcloud" {
  alias           = "fake"
  source          = "./tests/mocks/hcloud"
  override_during = plan
}

variables {
  project_name                                   = "agent-stack"
  admin_ssh_host_override                        = ""
  workspace_ssh_host_override                    = ""
  admin_username                                 = "admin"
  ssh_public_key                                 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAgentStackTerraformPlanTestsOnly agent-stack-tests"
  allowed_ssh_cidr                               = "203.0.113.0/24"
  tailscale_enabled                              = true
  tailscale_auth_key                             = "tskey-auth-agent-stack-tests"
  anthropic_api_key                              = ""
  anthropic_auth_key                             = ""
  openai_api_key                                 = ""
  openai_auth_mode                               = "api_key"
  openai_codex_auth_json_base64                  = ""
  groq_api_key                                   = ""
  gemini_api_key                                 = "gemini-agent-stack-tests"
  telegram_bot_token                             = ""
  telegram_allow_from                            = []
  openclaw_config_mode                           = "auto"
  agent_channel                                  = "telegram"
  model_providers_enabled                        = ["google"]
  default_model                                  = "google/gemini-3-flash-preview"
  fallback_models                                = []
  openclaw_version                               = "latest"
  openclaw_node_options                          = ""
  openclaw_swap_size_mb                          = 0
  openclaw_health_start_period_seconds           = 120
  openclaw_health_retries                        = 8
  seed_starter_workspace_files                   = true
  starter_soul_profile                           = "balanced"
  workspace_codex_release                        = "0.145.0"
  workspace_codex_auto_update_enabled            = false
  workspace_codex_auto_update_timezone           = "America/New_York"
  workspace_codex_auto_update_time               = "04:00"
  workspace_codex_auto_recover_interrupted_turns = false
  gateway_token                                  = "gateway-token-agent-stack-tests"
  enabled_services                               = ["openclaw", "hermes", "n8n"]
  hermes_image                                   = "nousresearch/hermes-agent:latest"
  hermes_dashboard_enabled                       = true
  hermes_api_server_enabled                      = true
  hermes_api_server_key                          = "hermes-key-agent-stack-tests"
  n8n_image                                      = "docker.n8n.io/n8nio/n8n:stable"
  n8n_database_mode                              = "local_postgres"
  n8n_encryption_key                             = "n8n-key-agent-stack-tests"
  n8n_public_webhooks_enabled                    = true
  n8n_generic_timezone                           = "America/New_York"
  postgres_image                                 = "postgres:17-alpine"
  postgres_database                              = "n8n"
  postgres_user                                  = "n8n"
  postgres_password                              = "postgres-password-agent-stack-tests"
  n8n_postgres_host                              = "postgres"
  n8n_postgres_port                              = 5432
  n8n_postgres_database                          = "n8n"
  n8n_postgres_user                              = "n8n"
  n8n_postgres_password                          = "postgres-password-agent-stack-tests"
  n8n_postgres_ssl_enabled                       = false
  public_domain_enabled                          = false
  openclaw_domain                                = ""
  hermes_domain                                  = ""
  n8n_domain                                     = ""
  acme_email                                     = ""
  ui_auth_mode                                   = "basic"
  ui_auth_username                               = "admin"
  ui_auth_password                               = "ui-password-agent-stack-tests"
}

run "aws_module_plans_with_mocked_provider" {
  command   = plan
  state_key = "aws-module"

  module {
    source = "./modules/aws"
  }

  providers = {
    aws = aws.fake
  }

  variables {
    aws_region             = "us-east-1"
    aws_instance_type      = "t3.small"
    aws_ami_id             = "ami-0agentstacktest"
    aws_disk_size_gb       = 50
    aws_existing_volume_id = "vol-existing-agentstacktest"
  }

  assert {
    condition     = length(aws_instance.this.credit_specification) == 0
    error_message = "Ordinary deployments must retain the implicit CPU credit mode."
  }

  assert {
    condition     = output.instance_public_ip == "203.0.113.10"
    error_message = "AWS module should use the mocked instance public IP."
  }

  assert {
    condition     = output.ebs_volume_id == "vol-existing-agentstacktest"
    error_message = "AWS module should preserve an explicitly supplied existing EBS volume ID."
  }

  assert {
    condition     = output.ssh_command == "ssh admin@203.0.113.10"
    error_message = "AWS module SSH handoff should use the admin user and mocked public IP."
  }
}

run "aws_showcase_cpu_and_disk_costs" {
  command = plan
  module { source = "./modules/aws" }
  providers = { aws = aws.fake }
  variables {
    project_name               = "as7-fixture01"
    aws_region                 = "us-east-1"
    aws_instance_type          = "t3.large"
    aws_ami_id                 = "ami-0123456789abcdef0"
    aws_disk_size_gb           = 50
    aws_existing_volume_id     = ""
    aws_showcase_cost_controls = true
  }
  assert {
    condition     = aws_instance.this.credit_specification[0].cpu_credits == "unlimited" && aws_instance.this.root_block_device[0].iops == 3000 && aws_instance.this.root_block_device[0].throughput == 125 && aws_ebs_volume.this[0].iops == 3000 && aws_ebs_volume.this[0].throughput == 125
    error_message = "The showcase must price the selected CPU mode and included performance of both disks explicitly."
  }
}

run "aws_showcase_nonburstable_cpu" {
  command = plan
  module { source = "./modules/aws" }
  providers = { aws = aws.fake }
  variables {
    aws_region                 = "us-east-1"
    aws_instance_type          = "m5.large"
    aws_ami_id                 = "ami-0123456789abcdef0"
    aws_disk_size_gb           = 50
    aws_existing_volume_id     = ""
    aws_showcase_cost_controls = true
  }
  assert {
    condition     = length(aws_instance.this.credit_specification) == 0 && aws_instance.this.root_block_device[0].iops == 3000 && aws_ebs_volume.this[0].throughput == 125
    error_message = "Nonburstable instances must retain no CPU-credit block while pricing the disks."
  }
}

run "digitalocean_module_plans_with_mocked_provider" {
  command   = plan
  state_key = "digitalocean-module"

  module {
    source = "./modules/digitalocean"
  }

  providers = {
    digitalocean = digitalocean.fake
  }

  variables {
    do_region               = "nyc3"
    do_droplet_size         = "s-2vcpu-2gb"
    do_disk_size_gb         = 20
    do_existing_volume_id   = "do-volume-existing-agentstacktest"
    do_existing_volume_name = "agent-stack-existing-data"
    do_project_id           = "12345678-1234-1234-1234-123456789abc"
  }

  assert {
    condition     = output.instance_public_ip == "203.0.113.20"
    error_message = "DigitalOcean module should use the mocked Droplet public IP."
  }

  assert {
    condition     = digitalocean_vpc.this.ip_range == "10.1.0.0/16"
    error_message = "Ordinary DigitalOcean deployments must preserve the original network range."
  }

  assert {
    condition     = output.volume_id == "do-volume-existing-agentstacktest"
    error_message = "DigitalOcean module should preserve an explicitly supplied existing volume ID."
  }

  assert {
    condition     = output.volume_name == "agent-stack-existing-data"
    error_message = "DigitalOcean module should preserve the existing volume name used for the device path."
  }
  assert {
    condition     = digitalocean_project_resources.this[0].project == var.do_project_id && length(digitalocean_project_resources.this[0].resources) == 1
    error_message = "Project assignment may include the new Droplet but must not move a reused operator disk."
  }
}

run "digitalocean_assigns_new_droplet_and_disk_to_selected_project" {
  command   = plan
  state_key = "digitalocean-fresh-project"
  module {
    source = "./modules/digitalocean"
  }
  providers = {
    digitalocean = digitalocean.fake
  }
  variables {
    do_region       = "nyc3"
    do_droplet_size = "s-4vcpu-8gb"
    do_disk_size_gb = 20
    do_project_id   = "12345678-1234-1234-1234-123456789abc"
    showcase_trial_limits = {
      deadline_utc     = "2026-10-08 00:00:00 UTC"
      egress_cap_bytes = 1073741824
    }
  }
  assert {
    condition     = digitalocean_project_resources.this[0].project == var.do_project_id && length(digitalocean_project_resources.this[0].resources) == 2
    error_message = "Both new project-capable resources must join the isolated test project."
  }
  assert {
    condition     = digitalocean_vpc.this.ip_range == "10.247.${parseint(substr(sha256(var.project_name), 0, 2), 16)}.0/24"
    error_message = "Bounded showcase trials must use the network range checked before apply."
  }
}

run "hetzner_module_plans_with_mocked_provider" {
  command   = plan
  state_key = "hetzner-module"

  module {
    source = "./modules/hetzner"
  }

  providers = {
    hcloud = hcloud.fake
  }

  variables {
    hcloud_location           = "ash"
    hcloud_server_type        = "cpx21"
    hcloud_image              = "ubuntu-22.04"
    hcloud_disk_size_gb       = 50
    hcloud_existing_volume_id = "31338"
  }

  assert {
    condition     = hcloud_server.this.image == "ubuntu-22.04" && length(data.hcloud_image.boot) == 0
    error_message = "Ordinary deployments must keep the named image and avoid an extra image data read."
  }

  assert {
    condition     = output.instance_public_ip == "203.0.113.30"
    error_message = "Hetzner module should use the mocked server public IP."
  }

  assert {
    condition     = output.volume_id == "31338"
    error_message = "Hetzner module should preserve an explicitly supplied existing volume ID."
  }

  assert {
    condition     = output.ssh_command == "ssh admin@203.0.113.30"
    error_message = "Hetzner module SSH handoff should use the admin user and mocked public IP."
  }
}

run "hetzner_showcase_requests_resolved_boot_image" {
  command = plan
  module { source = "./modules/hetzner" }
  providers = { hcloud = hcloud.fake }
  variables {
    project_name              = "as7-fixture01"
    hcloud_location           = "ash"
    hcloud_server_type        = "cpx31"
    hcloud_image              = "ubuntu-22.04"
    hcloud_disk_size_gb       = 50
    hcloud_existing_volume_id = ""
    hcloud_pin_boot_image     = true
  }
  assert {
    condition     = length(data.hcloud_image.boot) == 1 && data.hcloud_image.boot[0].name == "ubuntu-22.04" && data.hcloud_image.boot[0].with_architecture == "x86" && data.hcloud_image.boot[0].with_status == toset(["available"]) && data.hcloud_image.boot[0].include_deprecated && !data.hcloud_image.boot[0].most_recent
    error_message = "The showcase must request one available standard x86 Ubuntu image without choosing an ambiguous recent match."
  }
}

run "hetzner_pin_rejects_different_image" {
  command = plan
  module { source = "./modules/hetzner" }
  providers = { hcloud = hcloud.fake }
  variables {
    hcloud_location           = "ash"
    hcloud_server_type        = "cpx31"
    hcloud_image              = "debian-12"
    hcloud_disk_size_gb       = 50
    hcloud_existing_volume_id = ""
    hcloud_pin_boot_image     = true
  }
  expect_failures = [var.hcloud_pin_boot_image]
}
