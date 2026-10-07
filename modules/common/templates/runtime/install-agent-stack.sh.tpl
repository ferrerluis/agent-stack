#!/usr/bin/env bash
set -euo pipefail

staging="$1"
checksum="$2"
app=/opt/agent-stack
previous="$app/.previous-runtime"
log=/var/log/openclaw-bootstrap.log
NEEDS_RESTART=0

exec > >(tee -a "$log") 2>&1

fail() {
  echo "[runtime] ERROR: $*" >&2
  exit 1
}

require_file() {
  [ -f "$staging/$1" ] || fail "missing staged file: $1"
}

env_file_value() {
  local file="$1"
  local key="$2"
  sed -n "s/^$key=//p" "$file" | tail -n 1 || true
}

reload_sshd() {
  sshd -t
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null
}

require_file docker-compose.yml
require_file .env
require_file mount-agent-stack-volume.sh
require_file agent-stack-migrate-layout
require_file agent-stack.service
require_file openclaw.service
require_file enabled-services.json
require_file workspace.env
require_file host-tailscale-bootstrap.sh
require_file agent-stack-vpn
require_file agent-stack-vpn-openvpn
require_file workspace.Dockerfile
require_file workspace-entrypoint.sh
require_file workspace-para-memory-drive-skill.md
require_file workspace-codex-update.sh
require_file workspace-codex-control.py
require_file workspace-cli-task.py
require_file openclaw_config.py
require_file openclaw-seed.json
require_file hermes_config.py
require_file hermes-seed.json
require_file hermes_health.py
require_file agent-stack-workspace-codex-update
require_file agent-stack-workspace-codex-update.service
require_file agent-stack-workspace-codex-update.timer
require_file agent-stack-diagnostics
require_file agent-stack-diagnostics-ssh

echo "[runtime] applying AgentStack runtime checksum=$checksum"

if ! command -v docker >/dev/null 2>&1; then
  echo "[docker] Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker
usermod -aG docker "${admin_username}" || true

install -d -m 0755 "$app" "$app/templates" "$app/tailscale-state" "$app/data"
install -m 0755 "$staging/mount-agent-stack-volume.sh" /root/mount-agent-stack-volume.sh
/root/mount-agent-stack-volume.sh
install -m 0755 "$staging/agent-stack-migrate-layout" /usr/local/bin/agent-stack-migrate-layout
/usr/local/bin/agent-stack-migrate-layout

configure_swap() {
  local swap_mb=${openclaw_swap_size_mb}
  if [ "$swap_mb" -le 0 ]; then
    echo "[swap] Skipped (openclaw_swap_size_mb=$swap_mb)."
    return 0
  fi

  if swapon --show --noheadings 2>/dev/null | grep -q .; then
    echo "[swap] Swap already enabled:"
    swapon --show || true
    return 0
  fi

  echo "[swap] Configuring /swapfile (${openclaw_swap_size_mb} MB)..."
  if [ ! -f /swapfile ]; then
    if command -v fallocate >/dev/null 2>&1; then
      fallocate -l ${openclaw_swap_size_mb}M /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=${openclaw_swap_size_mb} status=none
    else
      dd if=/dev/zero of=/swapfile bs=1M count=${openclaw_swap_size_mb} status=none
    fi
  fi

  chmod 600 /swapfile
  mkswap /swapfile >/dev/null 2>&1 || true
  swapon /swapfile
  if ! grep -qE '^/swapfile[[:space:]]' /etc/fstab; then
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
  fi
  echo "vm.swappiness=10" > /etc/sysctl.d/99-openclaw-memory.conf
  sysctl -p /etc/sysctl.d/99-openclaw-memory.conf >/dev/null || true
  swapon --show || true
}

configure_caddyfile() {
  if ! grep -q '^CADDY_ENABLED=true$' "$staging/.env"; then
    echo "[caddy] Skipped (public_domain_enabled=false)."
    rm -f "$staging/Caddyfile"
    return 0
  fi

  require_file Caddyfile.template
  echo "[caddy] Rendering Caddyfile with hashed basic-auth password..."
  local ui_password
  ui_password="$(env_file_value "$staging/.env" UI_AUTH_PASSWORD)"
  local hash
  hash="$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$ui_password")"
  sed "s|__UI_AUTH_HASH__|$hash|g" "$staging/Caddyfile.template" > "$staging/Caddyfile"
  chmod 0600 "$staging/Caddyfile"
}

configure_admin_password_ssh() {
  local password access config marker
  password="$(env_file_value "$app/.env" ADMIN_PASSWORD)"
  access="$(env_file_value "$app/.env" ADMIN_PASSWORD_SSH_SCOPE)"
  [ -n "$access" ] || access="disabled"
  config="/etc/ssh/sshd_config.d/90-agent-stack-admin-password.conf"
  marker="/var/lib/agent-stack/admin-password-managed"

  install -d -m 0700 /var/lib/agent-stack

  case "$access" in
    disabled)
      rm -f "$config"
      if [ -f "$marker" ] && [ -z "$password" ]; then
        passwd -l "${admin_username}" >/dev/null 2>&1 || true
        rm -f "$marker"
        echo "[admin-password] Managed admin password login disabled; locked ${admin_username} password."
      else
        echo "[admin-password] Admin password login disabled."
      fi
      ;;
    tailnet|public)
      if [ -z "$password" ]; then
        fail "ADMIN_PASSWORD is required when ADMIN_PASSWORD_SSH_SCOPE=$access"
      fi

      printf '%s:%s\n' "${admin_username}" "$password" | chpasswd
      passwd -u "${admin_username}" >/dev/null 2>&1 || true
      touch "$marker"
      chmod 0600 "$marker"

      if [ "$access" = "tailnet" ]; then
        cat >"$config" <<'EOF'
# Managed by AgentStack.
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes

Match User ${admin_username} Address 100.64.0.0/10
  PasswordAuthentication yes

Match User ${admin_username} Address fd7a:115c:a1e0::/48
  PasswordAuthentication yes
EOF
        echo "[admin-password] Enabled admin password SSH login for Tailscale source addresses only."
      else
        cat >"$config" <<'EOF'
# Managed by AgentStack.
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes

Match User ${admin_username}
  PasswordAuthentication yes
EOF
        echo "[admin-password] Enabled admin password SSH login for provider-firewall-permitted SSH sources."
      fi
      chmod 0644 "$config"
      ;;
    *)
      fail "Unsupported ADMIN_PASSWORD_SSH_SCOPE=$access"
      ;;
  esac

  reload_sshd
}

install_openclaw_runtime_patches() {
  install -d -m 0755 "$app/patches/openclaw"

  case "${openclaw_version}" in
    2026.6.8|2026.6.9-beta.1)
      cat > "$app/patches/openclaw/telegram-ingress-worker.runtime.js" <<'PATCH'
