#!/usr/bin/env bash
set -euo pipefail

username="$${WORKSPACE_USERNAME:-user}"
password="$${WORKSPACE_PASSWORD:-}"
password_hash="$${WORKSPACE_PASSWORD_HASH:-}"

fail() {
  echo "[workspace] ERROR: $*" >&2
  exit 1
}

if [ -z "$password" ] && [ -z "$password_hash" ]; then
  fail "WORKSPACE_PASSWORD is required unless WORKSPACE_PASSWORD_HASH is set"
fi
if [ -n "$password" ] && [ -n "$password_hash" ]; then
  fail "supply only WORKSPACE_PASSWORD or WORKSPACE_PASSWORD_HASH"
fi
if [ -n "$password_hash" ] && ! printf '%s' "$password_hash" | grep -Eq '^\$6\$(rounds=[1-9][0-9]{3,8}\$)?[./A-Za-z0-9]{8,16}\$[./A-Za-z0-9]{86}$'; then
  fail "invalid WORKSPACE_PASSWORD_HASH"
fi

if ! printf '%s' "$username" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$'; then
  fail "invalid WORKSPACE_USERNAME: $username"
fi

if [ "$username" = "root" ]; then
  fail "WORKSPACE_USERNAME cannot be root"
fi

if ! id "$username" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$username"
fi

home_dir="$(getent passwd "$username" | cut -d: -f6)"
user_uid="$(id -u "$username")"
user_gid="$(id -g "$username")"
chown "$username:$username" "$home_dir"
chmod 0700 "$home_dir"
install -d -m 0700 -o "$username" -g "$username" "$home_dir/.ssh"
install -d -m 0700 -o "$username" -g "$username" "$home_dir/.codex"

if [ -n "$password_hash" ]; then
  printf '%s:%s\n' "$username" "$password_hash" | chpasswd --encrypted
else
  printf '%s:%s\n' "$username" "$password" | chpasswd
fi
unset password password_hash WORKSPACE_PASSWORD WORKSPACE_PASSWORD_HASH

authorized_keys_b64="$${WORKSPACE_AUTHORIZED_KEYS_BASE64:-}"
authorized_keys_file="$home_dir/.ssh/authorized_keys"
pubkey_auth="no"
if [ -n "$authorized_keys_b64" ]; then
  if ! printf '%s' "$authorized_keys_b64" | base64 -d >"$authorized_keys_file"; then
    fail "invalid WORKSPACE_AUTHORIZED_KEYS_BASE64"
  fi
  if [ -s "$authorized_keys_file" ]; then
    pubkey_auth="yes"
  fi
else
  : >"$authorized_keys_file"
fi
chown "$username:$username" "$authorized_keys_file"
chmod 0600 "$authorized_keys_file"

host_key_dir="$${WORKSPACE_HOST_KEY_DIR:-/var/lib/agent-stack-workspace/ssh-host-keys}"
install -d -m 0700 -o root -g root "$host_key_dir"

ensure_host_key() {
  local type="$1"
  local bits="$${2:-}"
  local key="$host_key_dir/ssh_host_$${type}_key"

  if [ ! -f "$key" ]; then
    if [ -n "$bits" ]; then
      ssh-keygen -q -t "$type" -b "$bits" -N "" -f "$key"
    else
      ssh-keygen -q -t "$type" -N "" -f "$key"
    fi
  fi
  if [ ! -f "$key.pub" ]; then
    ssh-keygen -y -f "$key" >"$key.pub"
  fi

  chown root:root "$key" "$key.pub"
  chmod 0600 "$key"
  chmod 0644 "$key.pub"
}

ensure_host_key ed25519
ensure_host_key ecdsa
ensure_host_key rsa 4096

cat >/etc/ssh/sshd_config.d/98-agent-stack-workspace-host-keys.conf <<EOF
HostKey $host_key_dir/ssh_host_ed25519_key
HostKey $host_key_dir/ssh_host_ecdsa_key
HostKey $host_key_dir/ssh_host_rsa_key
EOF
chmod 0644 /etc/ssh/sshd_config.d/98-agent-stack-workspace-host-keys.conf

cat >/etc/profile.d/agent-stack-workspace.sh <<EOF
export CODEX_HOME="$home_dir/.codex"
export PATH="$home_dir/.local/bin:/usr/local/bin:\$PATH"
EOF
chmod 0644 /etc/profile.d/agent-stack-workspace.sh

cat >/etc/ssh/sshd_config.d/99-agent-stack-workspace.conf <<EOF
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication $pubkey_auth
AuthorizedKeysFile .ssh/authorized_keys
PermitRootLogin no
AllowTcpForwarding no
X11Forwarding no
PermitTunnel no
EOF
printf 'SetEnv CODEX_HOME=%s\n' "$home_dir/.codex" >> /etc/ssh/sshd_config.d/99-agent-stack-workspace.conf
# SSH commands do not source /etc/profile.d.  Keep their Codex resolution
# identical to an interactive workspace shell so `ssh workspace codex` never
# accidentally selects the image fallback in /usr/local/bin.
printf 'SetEnv PATH=%s\n' "$home_dir/.local/bin:/usr/local/bin:/usr/bin:/bin" >> /etc/ssh/sshd_config.d/99-agent-stack-workspace.conf

