variable "project_name" { type = string }
variable "admin_username" { type = string }
variable "do_region" { type = string }
variable "do_project_id" {
  type    = string
  default = ""
}
variable "do_droplet_size" { type = string }
variable "do_disk_size_gb" { type = number }

variable "do_existing_volume_id" {
  type    = string
  default = ""
}

variable "do_existing_volume_name" {
  type    = string
  default = ""
}

variable "ssh_public_key" { type = string }
variable "allowed_ssh_cidr" { type = string }
variable "tailscale_enabled" { type = bool }

variable "tailscale_auth_key" {
  type      = string
  sensitive = true
}

variable "anthropic_api_key" {
  type      = string
  sensitive = true
}

variable "openai_api_key" {
  type      = string
  sensitive = true
}

variable "openai_auth_mode" { type = string }

variable "openai_codex_auth_json_base64" {
  type      = string
  sensitive = true
}

variable "groq_api_key" {
  type      = string
  sensitive = true
}

variable "gemini_api_key" {
  type      = string
  sensitive = true
}

variable "anthropic_auth_key" {
  type      = string
  sensitive = true
}

variable "telegram_bot_token" {
  type      = string
  sensitive = true
}

variable "telegram_allow_from" {
  type = list(string)
}

variable "openclaw_config_mode" { type = string }
variable "agent_channel" { type = string }
variable "model_providers_enabled" { type = list(string) }
variable "default_model" { type = string }
variable "fallback_models" { type = list(string) }

variable "openclaw_version" { type = string }
variable "openclaw_node_options" { type = string }
variable "openclaw_swap_size_mb" { type = number }
variable "openclaw_health_start_period_seconds" { type = number }
variable "openclaw_health_retries" { type = number }
variable "seed_starter_workspace_files" { type = bool }
variable "starter_soul_profile" { type = string }

variable "gateway_token" {
  type      = string
  sensitive = true
}

variable "enabled_services" { type = list(string) }
variable "hermes_image" { type = string }
variable "hermes_dashboard_enabled" { type = bool }
variable "hermes_api_server_enabled" { type = bool }

variable "hermes_api_server_key" {
  type      = string
  sensitive = true
}

variable "n8n_image" { type = string }
variable "n8n_database_mode" { type = string }

variable "n8n_encryption_key" {
  type      = string
  sensitive = true
}

variable "n8n_public_webhooks_enabled" { type = bool }
variable "n8n_generic_timezone" { type = string }
variable "postgres_image" { type = string }
variable "postgres_database" { type = string }
variable "postgres_user" { type = string }

variable "postgres_password" {
  type      = string
  sensitive = true
}

variable "n8n_postgres_host" { type = string }
variable "n8n_postgres_port" { type = number }
variable "n8n_postgres_database" { type = string }
variable "n8n_postgres_user" { type = string }

variable "n8n_postgres_password" {
  type      = string
  sensitive = true
}

variable "n8n_postgres_ssl_enabled" { type = bool }
variable "public_domain_enabled" { type = bool }
variable "openclaw_domain" { type = string }
variable "hermes_domain" { type = string }
variable "n8n_domain" { type = string }
variable "acme_email" { type = string }
variable "ui_auth_mode" { type = string }
variable "ui_auth_username" { type = string }

variable "ui_auth_password" {
  type      = string
  sensitive = true
}
