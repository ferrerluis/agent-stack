# ─────────────────────────────────────────────────────────────────────────────
# Providers
# ─────────────────────────────────────────────────────────────────────────────

provider "google" {
  project = trimspace(var.gcp_project_id) != "" ? var.gcp_project_id : "agent-stack-inactive"
  region  = var.gcp_region
  zone    = var.gcp_zone
  # Avoid ADC discovery when another provider is selected; never call Google APIs then.
  access_token = var.cloud_provider == "gcp" ? null : "agent-stack-inactive"
}

provider "aws" {
  region     = var.aws_region
  access_key = var.cloud_provider == "aws" ? (var.aws_access_key != "" ? var.aws_access_key : null) : "mock_access_key"
  secret_key = var.cloud_provider == "aws" ? (var.aws_secret_key != "" ? var.aws_secret_key : null) : "mock_secret_key"

  # These skip flags prevent hard validation failures when AWS credentials are
  # not supplied (i.e. when another cloud_provider is selected).
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
}

provider "digitalocean" {
  token = var.cloud_provider == "digitalocean" ? (var.do_token != "" ? var.do_token : null) : "agent-stack-inactive"
}

provider "hcloud" {
  token = var.cloud_provider == "hetzner" ? (var.hcloud_token != "" ? var.hcloud_token : null) : "0000000000000000000000000000000000000000000000000000000000000000"
}

resource "random_password" "gateway_token" {
  length  = 48
  special = false
}

resource "random_password" "hermes_api_server_key" {
  length  = 64
  special = false
}

resource "random_password" "hermes_dashboard_password" {
  length  = 48
  special = false
}

resource "random_password" "hermes_dashboard_secret" {
  length  = 64
  special = false
}

resource "random_password" "n8n_encryption_key" {
  length  = 48
  special = false
}

resource "random_password" "postgres_password" {
  length  = 48
  special = false
}

resource "random_password" "ui_auth_password" {
  length  = 32
  special = false
}

locals {
  resolved_gateway_token         = trimspace(var.gateway_token) != "" ? var.gateway_token : random_password.gateway_token.result
  resolved_hermes_api_server_key = trimspace(var.hermes_api_server_key) != "" ? var.hermes_api_server_key : random_password.hermes_api_server_key.result
  resolved_n8n_encryption_key    = trimspace(var.n8n_encryption_key) != "" ? var.n8n_encryption_key : random_password.n8n_encryption_key.result
  resolved_postgres_password     = trimspace(var.postgres_password) != "" ? var.postgres_password : random_password.postgres_password.result
  resolved_ui_auth_password      = trimspace(var.ui_auth_password) != "" ? var.ui_auth_password : random_password.ui_auth_password.result
  openclaw_enabled               = contains(var.enabled_services, "openclaw")
  hermes_enabled                 = contains(var.enabled_services, "hermes")
  n8n_enabled                    = contains(var.enabled_services, "n8n")
  workspace_enabled              = contains(var.enabled_services, "workspace")
  tailscale_sidecar_enabled      = var.tailscale_enabled && var.tailscale_mode == "sidecar"
  tailscale_host_enabled         = var.tailscale_enabled && var.tailscale_mode == "host"
  local_postgres_enabled         = local.n8n_enabled && var.n8n_database_mode == "local_postgres"
  public_base_domain             = trimspace(var.base_domain)
  resolved_openclaw_domain       = trimspace(var.openclaw_domain) != "" ? trimspace(var.openclaw_domain) : (local.public_base_domain != "" ? "openclaw.${local.public_base_domain}" : "")
  resolved_hermes_domain         = trimspace(var.hermes_domain) != "" ? trimspace(var.hermes_domain) : (local.public_base_domain != "" ? "hermes.${local.public_base_domain}" : "")
  resolved_n8n_domain            = trimspace(var.n8n_domain) != "" ? trimspace(var.n8n_domain) : (local.public_base_domain != "" ? "n8n.${local.public_base_domain}" : "")
  resolved_n8n_postgres_host     = var.n8n_database_mode == "local_postgres" ? "postgres" : trimspace(var.external_postgres_host)
  resolved_n8n_postgres_port     = var.n8n_database_mode == "local_postgres" ? 5432 : var.external_postgres_port
  resolved_n8n_postgres_database = var.n8n_database_mode == "local_postgres" ? var.postgres_database : var.external_postgres_database
  resolved_n8n_postgres_user     = var.n8n_database_mode == "local_postgres" ? var.postgres_user : var.external_postgres_user
  resolved_n8n_postgres_password = var.n8n_database_mode == "local_postgres" ? local.resolved_postgres_password : var.external_postgres_password
  resolved_n8n_postgres_ssl      = var.n8n_database_mode == "local_postgres" ? false : var.external_postgres_ssl_enabled
}

resource "terraform_data" "input_validation" {
  input = "agent-stack-input-validation"

  lifecycle {
    precondition {
      condition = (
        !var.public_domain_enabled ||
        trimspace(var.base_domain) != "" ||
        trimspace(var.openclaw_domain) != "" ||
        trimspace(var.hermes_domain) != "" ||
        trimspace(var.n8n_domain) != ""
      )
      error_message = "public_domain_enabled requires base_domain or at least one explicit service domain."
    }

    precondition {
      condition     = !var.public_domain_enabled || var.ui_auth_mode == "basic"
      error_message = "public_domain_enabled supports ui_auth_mode = \"basic\" only in v1."
    }

    precondition {
      condition = (
        var.n8n_database_mode != "external_postgres" ||
        (
          trimspace(var.external_postgres_host) != "" &&
          trimspace(var.external_postgres_database) != "" &&
          trimspace(var.external_postgres_user) != "" &&
          trimspace(var.external_postgres_password) != ""
        )
      )
      error_message = "n8n_database_mode = \"external_postgres\" requires external_postgres_host, external_postgres_database, external_postgres_user, and external_postgres_password."
    }
  }
}

module "runtime_access" {
  count      = var.tailscale_enabled ? 1 : 0
  source     = "./modules/common/runtime-access"
  depends_on = [terraform_data.runtime_apply]
  repo_root  = abspath(path.root)
  key_path   = var.repo_ssh_private_key_path
  host       = local.admin_ssh_host
  user       = var.admin_username
}

data "external" "repo_ssh_public_key" {
  count = trimspace(var.ssh_public_key) == "" ? 1 : 0

  program = ["python3", "${path.root}/scripts/resolve_ssh_public_key.py"]
  query = {
    repo_root           = path.root
    private_key_relpath = var.repo_ssh_private_key_path
  }
}

locals {
  resolved_ssh_public_key = trimspace(var.ssh_public_key) != "" ? var.ssh_public_key : data.external.repo_ssh_public_key[0].result.ssh_public_key
  resolved_ssh_key_source = trimspace(var.ssh_public_key) != "" ? "tfvars_or_env" : data.external.repo_ssh_public_key[0].result.source
}

# ─────────────────────────────────────────────────────────────────────────────
# AWS deployment (activated when cloud_provider = "aws")
# ─────────────────────────────────────────────────────────────────────────────

module "aws" {
  count  = var.cloud_provider == "aws" ? 1 : 0
  source = "./modules/aws"

