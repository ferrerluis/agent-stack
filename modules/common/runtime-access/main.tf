terraform {
  required_providers {
    external = { source = "hashicorp/external" }
  }
}

data "external" "this" {
  program = ["python3", "${var.repo_root}/scripts/read_runtime_access.py"]
  query = {
    repo_root = var.repo_root
    key_path  = var.key_path
    host      = var.host
    user      = var.user
  }
}

output "tailscale_dns" {
  description = "Non-secret full tailnet DNS name observed after runtime installation."
  value       = data.external.this.result.tailscale_dns
}
