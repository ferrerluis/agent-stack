#!/usr/bin/env bash
set -euo pipefail

if [ "${tailscale_host_enabled}" != "true" ]; then
  echo "[tailscale-host] Skipped (tailscale_mode=${tailscale_mode})."
  exit 0
fi

if [ -z "$${TAILSCALE_HOSTNAME:-}" ]; then
  echo "TAILSCALE_HOSTNAME is required" >&2
  exit 1
fi

if ! command -v tailscale >/dev/null 2>&1 || ! command -v tailscaled >/dev/null 2>&1; then
  echo "[tailscale-host] Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
fi

systemctl enable --now tailscaled

tailscale_up() {
  if tailscale status --json 2>/dev/null | jq -e '.Self.Online == true' >/dev/null 2>&1; then
    echo "[tailscale-host] Already online; keeping current login."
    tailscale set --hostname="$TAILSCALE_HOSTNAME" --accept-routes || true
    return 0
  fi

  if [ -n "$${TAILSCALE_AUTH_KEY:-}" ]; then
    tailscale up \
      --authkey="$TAILSCALE_AUTH_KEY" \
      --hostname="$TAILSCALE_HOSTNAME" \
      --accept-routes
  else
    tailscale up \
      --hostname="$TAILSCALE_HOSTNAME" \
      --accept-routes
  fi
}

wait_tailscale_online() {
  for _ in $(seq 1 30); do
    if tailscale status --json 2>/dev/null | jq -e '.Self.Online == true' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if ! tailscale_up; then
  if tailscale status --json 2>/dev/null | jq -e '.Self.Online == true' >/dev/null 2>&1; then
    echo "[tailscale-host] Already online; keeping current login."
  else
    echo "[tailscale-host] ERROR: tailscale up failed and host is not online." >&2
    exit 1
  fi
fi

if ! wait_tailscale_online; then
  echo "[tailscale-host] ERROR: Tailscale did not reach online state." >&2
  exit 1
fi

tailscale serve reset || true
if [ "$${OPENCLAW_ENABLED:-false}" = "true" ]; then
  tailscale serve --bg --yes --https=443 127.0.0.1:18789
  tailscale serve status || true
else
  echo "[tailscale-host] OpenClaw is disabled; no Tailscale Serve route configured."
fi

%{ if hermes_enabled && hermes_dashboard_enabled }
tailscale serve --bg --yes --https=8443 127.0.0.1:9119
tailscale serve status
%{ endif }
