#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$${AGENT_STACK_APP_ROOT:-/opt/agent-stack}"
DATA_ROOT="$${AGENT_STACK_DATA_ROOT:-$APP_ROOT/data}"
LEGACY_ROOT="$${AGENT_STACK_LEGACY_ROOT:-/opt/openclaw}"
LEGACY_SERVICES="$DATA_ROOT/services"
MARKER="$DATA_ROOT/.agent-stack-layout-version"

log() {
  echo "[layout] $*"
}

ensure_dirs() {
  install -d -m 0755 "$APP_ROOT" "$DATA_ROOT"
  install -d -m 0755 \
    "$DATA_ROOT/openclaw" \
    "$DATA_ROOT/hermes" \
    "$DATA_ROOT/n8n" \
    "$DATA_ROOT/postgres" \
    "$DATA_ROOT/caddy/data" \
    "$DATA_ROOT/caddy/config" \
    "$APP_ROOT/tailscale-state"
}

move_children() {
  local src="$1"
  local dest="$2"
  [ -d "$src" ] || return 0
  install -d -m 0755 "$dest"
  shopt -s dotglob nullglob
  local child
  for child in "$src"/*; do
    local base
    base="$(basename "$child")"
    if [ -e "$dest/$base" ]; then
      log "Keeping existing $dest/$base; leaving legacy $child in place."
      continue
    fi
    mv "$child" "$dest/"
  done
  shopt -u dotglob nullglob
  rmdir "$src" 2>/dev/null || true
}

migrate_legacy_volume_layout() {
  [ -d "$DATA_ROOT" ] || return 0
  if [ -f "$MARKER" ]; then
    log "AgentStack data layout already present."
    return 0
  fi

  if [ ! -f "$DATA_ROOT/openclaw.json" ] && [ ! -d "$DATA_ROOT/workspace" ] && [ ! -d "$DATA_ROOT/services" ]; then
    log "Fresh AgentStack data volume detected."
    return 0
  fi

  log "Migrating legacy /opt/openclaw/data payload into peer service layout."
  install -d -m 0755 "$DATA_ROOT/openclaw"

  move_children "$LEGACY_SERVICES/hermes" "$DATA_ROOT/hermes"
  move_children "$LEGACY_SERVICES/n8n" "$DATA_ROOT/n8n"
  move_children "$LEGACY_SERVICES/postgres" "$DATA_ROOT/postgres"
  move_children "$LEGACY_SERVICES/caddy" "$DATA_ROOT/caddy"
  rmdir "$LEGACY_SERVICES" 2>/dev/null || true

  shopt -s dotglob nullglob
  local child
  for child in "$DATA_ROOT"/*; do
    local base
    base="$(basename "$child")"
    case "$base" in
      openclaw|hermes|n8n|postgres|caddy|lost+found)
        continue
        ;;
    esac
    if [ -e "$DATA_ROOT/openclaw/$base" ]; then
      log "Keeping existing $DATA_ROOT/openclaw/$base; leaving $child in place."
      continue
    fi
    mv "$child" "$DATA_ROOT/openclaw/"
  done
  shopt -u dotglob nullglob
}

ensure_legacy_symlinks() {
  if [ ! -e "$DATA_ROOT/openclaw.json" ]; then
    ln -s openclaw/openclaw.json "$DATA_ROOT/openclaw.json" 2>/dev/null || true
  fi
  if [ ! -e "$DATA_ROOT/workspace" ]; then
    # The workspace container is installed before OpenClaw seeds its files.
    # A fresh volume therefore needs the compatibility link's target now.
    # Create it after legacy migration so old workspace files still move.
    install -d -m 0755 "$DATA_ROOT/openclaw/workspace"
    ln -s openclaw/workspace "$DATA_ROOT/workspace" 2>/dev/null || true
  fi

  if [ -L "$LEGACY_ROOT" ]; then
    return 0
  fi
  if [ ! -e "$LEGACY_ROOT" ]; then
    ln -s "$APP_ROOT" "$LEGACY_ROOT"
    return 0
  fi
  if [ -d "$LEGACY_ROOT" ] && [ -z "$(find "$LEGACY_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    rmdir "$LEGACY_ROOT"
    ln -s "$APP_ROOT" "$LEGACY_ROOT"
    return 0
  fi
  log "Leaving existing $LEGACY_ROOT in place; it is not safe to replace with a symlink."
}

fix_service_ownership() {
  chown -R 1000:1000 "$DATA_ROOT/openclaw" || true
  chown -R 10000:10000 "$DATA_ROOT/hermes" || true
  chown -R 1000:1000 "$DATA_ROOT/n8n" || true
}

ensure_dirs
migrate_legacy_volume_layout
ensure_dirs
ensure_legacy_symlinks
fix_service_ownership
printf '1\n' > "$MARKER"
log "Layout ready at $DATA_ROOT."