  project_name               = var.project_name
  showcase_trial_limits      = var.showcase_trial_limits
  admin_username             = var.admin_username
  aws_region                 = var.aws_region
  aws_instance_type          = var.aws_instance_type
  aws_showcase_cost_controls = var.aws_showcase_cost_controls
  aws_ami_id                 = var.aws_ami_id
  aws_disk_size_gb           = var.aws_disk_size_gb
  aws_existing_volume_id     = var.aws_existing_volume_id

  ssh_public_key   = local.resolved_ssh_public_key
  allowed_ssh_cidr = var.allowed_ssh_cidr

  anthropic_api_key                    = var.anthropic_api_key
  anthropic_auth_key                   = var.anthropic_auth_key
  openai_api_key                       = var.openai_api_key
  openai_auth_mode                     = var.openai_auth_mode
  openai_codex_auth_json_base64        = var.openai_codex_auth_json_base64
  groq_api_key                         = var.groq_api_key
  gemini_api_key                       = var.gemini_api_key
  telegram_bot_token                   = var.telegram_bot_token
  telegram_allow_from                  = var.telegram_allow_from
  openclaw_config_mode                 = var.openclaw_config_mode
  agent_channel                        = var.agent_channel
  model_providers_enabled              = var.model_providers_enabled
  default_model                        = var.default_model
  fallback_models                      = var.fallback_models
  openclaw_version                     = var.openclaw_version
  openclaw_node_options                = var.openclaw_node_options
  openclaw_swap_size_mb                = var.openclaw_swap_size_mb
  openclaw_health_start_period_seconds = var.openclaw_health_start_period_seconds
  openclaw_health_retries              = var.openclaw_health_retries
  seed_starter_workspace_files         = var.seed_starter_workspace_files
  starter_soul_profile                 = var.starter_soul_profile
  gateway_token                        = local.resolved_gateway_token
  enabled_services                     = var.enabled_services
  hermes_image                         = var.hermes_image
  hermes_dashboard_enabled             = var.hermes_dashboard_enabled
  hermes_api_server_enabled            = var.hermes_api_server_enabled
  hermes_api_server_key                = local.resolved_hermes_api_server_key
  n8n_image                            = var.n8n_image
  n8n_database_mode                    = var.n8n_database_mode
  n8n_encryption_key                   = local.resolved_n8n_encryption_key
  n8n_public_webhooks_enabled          = var.n8n_public_webhooks_enabled
  n8n_generic_timezone                 = var.n8n_generic_timezone
  postgres_image                       = var.postgres_image
  postgres_database                    = var.postgres_database
  postgres_user                        = var.postgres_user
  postgres_password                    = local.resolved_postgres_password
  n8n_postgres_host                    = local.resolved_n8n_postgres_host
  n8n_postgres_port                    = local.resolved_n8n_postgres_port
  n8n_postgres_database                = local.resolved_n8n_postgres_database
  n8n_postgres_user                    = local.resolved_n8n_postgres_user
  n8n_postgres_password                = local.resolved_n8n_postgres_password
  n8n_postgres_ssl_enabled             = local.resolved_n8n_postgres_ssl
  public_domain_enabled                = var.public_domain_enabled
  openclaw_domain                      = local.resolved_openclaw_domain
  hermes_domain                        = local.resolved_hermes_domain
  n8n_domain                           = local.resolved_n8n_domain
  acme_email                           = var.acme_email
  ui_auth_mode                         = var.ui_auth_mode
  ui_auth_username                     = var.ui_auth_username
  ui_auth_password                     = local.resolved_ui_auth_password

  tailscale_enabled  = var.tailscale_enabled
  tailscale_auth_key = var.tailscale_auth_key
}

# ─────────────────────────────────────────────────────────────────────────────
# DigitalOcean deployment (activated when cloud_provider = "digitalocean")
# ─────────────────────────────────────────────────────────────────────────────

module "digitalocean" {
  count  = var.cloud_provider == "digitalocean" ? 1 : 0
  source = "./modules/digitalocean"

  project_name          = var.project_name
  showcase_trial_limits = var.showcase_trial_limits
  admin_username        = var.admin_username
  do_region             = var.do_region
  do_project_id         = var.do_project_id
  do_droplet_size       = var.do_droplet_size
  do_disk_size_gb       = var.do_disk_size_gb

  do_existing_volume_id   = var.do_existing_volume_id
  do_existing_volume_name = var.do_existing_volume_name

  ssh_public_key   = local.resolved_ssh_public_key
  allowed_ssh_cidr = var.allowed_ssh_cidr

  anthropic_api_key                    = var.anthropic_api_key
  anthropic_auth_key                   = var.anthropic_auth_key
  openai_api_key                       = var.openai_api_key
  openai_auth_mode                     = var.openai_auth_mode
  openai_codex_auth_json_base64        = var.openai_codex_auth_json_base64
  groq_api_key                         = var.groq_api_key
  gemini_api_key                       = var.gemini_api_key
  telegram_bot_token                   = var.telegram_bot_token
  telegram_allow_from                  = var.telegram_allow_from
  openclaw_config_mode                 = var.openclaw_config_mode
  agent_channel                        = var.agent_channel
  model_providers_enabled              = var.model_providers_enabled
  default_model                        = var.default_model
  fallback_models                      = var.fallback_models
  openclaw_version                     = var.openclaw_version
  openclaw_node_options                = var.openclaw_node_options
  openclaw_swap_size_mb                = var.openclaw_swap_size_mb
  openclaw_health_start_period_seconds = var.openclaw_health_start_period_seconds
  openclaw_health_retries              = var.openclaw_health_retries
  seed_starter_workspace_files         = var.seed_starter_workspace_files
  starter_soul_profile                 = var.starter_soul_profile
  gateway_token                        = local.resolved_gateway_token
  enabled_services                     = var.enabled_services
  hermes_image                         = var.hermes_image
  hermes_dashboard_enabled             = var.hermes_dashboard_enabled
  hermes_api_server_enabled            = var.hermes_api_server_enabled
  hermes_api_server_key                = local.resolved_hermes_api_server_key
  n8n_image                            = var.n8n_image
  n8n_database_mode                    = var.n8n_database_mode
  n8n_encryption_key                   = local.resolved_n8n_encryption_key
  n8n_public_webhooks_enabled          = var.n8n_public_webhooks_enabled
  n8n_generic_timezone                 = var.n8n_generic_timezone
  postgres_image                       = var.postgres_image
  postgres_database                    = var.postgres_database
  postgres_user                        = var.postgres_user
  postgres_password                    = local.resolved_postgres_password
  n8n_postgres_host                    = local.resolved_n8n_postgres_host
  n8n_postgres_port                    = local.resolved_n8n_postgres_port
  n8n_postgres_database                = local.resolved_n8n_postgres_database
  n8n_postgres_user                    = local.resolved_n8n_postgres_user
  n8n_postgres_password                = local.resolved_n8n_postgres_password
  n8n_postgres_ssl_enabled             = local.resolved_n8n_postgres_ssl
  public_domain_enabled                = var.public_domain_enabled
  openclaw_domain                      = local.resolved_openclaw_domain
  hermes_domain                        = local.resolved_hermes_domain
  n8n_domain                           = local.resolved_n8n_domain
  acme_email                           = var.acme_email
  ui_auth_mode                         = var.ui_auth_mode
  ui_auth_username                     = var.ui_auth_username
  ui_auth_password                     = local.resolved_ui_auth_password

