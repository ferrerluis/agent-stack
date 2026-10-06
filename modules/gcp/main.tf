terraform {
  required_providers {
    google = { source = "hashicorp/google" }
  }
}

locals {
  device_name = "agent-stack-data"
  cloud_init = templatefile("${path.module}/../common/templates/cloud_init.yaml.tpl", {
    provider_type        = "gcp"
    project_name         = var.project_name
    admin_username       = var.admin_username
    admin_ssh_public_key = var.ssh_public_key
    ebs_volume_id        = ""
    do_volume_name       = ""
    hcloud_volume_id     = ""
    gcp_device_name      = local.device_name
  })
}

resource "google_compute_network" "this" {
  project                 = var.gcp_project_id
  name                    = "${var.project_name}-network"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "this" {
  project       = var.gcp_project_id
  name          = "${var.project_name}-subnet"
  region        = var.gcp_region
  network       = google_compute_network.this.id
  ip_cidr_range = "10.2.0.0/24"
}

resource "google_compute_firewall" "ssh" {
  project       = var.gcp_project_id
  name          = "${var.project_name}-ssh"
  network       = google_compute_network.this.name
  source_ranges = [var.allowed_ssh_cidr]
  target_tags   = [var.project_name]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "tailscale" {
  count         = var.tailscale_enabled ? 1 : 0
  project       = var.gcp_project_id
  name          = "${var.project_name}-tailscale"
  network       = google_compute_network.this.name
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.project_name]
  allow {
    protocol = "udp"
    ports    = ["41641"]
  }
}

resource "google_compute_firewall" "public_web" {
  count         = var.public_domain_enabled ? 1 : 0
  project       = var.gcp_project_id
  name          = "${var.project_name}-web"
  network       = google_compute_network.this.name
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.project_name]
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_disk" "data" {
  project = var.gcp_project_id
  name    = "${var.project_name}-data"
  zone    = var.gcp_zone
  type    = "pd-balanced"
  size    = var.gcp_disk_size_gb
  labels  = { project = var.project_name }
}

resource "google_compute_instance" "this" {
  project      = var.gcp_project_id
  name         = var.project_name
  zone         = var.gcp_zone
  machine_type = var.gcp_machine_type
  tags         = [var.project_name]
  labels       = { project = var.project_name }

  boot_disk {
    initialize_params {
      image = var.gcp_image
      size  = var.gcp_boot_disk_size_gb
      type  = "pd-balanced"
    }
  }
  attached_disk {
    source      = google_compute_disk.data.id
    device_name = local.device_name
    mode        = "READ_WRITE"
  }
  network_interface {
    subnetwork = google_compute_subnetwork.this.id
    access_config {}
  }
  # Ubuntu's cloud-init consumes user-data. No model or tailnet keys belong here.
  metadata = {
    "user-data"                = local.cloud_init
    "ssh-keys"                 = "${var.admin_username}:${var.ssh_public_key}"
    "block-project-ssh-keys"   = "true"
    "disable-legacy-endpoints" = "true"
  }
  lifecycle {
    ignore_changes = [metadata["user-data"]]
  }
  depends_on = [google_compute_firewall.ssh]
}