cat >/usr/local/bin/agent-stack-diagnostics <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

key="$${AGENT_STACK_DIAGNOSTICS_KEY:-$HOME/.ssh/agent_stack_diagnostics}"
if [ ! -f "$key" ]; then
  echo "diagnostic key not found: $key" >&2
  exit 1
fi

exec ssh \
  -i "$key" \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$HOME/.ssh/known_hosts" \
  -o ConnectTimeout=10 \
  agent-stack-diagnostics@host.docker.internal \
  "$@"
EOF
chmod 0755 /usr/local/bin/agent-stack-diagnostics

install_para_memory_drive_skill() {
  local source=/usr/local/share/agent-stack/skills/para-memory-drive/SKILL.md
  local destination="$home_dir/.agents/skills/para-memory-drive/SKILL.md"

  [ -f "$source" ] || fail "missing bundled para-memory-drive skill: $source"
  install -d -m 0700 -o "$username" -g "$username" "$(dirname "$destination")"
  install -m 0644 -o "$username" -g "$username" "$source" "$destination"
}

upsert_workspace_agents_guidance() {
  local agents_file="$home_dir/AGENTS.md"
  local temporary
  local begin='<!-- agent-stack: para-memory-drive begin -->'
  local end='<!-- agent-stack: para-memory-drive end -->'

  temporary="$(mktemp "$home_dir/.AGENTS.md.XXXXXX")"
  if [ -f "$agents_file" ]; then
    awk -v begin="$begin" -v end="$end" '
      $0 == begin { managed = 1; next }
      $0 == end { managed = 0; next }
      !managed { print }
    ' "$agents_file" >"$temporary"
  fi
  cat >>"$temporary" <<EOF
$begin

## Drive-backed knowledge workspace

For PARA knowledge search or editing, read and follow
\`~/.agents/skills/para-memory-drive/SKILL.md\`. Google Drive is the source of
truth. This workspace does not mount Drive locally; use the connected Drive
capability rather than assuming \`~/workspace\` contains Drive files.

$end
EOF
  install -m 0644 -o "$username" -g "$username" "$temporary" "$agents_file"
  rm -f "$temporary"
}

install_para_memory_drive_skill
upsert_workspace_agents_guidance

# sshd does not forward the container environment into user sessions. The Muse
# wrapper loads this runtime-only credential; it never exists in an image layer.
%{ if workspace_muse_enabled }
MUSE_WORKSPACE_GROUP="$username" python3 - <<'MUSE_ENV'
import grp
import os
from pathlib import Path
import shlex
path = Path("/etc/agent-stack-muse.env")
path.unlink(missing_ok=True)
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
with os.fdopen(fd, "w") as stream:
    stream.write("export META_API_KEY=" + shlex.quote(os.environ.get("META_API_KEY", "")) + "\n")
os.chown(path, 0, grp.getgrnam(os.environ["MUSE_WORKSPACE_GROUP"]).gr_gid)
os.chmod(path, 0o640)
MUSE_ENV
%{ else }
rm -f /etc/agent-stack-muse.env
%{ endif }


%{ if workspace_opencode_enabled }
# Seed as the unprivileged user. Never replace user JSON/JSONC, auth or sessions.
# Failure must leave SSH available for repair and the other harnesses usable.
if ! runuser -u "$username" -- python3 - "$home_dir" '${workspace_opencode_model}' <<'OPENCODE_CONFIG'
import json
import os
from pathlib import Path
import sys
config = {"$schema": "https://opencode.ai/config.json", "model": sys.argv[2],
          "enabled_providers": [sys.argv[2].split("/", 1)[0]],
          "autoupdate": False, "permission": {"*": "ask"}}
directory = Path(sys.argv[1]) / ".config/opencode"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
if not any((directory / name).exists() for name in ("opencode.json", "opencode.jsonc", "config.json")):
    try:
        fd = os.open(directory / "opencode.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
OPENCODE_CONFIG
then
  echo "[workspace] WARNING: could not seed OpenCode config; configure ~/.config/opencode manually." >&2
fi
%{ endif }

if [ "$${WORKSPACE_CODEX_AUTO_UPDATE_ENABLED:-false}" = "true" ]; then
  if ! /usr/local/libexec/agent-stack-workspace-codex-update --initialize "$username"; then
    echo "[workspace] WARNING: could not initialize the user-scoped Codex fallback; starting SSH without blocking access." >&2
  fi
fi

exec /usr/sbin/sshd -D -e