  tailscale_enabled  = var.tailscale_enabled
  tailscale_auth_key = var.tailscale_auth_key
}

# ─────────────────────────────────────────────────────────────────────────────
# Hetzner deployment (activated when cloud_provider = "hetzner")
# ─────────────────────────────────────────────────────────────────────────────

module "hetzner" {
  count  = var.cloud_provider == "hetzner" ? 1 : 0
  source = "./modules/hetzner"

  project_name                         = var.project_name
  showcase_trial_limits                = var.showcase_trial_limits
  admin_username                       = var.admin_username
  hcloud_location                      = var.hcloud_location
  hcloud_server_type                   = var.hcloud_server_type
  hcloud_image                         = var.hcloud_image
  hcloud_pin_boot_image                = var.hcloud_pin_boot_image
  hcloud_disk_size_gb                  = var.hcloud_disk_size_gb
  hcloud_existing_volume_id            = var.hcloud_existing_volume_id
  ssh_public_key                       = local.resolved_ssh_public_key
  allowed_ssh_cidr                     = var.allowed_ssh_cidr
  anthropic_api_key                    = var.anthropic_api_key
  anthropic_auth_key                   = var.anthropic_auth_key
  openai_api_key                       = var.openai_api_key
  openai_auth_mode                     = var.openai_auth_mode
  openai_codex_auth_json_base64        = var.openai_codex_auth_json_base64
  groq_api_key                         = var.groq_api_key
  gemini_api_key                       = var.gemini_api_key
  telegram_bot_token                   = var.telegram_bot_token
  telegram_allow_from                  = var.telegram_allow_from
  openclaw_config_mode                 = var.openclaw_config_mode
  agent_channel                        = var.agent_channel
  model_providers_enabled              = var.model_providers_enabled
  default_model                        = var.default_model
  fallback_models                      = var.fallback_models
  openclaw_version                     = var.openclaw_version
  openclaw_node_options                = var.openclaw_node_options
  openclaw_swap_size_mb                = var.openclaw_swap_size_mb
  openclaw_health_start_period_seconds = var.openclaw_health_start_period_seconds
  openclaw_health_retries              = var.openclaw_health_retries
  seed_starter_workspace_files         = var.seed_starter_workspace_files
  starter_soul_profile                 = var.starter_soul_profile
  gateway_token                        = local.resolved_gateway_token
  enabled_services                     = var.enabled_services
  hermes_image                         = var.hermes_image
  hermes_dashboard_enabled             = var.hermes_dashboard_enabled
  hermes_api_server_enabled            = var.hermes_api_server_enabled
  hermes_api_server_key                = local.resolved_hermes_api_server_key
  n8n_image                            = var.n8n_image
  n8n_database_mode                    = var.n8n_database_mode
  n8n_encryption_key                   = local.resolved_n8n_encryption_key
  n8n_public_webhooks_enabled          = var.n8n_public_webhooks_enabled
  n8n_generic_timezone                 = var.n8n_generic_timezone
  postgres_image                       = var.postgres_image
  postgres_database                    = var.postgres_database
  postgres_user                        = var.postgres_user
  postgres_password                    = local.resolved_postgres_password
  n8n_postgres_host                    = local.resolved_n8n_postgres_host
  n8n_postgres_port                    = local.resolved_n8n_postgres_port
  n8n_postgres_database                = local.resolved_n8n_postgres_database
  n8n_postgres_user                    = local.resolved_n8n_postgres_user
  n8n_postgres_password                = local.resolved_n8n_postgres_password
  n8n_postgres_ssl_enabled             = local.resolved_n8n_postgres_ssl
  public_domain_enabled                = var.public_domain_enabled
  openclaw_domain                      = local.resolved_openclaw_domain
  hermes_domain                        = local.resolved_hermes_domain
  n8n_domain                           = local.resolved_n8n_domain
  acme_email                           = var.acme_email
  ui_auth_mode                         = var.ui_auth_mode
  ui_auth_username                     = var.ui_auth_username
  ui_auth_password                     = local.resolved_ui_auth_password
  tailscale_enabled                    = var.tailscale_enabled
  tailscale_auth_key                   = var.tailscale_auth_key
}

module "gcp" {
  count                      = var.cloud_provider == "gcp" ? 1 : 0
  source                     = "./modules/gcp"
  project_name               = var.project_name
  showcase_trial_limits      = var.showcase_trial_limits
  admin_username             = var.admin_username
  ssh_public_key             = local.resolved_ssh_public_key
  allowed_ssh_cidr           = var.allowed_ssh_cidr
  gcp_project_id             = var.gcp_project_id
  gcp_region                 = var.gcp_region
  gcp_zone                   = var.gcp_zone
  gcp_machine_type           = var.gcp_machine_type
  gcp_image                  = var.gcp_image
  gcp_pin_boot_image         = var.gcp_pin_boot_image
  gcp_showcase_cost_controls = var.gcp_showcase_cost_controls
  gcp_disk_size_gb           = var.gcp_disk_size_gb
  gcp_boot_disk_size_gb      = var.gcp_boot_disk_size_gb
  tailscale_enabled          = var.tailscale_enabled
  public_domain_enabled      = var.public_domain_enabled
}

