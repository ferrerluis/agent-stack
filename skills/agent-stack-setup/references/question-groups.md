# Question Groups

Use this order when gathering answers for `terraform.tfvars`.

Goal:

- Keep the normal setup interview to 8 prompts or fewer.
- Ask grouped decision prompts, not one prompt per variable.
- Fall back to Terraform defaults unless the user asks to override them.
- For each top-level prompt, present 2-3 choices plus a custom path when appropriate.
- Prefer "recommended default", "common alternative", and "other" style choices over open-ended questions.

## Prompt format

Use this structure whenever possible:

- Recommended default choice first.
- One or two common alternatives second.
- An `other` or custom path only when the underlying value is not already constrained to a small fixed set.

Only ask for raw values after the user picks a non-default or custom branch.

## Prompt 1: Deployment target

Collect in one prompt:

- `cloud_provider`
- whether this is a fresh deploy or reuse of existing storage
- whether the default naming profile is okay

Suggested choices:

- Provider: `aws`, `digitalocean`, `hetzner`
- Storage: `fresh volume`, `reuse existing volume`
- Naming: `use default names`, `custom names`

Follow-up rules:

- Ask for `project_name` and `admin_username` only if the user picks custom names.
- If reusing DigitalOcean storage, collect the volume name first and resolve the ID with `python3 skills/agent-stack-setup/scripts/resolve_do_volume.py --name <volume-name>` once a valid token is available.
- If reusing AWS storage, ask for `aws_existing_volume_id`.
- If reusing Hetzner storage, ask for `hcloud_existing_volume_id`.
- If an existing volume is reused, explain that `openclaw_config_mode = "auto"` can preserve an existing `openclaw.json`.
- If the user is changing providers, leave the normal setup path and use provider migration mode instead of reusing the old state.

## Prompt 2: Infrastructure and cloud auth profile

Ask whether the user wants the recommended infrastructure defaults for the chosen provider, and ask how Terraform will authenticate to that provider.

Default AWS profile:

- `aws_region = "us-east-1"`
- `aws_instance_type = "t3.small"`
- `aws_disk_size_gb = 50`
- `aws_ami_id = ""`

Default DigitalOcean profile:

- `do_region = "nyc3"`
- `do_droplet_size = "s-2vcpu-2gb"`
- `do_disk_size_gb = 20`

Default Hetzner profile:

- `hcloud_location = "ash"`
- `hcloud_server_type = "cpx21"`
- `hcloud_image = "ubuntu-22.04"`
- `hcloud_disk_size_gb = 50`

Only ask follow-ups if the user wants overrides or has not already authenticated locally.

Suggested choices:

- `recommended`
- `bigger instance`
- `other`
- Cloud auth: `use env or role auth`, `enter provider credentials now`, `other`

Follow-up rules:

- If provider is `aws` and the user chooses to enter credentials now, collect `aws_access_key` and `aws_secret_key`.
- If provider is `aws` and the user chooses env or role auth, explicitly confirm they already have `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, or an instance, SSO, or assumed role, available to Terraform.
- If provider is `digitalocean` and the user chooses to enter credentials now, collect `do_token`.
- If provider is `digitalocean` and the user chooses env-based auth, explicitly confirm `DIGITALOCEAN_TOKEN` is already set for Terraform.
- If provider is `hetzner` and the user chooses to enter credentials now, collect `hcloud_token`.
- If provider is `hetzner` and the user chooses env-based auth, explicitly confirm `HCLOUD_TOKEN` is already set for Terraform.

## Prompt 3: Access profile

Ask whether the user wants the recommended SSH and access defaults.

Default access profile:

- `ssh_public_key = ""` to auto-resolve or create the repo-local keypair
- `repo_ssh_private_key_path = ".ssh/id_ed25519_agent_stack"`
- `generate_repo_ssh_config = true`
- `repo_ssh_host_alias = "agent-stack"`
- `repo_ssh_identity_file = "./.ssh/id_ed25519_agent_stack"`
- `allowed_ssh_cidr = "0.0.0.0/0"`

Only ask follow-ups if the user wants overrides or already has an explicit SSH public key.

Suggested choices:

- `recommended access defaults`
- `paste my own SSH key`
- `other`

## Prompt 4: Stack and channels

Collect:

- `enabled_services`
- `agent_channel`
- any channel-specific bootstrap secret that is actually needed

Follow-up rules:

- Default to `enabled_services = ["openclaw", "hermes", "n8n"]`.
- Offer an OpenClaw-only preset without asking Hermes, n8n, or Postgres questions.
- Ask custom service-selection follow-ups only if the user rejects the all-services or OpenClaw-only presets.
- If the user chooses Telegram with bootstrap preconfiguration, ask for `telegram_bot_token`.
- If the user chooses Telegram without bootstrap preconfiguration, keep `agent_channel = "telegram"` and skip `telegram_bot_token`.
- Ask for `telegram_allow_from` only if the user wants a pre-approved allowlist.
- If `agent_channel = "whatsapp"` or OpenClaw is not enabled, do not ask Telegram questions.
- If the workspace service is selected, collect `workspace_username`, `workspace_password`, `workspace_ssh_host_port`, and optional public key strings.
- Explain that workspace knowledge access uses the bundled `para-memory-drive` skill with the connected Drive capability. Drive is authoritative and no local Drive filesystem, rclone configuration, or OAuth credential is provisioned.

Suggested choices:

- `all services, Telegram later`
- `OpenClaw only, Telegram later`
- `all services, WhatsApp`
- `custom stack`

## Prompt 5: Model routing

Collect in one prompt:

- `model_providers_enabled`
- `default_model`
- `fallback_models`

Rules:

- These values are required even though they have no Terraform defaults.
- Use `terraform.tfvars.example` as a suggestion source for these three values only.
- Ask which providers the user wants before asking for any model-provider secrets.

Suggested choices:

- `recommended repo default`
- `OpenAI API setup`
- `OpenAI Codex setup`
- `other`

Preset bundles:

- `recommended repo default`
  - `model_providers_enabled = ["anthropic", "google"]`
  - `default_model = "anthropic/claude-haiku-4-5"`
  - `fallback_models = ["google/gemini-3-flash-preview", "anthropic/claude-sonnet-4-6"]`
- `OpenAI API setup`
  - `model_providers_enabled = ["openai"]`
  - `openai_auth_mode = "api_key"`
  - `default_model = "openai/gpt-5.4"`
  - `fallback_models = ["openai/gpt-5.4-mini"]`
- `OpenAI Codex setup`
  - `model_providers_enabled = ["openai"]`
  - `openai_auth_mode = "codex"`
  - `default_model = "openai/gpt-5.5"`
  - `fallback_models = ["openai/gpt-5.4-mini"]`

## Prompt 6: Provider credentials

Ask only for secrets that match the chosen channel and model providers.

Examples:

- Ask `openai_api_key` only if any configured model uses `openai/*` with `openai_auth_mode = "api_key"`.
- If any configured model uses `openai/*` with `openai_auth_mode = "codex"` or legacy `openai-codex/*`, ask for explicit consent before inspecting or importing a local Codex CLI login into `openai_codex_auth_json_base64`.
- Ask `groq_api_key` only if the user selected Groq.
- Ask `gemini_api_key` only if the user selected Google Gemini.
- Ask `anthropic_api_key` only if the user selected Anthropic.

Special rules:

- Do not ask `anthropic_auth_key` in the normal flow. See `auth-and-schema-gaps.md`.
- If the user wants subscription-backed OpenAI access, first explain that local import reads `~/.codex/auth.json` and stores a base64 auth payload in `terraform.tfvars`, which may later be copied into Terraform state or cloud-init data.
- Only after the user chooses local import, ask them to run `codex login` if needed, choose the ChatGPT sign-in path, then verify importability with `python3 skills/agent-stack-setup/scripts/import_codex_auth.py --inspect` before storing the base64 output from `python3 skills/agent-stack-setup/scripts/import_codex_auth.py`.
- If the user does not want local import, offer to accept an already-exported `openai_codex_auth_json_base64`, switch to `openai/*` API-key models, or defer until they are ready.
- Do not ask the user to manually paste a raw OpenAI refresh token unless import is impossible.
- Ask `gateway_token` only if the user explicitly wants to override the auto-generated token.

Suggested choices:

- `enter API keys or paste auth payload`
- `review and import my local Codex login`
- `switch, defer, or other auth path`

## Prompt 7: Access and domains

Collect:

- whether `tailscale_enabled` should stay on
- `tailscale_auth_key` if it stays enabled
- whether `public_domain_enabled` should stay off
- domain/auth values only if public domains are enabled

Rule:

- If `tailscale_enabled = true`, `tailscale_auth_key` is required.
- If `public_domain_enabled = true`, collect `base_domain` or explicit service domains and keep `ui_auth_mode = "basic"`.
- Ask for `ui_auth_username` or `ui_auth_password` only if the user wants to override generated public-domain credentials.
- Do not ask domain questions in the normal path when public domains stay disabled.

Suggested choices:

- `Tailscale only, no public domains`
- `Tailscale plus public domains`
- `SSH tunnels only`

## Prompt 8: Advanced overrides

Ask once whether the user wants any advanced overrides. If no, keep defaults.

Only ask follow-up questions here for:

- `openclaw_config_mode`
- `openclaw_version`
- `workspace_codex_release` (when live updates are enabled, it must be a stable `x.y.z` image fallback)
- `workspace_codex_auto_update_enabled` (opt in only when the workspace service is enabled; it runs a hard maintenance cutover that may interrupt active Codex work when a new CLI release requires an app-server restart, never at startup or after an idle wait)
- `workspace_codex_auto_update_timezone` and `workspace_codex_auto_update_time` (default: `04:00 America/New_York`; technical retries are +5, +15, and +35 minutes after the configured cutover)
- `workspace_codex_auto_recover_interrupted_turns` (leave disabled for the first approved rollout; enable only in a second targeted approved apply after the root-admin-only disposable-thread desktop E2E probe passes)
- `openclaw_node_options`
- `openclaw_swap_size_mb`
- `openclaw_health_start_period_seconds`
- `openclaw_health_retries`
- `seed_starter_workspace_files`
- `starter_soul_profile`
- `hermes_image`
- `hermes_dashboard_enabled`
- `hermes_api_server_enabled`
- `hermes_api_server_key`
- `n8n_image`
- `n8n_database_mode`
- `n8n_encryption_key`
- `n8n_public_webhooks_enabled`
- `n8n_generic_timezone`
- `postgres_image`
- `postgres_database`
- `postgres_user`
- `postgres_password`
- `external_postgres_host`
- `external_postgres_port`
- `external_postgres_database`
- `external_postgres_user`
- `external_postgres_password`
- `external_postgres_ssl_enabled`
- `vpn_enabled`
- `vpn_provider`
- `vpn_nordvpn_token`
- `vpn_nordvpn_connect_target`
- `vpn_openvpn_config_url`
- `vpn_username`
- `vpn_password`
- `vpn_bypass_cidrs`
- `vpn_disable_ipv6`
- `vpn_healthcheck_url`
- `acme_email`
- `ui_auth_username`
- `ui_auth_password`

VPN follow-up rules:

- Offer `nordvpn_openvpn` for the backward-compatible manual configuration and `nordvpn_nordlynx` for the native Linux app.
- For `nordvpn_openvpn`, collect the HTTPS config URL plus Nord manual service username and password.
- For `nordvpn_nordlynx`, collect a non-expiring Nord access token as a sensitive value and optionally a country/server target such as `United_States`.
- Both providers require at least one public/admin fallback CIDR in `vpn_bypass_cidrs`.
- Never put `100.64.0.0/10` or another overlapping CIDR in `vpn_bypass_cidrs`; the runtime manager preserves Tailscale separately.

Workspace Codex updater follow-up rules:

- Explain that the scheduled job uses the official `codex update` command at the selected daily cutover. It does not update at startup, wait for idle time, or poll running Codex processes. It proceeds only when the canonical Codex managed daemon is present; it refuses an unmanaged server or competing legacy hourly updater rather than taking either one over.
- Explain that only pre-restart technical failures retry, at +5, +15, and +35 minutes after the selected time. A successful new version causes an intentional app-server restart, which can interrupt active work.
- Keep automatic interrupted-turn recovery disabled on the first approved rollout. The desktop E2E probe must use a root-admin-only disposable thread; enable recovery only through a second targeted approved apply after it succeeds.
- Explain the recovery limit before enabling it: it can append one new safety-constrained turn for a proven interrupted turn, but cannot resume the in-flight turn or replay its prior prompt/commands. A turn created between the snapshot and restart may not be recovered.
- The workspace image includes Python 3, `pip`, and `venv`, but no third-party packages are installed during build. Recommend `python3 -m venv ~/.venvs/<name>` for workspace-user packages.

Explain `openclaw_config_mode` only if the user wants to override it:

- `auto`: preserve reused installs, manage fresh installs
- `manage`: always apply optional channel and model bootstrap edits
- `preserve`: skip optional channel and model bootstrap edits

## Provider Migration Mode

Use this mode when the user wants to move between AWS, DigitalOcean, and Hetzner.

Rules:

- Never change `cloud_provider` in the same Terraform state for a live migration.
- Use a separate target checkout/state with fresh target storage.
- Bootstrap the target first, then copy the full `/opt/agent-stack/data` tree from source to target.
- Use `skills/agent-stack-setup/scripts/migrate_provider_data.sh --precopy` for low-downtime rehearsal; it leaves the source running and stops only the target during import.
- Use `skills/agent-stack-setup/scripts/migrate_provider_data.sh --final` for the final stopped copy.
- Validate the target before DNS cutover or old-provider cleanup.
- Never run `terraform destroy` as part of migration.

Suggested choices:

- `use recommended defaults`
- `show common overrides`
- `other advanced changes`

Advanced defaults:

- n8n uses local Postgres on the persistent data volume by default.
- External Postgres is an advanced branch and requires host, port, database, user, password, and SSL choice.
- Host VPN egress is an advanced branch. If enabled, collect manual VPN service credentials, an OpenVPN config URL, and at least one bypass CIDR for SSH or host Tailscale access.
- Hermes, n8n, Postgres image tags, generated secret overrides, and public webhook exposure are advanced config.

## Meta models and workspace tools

- Offer standard Muse Spark 1.3 (`meta/muse-spark-1.3`) and Muse Wonder 1.0 (`meta/muse-wonder-1.0`) with provider `meta`; request `meta_api_key` only for Meta or selected Muse Code authentication. Never accept the example placeholder.
- Coding tools are a multi-selection: Codex checked and locked; Claude Code and Muse Code optional. Store choices in `workspace_harnesses`. Ask for an exact Claude release if selected; use the pinned Muse default unless overridden.
- Keep model choices independent of installed coding tools. An installed optional CLI does not configure chief-of-staff delegation.
- For existing configs, explain `auto`/`preserve` behavior and select `manage` only when updating their model settings is requested.

## Hugging Face personal-agent option

Offer `huggingface` as an optional personal-agent provider. Use only
`huggingface/moonshotai/Kimi-K3:baseten`,
`huggingface/meta-models/Muse-Spark-Open-816B-A42B:baseten` and
`huggingface/deepseek-ai/DeepSeek-V4.1-Flash:baseten`
for primary/fallback selections. Preserve the inner Hub organization and backend
suffix. Collect an existing `huggingface_api_key` with Inference Providers
permission only after HF is selected; do not create/import credentials or assume
free inference. Explain account credits/access gating, preserved reasoning history
and the managed sequential-tool policy; parallel tool behavior is not live-verified.
See `docs/huggingface-integration.md`. This choice does not select a coding harness.
