# Runtime only: the user supplies a directly reachable, prepared Ubuntu guest.
# This target never manages its host, virtualization, storage devices or tunnels.
locals {
  existing_guest_connection_valid = (
    trimspace(var.ssh_public_key) != "" &&
    can(regex("^[A-Za-z0-9][A-Za-z0-9.-]*$", var.admin_ssh_host_override)) &&
    can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/=]+$", var.existing_guest_host_key)) &&
    can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]*$", var.repo_ssh_host_alias)) &&
    !can(regex("[\r\n]", var.repo_ssh_private_key_path)) &&
    try(fileexists(abspath(var.repo_ssh_private_key_path)), false)
  )
}

data "external" "existing_guest_preflight" {
  count   = var.cloud_provider == "existing" && local.existing_guest_connection_valid ? 1 : 0
  program = ["python3", "${path.root}/scripts/existing_guest.py"]
  query = {
    address             = var.admin_ssh_host_override
    username            = var.admin_username
    identity_file       = abspath(var.repo_ssh_private_key_path)
    host_key            = var.existing_guest_host_key
    port                = tostring(var.existing_guest_ssh_port)
    architecture        = var.existing_guest_architecture
    allow_existing_data = tostring(var.existing_guest_allow_existing_data)
    require_data_mount  = tostring(var.existing_guest_require_data_mount)
  }
}

resource "terraform_data" "existing_guest_validation" {
  input = "existing-guest-validation"
  lifecycle {
    precondition {
      condition     = var.cloud_provider != "existing" || local.existing_guest_connection_valid
      error_message = "existing requires a directly reachable IPv4/DNS admin_ssh_host_override, existing public/private login key and verified guest SSH public host key; it never creates login credentials or access tunnels."
    }
    precondition {
      condition = var.cloud_provider != "existing" || try(
        data.external.existing_guest_preflight[0].result.status == "ready" &&
        contains(["arm64", "amd64"], data.external.existing_guest_preflight[0].result.architecture) &&
      (var.existing_guest_architecture == "auto" || var.existing_guest_architecture == data.external.existing_guest_preflight[0].result.architecture), false)
      error_message = "Existing guest must pass read-only Ubuntu 24.04, architecture and data-path checks before runtime installation."
    }
  }
}

resource "local_file" "existing_guest_known_hosts" {
  count           = var.cloud_provider == "existing" && var.generate_repo_ssh_config ? 1 : 0
  filename        = "${path.root}/.ssh/known_hosts_agent_stack_guest"
  file_permission = "0600"
  content         = "${var.repo_ssh_host_alias} ${var.existing_guest_host_key}\n"
}

output "existing_guest_ssh_alias" {
  description = "Repo SSH config alias for the directly reachable existing guest; no tunnel or proxy is created."
  value       = var.cloud_provider == "existing" && var.generate_repo_ssh_config ? var.repo_ssh_host_alias : ""
}

output "existing_guest_architecture" {
  description = "Architecture verified on the supplied guest, not the physical host."
  value       = var.cloud_provider == "existing" ? try(data.external.existing_guest_preflight[0].result.architecture, "unverified") : "not applicable"
}