locals {
  runtime_instance_id = (
    var.cloud_provider == "aws"
    ? one(module.aws[*].instance_id)
    : (
      var.cloud_provider == "digitalocean"
      ? one(module.digitalocean[*].droplet_id)
      : (var.cloud_provider == "hetzner" ? one(module.hetzner[*].server_id) : one(module.gcp[*].instance_id))
    )
  )

  runtime_data_volume_id = (
    var.cloud_provider == "aws"
    ? one(module.aws[*].ebs_volume_id)
    : (
      var.cloud_provider == "digitalocean"
      ? one(module.digitalocean[*].volume_id)
      : (var.cloud_provider == "hetzner" ? one(module.hetzner[*].volume_id) : one(module.gcp[*].volume_id))
    )
  )

  runtime_do_volume_name = var.cloud_provider == "digitalocean" ? one(module.digitalocean[*].volume_name) : ""

  runtime_template_vars = {
    hermes_config_mode                             = var.hermes_config_mode
    hermes_dashboard_username                      = var.hermes_dashboard_username
    hermes_dashboard_password                      = trimspace(var.hermes_dashboard_password) != "" ? var.hermes_dashboard_password : random_password.hermes_dashboard_password.result
    hermes_dashboard_secret                        = trimspace(var.hermes_dashboard_secret) != "" ? var.hermes_dashboard_secret : random_password.hermes_dashboard_secret.result
    gcp_device_name                                = var.cloud_provider == "gcp" ? one(module.gcp[*].device_name) : ""
    provider_type                                  = var.cloud_provider
    project_name                                   = var.project_name
    admin_username                                 = var.admin_username
    admin_password                                 = var.admin_password
    admin_password_ssh_scope                       = var.admin_password_ssh_scope
    host_codex_cli_enabled                         = var.host_codex_cli_enabled
    ebs_volume_id                                  = var.cloud_provider == "aws" ? local.runtime_data_volume_id : ""
    do_volume_name                                 = local.runtime_do_volume_name
    hcloud_volume_id                               = var.cloud_provider == "hetzner" ? local.runtime_data_volume_id : ""
    anthropic_api_key                              = var.anthropic_api_key
    anthropic_auth_key                             = var.anthropic_auth_key
    openai_api_key                                 = var.openai_api_key
    openai_auth_mode                               = var.openai_auth_mode
    openai_codex_auth_json_base64                  = var.openai_codex_auth_json_base64
    groq_api_key                                   = var.groq_api_key
    gemini_api_key                                 = var.gemini_api_key
    telegram_bot_token                             = var.telegram_bot_token
    telegram_allow_from_json                       = jsonencode(var.telegram_allow_from)
    openclaw_config_mode                           = var.openclaw_config_mode
    agent_channel                                  = var.agent_channel
    model_providers_enabled_json                   = jsonencode(var.model_providers_enabled)
    default_model                                  = var.default_model
    fallback_models_json                           = jsonencode(var.fallback_models)
    tailscale_enabled                              = var.tailscale_enabled
    tailscale_auth_key                             = var.tailscale_auth_key
    openclaw_version                               = var.openclaw_version
    openclaw_image                                 = var.openclaw_image_digest != "" ? "ghcr.io/openclaw/openclaw@${var.openclaw_image_digest}" : "ghcr.io/openclaw/openclaw:${var.openclaw_version}"
    openclaw_gateway_bind                          = local.tailscale_sidecar_enabled || var.openclaw_host_network_enabled ? "loopback" : "lan"
    openclaw_host_network_enabled                  = var.openclaw_host_network_enabled
    openclaw_node_options                          = var.openclaw_node_options
    openclaw_swap_size_mb                          = var.openclaw_swap_size_mb
    openclaw_health_start_period_seconds           = var.openclaw_health_start_period_seconds
    openclaw_health_retries                        = var.openclaw_health_retries
    seed_starter_workspace_files                   = var.seed_starter_workspace_files
    starter_soul_profile                           = var.starter_soul_profile
    gateway_token                                  = local.resolved_gateway_token
    enabled_services_json                          = jsonencode(var.enabled_services)
    openclaw_enabled                               = local.openclaw_enabled
    hermes_enabled                                 = local.hermes_enabled
    n8n_enabled                                    = local.n8n_enabled
    local_postgres_enabled                         = local.local_postgres_enabled
    caddy_enabled                                  = var.public_domain_enabled
    hermes_image                                   = var.hermes_image
    hermes_npm_security_patch_enabled              = var.hermes_npm_security_patch_enabled
    hermes_dashboard_enabled                       = var.hermes_dashboard_enabled
    hermes_api_server_enabled                      = var.hermes_api_server_enabled
    hermes_api_server_key                          = local.resolved_hermes_api_server_key
    n8n_image                                      = var.n8n_image
    n8n_database_mode                              = var.n8n_database_mode
    n8n_encryption_key                             = local.resolved_n8n_encryption_key
    n8n_public_webhooks_enabled                    = var.n8n_public_webhooks_enabled
    n8n_generic_timezone                           = var.n8n_generic_timezone
    postgres_image                                 = var.postgres_image
    postgres_database                              = var.postgres_database
    postgres_user                                  = var.postgres_user
    postgres_password                              = local.resolved_postgres_password
    n8n_postgres_host                              = local.resolved_n8n_postgres_host
    n8n_postgres_port                              = local.resolved_n8n_postgres_port
    n8n_postgres_database                          = local.resolved_n8n_postgres_database
    n8n_postgres_user                              = local.resolved_n8n_postgres_user
    n8n_postgres_password                          = local.resolved_n8n_postgres_password
    n8n_postgres_ssl_enabled                       = local.resolved_n8n_postgres_ssl
    public_domain_enabled                          = var.public_domain_enabled
    openclaw_domain                                = local.resolved_openclaw_domain
    hermes_domain                                  = local.resolved_hermes_domain
    n8n_domain                                     = local.resolved_n8n_domain
    acme_email                                     = var.acme_email
    ui_auth_mode                                   = var.ui_auth_mode
    ui_auth_username                               = var.ui_auth_username
    ui_auth_password                               = local.resolved_ui_auth_password
    workspace_enabled                              = local.workspace_enabled
    workspace_claude_enabled                       = var.workspace_claude_enabled
    workspace_claude_release                       = var.workspace_claude_release
    workspace_codex_release                        = var.workspace_codex_release
    workspace_codex_sandbox_enabled                = var.workspace_codex_sandbox_enabled
    workspace_codex_auto_update_enabled            = var.workspace_codex_auto_update_enabled
    workspace_codex_auto_update_timezone           = trimspace(var.workspace_codex_auto_update_timezone)
    workspace_codex_auto_update_time               = trimspace(var.workspace_codex_auto_update_time)
    workspace_codex_auto_recover_interrupted_turns = var.workspace_codex_auto_recover_interrupted_turns
    workspace_username                             = var.workspace_username
    workspace_password                             = var.workspace_password
    workspace_password_hash                        = var.workspace_password_hash
    workspace_ssh_host_port                        = var.workspace_ssh_host_port
    workspace_ssh_host                             = trimspace(var.workspace_ssh_host_override) != "" ? trimspace(var.workspace_ssh_host_override) : var.project_name
    workspace_ssh_public_keys_base64               = base64encode(join("\n", [for key in var.workspace_ssh_public_keys : trimspace(key)]))
    vpn_enabled                                    = var.vpn_enabled
    vpn_provider                                   = var.vpn_provider
    vpn_nordvpn_connect_target                     = var.vpn_nordvpn_connect_target
    vpn_openvpn_config_url                         = var.vpn_openvpn_config_url
    vpn_bypass_cidrs_json                          = jsonencode(var.vpn_bypass_cidrs)
    vpn_disable_ipv6                               = var.vpn_disable_ipv6
    vpn_healthcheck_url                            = var.vpn_healthcheck_url
    tailscale_mode                                 = var.tailscale_mode
    tailscale_sidecar_enabled                      = local.tailscale_sidecar_enabled
    tailscale_host_enabled                         = local.tailscale_host_enabled
  }

  runtime_docker_compose             = templatefile("${path.module}/modules/common/templates/runtime/docker-compose.yml.tpl", local.runtime_template_vars)
  runtime_env                        = templatefile("${path.module}/modules/common/templates/runtime/env.tpl", local.runtime_template_vars)
  runtime_hermes_seed                = jsonencode({ model_ref = trimspace(var.hermes_model) != "" ? trimspace(var.hermes_model) : var.default_model })
  runtime_openclaw_seed              = jsonencode({ gateway_bind = local.runtime_template_vars.openclaw_gateway_bind, gateway_token = local.resolved_gateway_token })
  runtime_openclaw_config            = file("${path.module}/modules/common/runtime/openclaw_config.py")
  runtime_workspace_cli_task         = file("${path.module}/modules/common/runtime/workspace_cli_task.py")
  runtime_workspace_sandbox          = file("${path.module}/modules/common/runtime/workspace_sandbox.py")
  runtime_workspace_sandbox_profiles = file("${path.module}/modules/common/runtime/workspace_sandbox_profiles.json")
  runtime_workspace_sandbox_license  = file("${path.module}/modules/common/runtime/workspace_sandbox_LICENSE.txt")
  runtime_hermes_config              = file("${path.module}/modules/common/runtime/hermes_config.py")
  runtime_hermes_health              = file("${path.module}/modules/common/runtime/hermes_health.py")
  runtime_hermes_npm_patch           = file("${path.module}/modules/common/runtime/hermes_npm_patch.py")
  runtime_hermes_npm_patch_bundle    = file("${path.module}/modules/common/runtime/hermes_npm_patch.json")
  runtime_workspace_env              = templatefile("${path.module}/modules/common/templates/runtime/workspace.env.tpl", local.runtime_template_vars)
  runtime_caddyfile_template         = templatefile("${path.module}/modules/common/templates/runtime/Caddyfile.template.tpl", local.runtime_template_vars)
  runtime_tailscale_bootstrap        = templatefile("${path.module}/modules/common/templates/runtime/tailscale-bootstrap.sh.tpl", local.runtime_template_vars)
  runtime_host_tailscale             = templatefile("${path.module}/modules/common/templates/runtime/host-tailscale-bootstrap.sh.tpl", local.runtime_template_vars)
  runtime_workspace_dockerfile       = templatefile("${path.module}/modules/common/templates/runtime/workspace.Dockerfile.tpl", local.runtime_template_vars)
  runtime_workspace_entrypoint       = templatefile("${path.module}/modules/common/templates/runtime/workspace-entrypoint.sh.tpl", local.runtime_template_vars)
  runtime_workspace_drive_skill      = file("${path.module}/skills/para-memory-drive/SKILL.md")
  runtime_workspace_codex_update     = templatefile("${path.module}/modules/common/templates/runtime/workspace-codex-update.sh.tpl", local.runtime_template_vars)
  runtime_workspace_codex_control    = templatefile("${path.module}/modules/common/templates/runtime/workspace-codex-control.py.tpl", local.runtime_template_vars)
  runtime_workspace_update_host      = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-workspace-codex-update.sh.tpl", local.runtime_template_vars)
  runtime_workspace_update_unit      = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-workspace-codex-update.service.tpl", local.runtime_template_vars)
  runtime_workspace_update_timer     = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-workspace-codex-update.timer.tpl", local.runtime_template_vars)
  runtime_vpn_manager                = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-vpn.sh.tpl", local.runtime_template_vars)
  runtime_vpn_openvpn                = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-vpn-openvpn.sh.tpl", local.runtime_template_vars)
  runtime_vpn_auth                   = var.vpn_enabled && var.vpn_provider == "nordvpn_openvpn" ? "${var.vpn_username}\n${var.vpn_password}\n" : ""
  runtime_vpn_token                  = var.vpn_enabled && var.vpn_provider == "nordvpn_nordlynx" ? "${var.vpn_nordvpn_token}\n" : ""
  runtime_diagnostics_helper         = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-diagnostics.sh.tpl", local.runtime_template_vars)
  runtime_diagnostics_ssh_helper     = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-diagnostics-ssh.sh.tpl", local.runtime_template_vars)
  runtime_layout_migrator            = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-migrate-layout.sh.tpl", local.runtime_template_vars)
  runtime_mount_volume               = templatefile("${path.module}/modules/common/templates/runtime/mount-agent-stack-volume.sh.tpl", local.runtime_template_vars)
  runtime_installer                  = templatefile("${path.module}/modules/common/templates/runtime/install-agent-stack.sh.tpl", local.runtime_template_vars)
  runtime_agent_stack_service        = templatefile("${path.module}/modules/common/templates/runtime/agent-stack.service.tpl", local.runtime_template_vars)
  runtime_openclaw_service           = templatefile("${path.module}/modules/common/templates/runtime/openclaw.service.tpl", local.runtime_template_vars)
  runtime_watchdog                   = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-tailscale-watchdog.sh.tpl", local.runtime_template_vars)
  runtime_watchdog_service           = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-tailscale-watchdog.service.tpl", local.runtime_template_vars)
  runtime_watchdog_timer             = templatefile("${path.module}/modules/common/templates/runtime/agent-stack-tailscale-watchdog.timer.tpl", local.runtime_template_vars)
  runtime_enabled_services           = jsonencode(var.enabled_services)
  runtime_starter_soul               = file("${path.module}/modules/common/templates/starter/SOUL.${var.starter_soul_profile}.md")
  runtime_starter_agents             = file("${path.module}/modules/common/templates/starter/AGENTS.default.md")
  runtime_starter_tools              = file("${path.module}/modules/common/templates/starter/TOOLS.default.md")
  runtime_starter_user               = file("${path.module}/modules/common/templates/starter/USER.default.md")
  runtime_artifact_checksum = nonsensitive(sha256(join("\n---agent-stack-artifact---\n", [
    local.deployment_input_fingerprints.deployment,
    nonsensitive(local.runtime_docker_compose),
    nonsensitive(local.runtime_env),
    local.runtime_hermes_seed,
    nonsensitive(local.runtime_openclaw_seed),
    local.runtime_openclaw_config,
    local.runtime_workspace_cli_task,
    local.runtime_workspace_sandbox,
    local.runtime_workspace_sandbox_profiles,
    local.runtime_workspace_sandbox_license,
    local.runtime_hermes_config,
    local.runtime_hermes_health,
    local.runtime_hermes_npm_patch,
    local.runtime_hermes_npm_patch_bundle,
    nonsensitive(local.runtime_workspace_env),
    nonsensitive(local.runtime_caddyfile_template),
    nonsensitive(local.runtime_tailscale_bootstrap),
    nonsensitive(local.runtime_host_tailscale),
    nonsensitive(local.runtime_workspace_dockerfile),
    nonsensitive(local.runtime_workspace_entrypoint),
    nonsensitive(local.runtime_workspace_drive_skill),
    nonsensitive(local.runtime_workspace_codex_update),
    nonsensitive(local.runtime_workspace_codex_control),
    nonsensitive(local.runtime_workspace_update_host),
    nonsensitive(local.runtime_workspace_update_unit),
    nonsensitive(local.runtime_workspace_update_timer),
    nonsensitive(local.runtime_vpn_manager),
    nonsensitive(local.runtime_vpn_openvpn),
    nonsensitive(local.runtime_vpn_auth),
    nonsensitive(local.runtime_vpn_token),
    nonsensitive(local.runtime_diagnostics_helper),
    nonsensitive(local.runtime_diagnostics_ssh_helper),
    nonsensitive(local.runtime_layout_migrator),
    nonsensitive(local.runtime_mount_volume),
    nonsensitive(local.runtime_installer),
    nonsensitive(local.runtime_agent_stack_service),
    nonsensitive(local.runtime_openclaw_service),
    nonsensitive(local.runtime_watchdog),
    nonsensitive(local.runtime_watchdog_service),
    nonsensitive(local.runtime_watchdog_timer),
    nonsensitive(local.runtime_enabled_services),
    nonsensitive(local.runtime_starter_soul),
    nonsensitive(local.runtime_starter_agents),
    nonsensitive(local.runtime_starter_tools),
    nonsensitive(local.runtime_starter_user),
    nonsensitive(var.openai_codex_auth_json_base64),
  ])))
  runtime_staging_dir = "/opt/agent-stack/.staging-${substr(local.runtime_artifact_checksum, 0, 16)}"
}

