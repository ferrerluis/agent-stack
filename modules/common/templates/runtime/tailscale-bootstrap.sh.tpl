#!/bin/sh
set -eu

SOCK=/tmp/tailscaled.sock
STATE_FILE=/var/lib/tailscale/tailscaled.state

mkdir -p /var/lib/tailscale
tailscaled --state="$STATE_FILE" --socket="$SOCK" &

for _ in $(seq 1 60); do
  [ -S "$SOCK" ] && break
  sleep 1
done

if [ -z "$${TAILSCALE_HOSTNAME:-}" ]; then
  echo "TAILSCALE_HOSTNAME is required" >&2
  exit 1
fi

tailscale_up() {
  if [ -n "$${TAILSCALE_AUTH_KEY:-}" ]; then
    tailscale --socket="$SOCK" up \
      --authkey="$TAILSCALE_AUTH_KEY" \
      --hostname="$TAILSCALE_HOSTNAME" \
      --accept-routes
  else
    if [ ! -s "$STATE_FILE" ]; then
      echo "TAILSCALE_AUTH_KEY is required for first bootstrap" >&2
      return 1
    fi
    tailscale --socket="$SOCK" up \
      --hostname="$TAILSCALE_HOSTNAME" \
      --accept-routes
  fi
}

wait_tailscale_online() {
  for _ in $(seq 1 20); do
    if tailscale --socket="$SOCK" status --json 2>/dev/null | jq -e '.Self.Online == true' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

online=0
for attempt in $(seq 1 5); do
  if tailscale_up && wait_tailscale_online; then
    online=1
    break
  fi
  echo "tailscale up attempt $attempt/5 did not reach online state; retrying..."
  sleep 3
done

if [ "$online" != "1" ]; then
  echo "WARNING: Tailscale did not reach online state during bootstrap." >&2
fi

tailscale --socket="$SOCK" serve reset || true
if [ "$${OPENCLAW_ENABLED:-false}" = "true" ]; then
  tailscale --socket="$SOCK" serve --bg --yes --https=443 127.0.0.1:18789
  tailscale --socket="$SOCK" serve status || true
else
  echo "OpenClaw is disabled; Tailscale Serve route was not configured."
fi

%{ if hermes_enabled && hermes_dashboard_enabled }
tailscale --socket="$SOCK" serve --bg --yes --https=8443 127.0.0.1:9119
tailscale --socket="$SOCK" serve status
%{ endif }

wait
