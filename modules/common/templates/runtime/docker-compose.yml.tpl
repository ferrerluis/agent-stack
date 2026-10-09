services:
%{ if openclaw_enabled }
  openclaw:
    image: ${openclaw_image}
    restart: unless-stopped
    env_file: .env
%{ if openclaw_host_network_enabled }
    network_mode: "host"
%{ else }
    ports:
      - "127.0.0.1:18789:18789"
      - "127.0.0.1:18793:18793"
%{ endif }
    volumes:
      - /opt/agent-stack/data/openclaw:/home/node/.openclaw
%{ if openclaw_version == "2026.6.8" || openclaw_version == "2026.6.9-beta.1" }
      - /opt/agent-stack/patches/openclaw/telegram-ingress-worker.runtime.js:/app/extensions/telegram/src/telegram-ingress-worker.runtime.js:ro
%{ endif }
%{ if openai_codex_auth_json_base64 != "" }
      - /opt/agent-stack/codex:/home/node/.codex
%{ endif }
    healthcheck:
      test: ["CMD-SHELL", "node -e \"fetch('http://127.0.0.1:18789/healthz').then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\""]
      interval: 30s
      timeout: 10s
      retries: ${openclaw_health_retries}
      start_period: ${openclaw_health_start_period_seconds}s
%{ endif }
%{ if hermes_enabled }

  hermes:
    image: ${hermes_image}
%{ if hermes_npm_security_patch_enabled }
    entrypoint: ["/opt/hermes/.venv/bin/python", "-B", "/usr/local/libexec/agent-stack-hermes-npm-patch.py"]
%{ endif }
    restart: unless-stopped
    command: gateway run
    env_file: .env
    ports:
      - "127.0.0.1:8642:8642"
      - "127.0.0.1:9119:9119"
    volumes:
      - /opt/agent-stack/data/hermes:/opt/data
      - /opt/agent-stack/hermes_health.py:/usr/local/libexec/agent-stack-hermes-health.py:ro
%{ if hermes_npm_security_patch_enabled }
      - /opt/agent-stack/hermes_npm_patch.py:/usr/local/libexec/agent-stack-hermes-npm-patch.py:ro
      - /opt/agent-stack/hermes_npm_patch.json:/usr/local/libexec/agent-stack-hermes-npm-patch.json:ro
%{ endif }
%{ if hermes_api_server_enabled }
    healthcheck:
      test: ["CMD", "/opt/hermes/.venv/bin/python", "/usr/local/libexec/agent-stack-hermes-health.py"]
      interval: 30s
      timeout: 10s
      retries: 8
      start_period: ${hermes_npm_security_patch_enabled ? 480 : 120}s
%{ endif }
    environment:
      HERMES_DASHBOARD: "${hermes_dashboard_enabled ? "1" : "0"}"
      HERMES_DASHBOARD_HOST: "0.0.0.0"
      HERMES_DASHBOARD_PORT: "9119"
      API_SERVER_ENABLED: "${hermes_api_server_enabled ? "true" : "false"}"
      API_SERVER_HOST: "0.0.0.0"
      API_SERVER_KEY: "${hermes_api_server_key}"
      API_SERVER_CORS_ORIGINS: ""
%{ endif }
%{ if local_postgres_enabled }

  postgres:
    image: ${postgres_image}
    restart: unless-stopped
    env_file: .env
    environment:
      POSTGRES_DB: "${postgres_database}"
      POSTGRES_USER: "${postgres_user}"
      POSTGRES_PASSWORD: "${postgres_password}"
      PGDATA: /var/lib/postgresql/data/pgdata
    volumes:
      - /opt/agent-stack/data/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${postgres_user} -d ${postgres_database}"]
      interval: 30s
      timeout: 10s
      retries: 8
%{ endif }
%{ if n8n_enabled }

  n8n:
    image: ${n8n_image}
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:5678:5678"
    volumes:
      - /opt/agent-stack/data/n8n:/home/node/.n8n
%{ if local_postgres_enabled }
    depends_on:
      postgres:
        condition: service_healthy
%{ endif }
%{ endif }
%{ if workspace_enabled }

  workspace:
    image: agent-stack-workspace:local
%{ if workspace_codex_sandbox_enabled }
    security_opt:
      - "no-new-privileges:true"
      - "seccomp=/opt/agent-stack/workspace-seccomp.json"
      - "apparmor=agent-stack-workspace-codex"
%{ endif }
    restart: unless-stopped
    stop_grace_period: 2m
    env_file: workspace.env
    ports:
      - "${workspace_ssh_host_port}:22"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - /opt/agent-stack/data/workspace/home:/home/${workspace_username}
      - /opt/agent-stack/data/workspace/ssh-host-keys:/var/lib/agent-stack-workspace/ssh-host-keys
    healthcheck:
      test: ["CMD-SHELL", "pgrep -x sshd >/dev/null"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
%{ endif }
%{ if caddy_enabled }

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    env_file: .env
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /opt/agent-stack/Caddyfile:/etc/caddy/Caddyfile:ro
      - /opt/agent-stack/data/caddy/data:/data
      - /opt/agent-stack/data/caddy/config:/config
%{ endif }
%{ if tailscale_sidecar_enabled }

  tailscale:
    image: tailscale/tailscale:stable
    restart: unless-stopped
    network_mode: "host"
    env_file: .env
    cap_add:
      - NET_ADMIN
      - NET_RAW
    devices:
      - /dev/net/tun:/dev/net/tun
    volumes:
      - /opt/agent-stack/tailscale-state:/var/lib/tailscale
      - /opt/agent-stack/tailscale-bootstrap.sh:/bootstrap.sh:ro
    command: ["/bin/sh", "/bootstrap.sh"]
%{ endif }
