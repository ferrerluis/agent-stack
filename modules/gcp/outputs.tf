output "instance_public_ip" {
  value = google_compute_instance.this.network_interface[0].access_config[0].nat_ip
}
output "instance_id" { value = google_compute_instance.this.id }
output "volume_id" { value = google_compute_disk.data.id }
output "device_name" { value = local.device_name }
output "ssh_command" {
  value = "ssh ${var.admin_username}@${google_compute_instance.this.network_interface[0].access_config[0].nat_ip}"
}
