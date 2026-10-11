[Unit]
Description=AgentStack
Documentation=https://github.com/openclaw/openclaw
Requires=docker.service
%{ if vpn_enabled }
Requires=agent-stack-vpn.service
%{ endif }
After=docker.service network-online.target
%{ if vpn_enabled }
After=agent-stack-vpn.service
%{ endif }

[Service]
Type=simple
WorkingDirectory=/opt/agent-stack
ExecStartPre=/usr/local/bin/agent-stack-migrate-layout
%{ if workspace_enabled && workspace_codex_sandbox_enabled }
ExecStartPre=/usr/bin/python3 -B /opt/agent-stack/workspace-sandbox.py
%{ endif }
ExecStartPre=/bin/sh -lc '/usr/bin/docker compose pull --quiet --ignore-buildable || true'
ExecStart=/usr/bin/docker compose up --remove-orphans
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=15
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
