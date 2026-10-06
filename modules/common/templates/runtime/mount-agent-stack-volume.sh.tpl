#!/usr/bin/env bash
set -euo pipefail
exec >> /var/log/openclaw-bootstrap.log 2>&1

install -d -m 0755 /opt/agent-stack /opt/agent-stack/data /opt/agent-stack/tailscale-state

%{ if provider_type == "aws" }
volume_id="${ebs_volume_id}"
serial="$(printf '%s' "$volume_id" | tr -d '-')"
device=""
echo "[volume] Waiting for AWS EBS volume $volume_id..."
for attempt in $(seq 1 30); do
  for dev in /dev/nvme*n1; do
    [ -b "$dev" ] || continue
    if nvme id-ctrl "$dev" 2>/dev/null | grep -qi "$serial"; then
      device="$dev"
      break 2
    fi
  done
  sleep 5
done
[ -n "$device" ] || { echo "[volume] ERROR: AWS volume $volume_id not found" >&2; exit 1; }
%{ endif }
%{ if provider_type == "digitalocean" }
volume_name="${do_volume_name}"
device="/dev/disk/by-id/scsi-0DO_Volume_$volume_name"
echo "[volume] Waiting for DigitalOcean volume $volume_name..."
for attempt in $(seq 1 30); do
  [ -e "$device" ] && break
  sleep 5
done
[ -e "$device" ] || { echo "[volume] ERROR: DigitalOcean volume $volume_name not found" >&2; exit 1; }
if current_mount="$(findmnt -n -o TARGET --source "$device" 2>/dev/null)"; then
  [ "$current_mount" = "/opt/agent-stack/data" ] || umount "$current_mount" || true
fi
%{ endif }
%{ if provider_type == "hetzner" }
volume_id="${hcloud_volume_id}"
device="/dev/disk/by-id/scsi-0HC_Volume_$volume_id"
echo "[volume] Waiting for Hetzner Cloud volume $volume_id..."
for attempt in $(seq 1 30); do
  [ -e "$device" ] && break
  sleep 5
done
[ -e "$device" ] || { echo "[volume] ERROR: Hetzner volume $volume_id not found" >&2; exit 1; }
%{ endif }
%{ if provider_type == "gcp" }
device="/dev/disk/by-id/google-${gcp_device_name}"
for attempt in $(seq 1 30); do
  [ -e "$device" ] && break
  sleep 5
done
[ -e "$device" ] || { echo "[volume] ERROR: Google data disk not found" >&2; exit 1; }
%{ endif }

if ! blkid "$device" >/dev/null 2>&1; then
  mkfs.ext4 -L agent-stack-data -F "$device"
fi

uuid="$(blkid -s UUID -o value "$device")"
if mountpoint -q /opt/openclaw/data; then
  umount /opt/openclaw/data || true
fi
mountpoint -q /opt/agent-stack/data || mount -o defaults,nofail UUID="$uuid" /opt/agent-stack/data
sed -i "\|UUID=$uuid /opt/openclaw/data |d" /etc/fstab || true
grep -qE "UUID=$uuid[[:space:]]+/opt/agent-stack/data[[:space:]]" /etc/fstab || echo "UUID=$uuid /opt/agent-stack/data ext4 defaults,nofail 0 2" >> /etc/fstab
