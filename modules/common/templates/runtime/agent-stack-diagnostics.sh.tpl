#!/usr/bin/env bash
set -euo pipefail

app=/opt/agent-stack
compose="$app/docker-compose.yml"
VPN_ENABLED="${vpn_enabled}"
VPN_PROVIDER_DEFAULT="${vpn_provider}"
TAILSCALE_HOST_ENABLED="${tailscale_host_enabled}"
WORKSPACE_CODEX_AUTO_UPDATE_ENABLED="${workspace_codex_auto_update_enabled}"
WORKSPACE_CODEX_AUTO_RECOVER_INTERRUPTED_TURNS="${workspace_codex_auto_recover_interrupted_turns}"

usage() {
  cat <<'EOF'
Usage:
  agent-stack-diagnostics status
  agent-stack-diagnostics health [openclaw|workspace|tailscale|vpn]
  agent-stack-diagnostics logs <openclaw|hermes|n8n|postgres|caddy|workspace|tailscale|vpn|agent-stack> [lines]
  agent-stack-diagnostics inspect <openclaw|hermes|n8n|postgres|caddy|workspace|tailscale|vpn|agent-stack>
  agent-stack-diagnostics restart <openclaw|hermes|n8n|postgres|caddy|workspace|tailscale|vpn|agent-stack>
  agent-stack-diagnostics codex-update
  agent-stack-diagnostics codex-update status
EOF
}

compose_service() {
  case "$1" in
    openclaw|hermes|n8n|postgres|caddy|workspace) return 0 ;;
    *) return 1 ;;
  esac
}

managed_service() {
  case "$1" in
    openclaw|hermes|n8n|postgres|caddy|workspace|tailscale|vpn|agent-stack) return 0 ;;
    *) return 1 ;;
  esac
}

require_managed_service() {
  local service="$1"
  if ! managed_service "$service"; then
    echo "unsupported service: $service" >&2
    exit 64
  fi
}

lines_arg() {
  local lines="$${1:-120}"
  if ! printf '%s' "$lines" | grep -Eq '^[0-9]{1,4}$'; then
    echo "invalid line count: $lines" >&2
    exit 64
  fi
  printf '%s' "$lines"
}

container_id() {
  local service="$1"
  docker compose -f "$compose" ps -q "$service" 2>/dev/null || true
}

show_container_inspect() {
  local service="$1"
  local cid
  cid="$(container_id "$service")"
  if [ -z "$cid" ]; then
    echo "container not found for service: $service" >&2
    return 0
  fi

  docker inspect --format 'name={{.Name}} image={{.Config.Image}} created={{.Created}} status={{.State.Status}} running={{.State.Running}} restartCount={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} started={{.State.StartedAt}} finished={{.State.FinishedAt}}' "$cid"
  docker inspect --format '{{range .Mounts}}mount={{.Type}}:{{.Source}}->{{.Destination}}:{{.Mode}}{{println}}{{end}}' "$cid"
  docker inspect --format 'devices={{json .HostConfig.Devices}} capAdd={{json .HostConfig.CapAdd}} securityOpt={{json .HostConfig.SecurityOpt}} privileged={{.HostConfig.Privileged}}' "$cid"
}

