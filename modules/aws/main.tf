terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Data sources
# ─────────────────────────────────────────────────────────────────────────────

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ami" "ubuntu" {
  count       = var.aws_ami_id == "" ? 1 : 0
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Locals
# ─────────────────────────────────────────────────────────────────────────────

locals {
  az     = data.aws_availability_zones.available.names[0]
  ami_id = var.aws_ami_id != "" ? var.aws_ami_id : one(data.aws_ami.ubuntu[*].id)

  # If an existing volume ID is provided, use it; otherwise reference the new one.
  # Use one(resource[*].id) instead of resource[0].id so Terraform doesn't error
  # when count = 0 (Terraform evaluates both ternary branches regardless of condition).
  ebs_volume_id = var.aws_existing_volume_id != "" ? var.aws_existing_volume_id : one(aws_ebs_volume.this[*].id)

  cloud_init = templatefile("${path.module}/../common/templates/cloud_init.yaml.tpl", {
    provider_type                        = "aws"
    gcp_device_name                      = ""
    project_name                         = var.project_name
    showcase_trial_limits                = var.showcase_trial_limits
    admin_username                       = var.admin_username
    admin_ssh_public_key                 = var.ssh_public_key
    ebs_volume_id                        = local.ebs_volume_id
    do_volume_name                       = ""
    hcloud_volume_id                     = ""
    anthropic_api_key                    = var.anthropic_api_key
    anthropic_auth_key                   = var.anthropic_auth_key
    openai_api_key                       = var.openai_api_key
    openai_auth_mode                     = var.openai_auth_mode
    openai_codex_auth_json_base64        = var.openai_codex_auth_json_base64
    groq_api_key                         = var.groq_api_key
    gemini_api_key                       = var.gemini_api_key
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
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Networking
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_vpc" "this" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project_name}-vpc" }
}

resource "aws_subnet" "this" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = local.az
  map_public_ip_on_launch = true

  tags = { Name = "${var.project_name}-subnet" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project_name}-igw" }
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.project_name}-rt" }
}

resource "aws_route_table_association" "this" {
  subnet_id      = aws_subnet.this.id
  route_table_id = aws_route_table.this.id
}

# ─────────────────────────────────────────────────────────────────────────────
# Security group — SSH only (+ Tailscale UDP if enabled)
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_security_group" "this" {
  name        = "${var.project_name}-sg"
  description = "AgentStack: SSH access only. Tailscale handles private UI access."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  dynamic "ingress" {
    for_each = var.tailscale_enabled ? [1] : []
    content {
      description = "Tailscale VPN (UDP 41641)"
      from_port   = 41641
      to_port     = 41641
      protocol    = "udp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  dynamic "ingress" {
    for_each = var.public_domain_enabled ? [80, 443] : []
    content {
      description = "Public HTTPS reverse proxy"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-sg" }
}

# ─────────────────────────────────────────────────────────────────────────────
# SSH key pair
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_key_pair" "this" {
  key_name   = "${var.project_name}-key"
  public_key = var.ssh_public_key
}

# ─────────────────────────────────────────────────────────────────────────────
# EBS data volume (separate from root disk, survives instance replacement)
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_ebs_volume" "this" {
  count             = var.aws_existing_volume_id == "" ? 1 : 0
  availability_zone = local.az
  size              = var.aws_disk_size_gb
  type              = "gp3"
  iops              = var.aws_showcase_cost_controls ? 3000 : null
  throughput        = var.aws_showcase_cost_controls ? 125 : null
  encrypted         = true

  tags = {
    Name    = "${var.project_name}-data"
    Project = var.project_name
  }

  lifecycle {
    # Prevent accidental data loss on terraform destroy
    prevent_destroy = false
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# EC2 instance
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_instance" "this" {
  ami                    = local.ami_id
  instance_type          = var.aws_instance_type
  subnet_id              = aws_subnet.this.id
  vpc_security_group_ids = [aws_security_group.this.id]
  key_name               = aws_key_pair.this.key_name
  availability_zone      = local.az

  user_data                            = local.cloud_init
  instance_initiated_shutdown_behavior = var.showcase_trial_limits == null ? null : "terminate"

  dynamic "credit_specification" {
    for_each = var.aws_showcase_cost_controls && contains(["t2", "t3", "t3a", "t4g", "t8i"], split(".", var.aws_instance_type)[0]) ? [1] : []
    content { cpu_credits = "unlimited" }
  }

  root_block_device {
    volume_type           = "gp3"
    iops                  = var.aws_showcase_cost_controls ? 3000 : null
    throughput            = var.aws_showcase_cost_controls ? 125 : null
    volume_size           = 20
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  tags = {
    Name    = "${var.project_name}"
    Project = var.project_name
  }

  lifecycle {
    ignore_changes = [user_data]
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Attach data volume to instance
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_volume_attachment" "this" {
  # /dev/xvdf is the requested name; on Nitro instances the OS will see it as
  # /dev/nvme1n1 (or similar). The bootstrap script finds the device by NVMe
  # serial number (EBS volume ID without dashes) — see cloud_init.yaml.tpl.
  device_name = "/dev/xvdf"
  volume_id   = local.ebs_volume_id
  instance_id = aws_instance.this.id

  # Don't forcibly detach — safer for running workloads
  force_detach = false

  # If re-using an existing volume, skip the detach on destroy to protect data
  skip_destroy = var.aws_existing_volume_id != ""
}
