#!/usr/bin/env python3

from __future__ import annotations

import os
import pwd
import shutil
import shlex
import textwrap
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = REPO_ROOT / "modules/common/templates"
LOADER_TEMPLATE = TEMPLATE_DIR / "cloud_init.yaml.tpl"
RUNTIME_DIR = TEMPLATE_DIR / "runtime"
MAIN_TF = REPO_ROOT / "main.tf"


class CloudInitLoaderTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loader = LOADER_TEMPLATE.read_text(encoding="utf-8")

    def test_loader_stays_small_and_defers_runtime_to_terraform(self) -> None:
        self.assertIn("AgentStack first-boot loader", self.loader)
        self.assertIn("/root/agent-stack-loader.sh", self.loader)
        self.assertIn("/opt/agent-stack/.loader-ready.json", self.loader)
        self.assertIn("Terraform over SSH after cloud-init reports ready", self.loader)
        self.assertNotIn("/opt/agent-stack/docker-compose.yml", self.loader)
        self.assertNotIn("curl -fsSL https://get.docker.com", self.loader)

    def test_loader_prepares_admin_user_and_private_volume_mount(self) -> None:
        self.assertIn('admin="${admin_username}"', self.loader)
        self.assertIn("/etc/sudoers.d/90-agent-stack-admin", self.loader)
        self.assertIn("/opt/agent-stack/data", self.loader)
        self.assertIn("mount -o defaults,nofail", self.loader)

    def test_loader_supports_all_provider_volume_paths(self) -> None:
        self.assertIn('provider_type == "aws"', self.loader)
        self.assertIn('provider_type == "digitalocean"', self.loader)
        self.assertIn('provider_type == "hetzner"', self.loader)
        self.assertIn("/dev/disk/by-id/scsi-0DO_Volume_$volume_name", self.loader)
        self.assertIn("/dev/disk/by-id/scsi-0HC_Volume_$volume_id", self.loader)

    def test_openclaw_only_compact_template_is_removed(self) -> None:
        compact_template = TEMPLATE_DIR / "cloud_init_hetzner_openclaw.yaml.tpl"
        self.assertFalse(compact_template.exists())

    def test_trial_boot_can_finish_while_systemd_boot_targets_are_blocked(self) -> None:
        # Model the network-stage ordering: a blocking timer start cannot
        # finish until cloud-init itself returns. Exercise the loader shell.
        script = textwrap.dedent(self.loader.split("bootcmd:\n  - |\n", 1)[1].split("%{ endif }", 1)[0])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); commands = root / "bin"; commands.mkdir()
            script = script.replace("/etc/systemd/system", str(root / "systemd"))
            script = script.replace("/var/lib/agent-stack-trial", str(root / "trial"))
            script = script.replace("${showcase_trial_limits.egress_cap_bytes}", "1073741824")
            script = script.replace("${showcase_trial_limits.deadline_utc}", "2099-01-01 00:00:00 UTC")
            (root / "systemd").mkdir()
            for name in ("iptables", "ip6tables"):
                (commands / name).write_text("#!/bin/sh\nexit 0\n")
                (commands / name).chmod(0o700)
            (commands / "systemctl").write_text('''#!/bin/sh
if [ "$1" = enable ]; then
  for arg in "$@"; do [ "$arg" != --no-block ] || exit 0; done
  # The real systemd job waits on the boot target cloud-init blocks.
  exec sleep 10
fi
exit 0
''')
            (commands / "systemctl").chmod(0o700)
            env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ.get("PATH", ""))
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            timer = (root / "systemd/agent-stack-trial-deadline.timer").read_text()
            self.assertIn("Persistent=true", timer)
            self.assertEqual((root / "trial/boots").read_text().strip(), "1")


class RuntimeTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = (RUNTIME_DIR / "docker-compose.yml.tpl").read_text(encoding="utf-8")
        cls.caddy = (RUNTIME_DIR / "Caddyfile.template.tpl").read_text(encoding="utf-8")
        cls.env = (RUNTIME_DIR / "env.tpl").read_text(encoding="utf-8")
        cls.installer = (RUNTIME_DIR / "install-agent-stack.sh.tpl").read_text(encoding="utf-8")
        cls.agent_stack_service = (RUNTIME_DIR / "agent-stack.service.tpl").read_text(encoding="utf-8")
        cls.openclaw_service = (RUNTIME_DIR / "openclaw.service.tpl").read_text(encoding="utf-8")
        cls.vpn_manager = (RUNTIME_DIR / "agent-stack-vpn.sh.tpl").read_text(encoding="utf-8")
        cls.vpn_openvpn = (RUNTIME_DIR / "agent-stack-vpn-openvpn.sh.tpl").read_text(encoding="utf-8")
        cls.workspace_dockerfile = (RUNTIME_DIR / "workspace.Dockerfile.tpl").read_text(encoding="utf-8")
        cls.workspace_entrypoint = (RUNTIME_DIR / "workspace-entrypoint.sh.tpl").read_text(encoding="utf-8")
        cls.workspace_env = (RUNTIME_DIR / "workspace.env.tpl").read_text(encoding="utf-8")
        cls.workspace_codex_update = (RUNTIME_DIR / "workspace-codex-update.sh.tpl").read_text(encoding="utf-8")
        cls.workspace_codex_control = (RUNTIME_DIR / "workspace-codex-control.py.tpl").read_text(encoding="utf-8")
        cls.workspace_update_host = (RUNTIME_DIR / "agent-stack-workspace-codex-update.sh.tpl").read_text(encoding="utf-8")
        cls.workspace_update_service = (
            RUNTIME_DIR / "agent-stack-workspace-codex-update.service.tpl"
        ).read_text(encoding="utf-8")
        cls.workspace_update_timer = (
            RUNTIME_DIR / "agent-stack-workspace-codex-update.timer.tpl"
        ).read_text(encoding="utf-8")
        cls.host_tailscale = (RUNTIME_DIR / "host-tailscale-bootstrap.sh.tpl").read_text(encoding="utf-8")
        cls.diagnostics = (RUNTIME_DIR / "agent-stack-diagnostics.sh.tpl").read_text(encoding="utf-8")
        cls.diagnostics_ssh = (RUNTIME_DIR / "agent-stack-diagnostics-ssh.sh.tpl").read_text(encoding="utf-8")

    def test_runtime_includes_stack_services_and_private_ports(self) -> None:
        self.assertIn("openclaw:", self.compose)
        self.assertIn("hermes:", self.compose)
        self.assertIn("n8n:", self.compose)
        self.assertIn("postgres:", self.compose)
        self.assertIn("caddy:", self.compose)
        self.assertIn('"127.0.0.1:18789:18789"', self.compose)
        self.assertIn('"127.0.0.1:9119:9119"', self.compose)
        self.assertIn('"127.0.0.1:5678:5678"', self.compose)

    def test_runtime_mounts_service_data_under_neutral_peer_paths(self) -> None:
        self.assertIn("/opt/agent-stack/data/openclaw:/home/node/.openclaw", self.compose)
        self.assertIn("/opt/agent-stack/data/hermes:/opt/data", self.compose)
        self.assertIn("/opt/agent-stack/data/n8n:/home/node/.n8n", self.compose)
        self.assertIn("/opt/agent-stack/data/postgres:/var/lib/postgresql/data", self.compose)
        self.assertIn("/opt/agent-stack/data/caddy/data:/data", self.compose)
        self.assertNotIn("/opt/agent-stack/data/services/hermes:/opt/data", self.compose)

    def test_runtime_includes_optional_workspace_without_docker_socket(self) -> None:
        self.assertIn("%{ if workspace_enabled }", self.compose)
        self.assertIn("workspace:", self.compose)
        self.assertIn("image: agent-stack-workspace:local", self.compose)
        self.assertIn("env_file: workspace.env", self.compose)
        self.assertIn('${workspace_ssh_host_port}:22', self.compose)
        self.assertIn("/opt/agent-stack/data/workspace/home:/home/${workspace_username}", self.compose)
        self.assertIn(
            "/opt/agent-stack/data/workspace/ssh-host-keys:/var/lib/agent-stack-workspace/ssh-host-keys",
            self.compose,
        )
        self.assertIn("${workspace_username}@${workspace_ssh_host}", self.installer)
        self.assertIn("host.docker.internal:host-gateway", self.compose)
        self.assertNotIn("/dev/fuse", self.compose)
        self.assertNotIn("SYS_ADMIN", self.compose)
        self.assertNotIn("apparmor:unconfined", self.compose)
        self.assertNotIn("/var/run/docker.sock", self.compose)

    def test_caddy_protects_ui_and_allows_webhooks(self) -> None:
        self.assertIn("basic_auth", self.caddy)
        self.assertIn("__UI_AUTH_HASH__", self.caddy)
        self.assertIn("@n8n_webhooks path /webhook* /webhook-test*", self.caddy)

    def test_systemd_uses_agent_stack_with_openclaw_compatibility(self) -> None:
        self.assertIn("ExecStart=/usr/bin/docker compose up --remove-orphans", self.agent_stack_service)
        self.assertIn("docker compose pull --quiet --ignore-buildable || true", self.agent_stack_service)
        self.assertIn("ExecStart=/bin/systemctl start agent-stack.service", self.openclaw_service)
        self.assertNotIn("ExecStop=/bin/systemctl stop agent-stack.service", self.openclaw_service)

    def test_runtime_installer_keeps_openclaw_bootstrap_behavior(self) -> None:
        self.assertIn("configure_swap", self.installer)
        self.assertIn("seed_starter_workspace_files", self.installer)
        self.assertIn("configure_openclaw_channels_and_models", self.installer)
        self.assertIn("OPENAI_AUTH_MODE='${openai_auth_mode}'", self.installer)
        self.assertIn("configure_openai_codex_runtime_routes", self.installer)
        self.assertIn('.agentRuntime.id = "codex"', self.installer)
        self.assertIn("install_host_codex_cli", self.installer)
        self.assertIn("Refreshing gateway token and gateway.controlUi.allowedOrigins", self.installer)
        self.assertIn(".last-apply.json", self.installer)
        self.assertIn("restoring previous runtime files", self.installer)
        self.assertIn("configure_admin_password_ssh", self.installer)
        self.assertIn("install_workspace_runtime", self.installer)
        self.assertIn("install_workspace_diagnostics_bridge", self.installer)
        self.assertIn("configure_host_tailscale", self.installer)
        self.assertIn("host-tailscale-bootstrap.sh", self.installer)
        self.assertIn("configure_host_vpn", self.installer)
        self.assertIn("agent-stack-vpn", self.installer)
        self.assertIn("agent-stack-vpn-openvpn", self.installer)

    def test_runtime_admin_password_is_opt_in_and_guarded(self) -> None:
        self.assertIn("ADMIN_PASSWORD=${admin_password}", self.env)
        self.assertIn("ADMIN_PASSWORD_SSH_SCOPE=${admin_password_ssh_scope}", self.env)
        self.assertIn("passwd -l \"${admin_username}\"", self.installer)
        self.assertIn("printf '%s:%s\\n' \"${admin_username}\" \"$password\" | chpasswd", self.installer)
        self.assertIn("PasswordAuthentication no", self.installer)
        self.assertIn("Match User ${admin_username} Address 100.64.0.0/10", self.installer)
        self.assertIn("Match User ${admin_username} Address fd7a:115c:a1e0::/48", self.installer)

    def test_host_codex_cli_install_is_opt_in_and_uses_admin_home_for_auth(self) -> None:
        self.assertIn('[ "${host_codex_cli_enabled}" != "true" ]', self.installer)
        self.assertIn("https://chatgpt.com/codex/install.sh", self.installer)
        self.assertIn("CODEX_INSTALL_DIR=/usr/local/bin", self.installer)
        self.assertIn('CODEX_HOME="$codex_install_home"', self.installer)
        self.assertIn('install -d -m 0700 -o "${admin_username}" -g "$admin_group" "$admin_home/.codex"', self.installer)
        self.assertIn("codex --version", self.installer)

    def test_runtime_stages_sensitive_files_with_private_permissions(self) -> None:
        main_tf = MAIN_TF.read_text(encoding="utf-8")
        self.assertIn("install -d -m 0700", main_tf)
        self.assertIn("chmod 0700 ${local.runtime_staging_dir}", main_tf)
        self.assertIn("chmod 0600 ${local.runtime_staging_dir}/.env", main_tf)
        self.assertIn("${local.runtime_staging_dir}/workspace.env", main_tf)
        self.assertIn("${local.runtime_staging_dir}/openai_codex_auth_json_base64", main_tf)
        self.assertIn("${local.runtime_staging_dir}/workspace-para-memory-drive-skill.md", main_tf)
        self.assertIn("${local.runtime_staging_dir}/vpn-auth.txt", main_tf)
        self.assertIn("${local.runtime_staging_dir}/vpn-token.txt", main_tf)

    def test_host_vpn_runtime_is_opt_in_and_route_guarded(self) -> None:
        self.assertIn('VPN_PROVIDER="${vpn_provider}"', self.vpn_manager)
        self.assertIn("nordvpn_openvpn) enable_openvpn", self.vpn_manager)
        self.assertIn("nordvpn_nordlynx) enable_nordlynx", self.vpn_manager)
        self.assertIn("nordvpn set technology NordLynx", self.vpn_manager)
        self.assertIn("nordvpn set autoconnect on", self.vpn_manager)
        self.assertIn("nordvpn set killswitch off", self.vpn_manager)
        self.assertIn("nordvpn set threatprotectionlite off", self.vpn_manager)
        self.assertIn("nordvpn set meshnet off", self.vpn_manager)
        self.assertIn("100.64.0.0/10", self.vpn_manager)
        self.assertIn("agent-stack-vpn-rollback.timer", self.vpn_manager)
        self.assertIn("OnCalendar=", self.vpn_manager)
        self.assertIn("systemctl stop agent-stack.service", self.vpn_manager)
        self.assertIn("'nordvpn_openvpn' > \"$config_dir/provider\"", self.vpn_manager)
        self.assertIn("'nordvpn_openvpn' > \"$config_dir/provider\"", self.vpn_openvpn)
        self.assertIn("rm -f \"$token_file\"", self.vpn_manager)
        self.assertNotIn("nordvpn logout", self.vpn_manager)
        self.assertNotIn('echo "$token"', self.vpn_manager)
        self.assertIn('VPN_ENABLED="${vpn_enabled}"', self.vpn_openvpn)
        self.assertIn('VPN_PROVIDER="${vpn_provider}"', self.vpn_openvpn)
        self.assertIn("vpn-auth.txt", self.vpn_openvpn)
        self.assertIn("auth-user-pass /etc/agent-stack-vpn/auth.txt", self.vpn_openvpn)
        self.assertIn("agent-stack-vpn.service", self.vpn_openvpn)
        self.assertIn("agent-stack-vpn-routes setup", self.vpn_openvpn)
        self.assertIn("agent-stack-recovery.conf", self.vpn_openvpn)
        self.assertIn("clear_managed_vpn_dropins", self.vpn_openvpn)
        self.assertIn("vpn_bypass_cidrs_json", self.vpn_openvpn)
        self.assertIn("is_tailscale_ipv4_cidr", self.vpn_openvpn)
        self.assertIn("preserving Tailscale-managed route", self.vpn_openvpn)
        self.assertIn("net.ipv6.conf.all.disable_ipv6 = 1", self.vpn_openvpn)
        self.assertIn("Requires=agent-stack-vpn.service", self.agent_stack_service)
        self.assertIn("After=agent-stack-vpn.service", self.agent_stack_service)
        self.assertIn("vpn|agent-stack", self.diagnostics)
        self.assertIn("service_enabled=", self.diagnostics)
        self.assertIn("DropInPaths", self.diagnostics)
        self.assertIn("systemctl cat agent-stack-vpn", self.diagnostics)
        self.assertIn("journalctl -u agent-stack-vpn", self.diagnostics)
        self.assertIn("nordvpn status", self.diagnostics)
        self.assertIn("nordvpn_version=", self.diagnostics)
        self.assertIn("nordlynx", self.diagnostics)
        self.assertIn("tailscale_online=", self.diagnostics)

    def test_runtime_templates_include_tailscale_watchdog_units(self) -> None:
        watchdog = (RUNTIME_DIR / "agent-stack-tailscale-watchdog.sh.tpl").read_text(encoding="utf-8")
        watchdog_service = (RUNTIME_DIR / "agent-stack-tailscale-watchdog.service.tpl").read_text(encoding="utf-8")
        watchdog_timer = (RUNTIME_DIR / "agent-stack-tailscale-watchdog.timer.tpl").read_text(encoding="utf-8")
        self.assertIn("tailscale --socket=/tmp/tailscaled.sock status --json", watchdog)
        self.assertIn("serve status", watchdog)
        self.assertIn("agent-stack-tailscale-watchdog", watchdog_service)
        self.assertIn("OnBootSec", watchdog_timer)

    def test_workspace_image_installs_codex_and_ssh_tools(self) -> None:
        self.assertIn("FROM ubuntu:24.04", self.workspace_dockerfile)
        for package in [
            "openssh-server",
            "git",
            "curl",
            "jq",
            "bubblewrap",
            "util-linux",
            "python3",
            "python3-pip",
            "python3-venv",
            "tini",
        ]:
            self.assertIn(package, self.workspace_dockerfile)
        for removed in ["rclone", "fuse3", "/dev/fuse", "SYS_ADMIN"]:
            self.assertNotIn(removed, self.workspace_dockerfile)
        self.assertIn("para-memory-drive/SKILL.md", self.workspace_dockerfile)
        self.assertIn("https://chatgpt.com/codex/install.sh", self.workspace_dockerfile)
        self.assertIn("ARG CODEX_RELEASE=${workspace_codex_release}", self.workspace_dockerfile)
        self.assertIn('CODEX_RELEASE="$CODEX_RELEASE"', self.workspace_dockerfile)
        self.assertIn("CODEX_INSTALL_DIR=/usr/local/bin", self.workspace_dockerfile)
        self.assertIn("CODEX_HOME=/opt/codex", self.workspace_dockerfile)
        self.assertIn("WORKSPACE_PASSWORD is required", self.workspace_entrypoint)
        self.assertIn("PasswordAuthentication yes", self.workspace_entrypoint)
        self.assertIn("WORKSPACE_AUTHORIZED_KEYS_BASE64", self.workspace_entrypoint)
        self.assertIn("AuthorizedKeysFile .ssh/authorized_keys", self.workspace_entrypoint)
        self.assertIn("PubkeyAuthentication $pubkey_auth", self.workspace_entrypoint)
        self.assertIn("WORKSPACE_HOST_KEY_DIR", self.workspace_entrypoint)
        self.assertIn("ensure_host_key ed25519", self.workspace_entrypoint)
        self.assertIn("ensure_host_key ecdsa", self.workspace_entrypoint)
        self.assertIn("ensure_host_key rsa 4096", self.workspace_entrypoint)
        self.assertIn("HostKey $host_key_dir/ssh_host_ed25519_key", self.workspace_entrypoint)
        self.assertNotIn("rclone", self.workspace_entrypoint)
        self.assertNotIn("fuse", self.workspace_entrypoint.lower())
        self.assertIn("install_para_memory_drive_skill", self.workspace_entrypoint)
        self.assertIn("upsert_workspace_agents_guidance", self.workspace_entrypoint)
        self.assertIn("para-memory-drive/SKILL.md", self.workspace_entrypoint)
        self.assertIn("Google Drive is the source of", self.workspace_entrypoint)
        self.assertIn("does not mount Drive locally", self.workspace_entrypoint)
        self.assertIn("WORKSPACE_AUTHORIZED_KEYS_BASE64=${workspace_ssh_public_keys_base64}", self.workspace_env)
        self.assertIn("CODEX_HOME=/home/${workspace_username}/.codex", self.workspace_env)
        self.assertNotIn("FUSE", self.workspace_env)
        self.assertNotIn("workspace-drive", self.diagnostics)

    def test_workspace_codex_auto_updater_uses_the_canonical_user_install_and_fails_open_at_startup(self) -> None:
        self.assertIn("COPY workspace-codex-update.sh", self.workspace_dockerfile)
        self.assertIn("COPY workspace-codex-control.py", self.workspace_dockerfile)
        self.assertIn("python3-pip", self.workspace_dockerfile)
        self.assertIn("python3-venv", self.workspace_dockerfile)
        self.assertNotIn("pip install", self.workspace_dockerfile)
        self.assertIn("chmod 0755 /usr/local/libexec/agent-stack-workspace-codex-update", self.workspace_dockerfile)
        self.assertIn("chmod 0755 /usr/local/libexec/agent-stack-workspace-codex-control", self.workspace_dockerfile)
        self.assertIn("WORKSPACE_CODEX_AUTO_UPDATE_ENABLED", self.workspace_env)
        self.assertIn("WORKSPACE_CODEX_AUTO_RECOVER_INTERRUPTED_TURNS", self.workspace_env)
        self.assertIn('agent-stack-workspace-codex-update --initialize "$username"', self.workspace_entrypoint)
        self.assertIn("starting SSH without blocking access", self.workspace_entrypoint)
        self.assertLess(
            self.workspace_entrypoint.index("agent-stack-workspace-codex-update --initialize"),
            self.workspace_entrypoint.index("exec /usr/sbin/sshd -D -e"),
        )
        self.assertNotIn("--startup", self.workspace_entrypoint)
        self.assertIn('if [ "$(id -u)" -ne 0 ]', self.workspace_codex_update)
        self.assertIn('current_link="$standalone_dir/current"', self.workspace_codex_update)
        self.assertIn('pinned_current=/opt/codex/packages/standalone/current', self.workspace_codex_update)
        self.assertIn('expected="$current_link/codex"', self.workspace_codex_update)
        self.assertIn('ln -s "$expected" "$local_codex"', self.workspace_codex_update)
        self.assertIn('ln -s bin/codex "$direct"', self.workspace_codex_update)
        self.assertIn('exec runuser -u "$workspace_user" --', self.workspace_codex_update)
        self.assertIn('"$0" "$inner_mode" "$workspace_user"', self.workspace_codex_update)
        self.assertIn("--normalize-user)", self.workspace_codex_update)
        self.assertIn("unmanaged workspace Codex launcher would be overwritten; refusing", self.workspace_codex_update)
        self.assertNotIn("api.github.com/repos/openai/codex", self.workspace_codex_update)
        self.assertNotIn("pgrep", self.workspace_codex_update)

        self.assertIn('ALLOWED_ACTIONS: Final = {"preflight", "snapshot", "update", "restart-verify", "rollback", "recover"}', self.workspace_codex_control)
        self.assertIn('run_codex(ctx, "update", timeout=240)', self.workspace_codex_control)
        self.assertIn('"app-server", "proxy", "--sock"', self.workspace_codex_control)
        self.assertIn("RECOVERY_PROMPT", self.workspace_codex_control)
        self.assertIn("clientUserMessageId", self.workspace_codex_control)
        self.assertIn('"thread/turns/list"', self.workspace_codex_control)
        self.assertIn('"sortDirection": "desc"', self.workspace_codex_control)
        self.assertNotIn("shell=True", self.workspace_codex_control)

    def test_workspace_codex_auto_updater_uses_fixed_retry_slots_without_workspace_privilege_escalation(self) -> None:
        self.assertIn("retry_offsets=(0 300 900 2100)", self.workspace_update_host)
        self.assertIn("wait_until_epoch", self.workspace_update_host)
        self.assertIn("return 75", self.workspace_update_host)
        self.assertIn("restart-verify", self.workspace_update_host)
        self.assertIn("rollback_control_call", self.workspace_update_host)
        self.assertNotIn("pgrep", self.workspace_update_host)
        self.assertNotIn("workspace Codex is active", self.workspace_update_host)
        self.assertIn("docker exec --user root", self.workspace_update_host)
        self.assertIn('--user "$workspace_user"', self.workspace_update_host)
        self.assertNotIn("sudo", self.workspace_update_host)
        self.assertNotIn("docker.sock", self.workspace_update_host)
        self.assertIn("TimeoutStartSec=50min", self.workspace_update_service)
        self.assertIn("OnCalendar=*-*-* ${workspace_codex_auto_update_time}:00 ${workspace_codex_auto_update_timezone}", self.workspace_update_timer)
        self.assertIn("Persistent=false", self.workspace_update_timer)
        self.assertIn("AccuracySec=1s", self.workspace_update_timer)
        self.assertIn("RandomizedDelaySec=0", self.workspace_update_timer)
        self.assertNotIn("OnBootSec", self.workspace_update_timer)
        self.assertIn("configure_workspace_codex_auto_update", self.installer)
        self.assertIn("workspace-codex-update.sh", self.installer)
        self.assertIn("workspace-codex-control.py", self.installer)
        self.assertIn("backup_workspace_image", self.installer)
        self.assertIn("restore_workspace_image", self.installer)
        self.assertIn("all_ready=1", self.installer)
        self.assertIn('test "$(command -v codex)" = "$HOME/.local/bin/codex"', self.installer)
        self.assertIn('test "$actual" = "$expected"', self.installer)
        self.assertIn("agent-stack-workspace-codex-update.timer", self.installer)
        self.assertIn("configured timezone is not installed", self.installer)
        self.assertIn("backup_workspace_codex_auto_update_host", self.installer)
        updater_install = self.installer[
            self.installer.index("configure_workspace_codex_auto_update() {") : self.installer.index(
                "wait_agent_stack_initial_restart()"
            )
        ]
        self.assertLess(
            updater_install.index("systemctl daemon-reload"), updater_install.index('systemctl reset-failed "$unit"')
        )
        self.assertLess(
            updater_install.index('systemctl reset-failed "$unit"'), updater_install.index("systemctl enable --now")
        )

    def test_workspace_codex_update_bridge_has_only_queue_and_status_operations(self) -> None:
        self.assertIn("agent-stack-diagnostics codex-update", self.diagnostics)
        self.assertIn("agent-stack-diagnostics codex-update status", self.diagnostics)
        self.assertIn("systemctl start --no-block agent-stack-workspace-codex-update.service", self.diagnostics)
        self.assertIn("show_workspace_codex_update_status", self.diagnostics)
        self.assertIn("case $# in", self.diagnostics)
        self.assertIn("CODEX_HOME=/home/${workspace_username}/.codex", self.diagnostics)
        self.assertIn("PATH=/home/${workspace_username}/.local/bin:/usr/local/bin:/usr/bin:/bin", self.diagnostics)
        self.assertNotIn('if [ -r "$HOME/.bashrc" ]; then . "$HOME/.bashrc"', self.diagnostics)
        self.assertNotIn("docker.sock", self.workspace_entrypoint)

    def test_workspace_installer_prepares_persistent_host_keys(self) -> None:
        self.assertIn('install -d -m 0755 "$app/data/workspace/home"', self.installer)
        self.assertIn('install -d -m 0700 -o root -g root "$app/data/workspace/ssh-host-keys"', self.installer)

    def test_workspace_drive_skill_is_staged_without_fuse_support(self) -> None:
        main_tf = MAIN_TF.read_text(encoding="utf-8")
        self.assertIn("runtime_workspace_drive_skill", main_tf)
        self.assertIn("workspace-para-memory-drive-skill.md", main_tf)
        self.assertIn("workspace-para-memory-drive-skill.md", self.installer)
        for source in [self.compose, self.workspace_entrypoint, self.workspace_env, self.diagnostics, self.installer, main_tf]:
            self.assertNotIn("workspace_drive_fuse_enabled", source)
            self.assertNotIn("workspace_fuse_enabled", source)
        for source in [self.compose, self.workspace_entrypoint, self.workspace_dockerfile, self.diagnostics]:
            self.assertNotIn("rclone", source)
            self.assertNotIn("/dev/fuse", source)
            self.assertNotIn("SYS_ADMIN", source)

    def test_workspace_user_uses_stable_unprivileged_numeric_identity(self) -> None:
        self.assertIn('getent passwd 1000', self.workspace_dockerfile)
        self.assertIn('usermod --login "${workspace_username}"', self.workspace_dockerfile)
        self.assertIn('groupadd --gid 1000 "${workspace_username}"', self.workspace_dockerfile)
        self.assertIn(
            'useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash "${workspace_username}"',
            self.workspace_dockerfile,
        )
        self.assertIn('usermod --groups "" "${workspace_username}"', self.workspace_dockerfile)

    def test_runtime_restart_gate_is_observable_and_rollback_build_is_complete(self) -> None:
        restart_gate = self.installer.split("wait_agent_stack_initial_restart() {", 1)[1].split(
            "configure_caddyfile", 1
        )[0]
        self.assertIn("agent-stack=$agent_state", restart_gate)
        self.assertIn("openclaw=$openclaw_status", restart_gate)
        self.assertIn("workspace=$workspace_status", restart_gate)
        self.assertIn("codex=$codex_status", restart_gate)
        self.assertIn("codex_status=ready", restart_gate)
        self.assertIn("codex_status=not-ready", restart_gate)

        image_restore = self.installer.split("restore_workspace_image() {", 1)[1].split(
            "configure_host_tailscale() {", 1
        )[0]
        for required_build_input in [
            '"$app/workspace.Dockerfile"',
            '"$app/workspace-entrypoint.sh"',
            '"$app/workspace-para-memory-drive-skill.md"',
            '"$app/workspace-codex-update.sh"',
            '"$app/workspace-codex-control.py"',
        ]:
            self.assertIn(required_build_input, image_restore)

    def test_workspace_agents_guidance_preserves_user_content_and_is_idempotent(self) -> None:
        start = self.workspace_entrypoint.index("upsert_workspace_agents_guidance() {")
        end = self.workspace_entrypoint.index("install_para_memory_drive_skill", start)
        function = self.workspace_entrypoint[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            agents = home / "AGENTS.md"
            agents.write_text(
                "# My instructions\n\nKeep this user-authored section.\n",
                encoding="utf-8",
            )
            username = pwd.getpwuid(os.getuid()).pw_name
            harness = home / "seed-agents.sh"
            harness.write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\n"
                f'home_dir={shlex.quote(str(home))}\n'
                f'username={shlex.quote(username)}\n'
                f"{function}\n"
                "upsert_workspace_agents_guidance\n"
                "upsert_workspace_agents_guidance\n",
                encoding="utf-8",
            )
            harness.chmod(0o755)
            result = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = agents.read_text(encoding="utf-8")
            self.assertIn("Keep this user-authored section.", content)
            self.assertEqual(content.count("<!-- agent-stack: para-memory-drive begin -->"), 1)
            self.assertEqual(content.count("<!-- agent-stack: para-memory-drive end -->"), 1)
            self.assertIn("Google Drive is the source of", content)

    def test_workspace_diagnostics_bridge_is_limited_and_forced_command(self) -> None:
        self.assertIn("agent-stack-diagnostics@host.docker.internal", self.workspace_entrypoint)
        self.assertIn('command="/usr/local/bin/agent-stack-diagnostics-ssh"', self.installer)
        self.assertIn("SSH_ORIGINAL_COMMAND", self.diagnostics_ssh)
        self.assertIn("sudo -n /usr/local/bin/agent-stack-diagnostics", self.diagnostics_ssh)
        self.assertIn("openclaw|hermes|n8n|postgres|caddy|workspace", self.diagnostics)
        self.assertIn("tailscale|vpn|agent-stack", self.diagnostics)
        self.assertIn("show_container_inspect", self.diagnostics)
        self.assertNotIn("docker.sock", self.diagnostics)

    def test_host_tailscale_mode_disables_sidecar_and_serves_openclaw(self) -> None:
        self.assertIn("%{ if tailscale_sidecar_enabled }", self.compose)
        self.assertIn("[ \"${tailscale_sidecar_enabled}\" = \"true\" ]", self.installer)
        self.assertIn("[ \"${tailscale_host_enabled}\" = \"true\" ]", self.installer)
        self.assertIn(".Self.Online == true", self.host_tailscale)
        self.assertIn("Already online; keeping current login.", self.host_tailscale)
        self.assertIn('tailscale set --hostname="$TAILSCALE_HOSTNAME" --accept-routes || true', self.host_tailscale)
        self.assertNotIn("tailscale logout || true", self.host_tailscale)
        self.assertIn("tailscale serve --bg --yes --https=443 127.0.0.1:18789", self.host_tailscale)
        self.assertIn("tailscale serve --bg --yes --https=8443 127.0.0.1:9119", self.host_tailscale)


class RuntimeProvisioningContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main_tf = MAIN_TF.read_text(encoding="utf-8")

    def test_terraform_waits_for_cloud_init_and_provisions_as_admin(self) -> None:
        self.assertIn('resource "terraform_data" "runtime_apply"', self.main_tf)
        self.assertIn('"sudo cloud-init status --wait"', self.main_tf)
        self.assertIn("host        = local.admin_ssh_host", self.main_tf)
        self.assertIn("user        = var.admin_username", self.main_tf)
        self.assertNotIn('user        = "root"', self.main_tf)

    def test_runtime_reapplies_when_artifact_instance_or_volume_changes(self) -> None:
        self.assertIn("triggers_replace = {", self.main_tf)
        self.assertIn("artifact_checksum = local.runtime_artifact_checksum", self.main_tf)
        self.assertIn("instance_id       = local.runtime_instance_id", self.main_tf)
        self.assertIn("volume_id         = local.runtime_data_volume_id", self.main_tf)

    def test_runtime_uses_private_staging_directory(self) -> None:
        self.assertIn('runtime_staging_dir = "/opt/agent-stack/.staging-', self.main_tf)
        self.assertIn("install -d -m 0700", self.main_tf)
        self.assertIn("chmod 0700 ${local.runtime_staging_dir}", self.main_tf)



class LayoutMigratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (RUNTIME_DIR / "agent-stack-migrate-layout.sh.tpl").read_text(encoding="utf-8").replace("$${", "${")

    def run_migrator(self, data_setup: Callable[[Path], None]) -> Path:
        temp_path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp_path)
        app_root = temp_path / "agent-stack"
        data_root = app_root / "data"
        legacy_root = temp_path / "openclaw"
        data_root.mkdir(parents=True)
        data_setup(data_root)
        script_path = temp_path / "agent-stack-migrate-layout"
        script_path.write_text(self.script, encoding="utf-8")
        script_path.chmod(0o755)

        env = dict(os.environ)
        env["AGENT_STACK_APP_ROOT"] = str(app_root)
        env["AGENT_STACK_DATA_ROOT"] = str(data_root)
        env["AGENT_STACK_LEGACY_ROOT"] = str(legacy_root)
        subprocess.run(["bash", str(script_path)], check=True, env=env, capture_output=True, text=True)
        return app_root

    def test_fresh_layout_creates_peer_dirs(self) -> None:
        snapshot = self.run_migrator(lambda _data_root: None)
        data_root = snapshot / "data"
        for name in ["openclaw", "hermes", "n8n", "postgres", "caddy"]:
            self.assertTrue((data_root / name).exists(), name)
        self.assertTrue((data_root / ".agent-stack-layout-version").exists())

    def test_old_openclaw_only_layout_moves_into_openclaw_peer(self) -> None:
        def setup(data_root: Path) -> None:
            (data_root / "openclaw.json").write_text("{}", encoding="utf-8")
            (data_root / "workspace").mkdir()
            (data_root / "workspace" / "SOUL.md").write_text("soul", encoding="utf-8")

        snapshot = self.run_migrator(setup)
        data_root = snapshot / "data"
        self.assertTrue((data_root / "openclaw" / "openclaw.json").exists())
        self.assertTrue((data_root / "openclaw" / "workspace" / "SOUL.md").exists())
        self.assertTrue((data_root / "workspace").is_symlink())

    def test_old_mixed_layout_moves_peer_services(self) -> None:
        def setup(data_root: Path) -> None:
            (data_root / "openclaw.json").write_text("{}", encoding="utf-8")
            (data_root / "services" / "hermes").mkdir(parents=True)
            (data_root / "services" / "hermes" / "session.json").write_text("{}", encoding="utf-8")
            (data_root / "services" / "postgres").mkdir(parents=True)
            (data_root / "services" / "postgres" / "PG_VERSION").write_text("17", encoding="utf-8")

        snapshot = self.run_migrator(setup)
        data_root = snapshot / "data"
        self.assertTrue((data_root / "openclaw" / "openclaw.json").exists())
        self.assertTrue((data_root / "hermes" / "session.json").exists())
        self.assertTrue((data_root / "postgres" / "PG_VERSION").exists())

    def test_layout_migrator_is_idempotent(self) -> None:
        temp_path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp_path)
        app_root = temp_path / "agent-stack"
        data_root = app_root / "data"
        legacy_root = temp_path / "openclaw"
        data_root.mkdir(parents=True)
        script_path = temp_path / "agent-stack-migrate-layout"
        script_path.write_text(self.script, encoding="utf-8")
        script_path.chmod(0o755)

        env = dict(os.environ)
        env["AGENT_STACK_APP_ROOT"] = str(app_root)
        env["AGENT_STACK_DATA_ROOT"] = str(data_root)
        env["AGENT_STACK_LEGACY_ROOT"] = str(legacy_root)
        for _ in range(2):
            subprocess.run(["bash", str(script_path)], check=True, env=env, capture_output=True, text=True)

        for name in ["openclaw", "hermes", "n8n", "postgres", "caddy"]:
            self.assertTrue((data_root / name).is_dir(), name)
        self.assertTrue((data_root / ".agent-stack-layout-version").exists())


if __name__ == "__main__":
    unittest.main()
