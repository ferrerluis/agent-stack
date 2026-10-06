mock_provider "google" {
  override_during = plan
  mock_data "google_compute_image" {
    defaults = {
      name      = "ubuntu-2204-jammy-v20261001"
      image_id  = "1234567890123456789"
      status    = "READY"
      licenses  = ["https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/licenses/ubuntu-2204-lts"]
      self_link = "https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/ubuntu-2204-jammy-v20261001"
    }
  }
  mock_resource "google_compute_instance" {
    defaults = {
      id                = "projects/agent-stack-tests/zones/us-east1-b/instances/agent-stack"
      network_interface = { access_config = { nat_ip = "203.0.113.40" } }
    }
  }
}

variables {
  project_name          = "agent-stack"
  admin_username        = "admin"
  ssh_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAgentStackTerraformPlanTestsOnly agent-stack-tests"
  allowed_ssh_cidr      = "198.51.100.1/32"
  gcp_project_id        = "agent-stack-tests"
  gcp_region            = "us-east1"
  gcp_zone              = "us-east1-b"
  gcp_machine_type      = "e2-standard-2"
  gcp_image             = "ubuntu-os-cloud/ubuntu-2204-lts"
  gcp_disk_size_gb      = 50
  gcp_boot_disk_size_gb = 40
  tailscale_enabled     = true
  public_domain_enabled = false
}

run "gcp_private_stack" {
  command = plan
  module { source = "./modules/gcp" }
  assert {
    condition     = length(data.google_compute_image.boot) == 0 && google_compute_instance.this.boot_disk[0].initialize_params[0].image == var.gcp_image
    error_message = "Ordinary deployments must retain the existing image selection without another data read."
  }
  assert {
    condition     = output.instance_public_ip == "203.0.113.40"
    error_message = "GCP must expose the selected bootstrap IP."
  }
  assert {
    condition     = google_compute_firewall.ssh.source_ranges == toset(["198.51.100.1/32"]) && length(google_compute_firewall.public_web) == 0
    error_message = "The private profile must restrict SSH and avoid public web ports."
  }
  assert {
    condition     = google_compute_instance.this.attached_disk[0].device_name == output.device_name && strcontains(google_compute_instance.this.metadata["user-data"], "/dev/disk/by-id/google-agent-stack-data")
    error_message = "The attached data disk and cloud-init mount path must agree."
  }
  assert {
    condition     = google_compute_instance.this.metadata["block-project-ssh-keys"] == "true" && google_compute_disk.data.size == 50
    error_message = "GCP must isolate SSH access and provision separate persistent storage."
  }
}

run "gcp_showcase_pins_planned_image" {
  command = plan
  module { source = "./modules/gcp" }
  variables { gcp_pin_boot_image = true }
  assert {
    condition     = google_compute_instance.this.boot_disk[0].initialize_params[0].image == data.google_compute_image.boot[0].self_link && data.google_compute_image.boot[0].image_id == "1234567890123456789"
    error_message = "The showcase must save the resolved image URI and original image identity in the plan."
  }
}

run "gcp_pin_rejects_different_family" {
  command = plan
  module { source = "./modules/gcp" }
  variables {
    gcp_pin_boot_image = true
    gcp_image          = "ubuntu-pro-os-cloud/ubuntu-pro-2204-lts"
  }
  expect_failures = [var.gcp_pin_boot_image]
}
