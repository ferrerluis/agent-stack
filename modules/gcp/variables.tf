variable "project_name" { type = string }
variable "admin_username" { type = string }
variable "ssh_public_key" { type = string }
variable "allowed_ssh_cidr" { type = string }
variable "gcp_project_id" { type = string }
variable "gcp_region" { type = string }
variable "gcp_zone" { type = string }
variable "gcp_machine_type" { type = string }
variable "gcp_image" { type = string }
variable "gcp_pin_boot_image" {
  type    = bool
  default = false
  validation {
    condition     = !var.gcp_pin_boot_image || var.gcp_image == "ubuntu-os-cloud/ubuntu-2204-lts"
    error_message = "gcp_pin_boot_image requires the standard Ubuntu 22.04 family."
  }
}
variable "gcp_disk_size_gb" { type = number }
variable "gcp_boot_disk_size_gb" { type = number }
variable "tailscale_enabled" { type = bool }
variable "public_domain_enabled" { type = bool }