show_workspace_codex_update_status() {
  local unit="agent-stack-workspace-codex-update.service"
  local timer="agent-stack-workspace-codex-update.timer"
  local cid service_state pending version path canonical_target last_maintenance

  echo "enabled=$WORKSPACE_CODEX_AUTO_UPDATE_ENABLED"
  echo "recovery_enabled=$WORKSPACE_CODEX_AUTO_RECOVER_INTERRUPTED_TURNS"
  echo "timer_enabled=$(systemctl is-enabled "$timer" 2>/dev/null || true)"
  echo "timer_active=$(systemctl is-active "$timer" 2>/dev/null || true)"
  service_state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  echo "service_active=$service_state"
  case "$service_state" in
    active|activating) pending=true ;;
    *) pending=false ;;
  esac
  echo "pending=$pending"
  echo "last_result=$(systemctl show "$unit" -p Result --value 2>/dev/null || true)"
  echo "last_exit_status=$(systemctl show "$unit" -p ExecMainStatus --value 2>/dev/null || true)"
  echo "next_elapse=$(systemctl show "$timer" -p NextElapseUSecRealtime --value 2>/dev/null || true)"
  if [ -r /var/lib/agent-stack/workspace-codex-update/ledger.jsonl ]; then
    # The root-only ledger can contain thread and turn IDs for recovery
    # deduplication.  Status is reachable through the workspace's restricted
    # diagnostics bridge, so expose only operational timing/outcome and
    # release/error metadata—not per-thread maintenance state.
    last_maintenance="$(tail -n 1 /var/lib/agent-stack/workspace-codex-update/ledger.jsonl 2>/dev/null | jq -c '{at,eventId,phase,attempt,outcome,beforeVersion:(.data.before_version // ""),afterVersion:(.data.after_version // ""),error:(.data.error // "")}' 2>/dev/null || true)"
  else
    last_maintenance=""
  fi
  echo "last_maintenance=$last_maintenance"

  cid="$(container_id workspace)"
  if [ -z "$cid" ]; then
    echo "workspace_container=missing"
    echo "current_effective_path="
    echo "current_effective_version="
    echo "canonical_target="
    return 0
  fi

  path="$(docker exec --user ${workspace_username} "$cid" env HOME=/home/${workspace_username} CODEX_HOME=/home/${workspace_username}/.codex PATH=/home/${workspace_username}/.local/bin:/usr/local/bin:/usr/bin:/bin /bin/bash -c 'command -v codex 2>/dev/null || true' 2>/dev/null || true)"
  version="$(docker exec --user ${workspace_username} "$cid" env HOME=/home/${workspace_username} CODEX_HOME=/home/${workspace_username}/.codex PATH=/home/${workspace_username}/.local/bin:/usr/local/bin:/usr/bin:/bin /bin/bash -c 'codex --version 2>/dev/null || true' 2>/dev/null || true)"
  canonical_target="$(docker exec --user ${workspace_username} "$cid" env HOME=/home/${workspace_username} /bin/bash -c 'readlink -f "$HOME/.codex/packages/standalone/current" 2>/dev/null || true' 2>/dev/null || true)"
  echo "workspace_container=running"
  echo "current_effective_path=$path"
  echo "current_effective_version=$version"
  echo "canonical_target=$canonical_target"
}

show_workspace_coding_tools() {
  local cid tool version
  echo "default=codex"
  cid="$(container_id workspace)"
  [ -n "$cid" ] || { echo "workspace_container=missing"; return 0; }
  for tool in codex claude muse; do
    version="$(docker exec --user ${workspace_username} "$cid" /usr/bin/timeout 10 /bin/sh -c 'command -v "$1" >/dev/null 2>&1 && "$1" --version' sh "$tool" 2>/dev/null | head -n 1 || true)"
    if [ -n "$version" ]; then
      printf '%s=%s\n' "$tool" "$version"
    else
      printf '%s=unavailable\n' "$tool"
    fi
  done
}

queue_workspace_codex_update() {
  if [ "$WORKSPACE_CODEX_AUTO_UPDATE_ENABLED" != "true" ]; then
    echo "workspace Codex auto update is disabled" >&2
    return 1
  fi

  systemctl start --no-block agent-stack-workspace-codex-update.service
  echo "codex_update=queued"
}

