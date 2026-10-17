WORKSPACE_USERNAME=${workspace_username}
WORKSPACE_PASSWORD=${workspace_password}
WORKSPACE_PASSWORD_HASH='${workspace_password_hash}'
WORKSPACE_AUTHORIZED_KEYS_BASE64=${workspace_ssh_public_keys_base64}
WORKSPACE_CODEX_AUTO_UPDATE_ENABLED=${workspace_codex_auto_update_enabled}
WORKSPACE_CODEX_AUTO_RECOVER_INTERRUPTED_TURNS=${workspace_codex_auto_recover_interrupted_turns}
CODEX_HOME=/home/${workspace_username}/.codex
%{ if workspace_muse_enabled }
META_API_KEY='${meta_api_key}'
MUSE_NO_AUTO_UPDATE=1
%{ endif }