resource "terraform_data" "runtime_apply" {
  input = local.runtime_artifact_checksum

  triggers_replace = {
    artifact_checksum = local.runtime_artifact_checksum
    instance_id       = local.runtime_instance_id
    volume_id         = local.runtime_data_volume_id
  }

  depends_on = [
    module.aws,
    module.digitalocean,
    module.hetzner,
    module.gcp,
  ]

  connection {
    type        = "ssh"
    host        = local.admin_ssh_host
    user        = var.admin_username
    private_key = file("${path.root}/${var.repo_ssh_private_key_path}")
    timeout     = "15m"
  }

  provisioner "remote-exec" {
    inline = [
      "sudo cloud-init status --wait",
      "sudo -n true",
      "sudo test -f /opt/agent-stack/.loader-ready.json",
      "sudo test -d /opt/agent-stack/data",
      "group=$(id -gn ${var.admin_username}); sudo install -d -m 0755 /opt/agent-stack; sudo install -d -m 0700 -o ${var.admin_username} -g $group ${local.runtime_staging_dir}",
      "sudo find ${local.runtime_staging_dir} -mindepth 1 -maxdepth 1 -exec rm -rf {} +",
      "install -d -m 0700 ${local.runtime_staging_dir}/templates",
    ]
  }

  provisioner "file" {
    content     = local.runtime_docker_compose
    destination = "${local.runtime_staging_dir}/docker-compose.yml"
  }

  provisioner "file" {
    content     = local.runtime_env
    destination = "${local.runtime_staging_dir}/.env"
  }

  provisioner "file" {
    content     = local.runtime_hermes_seed
    destination = "${local.runtime_staging_dir}/hermes-seed.json"
  }
  provisioner "file" {
    content     = local.runtime_openclaw_seed
    destination = "${local.runtime_staging_dir}/openclaw-seed.json"
  }
  provisioner "file" {
    content     = local.runtime_openclaw_config
    destination = "${local.runtime_staging_dir}/openclaw_config.py"
  }
  provisioner "file" {
    content     = local.runtime_hermes_config
    destination = "${local.runtime_staging_dir}/hermes_config.py"
  }
  provisioner "file" {
    content     = local.runtime_hermes_health
    destination = "${local.runtime_staging_dir}/hermes_health.py"
  }
  provisioner "file" {
    content     = local.runtime_hermes_npm_patch
    destination = "${local.runtime_staging_dir}/hermes_npm_patch.py"
  }
  provisioner "file" {
    content     = local.runtime_hermes_npm_patch_bundle
    destination = "${local.runtime_staging_dir}/hermes_npm_patch.json"
  }
  provisioner "file" {
    content     = local.runtime_workspace_cli_task
    destination = "${local.runtime_staging_dir}/workspace-cli-task.py"
  }
  provisioner "file" {
    content     = local.runtime_workspace_sandbox
    destination = "${local.runtime_staging_dir}/workspace-sandbox.py"
  }
  provisioner "file" {
    content     = local.runtime_workspace_sandbox_profiles
    destination = "${local.runtime_staging_dir}/workspace-sandbox-profiles.json"
  }
  provisioner "file" {
    content     = local.runtime_workspace_sandbox_license
    destination = "${local.runtime_staging_dir}/workspace-sandbox-LICENSE.txt"
  }

  provisioner "file" {
    content     = local.runtime_workspace_env
    destination = "${local.runtime_staging_dir}/workspace.env"
  }

  provisioner "file" {
    content     = local.runtime_caddyfile_template
    destination = "${local.runtime_staging_dir}/Caddyfile.template"
  }

  provisioner "file" {
    content     = local.runtime_tailscale_bootstrap
    destination = "${local.runtime_staging_dir}/tailscale-bootstrap.sh"
  }

  provisioner "file" {
    content     = local.runtime_host_tailscale
    destination = "${local.runtime_staging_dir}/host-tailscale-bootstrap.sh"
  }

  provisioner "file" {
    content     = local.runtime_workspace_dockerfile
    destination = "${local.runtime_staging_dir}/workspace.Dockerfile"
  }

  provisioner "file" {
    content     = local.runtime_workspace_entrypoint
    destination = "${local.runtime_staging_dir}/workspace-entrypoint.sh"
  }

  provisioner "file" {
    content     = local.runtime_workspace_drive_skill
    destination = "${local.runtime_staging_dir}/workspace-para-memory-drive-skill.md"
  }

  provisioner "file" {
    content     = local.runtime_workspace_codex_update
    destination = "${local.runtime_staging_dir}/workspace-codex-update.sh"
  }

  provisioner "file" {
    content     = local.runtime_workspace_codex_control
    destination = "${local.runtime_staging_dir}/workspace-codex-control.py"
  }

  provisioner "file" {
    content     = local.runtime_workspace_update_host
    destination = "${local.runtime_staging_dir}/agent-stack-workspace-codex-update"
  }

  provisioner "file" {
    content     = local.runtime_workspace_update_unit
    destination = "${local.runtime_staging_dir}/agent-stack-workspace-codex-update.service"
  }

  provisioner "file" {
    content     = local.runtime_workspace_update_timer
    destination = "${local.runtime_staging_dir}/agent-stack-workspace-codex-update.timer"
  }

  provisioner "file" {
    content     = local.runtime_vpn_manager
    destination = "${local.runtime_staging_dir}/agent-stack-vpn"
  }

  provisioner "file" {
    content     = local.runtime_vpn_openvpn
    destination = "${local.runtime_staging_dir}/agent-stack-vpn-openvpn"
  }

  provisioner "file" {
    content     = local.runtime_vpn_auth
    destination = "${local.runtime_staging_dir}/vpn-auth.txt"
  }

  provisioner "file" {
    content     = local.runtime_vpn_token
    destination = "${local.runtime_staging_dir}/vpn-token.txt"
  }

  provisioner "file" {
    content     = local.runtime_diagnostics_helper
    destination = "${local.runtime_staging_dir}/agent-stack-diagnostics"
  }

  provisioner "file" {
    content     = local.runtime_diagnostics_ssh_helper
    destination = "${local.runtime_staging_dir}/agent-stack-diagnostics-ssh"
  }

  provisioner "file" {
    content     = local.runtime_layout_migrator
    destination = "${local.runtime_staging_dir}/agent-stack-migrate-layout"
  }

  provisioner "file" {
    content     = local.runtime_mount_volume
    destination = "${local.runtime_staging_dir}/mount-agent-stack-volume.sh"
  }

  provisioner "file" {
    content     = local.runtime_installer
    destination = "${local.runtime_staging_dir}/install-agent-stack.sh"
  }

  provisioner "file" {
    content     = local.runtime_agent_stack_service
    destination = "${local.runtime_staging_dir}/agent-stack.service"
  }

  provisioner "file" {
    content     = local.runtime_openclaw_service
    destination = "${local.runtime_staging_dir}/openclaw.service"
  }

  provisioner "file" {
    content     = local.runtime_watchdog
    destination = "${local.runtime_staging_dir}/agent-stack-tailscale-watchdog"
  }

  provisioner "file" {
    content     = local.runtime_watchdog_service
    destination = "${local.runtime_staging_dir}/agent-stack-tailscale-watchdog.service"
  }

  provisioner "file" {
    content     = local.runtime_watchdog_timer
    destination = "${local.runtime_staging_dir}/agent-stack-tailscale-watchdog.timer"
  }

  provisioner "file" {
    content     = local.runtime_enabled_services
    destination = "${local.runtime_staging_dir}/enabled-services.json"
  }

  provisioner "file" {
    content     = var.openai_codex_auth_json_base64
    destination = "${local.runtime_staging_dir}/openai_codex_auth_json_base64"
  }

  provisioner "file" {
    content     = local.runtime_starter_soul
    destination = "${local.runtime_staging_dir}/templates/SOUL.${var.starter_soul_profile}.md"
  }

  provisioner "file" {
    content     = local.runtime_starter_agents
    destination = "${local.runtime_staging_dir}/templates/AGENTS.default.md"
  }

  provisioner "file" {
    content     = local.runtime_starter_tools
    destination = "${local.runtime_staging_dir}/templates/TOOLS.default.md"
  }

  provisioner "file" {
    content     = local.runtime_starter_user
    destination = "${local.runtime_staging_dir}/templates/USER.default.md"
  }

  provisioner "remote-exec" {
    inline = [
      "chmod 0700 ${local.runtime_staging_dir}",
      "chmod 0600 ${local.runtime_staging_dir}/.env ${local.runtime_staging_dir}/workspace.env ${local.runtime_staging_dir}/openai_codex_auth_json_base64 ${local.runtime_staging_dir}/vpn-auth.txt ${local.runtime_staging_dir}/vpn-token.txt",
      "chmod 0755 ${local.runtime_staging_dir}/install-agent-stack.sh ${local.runtime_staging_dir}/agent-stack-migrate-layout ${local.runtime_staging_dir}/mount-agent-stack-volume.sh ${local.runtime_staging_dir}/tailscale-bootstrap.sh ${local.runtime_staging_dir}/host-tailscale-bootstrap.sh ${local.runtime_staging_dir}/workspace-entrypoint.sh ${local.runtime_staging_dir}/agent-stack-vpn ${local.runtime_staging_dir}/agent-stack-vpn-openvpn ${local.runtime_staging_dir}/agent-stack-diagnostics ${local.runtime_staging_dir}/agent-stack-diagnostics-ssh ${local.runtime_staging_dir}/agent-stack-tailscale-watchdog ${local.runtime_staging_dir}/agent-stack-workspace-codex-update",
      "chmod 0755 ${local.runtime_staging_dir}/workspace-codex-update.sh",
      "chmod 0755 ${local.runtime_staging_dir}/workspace-codex-control.py",
      "sudo bash ${local.runtime_staging_dir}/install-agent-stack.sh ${local.runtime_staging_dir} ${local.runtime_artifact_checksum}",
    ]
  }
}