show_vpn_state() {
  local strict="$${1:-false}"
  local provider="$VPN_PROVIDER_DEFAULT"
  local failed=0
  local status_output=""

  if [ -s /etc/agent-stack-vpn/provider ]; then
    provider="$(head -n 1 /etc/agent-stack-vpn/provider)"
  fi

  echo "configured_provider=$provider"
  echo "service_active=$(systemctl is-active agent-stack-vpn 2>/dev/null || true)"
  echo "service_enabled=$(systemctl is-enabled agent-stack-vpn 2>/dev/null || true)"

  if [ "$VPN_ENABLED" != "true" ]; then
    echo "vpn_state=disabled"
    return 0
  fi

  if ! systemctl is-active --quiet agent-stack-vpn; then
    echo "vpn_error=agent-stack-vpn.service is not active"
    failed=1
  fi

  case "$provider" in
    nordvpn_nordlynx)
      if command -v nordvpn >/dev/null 2>&1; then
        echo "nordvpn_version=$(nordvpn version 2>/dev/null || nordvpn --version 2>/dev/null || echo unknown)"
        status_output="$(nordvpn status 2>&1 || true)"
        printf '%s\n' "$status_output"
        if ! printf '%s\n' "$status_output" | grep -qi '^Status:[[:space:]]*Connected'; then
          echo "vpn_error=NordVPN is disconnected or unavailable"
          failed=1
        fi
        if ! printf '%s\n' "$status_output" | grep -qi '^Current technology:[[:space:]]*NORDLYNX'; then
          echo "vpn_error=NordVPN is not using NORDLYNX"
          failed=1
        fi
      else
        echo "vpn_error=nordvpn command is not installed"
        failed=1
      fi
      if ! ip link show nordlynx >/dev/null 2>&1; then
        echo "vpn_error=nordlynx interface is missing"
        failed=1
      fi
      ;;
    nordvpn_openvpn)
      if ! ip -o link show 2>/dev/null | grep -Eq ': tun[0-9]+@?'; then
        echo "vpn_error=OpenVPN tunnel interface is missing"
        failed=1
      fi
      ;;
    *)
      echo "vpn_error=unsupported configured provider: $provider"
      failed=1
      ;;
  esac

  echo "default_route=$(ip -4 route show default 2>/dev/null | head -n 1 || true)"
  echo "tunnel_links:"
  ip -o link show 2>/dev/null | grep -E ': (tun[0-9]+|nordlynx)@?' || true
  echo "egress_ip=$(curl -fsS --max-time 8 "${vpn_healthcheck_url}" 2>/dev/null || true)"
  echo "rollback_pending=$([ -e /var/lib/agent-stack/vpn-rollback/pending ] && echo true || echo false)"

  if [ "$TAILSCALE_HOST_ENABLED" = "true" ]; then
    if command -v tailscale >/dev/null 2>&1; then
      local tailscale_online
      tailscale_online="$(tailscale status --json 2>/dev/null | jq -r '.Self.Online // false' || echo false)"
      echo "tailscale_online=$tailscale_online"
      if [ "$tailscale_online" != "true" ]; then
        echo "vpn_error=host Tailscale is offline"
        failed=1
      fi
    else
      echo "vpn_error=host Tailscale command is missing"
      failed=1
    fi
  fi

  if [ "$strict" = "true" ] && [ "$failed" -ne 0 ]; then
    return 1
  fi
}

show_status() {
  echo "== systemd =="
  systemctl is-active agent-stack 2>/dev/null || true
  systemctl is-active tailscaled 2>/dev/null || true
  systemctl is-active agent-stack-vpn 2>/dev/null || true
  echo
  echo "== containers =="
  docker compose -f "$compose" ps 2>/dev/null || true
  echo
  echo "== tailscale =="
  if command -v tailscale >/dev/null 2>&1; then
    tailscale status --json 2>/dev/null | jq -r '.Self.Online, .Self.DNSName, (.Self.TailscaleIPs[]? // empty), (.Health[]? // empty)' || tailscale status || true
  else
    echo "tailscale command not installed"
  fi
  echo
  echo "== vpn =="
  if systemctl list-unit-files agent-stack-vpn.service >/dev/null 2>&1; then
    show_vpn_state false
  else
    echo "agent-stack-vpn service not installed"
  fi
  echo
  echo "== workspace Codex updater =="
  show_workspace_codex_update_status
  echo
  echo "== workspace coding tools =="
  show_workspace_coding_tools
}

