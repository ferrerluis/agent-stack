mock_data "hcloud_image" {
  defaults = {
    type         = "system"
    architecture = "x86"
    os_flavor    = "ubuntu"
    os_version   = "22.04"
  }
}

mock_resource "hcloud_server" {
  defaults = {
    id           = 31337
    ipv4_address = "203.0.113.30"
  }
}

mock_resource "hcloud_volume" {
  defaults = {
    id = 31338
  }
}