resource "local_file" "repo_ssh_config" {
  count = var.generate_repo_ssh_config ? 1 : 0

  filename        = "${path.root}/.ssh/config"
  file_permission = "0600"
  content         = <<-EOT
Host ${var.repo_ssh_host_alias}
  HostName ${local.admin_ssh_host}
  User ${var.admin_username}
  IdentityFile ${var.repo_ssh_identity_file}
  IdentitiesOnly yes
  ConnectTimeout 10
  ServerAliveInterval 20
  ServerAliveCountMax 3
  TCPKeepAlive yes
  StrictHostKeyChecking accept-new
EOT
}

# Stable acceptance identities complement the exact, secret-bearing apply
# checksum above. They are recipes, never proof of resolved image contents.
# Trial names, generated credentials/keys, deadlines and resource IDs are absent.
locals {
  proof_runtime_template_vars = {
    hermes_config_mode                             = var.hermes_config_mode
    hermes_dashboard_username                      = var.hermes_dashboard_username
    hermes_dashboard_password                      = "managed-credential"
    hermes_dashboard_secret                        = "managed-credential"
    gcp_device_name                                = var.cloud_provider == "gcp" ? "agent-stack-data" : ""
    provider_type                                  = var.cloud_provider
    project_name                                   = "agent-stack-proof"
    admin_username                                 = var.admin_username
    admin_password                                 = "managed-credential"
    admin_password_ssh_scope                       = var.admin_password_ssh_scope
    host_codex_cli_enabled                         = var.host_codex_cli_enabled
    ebs_volume_id                                  = var.cloud_provider == "aws" ? "owned-data-volume" : ""
    do_volume_name                                 = var.cloud_provider == "digitalocean" ? "owned-data-volume" : ""
    hcloud_volume_id                               = var.cloud_provider == "hetzner" ? "owned-data-volume" : ""
    anthropic_api_key                              = "managed-credential"
    anthropic_auth_key                             = "managed-credential"
    openai_api_key                                 = "managed-credential"
    openai_auth_mode                               = var.openai_auth_mode
    openai_codex_auth_json_base64                  = "managed-credential"
    groq_api_key                                   = "managed-credential"
    gemini_api_key                                 = "managed-credential"
    telegram_bot_token                             = "managed-credential"
    telegram_allow_from_json                       = jsonencode(var.telegram_allow_from)
    openclaw_config_mode                           = var.openclaw_config_mode
    agent_channel                                  = var.agent_channel
    model_providers_enabled_json                   = jsonencode(var.model_providers_enabled)
    default_model                                  = var.default_model
    fallback_models_json                           = jsonencode(var.fallback_models)
    tailscale_enabled                              = var.tailscale_enabled
    tailscale_auth_key                             = "managed-credential"
    openclaw_version                               = var.openclaw_version
    openclaw_image                                 = var.openclaw_image_digest != "" ? "ghcr.io/openclaw/openclaw@${var.openclaw_image_digest}" : "ghcr.io/openclaw/openclaw:${var.openclaw_version}"
    openclaw_gateway_bind                          = local.tailscale_sidecar_enabled || var.openclaw_host_network_enabled ? "loopback" : "lan"
    openclaw_host_network_enabled                  = var.openclaw_host_network_enabled
    openclaw_node_options                          = var.openclaw_node_options
    openclaw_swap_size_mb                          = var.openclaw_swap_size_mb
    openclaw_health_start_period_seconds           = var.openclaw_health_start_period_seconds
    openclaw_health_retries                        = var.openclaw_health_retries
    seed_starter_workspace_files                   = var.seed_starter_workspace_files
    starter_soul_profile                           = var.starter_soul_profile
    gateway_token                                  = "managed-credential"
    enabled_services_json                          = jsonencode(var.enabled_services)
    openclaw_enabled                               = local.openclaw_enabled
    hermes_enabled                                 = local.hermes_enabled
    n8n_enabled                                    = local.n8n_enabled
    local_postgres_enabled                         = local.local_postgres_enabled
    caddy_enabled                                  = var.public_domain_enabled
    hermes_image                                   = var.hermes_image
    hermes_npm_security_patch_enabled              = var.hermes_npm_security_patch_enabled
    hermes_dashboard_enabled                       = var.hermes_dashboard_enabled
    hermes_api_server_enabled                      = var.hermes_api_server_enabled
    hermes_api_server_key                          = "managed-credential"
    n8n_image                                      = var.n8n_image
    n8n_database_mode                              = var.n8n_database_mode
    n8n_encryption_key                             = "managed-credential"
    n8n_public_webhooks_enabled                    = var.n8n_public_webhooks_enabled
    n8n_generic_timezone                           = var.n8n_generic_timezone
    postgres_image                                 = var.postgres_image
    postgres_database                              = var.postgres_database
    postgres_user                                  = var.postgres_user
    postgres_password                              = "managed-credential"
    n8n_postgres_host                              = local.resolved_n8n_postgres_host
    n8n_postgres_port                              = local.resolved_n8n_postgres_port
    n8n_postgres_database                          = local.resolved_n8n_postgres_database
    n8n_postgres_user                              = local.resolved_n8n_postgres_user
    n8n_postgres_password                          = "managed-credential"
    n8n_postgres_ssl_enabled                       = local.resolved_n8n_postgres_ssl
    public_domain_enabled                          = var.public_domain_enabled
    openclaw_domain                                = local.resolved_openclaw_domain
    hermes_domain                                  = local.resolved_hermes_domain
    n8n_domain                                     = local.resolved_n8n_domain
    acme_email                                     = var.acme_email
    ui_auth_mode                                   = var.ui_auth_mode
    ui_auth_username                               = var.ui_auth_username
    ui_auth_password                               = "managed-credential"
    workspace_enabled                              = local.workspace_enabled
    workspace_claude_enabled                       = var.workspace_claude_enabled
    workspace_claude_release                       = var.workspace_claude_release
    workspace_codex_release                        = var.workspace_codex_release
    workspace_codex_sandbox_enabled                = var.workspace_codex_sandbox_enabled
    workspace_codex_auto_update_enabled            = var.workspace_codex_auto_update_enabled
    workspace_codex_auto_update_timezone           = trimspace(var.workspace_codex_auto_update_timezone)
    workspace_codex_auto_update_time               = trimspace(var.workspace_codex_auto_update_time)
    workspace_codex_auto_recover_interrupted_turns = var.workspace_codex_auto_recover_interrupted_turns
    workspace_username                             = var.workspace_username
    workspace_password                             = "managed-credential"
    workspace_password_hash                        = "managed-credential"
    workspace_ssh_host_port                        = var.workspace_ssh_host_port
    workspace_ssh_host                             = trimspace(var.workspace_ssh_host_override) != "" ? trimspace(var.workspace_ssh_host_override) : "agent-stack-proof"
    workspace_ssh_public_keys_base64               = "trial-generated-public-key"
    vpn_enabled                                    = var.vpn_enabled
    vpn_provider                                   = var.vpn_provider
    vpn_nordvpn_connect_target                     = var.vpn_nordvpn_connect_target
    vpn_openvpn_config_url                         = var.vpn_openvpn_config_url
    vpn_bypass_cidrs_json                          = jsonencode(var.vpn_bypass_cidrs)
    vpn_disable_ipv6                               = var.vpn_disable_ipv6
    vpn_healthcheck_url                            = var.vpn_healthcheck_url
    tailscale_mode                                 = var.tailscale_mode
    tailscale_sidecar_enabled                      = local.tailscale_sidecar_enabled
    tailscale_host_enabled                         = local.tailscale_host_enabled
  }
  proof_runtime_sources = {
    for name in fileset(path.module, "modules/common/{runtime,templates}/**") : name => filesha256("${path.module}/${name}")
  }
  proof_runtime_checksum = nonsensitive(sha256(jsonencode({
    schema       = 1
    settings     = local.proof_runtime_template_vars
    sources      = local.proof_runtime_sources
    hermes_model = trimspace(var.hermes_model) != "" ? trimspace(var.hermes_model) : var.default_model
    drive_skill  = filesha256("${path.module}/skills/para-memory-drive/SKILL.md")
    # Bind root rendering and apply logic, without hashing other cloud modules.
    root_runtime = sha256(regex("(?s)  runtime_template_vars = \\{.*?  runtime_staging_dir = [^\n]*", file("${path.module}/main.tf")))
  })))
  proof_provider_settings = {
    aws          = { admin_username = var.admin_username, allowed_ssh_cidr = var.allowed_ssh_cidr, aws_ami_id = var.aws_ami_id, aws_disk_size_gb = var.aws_disk_size_gb, aws_existing_volume_id = var.aws_existing_volume_id, aws_instance_type = var.aws_instance_type, aws_region = var.aws_region, aws_showcase_cost_controls = var.aws_showcase_cost_controls }
    digitalocean = { admin_username = var.admin_username, allowed_ssh_cidr = var.allowed_ssh_cidr, do_disk_size_gb = var.do_disk_size_gb, do_droplet_size = var.do_droplet_size, do_existing_volume_id = var.do_existing_volume_id, do_existing_volume_name = var.do_existing_volume_name, do_project_id = var.do_project_id, do_region = var.do_region }
    hetzner      = { admin_username = var.admin_username, allowed_ssh_cidr = var.allowed_ssh_cidr, hcloud_disk_size_gb = var.hcloud_disk_size_gb, hcloud_existing_volume_id = var.hcloud_existing_volume_id, hcloud_image = var.hcloud_image, hcloud_location = var.hcloud_location, hcloud_pin_boot_image = var.hcloud_pin_boot_image, hcloud_server_type = var.hcloud_server_type }
    gcp          = { admin_username = var.admin_username, allowed_ssh_cidr = var.allowed_ssh_cidr, gcp_boot_disk_size_gb = var.gcp_boot_disk_size_gb, gcp_disk_size_gb = var.gcp_disk_size_gb, gcp_image = var.gcp_image, gcp_machine_type = var.gcp_machine_type, gcp_pin_boot_image = var.gcp_pin_boot_image, gcp_project_id = var.gcp_project_id, gcp_region = var.gcp_region, gcp_showcase_cost_controls = var.gcp_showcase_cost_controls, gcp_zone = var.gcp_zone }
  }
  deployment_input_fingerprints = {
    schema   = 1
    provider = var.cloud_provider
    runtime  = local.proof_runtime_checksum
    deployment = sha256(jsonencode({
      schema                 = 1
      provider               = var.cloud_provider
      settings               = local.proof_provider_settings[var.cloud_provider]
      trial_egress_cap_bytes = var.showcase_trial_limits == null ? null : var.showcase_trial_limits.egress_cap_bytes
      sources                = { for name in fileset("${path.module}/modules/${var.cloud_provider}", "**") : name => filesha256("${path.module}/modules/${var.cloud_provider}/${name}") }
      root_provider          = sha256(regex("(?s)module \"${var.cloud_provider}\" \\{.*?\n}", file("${path.module}/main.tf")))
      provider_versions      = filesha256("${path.module}/versions.tf")
      provider_lock          = filesha256("${path.module}/.terraform.lock.hcl")
    }))
  }
}