show_health() {
  local service="$${1:-openclaw}"
  require_managed_service "$service"
  case "$service" in
    openclaw)
      curl -fsS --max-time 5 http://127.0.0.1:18789/healthz || true
      ;;
    workspace)
      local cid
      cid="$(container_id workspace)"
      [ -n "$cid" ] || { echo "workspace container not found" >&2; return 1; }
      docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid"
      docker inspect --format 'health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid"
      ;;
    tailscale)
      if command -v tailscale >/dev/null 2>&1; then
        tailscale status --json 2>/dev/null | jq -r '.Self.Online, .Self.DNSName, (.Health[]? // empty)' || true
      fi
      ;;
    vpn)
      show_vpn_state true
      ;;
    *)
      local cid
      cid="$(container_id "$service")"
      [ -n "$cid" ] && docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restartCount={{.RestartCount}}' "$cid" || true
      ;;
  esac
}

show_logs() {
  local service="$1"
  local lines
  require_managed_service "$service"
  lines="$(lines_arg "$${2:-120}")"
  case "$service" in
    tailscale)
      journalctl -u tailscaled --no-pager -n "$lines" || true
      ;;
    vpn)
      journalctl -u agent-stack-vpn --no-pager -n "$lines" || true
      journalctl -u nordvpnd --no-pager -n "$lines" 2>/dev/null || true
      ;;
    agent-stack)
      journalctl -u agent-stack --no-pager -n "$lines" || true
      ;;
    *)
      compose_service "$service" || { echo "unsupported compose service: $service" >&2; exit 64; }
      docker compose -f "$compose" logs --tail "$lines" "$service" || true
      ;;
  esac
}

show_inspect() {
  local service="$1"
  require_managed_service "$service"
  case "$service" in
    tailscale)
      systemctl status --no-pager tailscaled || true
      if command -v tailscale >/dev/null 2>&1; then
        tailscale status --json 2>/dev/null || true
      fi
      ;;
    vpn)
      systemctl status --no-pager agent-stack-vpn || true
      systemctl show agent-stack-vpn -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p DropInPaths -p ConditionResult -p AssertResult || true
      systemctl cat agent-stack-vpn || true
      systemctl status --no-pager nordvpnd 2>/dev/null || true
      systemctl status --no-pager agent-stack-vpn-rollback.timer 2>/dev/null || true
      if command -v nordvpn >/dev/null 2>&1; then
        nordvpn version 2>/dev/null || nordvpn --version 2>/dev/null || true
        nordvpn status 2>/dev/null || true
        nordvpn settings 2>/dev/null || true
      fi
      ip -4 route show || true
      ip -o addr show || true
      ;;
    agent-stack)
      systemctl status --no-pager agent-stack || true
      ;;
    *)
      compose_service "$service" || { echo "unsupported compose service: $service" >&2; exit 64; }
      show_container_inspect "$service"
      ;;
  esac
}

restart_service() {
  local service="$1"
  require_managed_service "$service"
  case "$service" in
    tailscale)
      systemctl restart tailscaled
      ;;
    vpn)
      systemctl restart agent-stack-vpn
      ;;
    agent-stack)
      systemctl restart agent-stack
      ;;
    *)
      compose_service "$service" || { echo "unsupported compose service: $service" >&2; exit 64; }
      docker compose -f "$compose" restart "$service"
      ;;
  esac
}

cmd="$${1:-status}"
case "$cmd" in
  status)
    show_status
    ;;
  health)
    show_health "$${2:-openclaw}"
    ;;
  logs)
    [ $# -ge 2 ] || { usage >&2; exit 64; }
    show_logs "$2" "$${3:-120}"
    ;;
  inspect)
    [ $# -eq 2 ] || { usage >&2; exit 64; }
    show_inspect "$2"
    ;;
  restart)
    [ $# -eq 2 ] || { usage >&2; exit 64; }
    restart_service "$2"
    ;;
  codex-update)
    case $# in
      1)
        queue_workspace_codex_update
        ;;
      2)
        [ "$2" = "status" ] || { usage >&2; exit 64; }
        show_workspace_codex_update_status
        ;;
      *)
        usage >&2
        exit 64
        ;;
    esac
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac
