#cloud-config
# AgentStack first-boot loader. Full runtime configuration is applied later by
# Terraform over SSH after cloud-init reports ready.
# Provider: ${provider_type}

%{ if showcase_trial_limits != null }
# Install limits before package downloads. Host and forwarded external traffic
# share a quota per protocol and boot at the final routing hook. Each protocol
# gets half the total allowance. Downloads delivered to a Docker bridge are
# internal traffic and must not consume this allowance.
bootcmd:
  - |
    set -eu
    set -f
    command -v iptables >/dev/null || { /sbin/shutdown -h now; exit 1; }
    command -v ip6tables >/dev/null || { /sbin/shutdown -h now; exit 1; }
    command -v ip >/dev/null || { /sbin/shutdown -h now; exit 1; }
    interfaces=$(ip -4 route show default | awk '{ for (i=1; i<NF; i++) if ($i == "dev") print $(i+1) }' | sort -u)
    set -- $interfaces
    [ "$#" -eq 1 ] || { /sbin/shutdown -h now; exit 1; }
    egress_interface="$1"
    case "$egress_interface" in
      ''|lo|*[!a-zA-Z0-9_.:-]*) /sbin/shutdown -h now; exit 1 ;;
    esac
    ip link show dev "$egress_interface" >/dev/null || { /sbin/shutdown -h now; exit 1; }
    mkdir -p /var/lib/agent-stack-trial
    count=$(cat /var/lib/agent-stack-trial/boots 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > /var/lib/agent-stack-trial/boots
    echo "$egress_interface" > /var/lib/agent-stack-trial/egress-interface
    [ "$count" -le 2 ] || { /sbin/shutdown -h now; exit 1; }
    protocol_quota=$(( ${showcase_trial_limits.egress_cap_bytes} / 2 ))
    iptables -t mangle -N AS7_EGRESS
    iptables -t mangle -A AS7_EGRESS -m quota --quota "$protocol_quota" -j ACCEPT
    iptables -t mangle -A AS7_EGRESS -j DROP
    iptables -t mangle -I POSTROUTING 1 -o "$egress_interface" -j AS7_EGRESS
    # Meter IPv6 too: registry clients can use either address family. The two
    # quotas together cannot exceed the total priced allowance, and Docker's
    # filter ACCEPT rules cannot bypass these final routing hooks.
    ip6tables -t mangle -N AS7_EGRESS
    ip6tables -t mangle -A AS7_EGRESS -m quota --quota "$protocol_quota" -j ACCEPT
    ip6tables -t mangle -A AS7_EGRESS -j DROP
    ip6tables -t mangle -I POSTROUTING 1 -o "$egress_interface" -j AS7_EGRESS
    cat > /etc/systemd/system/agent-stack-trial-deadline.service <<'AS7_SERVICE'
    [Unit]
    Description=Stop temporary AgentStack showcase VM
    [Service]
    Type=oneshot
    ExecStart=/sbin/shutdown -h now
    AS7_SERVICE
    cat > /etc/systemd/system/agent-stack-trial-deadline.timer <<'AS7_TIMER'
    [Unit]
    Description=Fixed temporary AgentStack showcase deadline
    [Timer]
    OnCalendar=${showcase_trial_limits.deadline_utc}
    Persistent=true
    AccuracySec=1s
    [Install]
    WantedBy=timers.target
    AS7_TIMER
    systemctl daemon-reload
    # cloud-init's network stage blocks normal boot targets. Queue the timer
    # without waiting for those targets, so SSH and time sync can start.
    systemctl enable --now --no-block agent-stack-trial-deadline.timer
%{ endif }

packages:
  - ca-certificates
  - curl
  - jq
  - nvme-cli

write_files:
  - path: /root/mount-agent-stack-volume.sh
    permissions: "0755"
    owner: root:root
    content: |
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
        echo "[volume] Attempt $attempt/30: volume not visible yet"
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
        echo "[volume] Attempt $attempt/30: volume not visible yet"
        sleep 5
      done
      [ -e "$device" ] || { echo "[volume] ERROR: DigitalOcean volume $volume_name not found" >&2; exit 1; }
      if current_mount="$(findmnt -n -o TARGET --source "$device" 2>/dev/null)"; then
        if [ "$current_mount" != "/opt/agent-stack/data" ]; then
          umount "$current_mount" || true
        fi
      fi
%{ endif }
%{ if provider_type == "hetzner" }
      volume_id="${hcloud_volume_id}"
      device="/dev/disk/by-id/scsi-0HC_Volume_$volume_id"
      echo "[volume] Waiting for Hetzner Cloud volume $volume_id..."
      for attempt in $(seq 1 30); do
        [ -e "$device" ] && break
        echo "[volume] Attempt $attempt/30: volume not visible yet"
        sleep 5
      done
      [ -e "$device" ] || { echo "[volume] ERROR: Hetzner volume $volume_id not found" >&2; exit 1; }
%{ endif }
%{ if provider_type == "gcp" }
      device="/dev/disk/by-id/google-${gcp_device_name}"
      echo "[volume] Waiting for Google persistent disk..."
      for attempt in $(seq 1 30); do
        [ -e "$device" ] && break
        sleep 5
      done
      [ -e "$device" ] || { echo "[volume] ERROR: Google data disk not found" >&2; exit 1; }
%{ endif }

      if ! blkid "$device" >/dev/null 2>&1; then
        echo "[volume] Formatting $device as ext4..."
        mkfs.ext4 -L agent-stack-data -F "$device"
      fi

      uuid="$(blkid -s UUID -o value "$device")"
      if mountpoint -q /opt/openclaw/data; then
        umount /opt/openclaw/data || true
      fi
      mountpoint -q /opt/agent-stack/data || mount -o defaults,nofail UUID="$uuid" /opt/agent-stack/data
      sed -i "\|UUID=$uuid /opt/openclaw/data |d" /etc/fstab || true
      grep -qE "UUID=$uuid[[:space:]]+/opt/agent-stack/data[[:space:]]" /etc/fstab || echo "UUID=$uuid /opt/agent-stack/data ext4 defaults,nofail 0 2" >> /etc/fstab
      echo "[volume] Mounted /opt/agent-stack/data"

  - path: /root/agent-stack-loader.sh
    permissions: "0755"
    owner: root:root
    content: |
      #!/usr/bin/env bash
      set -euo pipefail
      exec > >(tee -a /var/log/openclaw-bootstrap.log) 2>&1

      admin="${admin_username}"
      if ! getent group "$admin" >/dev/null 2>&1; then
        groupadd "$admin"
      fi
      if ! id "$admin" >/dev/null 2>&1; then
        useradd --create-home --shell /bin/bash --gid "$admin" --groups sudo "$admin"
      else
        usermod -aG sudo "$admin"
      fi

      home="$(getent passwd "$admin" | cut -d: -f6)"
      group="$(id -gn "$admin")"
      install -d -m 700 "$home/.ssh"
      touch "$home/.ssh/authorized_keys"
      grep -qxF '${admin_ssh_public_key}' "$home/.ssh/authorized_keys" || echo '${admin_ssh_public_key}' >> "$home/.ssh/authorized_keys"
      chown -R "$admin:$group" "$home/.ssh"
      chmod 700 "$home/.ssh"
      chmod 600 "$home/.ssh/authorized_keys"
      echo "$admin ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-agent-stack-admin
      chmod 440 /etc/sudoers.d/90-agent-stack-admin

      /root/mount-agent-stack-volume.sh
      install -d -m 0755 /opt/agent-stack
      cat > /opt/agent-stack/.loader-ready.json <<EOF
      {"status":"ready","provider":"${provider_type}","data_root":"/opt/agent-stack/data"}
      EOF
      chmod 0644 /opt/agent-stack/.loader-ready.json
      echo "[loader] AgentStack loader ready"

runcmd:
  - /root/agent-stack-loader.sh
