terraform {
  required_providers {
    hcloud = {
      source = "hetznercloud/hcloud"
    }
  }
}

data "hcloud_image" "boot" {
  count              = var.hcloud_pin_boot_image ? 1 : 0
  name               = "ubuntu-22.04"
  with_architecture  = "x86"
  with_status        = ["available"]
  most_recent        = false
  include_deprecated = true
}

locals {
  hcloud_volume_id = (
    var.hcloud_existing_volume_id != ""
    ? var.hcloud_existing_volume_id
    : one(hcloud_volume.this[*].id)
  )

  cloud_init_vars = {
    provider_type                        = "hetzner"
    gcp_device_name                      = ""
    project_name                         = var.project_name
    showcase_trial_limits                = var.showcase_trial_limits
    admin_username                       = var.admin_username
    admin_ssh_public_key                 = var.ssh_public_key
    ebs_volume_id                        = ""
    do_volume_name                       = ""
    hcloud_volume_id                     = local.hcloud_volume_id
    anthropic_api_key                    = var.anthropic_api_key
    anthropic_auth_key                   = var.anthropic_auth_key
    openai_api_key                       = var.openai_api_key
    openai_auth_mode                     = var.openai_auth_mode
    openai_codex_auth_json_base64        = var.openai_codex_auth_json_base64
    groq_api_key                         = var.groq_api_key
    gemini_api_key                       = var.gemini_api_key
    meta_api_key                         = var.meta_api_key
    telegram_bot_token                   = var.telegram_bot_token
    telegram_allow_from_json             = jsonencode(var.telegram_allow_from)
    openclaw_config_mode                 = var.openclaw_config_mode
    agent_channel                        = var.agent_channel
    model_providers_enabled_json         = jsonencode(var.model_providers_enabled)
    default_model                        = var.default_model
    fallback_models_json                 = jsonencode(var.fallback_models)
    tailscale_enabled                    = var.tailscale_enabled
    tailscale_auth_key                   = var.tailscale_auth_key
    openclaw_version                     = var.openclaw_version
    openclaw_node_options                = var.openclaw_node_options
    openclaw_swap_size_mb                = var.openclaw_swap_size_mb
    openclaw_health_start_period_seconds = var.openclaw_health_start_period_seconds
    openclaw_health_retries              = var.openclaw_health_retries
    seed_starter_workspace_files         = var.seed_starter_workspace_files
    starter_soul_profile                 = var.starter_soul_profile
    starter_soul_balanced_md             = file("${path.module}/../common/templates/starter/SOUL.balanced.md")
    starter_soul_builder_md              = file("${path.module}/../common/templates/starter/SOUL.builder.md")
    starter_soul_researcher_md           = file("${path.module}/../common/templates/starter/SOUL.researcher.md")
    starter_agents_md                    = file("${path.module}/../common/templates/starter/AGENTS.default.md")
    starter_tools_md                     = file("${path.module}/../common/templates/starter/TOOLS.default.md")
    starter_user_md                      = file("${path.module}/../common/templates/starter/USER.default.md")
    gateway_token                        = var.gateway_token
    enabled_services_json                = jsonencode(var.enabled_services)
    openclaw_enabled                     = contains(var.enabled_services, "openclaw")
    hermes_enabled                       = contains(var.enabled_services, "hermes")
    n8n_enabled                          = contains(var.enabled_services, "n8n")
    local_postgres_enabled               = contains(var.enabled_services, "n8n") && var.n8n_database_mode == "local_postgres"
    caddy_enabled                        = var.public_domain_enabled
    hermes_image                         = var.hermes_image
    hermes_dashboard_enabled             = var.hermes_dashboard_enabled
    hermes_api_server_enabled            = var.hermes_api_server_enabled
    hermes_api_server_key                = var.hermes_api_server_key
    n8n_image                            = var.n8n_image
    n8n_database_mode                    = var.n8n_database_mode
    n8n_encryption_key                   = var.n8n_encryption_key
    n8n_public_webhooks_enabled          = var.n8n_public_webhooks_enabled
    n8n_generic_timezone                 = var.n8n_generic_timezone
    postgres_image                       = var.postgres_image
    postgres_database                    = var.postgres_database
    postgres_user                        = var.postgres_user
    postgres_password                    = var.postgres_password
    n8n_postgres_host                    = var.n8n_postgres_host
    n8n_postgres_port                    = var.n8n_postgres_port
    n8n_postgres_database                = var.n8n_postgres_database
    n8n_postgres_user                    = var.n8n_postgres_user
    n8n_postgres_password                = var.n8n_postgres_password
    n8n_postgres_ssl_enabled             = var.n8n_postgres_ssl_enabled
    public_domain_enabled                = var.public_domain_enabled
    openclaw_domain                      = var.openclaw_domain
    hermes_domain                        = var.hermes_domain
    n8n_domain                           = var.n8n_domain
    acme_email                           = var.acme_email
    ui_auth_mode                         = var.ui_auth_mode
    ui_auth_username                     = var.ui_auth_username
    ui_auth_password                     = var.ui_auth_password
  }

  cloud_init = templatefile("${path.module}/../common/templates/cloud_init.yaml.tpl", local.cloud_init_vars)
}

resource "hcloud_ssh_key" "this" {
  name       = "${var.project_name}-key"
  public_key = var.ssh_public_key
}

resource "hcloud_volume" "this" {
  count    = var.hcloud_existing_volume_id == "" ? 1 : 0
  name     = "${var.project_name}-data"
  size     = var.hcloud_disk_size_gb
  location = var.hcloud_location
  labels = {
    project = var.project_name
  }
}

resource "hcloud_firewall" "this" {
  name = "${var.project_name}-fw"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = [var.allowed_ssh_cidr]
  }

  dynamic "rule" {
    for_each = var.tailscale_enabled ? [1] : []
    content {
      direction  = "in"
      protocol   = "udp"
      port       = "41641"
      source_ips = ["0.0.0.0/0", "::/0"]
    }
  }

  dynamic "rule" {
    for_each = var.public_domain_enabled ? ["80", "443"] : []
    content {
      direction  = "in"
      protocol   = "tcp"
      port       = rule.value
      source_ips = ["0.0.0.0/0", "::/0"]
    }
  }
}

resource "hcloud_server" "this" {
  name        = var.project_name
  image       = var.hcloud_pin_boot_image ? tostring(one(data.hcloud_image.boot[*].id)) : var.hcloud_image
  server_type = var.hcloud_server_type
  location    = var.hcloud_location
  ssh_keys    = [hcloud_ssh_key.this.id]
  firewall_ids = [
    hcloud_firewall.this.id,
  ]
  user_data = local.cloud_init

  labels = {
    project = var.project_name
  }

  lifecycle {
    ignore_changes = [user_data]
  }
}

resource "hcloud_volume_attachment" "this" {
  volume_id = local.hcloud_volume_id
  server_id = hcloud_server.this.id
  automount = false
}