import "/app/dist/telegram-ingress-worker.runtime.js";
PATCH
      chmod 0644 "$app/patches/openclaw/telegram-ingress-worker.runtime.js"
      echo "[patches] Installed OpenClaw Telegram ingress worker shim for ${openclaw_version}."
      ;;
    *)
      rm -f "$app/patches/openclaw/telegram-ingress-worker.runtime.js"
      echo "[patches] No OpenClaw runtime compatibility patches for ${openclaw_version}."
      ;;
  esac
}

sync_openai_codex_auth() {
  if [ ! -s "$staging/openai_codex_auth_json_base64" ]; then
    echo "[openai-codex] No Codex CLI auth import configured."
    return 0
  fi

  echo "[openai-codex] Importing Codex CLI auth into $app/codex/auth.json..."
  install -d -m 0700 -o 1000 -g 1000 "$app/codex"
  base64 --decode "$staging/openai_codex_auth_json_base64" > "$app/codex/auth.json"
  chown 1000:1000 "$app/codex/auth.json"
  chmod 0600 "$app/codex/auth.json"
}

install_host_codex_cli() {
  if [ "${host_codex_cli_enabled}" != "true" ]; then
    echo "[codex-host] Skipped (host_codex_cli_enabled=false)."
    return 0
  fi

  local admin_home admin_group
  admin_home="$(getent passwd "${admin_username}" | cut -d: -f6)"
  admin_group="$(id -gn "${admin_username}")"

  if command -v codex >/dev/null 2>&1; then
    echo "[codex-host] Codex CLI already installed: $(codex --version 2>/dev/null || echo unknown)"
    install -d -m 0700 -o "${admin_username}" -g "$admin_group" "$admin_home/.codex"
    return 0
  fi

  echo "[codex-host] Installing Codex CLI on the host..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl git jq bubblewrap
  rm -rf /var/lib/apt/lists/*

  local codex_install_home="/opt/agent-stack/codex-cli"
  local installer="/tmp/install-codex.sh"
  install -d -m 0755 "$codex_install_home"
  curl -fsSL https://chatgpt.com/codex/install.sh -o "$installer"
  CODEX_NON_INTERACTIVE=1 CODEX_INSTALL_DIR=/usr/local/bin CODEX_HOME="$codex_install_home" sh "$installer"
  rm -f "$installer"

  install -d -m 0700 -o "${admin_username}" -g "$admin_group" "$admin_home/.codex"
  codex --version
}

install_workspace_diagnostics_bridge() {
  if [ "${workspace_enabled}" != "true" ]; then
    echo "[workspace] Diagnostics bridge skipped (workspace disabled)."
    return 0
  fi

  local diag_user="agent-stack-diagnostics"
  local diag_home="/var/lib/agent-stack-diagnostics"
  local workspace_home="$app/data/workspace/home"
  local workspace_ssh="$workspace_home/.ssh"
  local diag_key="$workspace_ssh/agent_stack_diagnostics"

  install -m 0755 "$staging/agent-stack-diagnostics" /usr/local/bin/agent-stack-diagnostics
  install -m 0755 "$staging/agent-stack-diagnostics-ssh" /usr/local/bin/agent-stack-diagnostics-ssh

  if ! id "$diag_user" >/dev/null 2>&1; then
    useradd --system --home-dir "$diag_home" --create-home --shell /bin/bash "$diag_user"
  fi

  install -d -m 0700 -o "$diag_user" -g "$diag_user" "$diag_home/.ssh"
  install -d -m 0700 -o 1000 -g 1000 "$workspace_ssh"

  if [ ! -f "$diag_key" ]; then
    ssh-keygen -t ed25519 -N "" -f "$diag_key" -C "agent-stack-workspace-diagnostics"
  fi
  chown 1000:1000 "$diag_key" "$diag_key.pub"
  chmod 0600 "$diag_key"
  chmod 0644 "$diag_key.pub"

  local pubkey
  pubkey="$(cat "$diag_key.pub")"
  printf 'command="/usr/local/bin/agent-stack-diagnostics-ssh",no-agent-forwarding,no-X11-forwarding,no-pty,no-user-rc,no-port-forwarding %s\n' "$pubkey" > "$diag_home/.ssh/authorized_keys"
  chown "$diag_user:$diag_user" "$diag_home/.ssh/authorized_keys"
  chmod 0600 "$diag_home/.ssh/authorized_keys"

  cat >/etc/sudoers.d/agent-stack-diagnostics <<'EOF'
agent-stack-diagnostics ALL=(root) NOPASSWD: /usr/local/bin/agent-stack-diagnostics
agent-stack-diagnostics ALL=(root) NOPASSWD: /usr/local/bin/agent-stack-diagnostics *
EOF
  chmod 0440 /etc/sudoers.d/agent-stack-diagnostics

  echo "[workspace] Diagnostics bridge installed for workspace container."
}

build_workspace_image() {
  local build_context="$app/.workspace-image-context"
  rm -rf "$build_context"
  install -d -m 0700 "$build_context"
  install -m 0644 "$app/workspace.Dockerfile" "$build_context/Dockerfile"
  install -m 0755 "$app/workspace-entrypoint.sh" "$build_context/workspace-entrypoint.sh"
  install -d -m 0755 "$build_context/para-memory-drive"
  install -m 0644 "$app/workspace-para-memory-drive-skill.md" "$build_context/para-memory-drive/SKILL.md"
  install -m 0755 "$app/workspace-codex-update.sh" "$build_context/workspace-codex-update.sh"
  install -m 0755 "$app/workspace-codex-control.py" "$build_context/workspace-codex-control.py"
  # Old rollback Dockerfiles predate the task helper; current images require it.
  if [ -f "$app/workspace-cli-task.py" ] || grep -q '^COPY workspace-cli-task.py ' "$build_context/Dockerfile"; then
    install -m 0755 "$app/workspace-cli-task.py" "$build_context/workspace-cli-task.py" || return 1
  fi
  docker build -t agent-stack-workspace:local -f "$build_context/Dockerfile" "$build_context" || return 1
  rm -rf "$build_context"
}

install_workspace_runtime() {
  if [ "${workspace_enabled}" != "true" ]; then
    echo "[workspace] Skipped (workspace disabled)."
    return 0
  fi

  echo "[workspace] Installing workspace runtime files..."
  install -d -m 0755 "$app/data/workspace"
  install -d -m 0755 "$app/data/workspace/home"
  install -d -m 0700 -o root -g root "$app/data/workspace/ssh-host-keys"
  install -m 0644 "$staging/workspace.Dockerfile" "$app/workspace.Dockerfile"
  install -m 0755 "$staging/workspace-entrypoint.sh" "$app/workspace-entrypoint.sh"
  install -m 0644 "$staging/workspace-para-memory-drive-skill.md" "$app/workspace-para-memory-drive-skill.md"
  install -m 0755 "$staging/workspace-codex-update.sh" "$app/workspace-codex-update.sh"
  install -m 0755 "$staging/workspace-codex-control.py" "$app/workspace-codex-control.py"
  install_workspace_diagnostics_bridge

  echo "[workspace] Building local workspace image..."
  build_workspace_image
}

backup_workspace_image() {
  local image_id

  [ "${workspace_enabled}" = "true" ] || return 0
  image_id="$(docker image inspect --format '{{.Id}}' agent-stack-workspace:local 2>/dev/null || true)"
  if [ -n "$image_id" ]; then
    printf '%s\n' "$image_id" >"$previous/workspace-image-id"
    chmod 0600 "$previous/workspace-image-id"
  fi
}

restore_workspace_image() {
  local image_id

  [ "${workspace_enabled}" = "true" ] || return 0
  image_id="$(cat "$previous/workspace-image-id" 2>/dev/null || true)"
  if [ -n "$image_id" ] && docker image inspect "$image_id" >/dev/null 2>&1; then
    docker tag "$image_id" agent-stack-workspace:local
    return 0
  fi
  if [ -f "$app/workspace.Dockerfile" ] \
      && [ -f "$app/workspace-entrypoint.sh" ] \
      && [ -f "$app/workspace-para-memory-drive-skill.md" ] \
      && [ -f "$app/workspace-codex-update.sh" ] \
      && [ -f "$app/workspace-codex-control.py" ]; then
    build_workspace_image || return 1
    return 0
  fi
  return 1
}

configure_host_tailscale() {
  if [ "${tailscale_host_enabled}" != "true" ]; then
    echo "[tailscale-host] Skipped (tailscale_mode=${tailscale_mode})."
    return 0
  fi

  echo "[tailscale-host] Configuring host-level Tailscale..."
  TAILSCALE_AUTH_KEY="$(env_file_value "$app/.env" TAILSCALE_AUTH_KEY)" \
  TAILSCALE_HOSTNAME="$(env_file_value "$app/.env" TAILSCALE_HOSTNAME)" \
  OPENCLAW_ENABLED="$(env_file_value "$app/.env" OPENCLAW_ENABLED)" \
    "$app/host-tailscale-bootstrap.sh"
}

configure_host_vpn() {
  if [ "${vpn_enabled}" != "true" ]; then
    echo "[vpn] Skipped (vpn_enabled=false)."
    if [ -x "$app/agent-stack-vpn" ]; then
      "$app/agent-stack-vpn" disable || true
    elif [ -x "$app/agent-stack-vpn-openvpn" ]; then
      "$app/agent-stack-vpn-openvpn" disable || true
    fi
    return 0
  fi

  echo "[vpn] Configuring host-level VPN..."
  "$app/agent-stack-vpn" enable "$staging"
}

configure_workspace_codex_auto_update() {
  local unit="agent-stack-workspace-codex-update.service"
  local timer="agent-stack-workspace-codex-update.timer"
  local timezone="${workspace_codex_auto_update_timezone}"

  if [ "${workspace_enabled}" != "true" ] || [ "${workspace_codex_auto_update_enabled}" != "true" ]; then
    echo "[workspace-codex-update] Disabled; removing host timer and worker."
    systemctl disable --now "$timer" 2>/dev/null || true
    systemctl stop "$unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/$unit" "/etc/systemd/system/$timer" \
      /usr/local/bin/agent-stack-workspace-codex-update
    systemctl daemon-reload
    return 0
  fi

  if [ ! -f "/usr/share/zoneinfo/$timezone" ]; then
    echo "[workspace-codex-update] configured timezone is not installed: $timezone" >&2
    return 1
  fi

  echo "[workspace-codex-update] Installing root-mediated stable-channel updater."
  # A pre-hard-cutover release could remain active indefinitely while it
  # polled for a quiet workspace. Stop its timer and worker before replacing
  # the executable so it cannot inherit the new command contract mid-run.
  systemctl disable --now "$timer" 2>/dev/null || true
  systemctl stop "$unit" 2>/dev/null || true
  install -d -m 0700 -o root -g root /var/lib/agent-stack/workspace-codex-update
  install -m 0755 "$staging/agent-stack-workspace-codex-update" \
    /usr/local/bin/agent-stack-workspace-codex-update || return 1
  install -m 0644 "$staging/agent-stack-workspace-codex-update.service" \
    "/etc/systemd/system/$unit" || return 1
  install -m 0644 "$staging/agent-stack-workspace-codex-update.timer" \
    "/etc/systemd/system/$timer" || return 1
  systemctl daemon-reload || return 1
  systemctl reset-failed "$unit" 2>/dev/null || true
  systemctl enable --now "$timer" || return 1
}

backup_workspace_codex_auto_update_host() {
  local backup="$previous/workspace-codex-update-host"
  local source target

  rm -rf "$backup"
  install -d -m 0700 "$backup"
  for source in \
    /usr/local/bin/agent-stack-workspace-codex-update \
    /etc/systemd/system/agent-stack-workspace-codex-update.service \
    /etc/systemd/system/agent-stack-workspace-codex-update.timer; do
    [ -e "$source" ] || continue
    target="$backup/$(basename "$source")"
    cp -a "$source" "$target"
  done
}

restore_workspace_codex_auto_update_host() {
  local backup="$previous/workspace-codex-update-host"
  local source target

  systemctl disable --now agent-stack-workspace-codex-update.timer 2>/dev/null || true
  systemctl stop agent-stack-workspace-codex-update.service 2>/dev/null || true
  rm -f /usr/local/bin/agent-stack-workspace-codex-update \
    /etc/systemd/system/agent-stack-workspace-codex-update.service \
    /etc/systemd/system/agent-stack-workspace-codex-update.timer
  for source in "$backup"/*; do
    [ -e "$source" ] || continue
    case "$(basename "$source")" in
      agent-stack-workspace-codex-update)
        target=/usr/local/bin/agent-stack-workspace-codex-update
        ;;
      agent-stack-workspace-codex-update.service|agent-stack-workspace-codex-update.timer)
        target="/etc/systemd/system/$(basename "$source")"
        ;;
      *) continue ;;
    esac
    cp -a "$source" "$target"
  done
  systemctl daemon-reload
  if [ -e /etc/systemd/system/agent-stack-workspace-codex-update.timer ]; then
    systemctl enable --now agent-stack-workspace-codex-update.timer || true
  fi
}

wait_agent_stack_initial_restart() {
  for attempt in $(seq 1 60); do
    local agent_state openclaw_status hermes_status workspace_status codex_status
    agent_state="$(systemctl is-active agent-stack 2>/dev/null || true)"
    openclaw_status=disabled
    hermes_status=disabled
    workspace_status=disabled
    codex_status=disabled
    if [ "$agent_state" = "active" ]; then
      local all_ready=1
      local container_id health_status
      if [ "${openclaw_enabled}" = "true" ]; then
        container_id="$(docker compose -f "$app/docker-compose.yml" ps -q openclaw 2>/dev/null || true)"
        if [ -z "$container_id" ]; then
          openclaw_status=missing
          all_ready=0
        else
          health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
          openclaw_status="$${health_status:-unknown}"
          [ "$health_status" = "healthy" ] || all_ready=0
        fi
      fi
      if [ "${workspace_enabled}" = "true" ]; then
        container_id="$(docker compose -f "$app/docker-compose.yml" ps -q workspace 2>/dev/null || true)"
        if [ -z "$container_id" ]; then
          workspace_status=missing
          all_ready=0
        else
          health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
          workspace_status="$${health_status:-unknown}"
          [ "$health_status" = "healthy" ] || all_ready=0
          if [ "${workspace_codex_auto_update_enabled}" = "true" ]; then
            if docker exec --user ${workspace_username} "$container_id" env HOME=/home/${workspace_username} CODEX_HOME=/home/${workspace_username}/.codex PATH=/home/${workspace_username}/.local/bin:/usr/local/bin:/usr/bin:/bin /bin/sh -c 'expected="$(readlink -f "$HOME/.codex/packages/standalone/current/codex" 2>/dev/null || true)"; actual="$(readlink -f "$HOME/.local/bin/codex" 2>/dev/null || true)"; test "$(command -v codex)" = "$HOME/.local/bin/codex" && test -n "$expected" && test "$actual" = "$expected" && codex --version >/dev/null'; then
              codex_status=ready
            else
              codex_status=not-ready
              all_ready=0
            fi
          fi
        fi
      fi
      if [ "${hermes_enabled}" = "true" ]; then
        container_id="$(docker compose -f "$app/docker-compose.yml" ps -q hermes 2>/dev/null || true)"
        if [ -z "$container_id" ]; then
          hermes_status=missing
          all_ready=0
        elif [ "${hermes_api_server_enabled}" = "true" ]; then
          health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)"
          hermes_status="$${health_status:-unknown}"
          [ "$health_status" = "healthy" ] || all_ready=0
        else
          health_status="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
          hermes_status="$${health_status:-unknown}"
          [ "$health_status" = "running" ] || all_ready=0
        fi
      fi
      if [ "$all_ready" -eq 1 ]; then
        return 0
      fi
    fi
    if [ "$attempt" -eq 1 ] || [ $((attempt % 10)) -eq 0 ]; then
      echo "[runtime] readiness attempt $attempt/60: agent-stack=$agent_state openclaw=$openclaw_status hermes=$hermes_status workspace=$workspace_status codex=$codex_status" >&2
    fi
    sleep 3
  done
  return 1
}

configure_caddyfile
configure_swap
install_openclaw_runtime_patches
docker compose --env-file "$staging/.env" -f "$staging/docker-compose.yml" config >/dev/null

rm -rf "$previous"
install -d -m 0700 "$previous"
for path in docker-compose.yml .env workspace.env hermes_health.py hermes_config.py openclaw_config.py Caddyfile tailscale-bootstrap.sh host-tailscale-bootstrap.sh agent-stack-vpn agent-stack-vpn-openvpn workspace.Dockerfile workspace-entrypoint.sh workspace-para-memory-drive-skill.md workspace-codex-update.sh workspace-codex-control.py workspace-cli-task.py; do
  [ -e "$app/$path" ] && cp -a "$app/$path" "$previous/" || true
done
backup_workspace_image

install -m 0644 "$staging/docker-compose.yml" "$app/docker-compose.yml"
install -m 0600 "$staging/.env" "$app/.env"
install -m 0600 "$staging/workspace.env" "$app/workspace.env"
install -m 0644 "$staging/hermes_health.py" "$app/hermes_health.py"
install -m 0644 "$staging/hermes_config.py" "$app/hermes_config.py"
install -m 0644 "$staging/openclaw_config.py" "$app/openclaw_config.py"
install -m 0755 "$staging/workspace-cli-task.py" "$app/workspace-cli-task.py"
OPENCLAW_CONFIG="$app/data/openclaw/openclaw.json"
PREEXISTING_OPENCLAW_CONFIG=0
[ ! -f "$OPENCLAW_CONFIG" ] || PREEXISTING_OPENCLAW_CONFIG=1
openclaw_seed_changed=0
if [ "${openclaw_enabled}" = "true" ]; then
  openclaw_seed_result="$(python3 "$app/openclaw_config.py" --data-dir "$app/data/openclaw" --seed "$staging/openclaw-seed.json")"
  [ "$openclaw_seed_result" != "changed" ] || openclaw_seed_changed=1
fi
hermes_config_changed=0
if [ "${hermes_enabled}" = "true" ]; then
  if [ -f "$app/data/hermes/config.yaml" ]; then
    cp -a "$app/data/hermes/config.yaml" "$previous/hermes-config.yaml"
  fi
  hermes_config_result="$(python3 "$app/hermes_config.py" --data-dir "$app/data/hermes" --seed "$staging/hermes-seed.json" --mode "${hermes_config_mode}")"
  [ "$hermes_config_result" != "changed" ] || hermes_config_changed=1
fi
if [ -f "$staging/Caddyfile" ]; then
  install -m 0600 "$staging/Caddyfile" "$app/Caddyfile"
else
  rm -f "$app/Caddyfile"
fi
if [ -f "$staging/tailscale-bootstrap.sh" ]; then
  install -m 0700 "$staging/tailscale-bootstrap.sh" "$app/tailscale-bootstrap.sh"
fi
if [ -f "$staging/host-tailscale-bootstrap.sh" ]; then
  install -m 0700 "$staging/host-tailscale-bootstrap.sh" "$app/host-tailscale-bootstrap.sh"
fi
if [ -f "$staging/agent-stack-vpn" ]; then
  install -m 0700 "$staging/agent-stack-vpn" "$app/agent-stack-vpn"
fi
if [ -f "$staging/agent-stack-vpn-openvpn" ]; then
  install -m 0700 "$staging/agent-stack-vpn-openvpn" "$app/agent-stack-vpn-openvpn"
fi
if [ -d "$staging/templates" ]; then
  rm -rf "$app/templates"
  install -d -m 0755 "$app/templates"
  cp -a "$staging/templates/." "$app/templates/"
  chmod -R u=rwX,go=rX "$app/templates"
fi
sync_openai_codex_auth
install_host_codex_cli
configure_admin_password_ssh
install_workspace_runtime

install -m 0644 "$staging/agent-stack.service" /etc/systemd/system/agent-stack.service
install -m 0644 "$staging/openclaw.service" /etc/systemd/system/openclaw.service
if [ -f "$staging/agent-stack-tailscale-watchdog" ]; then
  install -m 0755 "$staging/agent-stack-tailscale-watchdog" /usr/local/bin/agent-stack-tailscale-watchdog
  install -m 0644 "$staging/agent-stack-tailscale-watchdog.service" /etc/systemd/system/agent-stack-tailscale-watchdog.service
  install -m 0644 "$staging/agent-stack-tailscale-watchdog.timer" /etc/systemd/system/agent-stack-tailscale-watchdog.timer
fi

systemctl daemon-reload
configure_host_vpn
systemctl enable agent-stack openclaw
if [ "${tailscale_sidecar_enabled}" = "true" ] && grep -q '^TAILSCALE_AUTH_KEY=' "$app/.env"; then
  systemctl enable --now agent-stack-tailscale-watchdog.timer || true
else
  systemctl disable --now agent-stack-tailscale-watchdog.timer 2>/dev/null || true
fi

restart_status=0
systemctl restart agent-stack || restart_status=$?
if [ "$restart_status" -ne 0 ]; then
  echo "[runtime] WARNING: systemctl restart agent-stack exited $restart_status; waiting for service recovery before deciding failure." >&2
fi
if ! wait_agent_stack_initial_restart; then
  echo "[runtime] restart did not recover; restoring previous runtime files" >&2
  [ "$openclaw_seed_changed" != "1" ] || rm -f "$OPENCLAW_CONFIG"
  if [ "$hermes_config_changed" = "1" ]; then
    if [ -f "$previous/hermes-config.yaml" ]; then
      cp -a "$previous/hermes-config.yaml" "$app/data/hermes/config.yaml"
    else
      rm -f "$app/data/hermes/config.yaml"
    fi
  fi
  for path in docker-compose.yml .env workspace.env hermes_health.py hermes_config.py openclaw_config.py Caddyfile tailscale-bootstrap.sh host-tailscale-bootstrap.sh agent-stack-vpn agent-stack-vpn-openvpn workspace.Dockerfile workspace-entrypoint.sh workspace-para-memory-drive-skill.md workspace-codex-update.sh workspace-codex-control.py workspace-cli-task.py; do
    rm -rf "$app/$path"
    if [ -e "$previous/$path" ]; then
      cp -a "$previous/$path" "$app/$path"
    fi
  done
  restore_workspace_image || echo "[workspace] WARNING: could not restore the prior workspace image" >&2
  systemctl restart agent-stack || true
  fail "agent-stack restart failed"
fi
systemctl start openclaw || true
configure_host_tailscale
backup_workspace_codex_auto_update_host
if ! configure_workspace_codex_auto_update; then
  echo "[workspace-codex-update] installation failed; restoring the prior host updater files" >&2
  restore_workspace_codex_auto_update_host
  fail "workspace Codex updater installation failed"
fi

OPENCLAW_ENABLED="${openclaw_enabled}"
OPENCLAW_CONFIG_MODE_INPUT="${openclaw_config_mode}"
case "$OPENCLAW_CONFIG_MODE_INPUT" in
  manage|preserve)
    OPENCLAW_CONFIG_MODE_EFFECTIVE="$OPENCLAW_CONFIG_MODE_INPUT"
    ;;
  auto)
    if [ "$PREEXISTING_OPENCLAW_CONFIG" = "1" ]; then
      OPENCLAW_CONFIG_MODE_EFFECTIVE="preserve"
    else
      OPENCLAW_CONFIG_MODE_EFFECTIVE="manage"
    fi
    ;;
  *)
    OPENCLAW_CONFIG_MODE_EFFECTIVE="manage"
    ;;
esac
AGENT_CHANNEL="${agent_channel}"
MODEL_PROVIDERS_ENABLED_JSON='${model_providers_enabled_json}'
OPENAI_AUTH_MODE='${openai_auth_mode}'
DEFAULT_MODEL_REF='${default_model}'
FALLBACK_MODELS_JSON='${fallback_models_json}'
TELEGRAM_ALLOW_FROM_JSON='${telegram_allow_from_json}'
STARTER_SOUL_PROFILE='${starter_soul_profile}'
SHOULD_SEED_STARTER_FILES='${seed_starter_workspace_files}'

echo "[config] openclaw_config_mode=$OPENCLAW_CONFIG_MODE_INPUT effective=$OPENCLAW_CONFIG_MODE_EFFECTIVE preexisting_config=$PREEXISTING_OPENCLAW_CONFIG"
echo "[config] agent_channel=$AGENT_CHANNEL starter_soul_profile=$STARTER_SOUL_PROFILE"
echo "[config] openai_auth_mode=$OPENAI_AUTH_MODE"

wait_openclaw_healthy() {
  if [ "$OPENCLAW_ENABLED" != "true" ]; then
    return 1
  fi
  for attempt in $(seq 1 60); do
    local container_id
    container_id="$(docker compose -f "$app/docker-compose.yml" ps -q openclaw 2>/dev/null || true)"
    if [ -n "$container_id" ]; then
      local health_status
      health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      if [ "$health_status" = "healthy" ] || [ "$health_status" = "running" ]; then
        return 0
      fi
    fi
    sleep 3
  done
  return 1
}

wait_for_openclaw_config() {
  for attempt in $(seq 1 60); do
    [ -f "$OPENCLAW_CONFIG" ] && return 0
    sleep 2
  done
  return 1
}

mark_openclaw_restart_needed() {
  NEEDS_RESTART=1
}

run_openclaw_cli() {
  local cli_seconds="$${OPENCLAW_CLI_TIMEOUT_SECONDS:-30}"
  if [[ ! "$cli_seconds" =~ ^[1-9][0-9]{0,3}$ ]]; then
    echo "[openclaw-cli] Invalid timeout in seconds." >&2
    return 2
  fi
  if command -v timeout >/dev/null 2>&1; then
    # Docker clients can forward TERM without exiting. Bound the command
    # inside the container and force-stop an overdue client after its grace.
    timeout --kill-after=5s "$((cli_seconds + 10))s" \
      docker compose -f "$app/docker-compose.yml" exec --interactive=false -T openclaw \
      sh -c 'seconds="$1"; shift; if command -v timeout >/dev/null 2>&1; then exec timeout -k 5s "$seconds" openclaw "$@"; else exec openclaw "$@"; fi' \
      sh "$cli_seconds" "$@"
  else
    docker compose -f "$app/docker-compose.yml" exec --interactive=false -T openclaw openclaw "$@"
  fi
}

env_value() {
  env_file_value "$app/.env" "$1"
}

has_env_key() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  [ -n "$value" ]
}

seed_file_if_missing() {
  local source="$1"
  local destination="$2"
  if [ -f "$destination" ]; then
    echo "[starter] Keeping existing file: $destination"
    return 0
  fi
  install -D -m 0644 "$source" "$destination"
  chown 1000:1000 "$destination" || true
  echo "[starter] Seeded: $destination"
}

seed_starter_workspace_files() {
  if [ "$SHOULD_SEED_STARTER_FILES" != "true" ]; then
    echo "[starter] Skipped (seed_starter_workspace_files=$SHOULD_SEED_STARTER_FILES)."
    return 0
  fi

  local workspace_dir="$app/data/openclaw/workspace"
  local soul_source="$app/templates/SOUL.${starter_soul_profile}.md"
  if [ ! -f "$soul_source" ]; then
    soul_source="$app/templates/SOUL.balanced.md"
  fi

  mkdir -p "$workspace_dir"
  chown 1000:1000 "$workspace_dir" || true
  seed_file_if_missing "$soul_source" "$workspace_dir/SOUL.md"
  seed_file_if_missing "$app/templates/AGENTS.default.md" "$workspace_dir/AGENTS.md"
  seed_file_if_missing "$app/templates/TOOLS.default.md" "$workspace_dir/TOOLS.md"
  seed_file_if_missing "$app/templates/USER.default.md" "$workspace_dir/USER.md"
}

if [ "$OPENCLAW_ENABLED" = "true" ]; then
  if wait_openclaw_healthy; then
    seed_starter_workspace_files
  else
    echo "[starter] WARNING: OpenClaw did not become healthy; skipping starter file seed."
  fi
else
  echo "[starter] OpenClaw is disabled; skipping OpenClaw starter files."
fi

config_customizations_enabled() {
  [ "$OPENCLAW_CONFIG_MODE_EFFECTIVE" = "manage" ]
}

provider_selected() {
  local provider="$1"
  printf '%s' "$MODEL_PROVIDERS_ENABLED_JSON" | jq -e --arg provider "$provider" 'index($provider) != null' >/dev/null 2>&1
}

model_provider_from_ref() {
  local model_ref="$1"
  local raw_provider
  raw_provider="$${model_ref%%/*}"
  case "$raw_provider" in
    openai-codex)
      echo "openai"
      ;;
    *)
      echo "$raw_provider"
      ;;
  esac
}

model_route_has_credentials() {
  local route="$1"
  case "$route" in
    anthropic)
      has_env_key ANTHROPIC_API_KEY || has_env_key ANTHROPIC_AUTH_KEY
      ;;
    openai)
      if [ "$OPENAI_AUTH_MODE" = "codex" ]; then
        [ -s "$app/codex/auth.json" ]
      else
        has_env_key OPENAI_API_KEY
      fi
      ;;
    openai-codex)
      [ -s "$app/codex/auth.json" ]
      ;;
    google)
      has_env_key GEMINI_API_KEY
      ;;
    groq)
      has_env_key GROQ_API_KEY
      ;;
    *)
      return 1
      ;;
  esac
}

model_exists() {
  local model_ref="$1"
  printf '%s\n' "$MODEL_CATALOG" | grep -Fxq "$model_ref"
}

model_is_usable() {
  local model_ref="$1"
  local provider
  local route
  provider="$(model_provider_from_ref "$model_ref")"
  route="$${model_ref%%/*}"

  if ! provider_selected "$provider"; then
    echo "[models] WARNING: Skipping $model_ref because provider '$provider' was not selected."
    return 1
  fi
  if ! model_route_has_credentials "$route"; then
    echo "[models] WARNING: Skipping $model_ref because route '$route' credentials are missing."
    return 1
  fi
  if ! model_exists "$model_ref"; then
    echo "[models] WARNING: Skipping $model_ref because it is unavailable in the model catalog."
    return 1
  fi
  return 0
}

openai_codex_runtime_enabled() {
  [ "$OPENAI_AUTH_MODE" = "codex" ]
}

configured_openai_codex_models_json() {
  jq -nc \
    --arg default_model "$DEFAULT_MODEL_REF" \
    --argjson fallback_models "$FALLBACK_MODELS_JSON" \
    '([$default_model] + $fallback_models) | map(select(startswith("openai/"))) | unique'
}

configure_openai_codex_runtime_routes() {
  if ! openai_codex_runtime_enabled; then
    return 0
  fi
  if ! wait_for_openclaw_config; then
    echo "[openai] WARNING: $OPENCLAW_CONFIG not found; skipped Codex runtime routing."
    return 1
  fi

  local codex_models_json
  codex_models_json="$(configured_openai_codex_models_json)"
  if [ "$(printf '%s' "$codex_models_json" | jq 'length')" -eq 0 ]; then
    echo "[openai] No canonical openai/* models selected for Codex runtime routing."
    return 0
  fi

  local tmp_config
  tmp_config="$(mktemp)"
  if jq --argjson codex_models "$codex_models_json" '
    def set_agent_runtime:
      (.model // "") as $model
      | if (($codex_models | index($model)) != null) then
          .models = (.models // {})
          | .models[$model].agentRuntime.id = "codex"
        else
          .
        end;
    def walk_agents:
      if type == "object" then
        with_entries(.value |= walk_agents) | set_agent_runtime
      elif type == "array" then
        map(walk_agents)
      else
        .
      end;

    .agents = (.agents // {})
    | .agents.defaults = (.agents.defaults // {})
    | .agents.defaults.models = (.agents.defaults.models // {})
    | reduce $codex_models[] as $model (.;
        .agents.defaults.models[$model].agentRuntime.id = "codex"
      )
    | .agents |= walk_agents
  ' "$OPENCLAW_CONFIG" > "$tmp_config"; then
    if ! cmp -s "$tmp_config" "$OPENCLAW_CONFIG"; then
      mv "$tmp_config" "$OPENCLAW_CONFIG"
      chown 1000:1000 "$OPENCLAW_CONFIG" || true
      echo "[openai] Codex runtime routing configured for openai/* models: $(printf '%s' "$codex_models_json" | jq -r 'join(", ")')"
      mark_openclaw_restart_needed
    else
      rm -f "$tmp_config"
      echo "[openai] Codex runtime routing already up to date."
    fi
  else
    rm -f "$tmp_config"
    echo "[openai] WARNING: Failed to configure Codex runtime routing."
    return 1
  fi
}

ensure_plugin_enabled() {
  local plugin="$1"
  local install_log="/tmp/openclaw-plugin-install-$plugin.log"
  local enable_log="/tmp/openclaw-plugin-enable-$plugin.log"

  for attempt in $(seq 1 5); do
    if run_openclaw_cli plugins install "$plugin" >"$install_log" 2>&1; then
      break
    fi
    if grep -qi "already installed" "$install_log"; then
      break
    fi
    sleep 3
  done

  for attempt in $(seq 1 10); do
    if run_openclaw_cli plugins enable "$plugin" >"$enable_log" 2>&1; then
      echo "[plugins] $plugin plugin enabled."
      mark_openclaw_restart_needed
      return 0
    fi
    if grep -qi "already enabled" "$enable_log"; then
      echo "[plugins] $plugin plugin already enabled."
      return 0
    fi
    sleep 3
  done

  echo "[plugins] WARNING: Failed to enable plugin $plugin after retries."
  tail -n 5 "$enable_log" || true
  return 1
}

configure_telegram_channel() {
  if ! wait_for_openclaw_config; then
    echo "[telegram] WARNING: $OPENCLAW_CONFIG not found; skipped Telegram setup."
    return 1
  fi

  local token
  token="$(env_value TELEGRAM_BOT_TOKEN)"
  local tmp_config
  tmp_config="$(mktemp)"
  if jq --arg token "$token" --argjson allow_from "$TELEGRAM_ALLOW_FROM_JSON" '.channels = (.channels // {}) | .channels.telegram = ((.channels.telegram // {}) + { enabled: true, botToken: $token, streaming: "off" }) | if (($allow_from | type) == "array" and ($allow_from | length) > 0) then .channels.telegram.allowFrom = $allow_from else . end' "$OPENCLAW_CONFIG" > "$tmp_config"; then
    if ! cmp -s "$tmp_config" "$OPENCLAW_CONFIG"; then
      mv "$tmp_config" "$OPENCLAW_CONFIG"
      chown 1000:1000 "$OPENCLAW_CONFIG" || true
      echo "[telegram] Telegram channel config updated."
      mark_openclaw_restart_needed
    else
      rm -f "$tmp_config"
      echo "[telegram] Telegram channel config already up to date."
    fi
  else
    rm -f "$tmp_config"
    echo "[telegram] WARNING: Failed to update Telegram channel config."
    return 1
  fi
}

configure_openclaw_channels_and_models() {
  if [ "$OPENCLAW_ENABLED" != "true" ] || ! config_customizations_enabled; then
    echo "[config] OpenClaw disabled or config mode is '$OPENCLAW_CONFIG_MODE_EFFECTIVE'; skipping optional channel and model customizations."
    return 0
  fi

  echo "[config] Applying optional channel and model bootstrap customizations."
  if wait_openclaw_healthy; then
    case "$AGENT_CHANNEL" in
      telegram)
        if has_env_key TELEGRAM_BOT_TOKEN; then
          ensure_plugin_enabled "telegram" || true
          configure_telegram_channel || true
        else
          echo "[plugins] WARNING: agent_channel=telegram but TELEGRAM_BOT_TOKEN is missing; skipping Telegram plugin/config setup."
          if [ "$TELEGRAM_ALLOW_FROM_JSON" != "[]" ]; then
            echo "[telegram] NOTE: telegram_allow_from was provided but TELEGRAM_BOT_TOKEN is missing; allowlist was not applied."
          fi
        fi
        ;;
      whatsapp)
        ensure_plugin_enabled "whatsapp" || true
        ;;
    esac
  else
    echo "[plugins] WARNING: OpenClaw did not become ready in time; skipping plugin setup."
  fi

  if provider_selected "anthropic"; then
    if has_env_key ANTHROPIC_AUTH_KEY; then
      echo "[anthropic] Registering legacy Anthropic setup-token..."
      if wait_openclaw_healthy; then
        local anthropic_auth_key
        anthropic_auth_key="$(env_value ANTHROPIC_AUTH_KEY)"
        if run_openclaw_cli onboard --non-interactive \
            --auth-choice token \
            --token-provider anthropic \
            --token "$anthropic_auth_key" \
            --token-expires-in 365d; then
          echo "[anthropic] Legacy setup-token registered successfully."
        else
          echo "[anthropic] WARNING: Legacy onboard --non-interactive command failed."
        fi
      else
        echo "[anthropic] WARNING: OpenClaw not healthy; skipped legacy token registration."
      fi
    elif has_env_key ANTHROPIC_API_KEY; then
      echo "[anthropic] Using ANTHROPIC_API_KEY runtime auth."
    else
      echo "[anthropic] WARNING: Provider anthropic selected, but no Anthropic credential is configured."
    fi
  else
    echo "[anthropic] Provider not selected; skipping Anthropic auth bootstrap."
  fi

  echo "[models] Configuring user-selected model defaults and fallbacks..."
  if wait_openclaw_healthy; then
    MODEL_CATALOG="$(run_openclaw_cli models list --all --plain 2>/dev/null || true)"
    if model_is_usable "$DEFAULT_MODEL_REF"; then
      if run_openclaw_cli models set "$DEFAULT_MODEL_REF" >/tmp/openclaw-models-set.log 2>&1; then
        echo "[models] Default model set: $DEFAULT_MODEL_REF"
        if run_openclaw_cli models fallbacks clear >/tmp/openclaw-models-fallback-clear.log 2>&1; then
          echo "[models] Cleared existing fallbacks."
        else
          echo "[models] WARNING: Failed to clear existing fallbacks."
          tail -n 5 /tmp/openclaw-models-fallback-clear.log || true
        fi

        while IFS= read -r fallback_model; do
          [ -n "$fallback_model" ] || continue
          if model_is_usable "$fallback_model"; then
            if run_openclaw_cli models fallbacks add "$fallback_model" >/tmp/openclaw-models-fallback-add.log 2>&1; then
              echo "[models] Added fallback: $fallback_model"
            else
              echo "[models] WARNING: Failed to add fallback: $fallback_model"
              tail -n 5 /tmp/openclaw-models-fallback-add.log || true
            fi
          fi
        done < <(printf '%s' "$FALLBACK_MODELS_JSON" | jq -r '.[]')
        configure_openai_codex_runtime_routes || true
      else
        echo "[models] WARNING: Failed to set default model: $DEFAULT_MODEL_REF"
        tail -n 5 /tmp/openclaw-models-set.log || true
      fi
    fi
  else
    echo "[models] WARNING: OpenClaw not ready; skipped model configuration."
  fi
}

read_tailscale_dns() {
  local tailscale_dns=""
  if [ "${tailscale_host_enabled}" = "true" ]; then
    echo "[tailscale] Reading host Tailscale status..." >&2
    for attempt in $(seq 1 40); do
      if tailscale status --json 2>/dev/null | jq -e '.Self.Online == true' >/dev/null 2>&1; then
        break
      fi
      sleep 3
    done
    tailscale_dns="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' || true)"
    tailscale_dns="$${tailscale_dns%.}"
    if [ -n "$tailscale_dns" ]; then
      echo "[tailscale] Host Serve URL: https://$tailscale_dns" >&2
    else
      echo "[tailscale] WARNING: Host Tailscale DNS name unavailable." >&2
    fi
    printf '%s' "$tailscale_dns"
    return 0
  fi

  if [ "${tailscale_sidecar_enabled}" != "true" ]; then
    printf '%s' "$tailscale_dns"
    return 0
  fi

  echo "[tailscale] Waiting for Tailscale sidecar..." >&2
  local ts_container_id=""
  for attempt in $(seq 1 40); do
    ts_container_id="$(docker compose -f "$app/docker-compose.yml" ps -q tailscale 2>/dev/null || true)"
    if [ -n "$ts_container_id" ] && docker exec "$ts_container_id" tailscale --socket=/tmp/tailscaled.sock status --json 2>/dev/null | jq -e '.Self.Online == true' >/dev/null 2>&1; then
      break
    fi
    sleep 3
  done

  if [ -n "$ts_container_id" ]; then
    tailscale_dns="$(docker exec "$ts_container_id" tailscale --socket=/tmp/tailscaled.sock status --json 2>/dev/null | jq -r '.Self.DNSName // empty' || true)"
    tailscale_dns="$${tailscale_dns%.}"
    if [ -n "$tailscale_dns" ]; then
      echo "[tailscale] Serve URL: https://$tailscale_dns" >&2
    else
      echo "[tailscale] Sidecar started. Check logs with: docker compose -f $app/docker-compose.yml logs tailscale" >&2
    fi
  else
    echo "[tailscale] WARNING: Tailscale sidecar container not detected." >&2
  fi

  printf '%s' "$tailscale_dns"
}

refresh_openclaw_gateway_config() {
  if [ "$OPENCLAW_ENABLED" != "true" ]; then
    echo "[openclaw] OpenClaw is disabled; skipped gateway config update."
    return 0
  fi

  echo "[openclaw] Refreshing gateway token and gateway.controlUi.allowedOrigins..."
  local tailscale_dns="$1"
  local project_origin=""
  if [ "${tailscale_enabled}" = "true" ]; then
    project_origin="https://${project_name}"
  fi
  local public_openclaw_origin=""
  if [ "${openclaw_domain}" != "" ]; then
    public_openclaw_origin="https://${openclaw_domain}"
  fi

  if ! wait_for_openclaw_config; then
    echo "[openclaw] WARNING: $OPENCLAW_CONFIG not found; skipped gateway config update."
    return 0
  fi

  local origins_json
  origins_json="$(jq -nc --arg project_origin "$project_origin" --arg tailscale_dns "$tailscale_dns" --arg public_origin "$public_openclaw_origin" '[
    "http://127.0.0.1:18789",
    "http://localhost:18789",
    (if $project_origin != "" then $project_origin else empty end),
    (if $tailscale_dns != "" then "https://" + $tailscale_dns else empty end),
    (if $public_origin != "" then $public_origin else empty end)
  ] | unique')"
  local gateway_token
  gateway_token="$(env_value OPENCLAW_GATEWAY_TOKEN)"
  local tmp_config
  tmp_config="$(mktemp)"
  if jq --argjson origins "$origins_json" --arg gateway_token "$gateway_token" --arg bind "${openclaw_gateway_bind}" '.gateway = ((.gateway // {}) + {bind:$bind}) | .gateway.auth = ((.gateway.auth // {}) + { mode: "token", token: $gateway_token }) | .gateway.controlUi = ((.gateway.controlUi // {}) + { allowedOrigins: $origins })' "$OPENCLAW_CONFIG" > "$tmp_config"; then
    if ! cmp -s "$tmp_config" "$OPENCLAW_CONFIG"; then
      mv "$tmp_config" "$OPENCLAW_CONFIG"
      chown 1000:1000 "$OPENCLAW_CONFIG" || true
      echo "[openclaw] Updated gateway.auth.token and gateway.controlUi.allowedOrigins."
      mark_openclaw_restart_needed
    else
      rm -f "$tmp_config"
      echo "[openclaw] Gateway config already up to date."
    fi
  else
    rm -f "$tmp_config"
    echo "[openclaw] WARNING: Failed to update gateway config."
  fi
}

restart_agent_stack_for_config() {
  local restart_status=0
  systemctl restart agent-stack || restart_status=$?
  if [ "$restart_status" != "0" ]; then
    echo "[openclaw] WARNING: systemctl restart agent-stack exited $restart_status; waiting for service recovery before deciding failure."
  fi

  wait_agent_stack_initial_restart
}

openclaw_config_backup=""
if [ -f "$OPENCLAW_CONFIG" ]; then
  openclaw_config_backup="$(mktemp)"
  cp -a "$OPENCLAW_CONFIG" "$openclaw_config_backup"
fi

configure_openclaw_channels_and_models
TAILSCALE_DNS="$(read_tailscale_dns)"
refresh_openclaw_gateway_config "$TAILSCALE_DNS"

if [ "$NEEDS_RESTART" = "1" ]; then
  echo "[openclaw] Applying accumulated config changes with one final restart..."
  if ! restart_agent_stack_for_config; then
    if [ -n "$openclaw_config_backup" ] && [ -f "$openclaw_config_backup" ]; then
      cp -a "$openclaw_config_backup" "$OPENCLAW_CONFIG"
      chown 1000:1000 "$OPENCLAW_CONFIG" || true
      systemctl restart agent-stack || true
    fi
    fail "agent-stack restart failed after OpenClaw config changes"
  fi
fi
rm -f "$openclaw_config_backup"

jq -n --arg dns "$TAILSCALE_DNS" '{schema:1, tailscale_dns:$dns}' > "$app/.access.json"
chmod 0644 "$app/.access.json"

jq -n \
  --arg checksum "$checksum" \
  --arg applied_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --slurpfile services "$staging/enabled-services.json" \
  '{status:"success", checksum:$checksum, applied_at:$applied_at, services:($services[0] // [])}' \
  > "$app/.last-apply.json"
chmod 0600 "$app/.last-apply.json"
rm -rf "$staging"

echo "========================================================"
echo " AgentStack runtime apply complete  $(date)"
echo " Services: ${enabled_services_json}"
if [ "$OPENCLAW_ENABLED" = "true" ]; then
  if [ -n "$TAILSCALE_DNS" ] && [ "${tailscale_enabled}" = "true" ]; then
    echo " OpenClaw: https://$TAILSCALE_DNS (via Tailscale Serve, mode=${tailscale_mode})"
  else
    echo " OpenClaw: ssh -L 18789:127.0.0.1:18789 ${admin_username}@<IP>"
  fi
fi
if [ "${hermes_enabled}" = "true" ] && [ "${hermes_dashboard_enabled}" = "true" ] && [ -n "$TAILSCALE_DNS" ]; then
  echo " Hermes: https://$TAILSCALE_DNS:8443 (via Tailscale Serve)"
fi
if [ "${workspace_enabled}" = "true" ]; then
  echo " Workspace SSH: ssh -p ${workspace_ssh_host_port} ${workspace_username}@${workspace_ssh_host} (via Tailscale)"
fi
echo " Logs: journalctl -u agent-stack -f"
echo "========================================================"
