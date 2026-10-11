#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SSH_WRAPPER="${ROOT_DIR}/bin/agent-stack-ssh"
LOG_LINES=120

# Keep the human collector compatible; strict mode has enforceable pass/fail.
for argument in "$@"; do
  if [[ "$argument" == "--strict" ]]; then
    exec python3 "${ROOT_DIR}/skills/agent-stack-doctor/scripts/strict_health.py" "$@"
  fi
done

usage() {
  cat <<'EOF'
Usage: skills/agent-stack-doctor/scripts/check_remote_health.sh [--ssh-wrapper <path>] [--log-lines <count>]
       skills/agent-stack-doctor/scripts/check_remote_health.sh --strict --json --services openclaw,hermes,workspace --require-claude --check-tailscale --tailscale-mode host
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-wrapper)
      SSH_WRAPPER="${2:-}"
      shift 2
      ;;
    --log-lines)
      LOG_LINES="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -x "$SSH_WRAPPER" ]]; then
  echo "error: SSH wrapper not found or not executable: $SSH_WRAPPER" >&2
  exit 1
fi

section() {
  printf '\n== %s ==\n' "$1"
}

run_remote() {
  local quoted_command
  printf -v quoted_command "%q" "$1"
  "$SSH_WRAPPER" -- "sh -lc $quoted_command"
}

section "Runtime containers"
run_remote "sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true"

APP_CONTAINER="$(run_remote "sudo docker ps --format '{{.Names}}' | grep -E '(^|-)openclaw($|-)' | grep -v tailscale | head -n 1" 2>/dev/null || true)"
HERMES_CONTAINER="$(run_remote "sudo docker ps --format '{{.Names}}' | grep -E '(^|-)hermes($|-)' | head -n 1" 2>/dev/null || true)"
N8N_CONTAINER="$(run_remote "sudo docker ps --format '{{.Names}}' | grep -E '(^|-)n8n($|-)' | head -n 1" 2>/dev/null || true)"
POSTGRES_CONTAINER="$(run_remote "sudo docker ps --format '{{.Names}}' | grep -E '(^|-)postgres($|-)' | head -n 1" 2>/dev/null || true)"
CADDY_CONTAINER="$(run_remote "sudo docker ps --format '{{.Names}}' | grep -E '(^|-)caddy($|-)' | head -n 1" 2>/dev/null || true)"
TS_CONTAINER="$(run_remote "sudo docker ps --format '{{.Names}}' | grep -E 'tailscale' | head -n 1" 2>/dev/null || true)"

section "systemd"
run_remote "sudo systemctl status --no-pager agent-stack || sudo systemctl status --no-pager openclaw || true"

section "VPN health"
run_remote "sudo agent-stack-diagnostics health vpn || true"

section "VPN inspect"
run_remote "sudo agent-stack-diagnostics inspect vpn || true"

section "VPN logs"
run_remote "sudo agent-stack-diagnostics logs vpn ${LOG_LINES} || true"

section "Workspace Codex updater"
run_remote "sudo agent-stack-diagnostics codex-update status || true"

section "Layout"
run_remote "if [ -d /opt/agent-stack ]; then echo agent_stack_root=present; else echo agent_stack_root=missing; fi; if [ -L /opt/openclaw ]; then echo legacy_root=symlink; elif [ -d /opt/openclaw ]; then echo legacy_root=directory; else echo legacy_root=missing; fi; if [ -f /opt/agent-stack/data/.agent-stack-layout-version ]; then echo layout_marker=present; else echo layout_marker=missing; fi"

section "Bootstrap log"
run_remote "sudo tail -n ${LOG_LINES} /var/log/openclaw-bootstrap.log || true"

section "Gateway health"
run_remote "curl -sv --max-time 5 http://127.0.0.1:18789/healthz 2>&1 | tail -n 40 || true"

if [[ -n "$APP_CONTAINER" ]]; then
  section "App inspect"
  run_remote "sudo docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restartCount={{.RestartCount}} started={{.State.StartedAt}}' ${APP_CONTAINER} || true"

  section "App health"
  run_remote "sudo docker exec ${APP_CONTAINER} openclaw health --json || true"

  section "App logs"
  run_remote "sudo docker logs --since 20m ${APP_CONTAINER} | tail -n 200 || true"
else
  section "App container"
  echo "No OpenClaw app container detected."
fi

if [[ -n "$HERMES_CONTAINER" ]]; then
  section "Hermes inspect"
  run_remote "sudo docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restartCount={{.RestartCount}} started={{.State.StartedAt}}' ${HERMES_CONTAINER} || true"

  section "Hermes dashboard"
  run_remote "curl -sv --max-time 5 http://127.0.0.1:9119 2>&1 | tail -n 40 || true"

  section "Hermes logs"
  run_remote "sudo docker logs --since 20m ${HERMES_CONTAINER} | tail -n 200 || true"
else
  section "Hermes container"
  echo "No Hermes container detected."
fi

if [[ -n "$N8N_CONTAINER" ]]; then
  section "n8n inspect"
  run_remote "sudo docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restartCount={{.RestartCount}} started={{.State.StartedAt}}' ${N8N_CONTAINER} || true"

  section "n8n health"
  run_remote "curl -sv --max-time 5 http://127.0.0.1:5678/healthz 2>&1 | tail -n 40 || true"

  section "n8n logs"
  run_remote "sudo docker logs --since 20m ${N8N_CONTAINER} | tail -n 200 || true"
else
  section "n8n container"
  echo "No n8n container detected."
fi

if [[ -n "$POSTGRES_CONTAINER" ]]; then
  section "Postgres inspect"
  run_remote "sudo docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restartCount={{.RestartCount}} started={{.State.StartedAt}}' ${POSTGRES_CONTAINER} || true"

  section "Postgres readiness"
  run_remote "sudo docker exec ${POSTGRES_CONTAINER} pg_isready || true"
else
  section "Postgres container"
  echo "No local Postgres container detected."
fi

if [[ -n "$CADDY_CONTAINER" ]]; then
  section "Caddy inspect"
  run_remote "sudo docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restartCount={{.RestartCount}} started={{.State.StartedAt}}' ${CADDY_CONTAINER} || true"

  section "Caddy logs"
  run_remote "sudo docker logs --since 20m ${CADDY_CONTAINER} | tail -n 200 || true"
else
  section "Caddy container"
  echo "No Caddy container detected."
fi

if [[ -n "$TS_CONTAINER" ]]; then
  section "Tailscale status"
  run_remote "sudo docker exec ${TS_CONTAINER} tailscale --socket=/tmp/tailscaled.sock status --json | jq -r '.Self.Online, .Self.DNSName, (.Health[]? // empty)' || true"

  section "Tailscale serve"
  run_remote "sudo docker exec ${TS_CONTAINER} tailscale --socket=/tmp/tailscaled.sock serve status || true"

  section "Sidecar to app"
  run_remote "sudo docker exec ${TS_CONTAINER} sh -lc 'wget -S -O /dev/null -T 5 http://127.0.0.1:18789 2>&1 | sed -n \"1,40p\"' || true"
else
  section "Tailscale container"
  echo "No Tailscale sidecar container detected."
fi
