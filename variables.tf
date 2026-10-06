# ─────────────────────────────────────────────────────────
# Provider selection
# ─────────────────────────────────────────────────────────

variable "cloud_provider" {
  description = "Which cloud provider to deploy on: aws, digitalocean, hetzner, or gcp."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "digitalocean", "hetzner", "gcp"], var.cloud_provider)
    error_message = "cloud_provider must be aws, digitalocean, hetzner, or gcp."
  }
}

variable "project_name" {
  description = "Short name used to tag/name all created resources."
  type        = string
  default     = "agent-stack"
  validation {
    condition     = var.cloud_provider != "gcp" || can(regex("^[a-z]([a-z0-9-]{0,37}[a-z0-9])?$", var.project_name))
    error_message = "For GCP, project_name must use lowercase letters, digits and hyphens, start with a letter, end with a letter/digit, and be at most 39 characters (leaving room for resource suffixes)."
  }
}

variable "admin_username" {
  description = "OS username to use for SSH/admin access across providers."
  type        = string
  default     = "admin"

  validation {
    condition = (
      can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.admin_username)) &&
      var.admin_username != "root"
    )
    error_message = "admin_username must be a valid Linux username and cannot be \"root\"."
  }
}

variable "admin_ssh_host_override" {
  description = "Optional SSH host or IP for Terraform runtime provisioning and admin helper outputs. Blank uses the cloud provider public IP. Set to a Tailscale DNS name when public SSH is intentionally unavailable."
  type        = string
  default     = ""

  validation {
    condition     = trimspace(var.admin_ssh_host_override) == "" || !can(regex("\\s", trimspace(var.admin_ssh_host_override)))
    error_message = "admin_ssh_host_override must be blank or a single host/IP value without spaces."
  }
}

variable "admin_password" {
  description = "Optional password for the SSH/admin user. Blank keeps the managed admin password locked and key-only. If set, also set admin_password_ssh_scope."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition = (
      trimspace(var.admin_password) == "" ||
      length(var.admin_password) >= 14
    )
    error_message = "admin_password must be blank or at least 14 characters."
  }
}

variable "admin_password_ssh_scope" {
  description = "Controls where SSH password login is accepted for the admin user: disabled, tailnet, or public. public requires allowed_ssh_cidr to be narrower than 0.0.0.0/0."
  type        = string
  default     = "disabled"

  validation {
    condition     = contains(["disabled", "tailnet", "public"], var.admin_password_ssh_scope)
    error_message = "admin_password_ssh_scope must be one of: disabled, tailnet, public."
  }

  validation {
    condition = (
      (trimspace(var.admin_password) == "" && var.admin_password_ssh_scope == "disabled") ||
      (trimspace(var.admin_password) != "" && var.admin_password_ssh_scope != "disabled")
    )
    error_message = "admin_password and admin_password_ssh_scope must be set together: leave both disabled/blank, or set a password with tailnet/public scope."
  }

  validation {
    condition = (
      var.admin_password_ssh_scope != "tailnet" ||
      (var.tailscale_enabled && var.tailscale_mode == "host")
    )
    error_message = "admin_password_ssh_scope = \"tailnet\" requires tailscale_enabled = true and tailscale_mode = \"host\"."
  }

  validation {
    condition = (
      var.admin_password_ssh_scope != "public" ||
      trimspace(var.allowed_ssh_cidr) != "0.0.0.0/0"
    )
    error_message = "admin_password_ssh_scope = \"public\" requires allowed_ssh_cidr to be narrower than 0.0.0.0/0."
  }
}

variable "host_codex_cli_enabled" {
  description = "Install Codex CLI on the VM host for admin troubleshooting. Authentication is created on the host with codex login --device-auth; local Codex auth is not copied."
  type        = bool
  default     = true
}

# ─────────────────────────────────────────────────────────
# AWS
# ─────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "aws_access_key" {
  description = "AWS access key ID. Leave blank to rely on environment variables or instance profiles."
  type        = string
  default     = ""
  sensitive   = true
}

variable "aws_secret_key" {
  description = "AWS secret access key. Leave blank to rely on environment variables or instance profiles."
  type        = string
  default     = ""
  sensitive   = true
}

variable "aws_instance_type" {
  description = "EC2 instance type. Default t3.small (1 vCPU / 2 GB). Use t3.medium for 4 GB, t3.large for 8 GB."
  type        = string
  default     = "t3.small"
}

variable "aws_ami_id" {
  description = "AMI ID to use. Leave blank to auto-select the latest Ubuntu 22.04 LTS in the chosen region."
  type        = string
  default     = ""
}

variable "aws_disk_size_gb" {
  description = "Size in GB for the EBS data volume (separate from the 20 GB OS root disk)."
  type        = number
  default     = 50
}

variable "aws_existing_volume_id" {
  description = "EBS volume ID (e.g. \"vol-0abc123\") to attach instead of creating a new one. Leave blank to create a fresh volume."
  type        = string
  default     = ""
}

# ─────────────────────────────────────────────────────────
# DigitalOcean
# ─────────────────────────────────────────────────────────

variable "do_token" {
  description = "DigitalOcean API token."
  type        = string
  default     = ""
  sensitive   = true
}

variable "do_region" {
  description = "DigitalOcean region slug (e.g. \"nyc3\", \"sfo3\", \"fra1\")."
  type        = string
  default     = "nyc3"
}

variable "do_project_id" {
  description = "Optional existing DigitalOcean project ID for the newly created Droplet and data volume. Blank preserves the default-project behavior."
  type        = string
  default     = ""

  validation {
    condition     = var.do_project_id == "" || can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.do_project_id))
    error_message = "do_project_id must be blank or a DigitalOcean project UUID."
  }
}

variable "do_droplet_size" {
  description = "Droplet size slug. Default s-2vcpu-2gb (Basic 2 GB / 2 vCPU, 60 GB root disk). Use s-2vcpu-4gb for 4 GB RAM."
  type        = string
  default     = "s-2vcpu-2gb"
}

variable "do_disk_size_gb" {
  description = "Size in GB for the extra DigitalOcean Block Storage volume. The Droplet root disk is already 60 GB; this volume is for portable persistent data."
  type        = number
  default     = 20
}

variable "do_existing_volume_id" {
  description = "Existing DigitalOcean volume ID to attach instead of creating a new one. Must also set do_existing_volume_name."
  type        = string
  default     = ""

  validation {
    condition = (
      trimspace(var.do_existing_volume_id) == "" ||
      trimspace(var.do_existing_volume_name) != ""
    )
    error_message = "When do_existing_volume_id is set, do_existing_volume_name must also be set."
  }
}

variable "do_existing_volume_name" {
  description = "Name of the existing DigitalOcean volume (required when do_existing_volume_id is set, used to derive the device path)."
  type        = string
  default     = ""
}

# ─────────────────────────────────────────────────────────
# Hetzner Cloud
# ─────────────────────────────────────────────────────────

variable "hcloud_token" {
  description = "Hetzner Cloud API token."
  type        = string
  default     = ""
  sensitive   = true
}

variable "hcloud_location" {
  description = "Hetzner Cloud location slug (for example \"ash\", \"hil\", \"fsn1\", \"nbg1\", or \"hel1\")."
  type        = string
  default     = "ash"
}

variable "hcloud_server_type" {
  description = "Hetzner Cloud server type. Default cpx21 is sized for the full AgentStack profile."
  type        = string
  default     = "cpx21"
}

variable "hcloud_image" {
  description = "Hetzner Cloud image slug."
  type        = string
  default     = "ubuntu-22.04"
}

variable "hcloud_disk_size_gb" {
  description = "Size in GB for the extra Hetzner Cloud volume."
  type        = number
  default     = 50
}

variable "hcloud_existing_volume_id" {
  description = "Existing Hetzner Cloud volume ID to attach instead of creating a new one."
  type        = string
  default     = ""
}

# ─────────────────────────────────────────────────────────
# SSH / network
# ─────────────────────────────────────────────────────────

variable "ssh_public_key" {
  description = "SSH public key content. If empty, Terraform auto-resolves/creates a repo-local keypair and uses its .pub content."
  type        = string
  default     = ""
}

variable "allowed_ssh_cidr" {
  description = "CIDR that is allowed to reach port 22. Restrict to your own IP for best security (e.g. \"203.0.113.5/32\")."
  type        = string
  default     = "0.0.0.0/0"
}

variable "generate_repo_ssh_config" {
  description = "If true, terraform apply writes ./.ssh/config with the current instance IP and SSH defaults."
  type        = bool
  default     = true
}

variable "repo_ssh_host_alias" {
  description = "Host alias written into ./.ssh/config."
  type        = string
  default     = "agent-stack"
}

variable "repo_ssh_identity_file" {
  description = "IdentityFile value written into ./.ssh/config."
  type        = string
  default     = "./.ssh/id_ed25519_agent_stack"
}

variable "repo_ssh_private_key_path" {
  description = "Repo-relative private key path used for auto key resolution/generation when ssh_public_key is empty."
  type        = string
  default     = ".ssh/id_ed25519_agent_stack"
}

# ─────────────────────────────────────────────────────────
# OpenClaw / LLM API keys
# ─────────────────────────────────────────────────────────

variable "anthropic_api_key" {
  description = "Anthropic API key for Claude models. This is the preferred Anthropic credential for AgentStack."
  type        = string
  default     = ""
  sensitive   = true
}

variable "anthropic_auth_key" {
  description = "Legacy Anthropic setup-token for Claude Code style auth flows. Optional and not recommended for normal AgentStack setup; prefer anthropic_api_key."
  type        = string
  default     = ""
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key for openai/* models when openai_auth_mode = \"api_key\"."
  type        = string
  default     = ""
  sensitive   = true
}

variable "openai_auth_mode" {
  description = "Auth/runtime mode for openai/* models. api_key uses OPENAI_API_KEY; codex routes OpenAI model refs through imported Codex ChatGPT subscription auth."
  type        = string
  default     = "api_key"

  validation {
    condition     = contains(["api_key", "codex"], var.openai_auth_mode)
    error_message = "openai_auth_mode must be one of: api_key, codex."
  }
}

variable "openai_codex_auth_json_base64" {
  description = "Base64-encoded contents of ~/.codex/auth.json for subscription-backed OpenAI models when openai_auth_mode = \"codex\". Leave blank unless you want to import an existing Codex CLI login."
  type        = string
  default     = ""
  sensitive   = true
}

variable "groq_api_key" {
  description = "Groq API key for fast open-weight inference."
  type        = string
  default     = ""
  sensitive   = true
}

variable "gemini_api_key" {
  description = "Google Gemini API key."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_bot_token" {
  description = "Telegram BotFather token used to preconfigure channels.telegram.botToken."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_allow_from" {
  description = "Optional list of pre-approved Telegram user IDs for DM access (channels.telegram.allowFrom)."
  type        = list(string)
  default     = []
}

variable "openclaw_config_mode" {
  description = "Controls bootstrap edits to openclaw.json. auto=preserve existing config / manage fresh installs, manage=always apply starter bootstrap edits, preserve=skip optional edits."
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "manage", "preserve"], var.openclaw_config_mode)
    error_message = "openclaw_config_mode must be one of: auto, manage, preserve."
  }
}

variable "agent_channel" {
  description = "Primary channel plugin to bootstrap: telegram, whatsapp or none for dashboard/API-only operation."
  type        = string
  default     = "telegram"

  validation {
    condition     = contains(["telegram", "whatsapp", "none"], var.agent_channel)
    error_message = "agent_channel must be telegram, whatsapp or none."
  }
}

variable "model_providers_enabled" {
  description = "Required list of model providers to configure at bootstrap. Allowed values: anthropic, openai, google, groq."
  type        = list(string)

  validation {
    condition = (
      length(var.model_providers_enabled) > 0 &&
      alltrue([
        for provider in var.model_providers_enabled : contains(["anthropic", "openai", "google", "groq"], provider)
      ])
    )
    error_message = "model_providers_enabled must include at least one provider and only: anthropic, openai, google, groq."
  }
}

variable "default_model" {
  description = "Required default model reference to set during bootstrap (for example \"anthropic/claude-haiku-4-5\")."
  type        = string

  validation {
    condition     = trimspace(var.default_model) != ""
    error_message = "default_model is required and cannot be empty."
  }
}

variable "fallback_models" {
  description = "Required ordered list of fallback model references to configure at bootstrap (can be an empty list)."
  type        = list(string)
}

variable "openclaw_version" {
  description = "OpenClaw Docker image tag (e.g. \"latest\" or a pinned version)."
  type        = string
  default     = "latest"
}

variable "openclaw_image_digest" {
  description = "Optional immutable sha256 image digest for OpenClaw; blank preserves tag selection. Set openclaw_version to the corresponding release for version-specific compatibility patches."
  type        = string
  default     = ""
  validation {
    condition     = var.openclaw_image_digest == "" || can(regex("^sha256:[a-f0-9]{64}$", var.openclaw_image_digest))
    error_message = "openclaw_image_digest must be blank or sha256 followed by 64 lowercase hex digits."
  }
}

variable "openclaw_node_options" {
  description = "Optional NODE_OPTIONS passed to the OpenClaw container (e.g. \"--max-old-space-size=1536\")."
  type        = string
  default     = ""
}

variable "openclaw_swap_size_mb" {
  description = "Optional swap size in MB created at bootstrap time (set > 0 for small RAM nodes, e.g. 2048)."
  type        = number
  default     = 0
}

variable "openclaw_health_start_period_seconds" {
  description = "Docker healthcheck start_period in seconds for OpenClaw container warm-up."
  type        = number
  default     = 120
}

variable "openclaw_health_retries" {
  description = "Docker healthcheck retries before marking OpenClaw unhealthy."
  type        = number
  default     = 8
}

variable "seed_starter_workspace_files" {
  description = "If true, bootstrap creates starter workspace files (SOUL.md, AGENTS.md, TOOLS.md, USER.md) when missing."
  type        = bool
  default     = true
}

variable "starter_soul_profile" {
  description = "Starter SOUL.md profile to seed. Valid values: \"balanced\", \"builder\", \"researcher\"."
  type        = string
  default     = "balanced"

  validation {
    condition     = contains(["balanced", "builder", "researcher"], var.starter_soul_profile)
    error_message = "starter_soul_profile must be one of: balanced, builder, researcher."
  }
}

variable "gateway_token" {
  description = "Gateway token used by the Control UI/WebSocket auth. Leave blank to auto-generate and persist in Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

# ─────────────────────────────────────────────────────────
# Agent / automation stack
# ─────────────────────────────────────────────────────────

variable "enabled_services" {
  description = "Services to run on the instance. Allowed values: openclaw, hermes, n8n, workspace."
  type        = list(string)
  default     = ["openclaw", "hermes", "n8n"]

  validation {
    condition = (
      length(var.enabled_services) > 0 &&
      alltrue([
        for service in var.enabled_services : contains(["openclaw", "hermes", "n8n", "workspace"], service)
      ])
    )
    error_message = "enabled_services must include at least one service and only: openclaw, hermes, n8n, workspace."
  }
}

variable "workspace_claude_enabled" {
  description = "Install Claude Code alongside Codex in the workspace. Requires an explicit workspace_claude_release."
  type        = bool
  default     = false
  validation {
    condition     = !var.workspace_claude_enabled || contains(var.enabled_services, "workspace")
    error_message = "workspace_claude_enabled requires the workspace service."
  }
}

variable "workspace_claude_release" {
  description = "Exact native Claude Code release; required when workspace_claude_enabled is true."
  type        = string
  default     = ""
  validation {
    condition     = !var.workspace_claude_enabled || can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.workspace_claude_release))
    error_message = "workspace_claude_release must be an exact x.y.z version when Claude Code is enabled."
  }
}

variable "workspace_codex_release" {
  description = "Pinned Codex CLI release baked into the optional workspace image. Use an explicit stable, alpha, or beta release version."
  type        = string
  default     = "0.145.0"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+(-(alpha|beta)(\\.[0-9]+)?)?$", trimspace(var.workspace_codex_release)))
    error_message = "workspace_codex_release must be an explicit Codex version such as 0.145.0, 0.145.0-alpha.18, or 0.145.0-beta.1."
  }
}

variable "workspace_codex_auto_update_enabled" {
  description = "Opt in to a root-managed workspace Codex updater. It checks the latest stable release daily at workspace_codex_auto_update_time in workspace_codex_auto_update_timezone without rebuilding the stack. It requires a stable pinned image fallback. A new release may interrupt active Codex work when the app server restarts."
  type        = bool
  default     = false

  validation {
    condition = !var.workspace_codex_auto_update_enabled || (
      contains(var.enabled_services, "workspace") &&
      can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", trimspace(var.workspace_codex_release)))
    )
    error_message = "workspace_codex_auto_update_enabled requires enabled_services to include \"workspace\" and workspace_codex_release to be a stable x.y.z fallback."
  }
}

variable "workspace_codex_auto_update_timezone" {
  description = "IANA timezone used for the daily workspace Codex auto-update timer, such as America/New_York."
  type        = string
  default     = "America/New_York"

  validation {
    condition     = trimspace(var.workspace_codex_auto_update_timezone) == var.workspace_codex_auto_update_timezone && can(regex("^(UTC|[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z][A-Za-z0-9_+-]*)+)$", var.workspace_codex_auto_update_timezone))
    error_message = "workspace_codex_auto_update_timezone must be UTC or an IANA timezone such as America/New_York."
  }
}

variable "workspace_codex_auto_update_time" {
  description = "Local daily time for the workspace Codex auto-update timer in 24-hour HH:MM format."
  type        = string
  default     = "04:00"

  validation {
    condition     = trimspace(var.workspace_codex_auto_update_time) == var.workspace_codex_auto_update_time && can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.workspace_codex_auto_update_time))
    error_message = "workspace_codex_auto_update_time must use 24-hour HH:MM format, such as 04:00."
  }
}

variable "workspace_codex_auto_recover_interrupted_turns" {
  description = "After a verified visual end-to-end probe, opt in to one safety-constrained recovery turn for each proven Codex turn interrupted by the scheduled updater."
  type        = bool
  default     = false

  validation {
    condition = (
      !var.workspace_codex_auto_recover_interrupted_turns || (
        var.workspace_codex_auto_update_enabled &&
        contains(var.enabled_services, "workspace")
      )
    )
    error_message = "workspace_codex_auto_recover_interrupted_turns requires workspace_codex_auto_update_enabled = true and enabled_services to include \"workspace\"."
  }
}

variable "workspace_username" {
  description = "Username created inside the optional workspace container."
  type        = string
  default     = "user"

  validation {
    condition = (
      can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.workspace_username)) &&
      var.workspace_username != "root"
    )
    error_message = "workspace_username must be a valid Linux username and cannot be \"root\"."
  }
}

variable "workspace_password" {
  description = "Password for SSH access to the optional workspace container. Required with workspace unless workspace_password_hash is supplied. Prefer the hash for automated runs."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition = (
      !contains(var.enabled_services, "workspace") ||
      trimspace(var.workspace_password) != "" ||
      var.workspace_password_hash != ""
    )
    error_message = "workspace_password or workspace_password_hash must be set when enabled_services includes workspace."
  }

  validation {
    condition     = var.workspace_password == "" || var.workspace_password_hash == ""
    error_message = "Supply only one of workspace_password and workspace_password_hash."
  }
}

variable "workspace_password_hash" {
  description = "Optional salted SHA-512 crypt password hash for workspace SSH. The plaintext password is never needed by Terraform when this is set. The hash remains sensitive and is saved in private state/runtime files."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.workspace_password_hash == "" || can(regex("^\\$6\\$(rounds=[1-9][0-9]{3,8}\\$)?[./A-Za-z0-9]{8,16}\\$[./A-Za-z0-9]{86}$", var.workspace_password_hash))
    error_message = "workspace_password_hash must be blank or a salted SHA-512 crypt hash."
  }
}

variable "workspace_ssh_host_port" {
  description = "Host port mapped to SSH inside the optional workspace container. Do not open this port in provider firewalls."
  type        = number
  default     = 2222

  validation {
    condition     = var.workspace_ssh_host_port >= 1024 && var.workspace_ssh_host_port <= 65535
    error_message = "workspace_ssh_host_port must be between 1024 and 65535."
  }
}

variable "workspace_ssh_host_override" {
  description = "Optional SSH host or IP to show for workspace connection outputs. Blank uses project_name, which is usually the Tailscale device name."
  type        = string
  default     = ""

  validation {
    condition     = trimspace(var.workspace_ssh_host_override) == "" || !can(regex("\\s", trimspace(var.workspace_ssh_host_override)))
    error_message = "workspace_ssh_host_override must be blank or a single host/IP value without spaces."
  }
}

variable "workspace_ssh_public_keys" {
  description = "OpenSSH public keys authorized for the optional workspace user. Supply public key strings only, never private keys or local agent/socket paths."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for key in var.workspace_ssh_public_keys : can(regex("^(ssh-(rsa|ed25519)|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)\\s+\\S+(\\s+.*)?$", trimspace(key)))
    ])
    error_message = "workspace_ssh_public_keys entries must be OpenSSH public key strings, not private keys, file paths, or agent socket paths."
  }
}

# ─────────────────────────────────────────────────────────
# Host VPN
# ─────────────────────────────────────────────────────────

variable "vpn_enabled" {
  description = "Install and start a host-level VPN so Docker service egress routes through the VPN tunnel. Disabled by default because route changes can break remote access if bypass CIDRs are wrong."
  type        = bool
  default     = false
}

variable "vpn_provider" {
  description = "VPN integration to configure. Supports NordVPN manual OpenVPN and the native Linux app with NordLynx."
  type        = string
  default     = "nordvpn_openvpn"

  validation {
    condition     = contains(["nordvpn_openvpn", "nordvpn_nordlynx"], var.vpn_provider)
    error_message = "vpn_provider must be \"nordvpn_openvpn\" or \"nordvpn_nordlynx\"."
  }
}

variable "vpn_nordvpn_token" {
  description = "Nord Account access token used by the headless NordVPN Linux app. Required only for vpn_provider = \"nordvpn_nordlynx\"."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition = (
      !var.vpn_enabled ||
      var.vpn_provider != "nordvpn_nordlynx" ||
      trimspace(var.vpn_nordvpn_token) != ""
    )
    error_message = "vpn_nordvpn_token is required when vpn_enabled is true and vpn_provider is \"nordvpn_nordlynx\"."
  }
}

variable "vpn_nordvpn_connect_target" {
  description = "Optional NordVPN Linux app connection target such as United_States or us1234. Blank lets NordVPN choose the globally recommended server."
  type        = string
  default     = ""

  validation {
    condition = (
      trimspace(var.vpn_nordvpn_connect_target) == "" ||
      can(regex("^[A-Za-z0-9_-]+$", trimspace(var.vpn_nordvpn_connect_target)))
    )
    error_message = "vpn_nordvpn_connect_target must be blank or contain only letters, numbers, underscores, and hyphens."
  }
}

variable "vpn_openvpn_config_url" {
  description = "HTTPS URL to a provider OpenVPN .ovpn config file, for example a NordVPN UDP server config from downloads.nordcdn.com."
  type        = string
  default     = ""

  validation {
    condition = (
      !var.vpn_enabled ||
      var.vpn_provider != "nordvpn_openvpn" ||
      can(regex("^https://", trimspace(var.vpn_openvpn_config_url)))
    )
    error_message = "vpn_openvpn_config_url must be an https:// URL when the enabled VPN provider is \"nordvpn_openvpn\"."
  }
}

variable "vpn_username" {
  description = "VPN service username. For NordVPN, use the manual service credential username from the Nord Account dashboard, not your account email."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition = (
      !var.vpn_enabled ||
      var.vpn_provider != "nordvpn_openvpn" ||
      trimspace(var.vpn_username) != ""
    )
    error_message = "vpn_username is required when the enabled VPN provider is \"nordvpn_openvpn\"."
  }
}

variable "vpn_password" {
  description = "VPN service password. For NordVPN, use the manual service credential password from the Nord Account dashboard, not your account password."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition = (
      !var.vpn_enabled ||
      var.vpn_provider != "nordvpn_openvpn" ||
      trimspace(var.vpn_password) != ""
    )
    error_message = "vpn_password is required when the enabled VPN provider is \"nordvpn_openvpn\"."
  }
}

variable "vpn_bypass_cidrs" {
  description = "IPv4 CIDRs that must keep using the original host gateway after the VPN connects, such as your current public admin SSH /32. Do not add Tailscale 100.64.0.0/10 here; Tailscale routes are preserved separately."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.vpn_bypass_cidrs :
      can(cidrhost(cidr, 0)) &&
      can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$", cidr))
    ])
    error_message = "vpn_bypass_cidrs must contain IPv4 CIDRs such as \"203.0.113.5/32\"."
  }

  validation {
    condition = alltrue([
      for cidr in var.vpn_bypass_cidrs :
      can(cidrhost(cidr, 0)) ? !(
        can(regex("^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.", cidrhost(cidr, 0))) ||
        can(regex(
          "^(0\\.0\\.0\\.0/[01]|64\\.0\\.0\\.0/2|96\\.0\\.0\\.0/[3-5]|100\\.0\\.0\\.0/[6-9])$",
          "${cidrhost(cidr, 0)}/${split("/", cidr)[1]}"
        ))
      ) : true
    ])
    error_message = "vpn_bypass_cidrs must not contain or overlap Tailscale 100.64.0.0/10; AgentStack preserves Tailscale separately."
  }

  validation {
    condition     = !var.vpn_enabled || length(var.vpn_bypass_cidrs) > 0
    error_message = "vpn_bypass_cidrs must include at least one non-Tailscale access CIDR when vpn_enabled is true so public fallback SSH is not routed into the VPN tunnel."
  }
}

variable "vpn_disable_ipv6" {
  description = "Disable host IPv6 while the host VPN is enabled to avoid IPv6 egress bypassing the managed VPN tunnel."
  type        = bool
  default     = true
}

variable "vpn_healthcheck_url" {
  description = "URL used by diagnostics to report the current public egress IP when VPN is enabled."
  type        = string
  default     = "https://api.ipify.org"
}

variable "hermes_model" {
  description = "Optional Hermes model reference; blank inherits default_model."
  type        = string
  default     = ""
}

variable "hermes_config_mode" {
  description = "Hermes model bootstrap: auto seeds fresh configs, preserve skips changes, manage replaces the model in AgentStack JSON configs (operator-written YAML fails safely)."
  type        = string
  default     = "auto"
  validation {
    condition     = contains(["auto", "manage", "preserve"], var.hermes_config_mode)
    error_message = "hermes_config_mode must be auto, manage or preserve."
  }
}

variable "hermes_dashboard_username" {
  description = "Username for the private Hermes dashboard's native login."
  type        = string
  default     = "admin"
}

variable "hermes_dashboard_password" {
  description = "Native Hermes dashboard password; blank generates a stable Terraform-managed value."
  type        = string
  default     = ""
  sensitive   = true
}

variable "hermes_dashboard_secret" {
  description = "Hermes dashboard session signing secret; blank generates a stable value."
  type        = string
  default     = ""
  sensitive   = true
}

variable "hermes_image" {
  description = "Hermes Agent Docker image."
  type        = string
  default     = "nousresearch/hermes-agent:latest"
}

variable "hermes_dashboard_enabled" {
  description = "Run the Hermes dashboard side-process inside the Hermes gateway container."
  type        = bool
  default     = true
}

variable "hermes_api_server_enabled" {
  description = "Enable the Hermes OpenAI-compatible API server."
  type        = bool
  default     = true
}

variable "hermes_api_server_key" {
  description = "Hermes API server key. Leave blank to auto-generate and persist in Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

variable "n8n_image" {
  description = "n8n Docker image."
  type        = string
  default     = "docker.n8n.io/n8nio/n8n:stable"
}

variable "n8n_database_mode" {
  description = "Database mode for n8n. local_postgres runs Postgres on the persistent volume; external_postgres uses the supplied connection settings."
  type        = string
  default     = "local_postgres"

  validation {
    condition     = contains(["local_postgres", "external_postgres"], var.n8n_database_mode)
    error_message = "n8n_database_mode must be \"local_postgres\" or \"external_postgres\"."
  }
}

variable "n8n_encryption_key" {
  description = "n8n encryption key. Leave blank to auto-generate and persist in Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

variable "n8n_public_webhooks_enabled" {
  description = "When public domains are enabled, leave n8n webhook routes unauthenticated for external services."
  type        = bool
  default     = true
}

variable "n8n_generic_timezone" {
  description = "Timezone passed to n8n's GENERIC_TIMEZONE setting."
  type        = string
  default     = "America/New_York"
}

variable "postgres_image" {
  description = "Postgres Docker image used when n8n_database_mode = local_postgres."
  type        = string
  default     = "postgres:17-alpine"
}

variable "postgres_database" {
  description = "Local Postgres database name for n8n."
  type        = string
  default     = "n8n"
}

variable "postgres_user" {
  description = "Local Postgres username for n8n."
  type        = string
  default     = "n8n"
}

variable "postgres_password" {
  description = "Local Postgres password. Leave blank to auto-generate and persist in Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

variable "external_postgres_host" {
  description = "External Postgres host used when n8n_database_mode = external_postgres."
  type        = string
  default     = ""
}

variable "external_postgres_port" {
  description = "External Postgres port used when n8n_database_mode = external_postgres."
  type        = number
  default     = 5432
}

variable "external_postgres_database" {
  description = "External Postgres database used when n8n_database_mode = external_postgres."
  type        = string
  default     = "n8n"
}

variable "external_postgres_user" {
  description = "External Postgres user used when n8n_database_mode = external_postgres."
  type        = string
  default     = "n8n"
}

variable "external_postgres_password" {
  description = "External Postgres password used when n8n_database_mode = external_postgres."
  type        = string
  default     = ""
  sensitive   = true
}

variable "external_postgres_ssl_enabled" {
  description = "Enable SSL for n8n's external Postgres connection."
  type        = bool
  default     = true
}

variable "public_domain_enabled" {
  description = "Expose selected UIs through public HTTPS domains behind a reverse-proxy login."
  type        = bool
  default     = false
}

variable "base_domain" {
  description = "Base domain used to derive service hostnames when public_domain_enabled = true (openclaw.<base>, hermes.<base>, n8n.<base>)."
  type        = string
  default     = ""
}

variable "openclaw_domain" {
  description = "Explicit public domain for the OpenClaw UI. Overrides base_domain derivation."
  type        = string
  default     = ""
}

variable "hermes_domain" {
  description = "Explicit public domain for the Hermes dashboard/API. Overrides base_domain derivation."
  type        = string
  default     = ""
}

variable "n8n_domain" {
  description = "Explicit public domain for the n8n UI and webhooks. Overrides base_domain derivation."
  type        = string
  default     = ""
}

variable "acme_email" {
  description = "Email address Caddy uses for ACME certificate registration. Optional, but recommended when public domains are enabled."
  type        = string
  default     = ""
}

variable "ui_auth_mode" {
  description = "Reverse-proxy UI auth mode. v1 supports basic auth only."
  type        = string
  default     = "basic"

  validation {
    condition     = contains(["basic"], var.ui_auth_mode)
    error_message = "ui_auth_mode must be \"basic\"."
  }
}

variable "ui_auth_username" {
  description = "Username for the public-domain reverse-proxy login."
  type        = string
  default     = "admin"
}

variable "ui_auth_password" {
  description = "Password for the public-domain reverse-proxy login. Leave blank to auto-generate and persist in Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

# ─────────────────────────────────────────────────────────
# Tailscale
# ─────────────────────────────────────────────────────────

variable "tailscale_enabled" {
  description = "Install Tailscale for private access to selected AgentStack UIs. Strongly recommended — disabling this leaves UIs accessible only via SSH tunnel unless public domains are enabled."
  type        = bool
  default     = true
}

variable "tailscale_mode" {
  description = "How to run Tailscale when enabled. sidecar preserves the existing Docker sidecar; host installs and manages tailscaled directly on the VM."
  type        = string
  default     = "sidecar"

  validation {
    condition     = contains(["sidecar", "host"], var.tailscale_mode)
    error_message = "tailscale_mode must be one of: sidecar, host."
  }
}

variable "tailscale_auth_key" {
  description = "Tailscale auth key (generate one-time or reusable keys at https://login.tailscale.com/admin/settings/keys)."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition = (
      var.tailscale_enabled == false ||
      trimspace(var.tailscale_auth_key) != ""
    )
    error_message = "tailscale_auth_key must be set when tailscale_enabled is true."
  }
}

# Google Cloud
# Credentials use Google's normal application-default/workload identity chain.
# Project/billing creation is a separate, explicit execution prerequisite.
variable "gcp_project_id" {
  description = "Existing Google Cloud project ID with Compute Engine API enabled. Required only for gcp."
  type        = string
  default     = ""
  validation {
    condition     = var.cloud_provider != "gcp" || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.gcp_project_id))
    error_message = "gcp_project_id must be a valid Google project ID when cloud_provider is gcp."
  }
}
variable "gcp_region" {
  description = "Google Cloud region."
  type        = string
  default     = "us-east1"
}
variable "gcp_zone" {
  description = "Google Cloud zone in gcp_region."
  type        = string
  default     = "us-east1-b"
  validation {
    condition     = var.cloud_provider != "gcp" || startswith(var.gcp_zone, "${var.gcp_region}-")
    error_message = "gcp_zone must belong to gcp_region."
  }
}
variable "gcp_machine_type" {
  description = "Compute Engine machine type; e2-standard-2 provides 2 vCPU and 8 GiB for the showcase."
  type        = string
  default     = "e2-standard-2"
}
variable "gcp_image" {
  description = "Ubuntu image family or immutable image reference for Compute Engine."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2204-lts"
}
variable "gcp_pin_boot_image" {
  description = "Resolve the standard Ubuntu family during planning so the saved showcase plan uses that exact image."
  type        = bool
  default     = false
  validation {
    condition     = !var.gcp_pin_boot_image || var.gcp_image == "ubuntu-os-cloud/ubuntu-2204-lts"
    error_message = "gcp_pin_boot_image requires the standard ubuntu-os-cloud/ubuntu-2204-lts family."
  }
}
variable "gcp_disk_size_gb" {
  description = "Size of the separate persistent data disk."
  type        = number
  default     = 50
  validation {
    condition     = var.gcp_disk_size_gb >= 10 && floor(var.gcp_disk_size_gb) == var.gcp_disk_size_gb
    error_message = "gcp_disk_size_gb must be an integer of at least 10."
  }
}
variable "gcp_boot_disk_size_gb" {
  description = "Boot disk size; leave room for the runtime images and workspace build."
  type        = number
  default     = 40
  validation {
    condition     = var.gcp_boot_disk_size_gb >= 20 && floor(var.gcp_boot_disk_size_gb) == var.gcp_boot_disk_size_gb
    error_message = "gcp_boot_disk_size_gb must be an integer of at least 20."
  }
}
