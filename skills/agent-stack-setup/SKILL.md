---
name: agent-stack-setup
description: Guided setup and deployment workflow for this AgentStack Terraform repo. Use when a user wants help setting up AgentStack, needs terraform.tfvars created or updated, wants Terraform init or apply run, or needs deployment-time troubleshooting.
---

# AgentStack Setup

Use this skill for first-time setup, reconfiguration, or guided redeploys.

Core rules:
- Treat `variables.tf` as the schema and true-default source of truth.
- Treat `terraform.tfvars.example` as a suggestion source only for required values that have no Terraform default and for human-friendly examples.
- The target file is `terraform.tfvars` in the active repo root. Showcase acceptance uses a fresh isolated checkout/state and the named example instead of existing operator inputs.
- Never silently accept placeholder secrets such as `YOUR_AWS_ACCESS_KEY_ID`, `sk-...`, or `tskey-auth-...`.
- Prefer a default-first setup flow. Do not ask one question per Terraform variable when the repo already has a sensible default.
- Keep the normal setup interview to 8 prompts or fewer. Only ask follow-up questions when the user rejects a default, a required value is missing, or the repo has a known limitation.
- Ask grouped decision prompts instead of scalar variable prompts. Collect related values together when that reduces back-and-forth.
- For each high-level prompt, prefer 2-3 clear choices and a custom path when that speeds up intake. Put the recommended option first.
- Ask how the chosen cloud provider will authenticate before moving on. Either collect the provider credentials now or confirm the user already has env or role based auth in place.
- Ask which stack services, model providers, and channel the user wants before asking for provider-specific secrets.
- Keep Hermes, n8n, Postgres, public-domain, and generated-secret overrides in the advanced path unless the user selects the relevant branch.
- If a cloud resource can be resolved from a human-friendly identifier, such as a DigitalOcean volume name, resolve it with a repo-local helper instead of bouncing the lookup back to them.
- Treat `anthropic_auth_key` as a legacy fallback, not the normal Anthropic path. Prefer `anthropic_api_key`.
- If the user wants ChatGPT subscription-backed OpenAI models, set `openai_auth_mode = "codex"` with canonical `openai/*` model refs; legacy `openai-codex/*` refs are still supported but not recommended for new setup.
- For Codex auth, do not ask for a raw refresh token. Ask whether they want to use a local Codex CLI login before inspecting or importing `~/.codex/auth.json`.
- Before running `skills/agent-stack-setup/scripts/import_codex_auth.py --inspect` or `skills/agent-stack-setup/scripts/import_codex_auth.py`, explain that this reads local Codex auth and writes a base64 auth payload into `terraform.tfvars`, which may later appear in Terraform-managed state or the SSH-provisioned runtime bundle.
- Never auto-detect, inspect, import, print, or summarize local Codex auth without explicit user consent in that setup conversation.
- Treat provider migration as a separate guided mode. Do not rewrite `cloud_provider` in the same Terraform state when the user wants to move between AWS, DigitalOcean, and Hetzner.

## Workflow

1. Inspect the repo before asking questions.
   - Read `variables.tf`, `main.tf`, and the current `terraform.tfvars` if it exists.
   - Read `terraform.tfvars.example` only for suggestion values and example copy.
   - Run `python3 skills/agent-stack-setup/scripts/render_tfvars.py inspect --repo-root .` to capture the current schema, defaults, and example-backed suggestions.

2. Detect provider migration intent before normal setup.
   - Enter migration mode when the user says they want to switch provider, move to Hetzner, port a volume, migrate storage, or when their requested provider differs from `terraform output -raw provider_used`.
   - Refuse in-place provider flips in the same Terraform state.
   - Guide the user through a separate target checkout/state, fresh target storage, target bootstrap, `skills/agent-stack-setup/scripts/migrate_provider_data.sh --precopy`, final stop-and-copy with `--final`, validation, DNS cutover, and later explicit cleanup of the old provider.
   - Explain that `--precopy` leaves the source running but stops the target during import; `--final` stops both stacks and starts only the target afterward.

3. Gather answers with a condensed decision flow.
   - Use `references/question-groups.md` for the canonical prompt order and follow-up rules.
   - Ask provider-specific questions only for the chosen provider.
   - Accept Terraform defaults silently unless the user asks to override them or the field is required and has no default.
   - Do not ask advanced runtime questions unless the user opts into advanced overrides.
   - When the user wants to reuse a DigitalOcean volume by name, resolve it with `python3 skills/agent-stack-setup/scripts/resolve_do_volume.py --name <volume-name>` once a token is available.
   - When the user wants `openai_auth_mode = "codex"` or legacy `openai-codex/*`, offer a choice before touching local auth: review/import the local Codex login, paste an already-exported base64 payload, or switch/defer auth.

4. Write `terraform.tfvars` canonically.
   - Save answers to a temporary JSON file.
   - If `terraform.tfvars` already exists and the user is updating or redeploying, merge the new answers on top of the existing file with `python3 skills/agent-stack-setup/scripts/render_tfvars.py render --repo-root . --answers <answers.json> --base terraform.tfvars --output terraform.tfvars`.
   - If this is a first-time setup, run `python3 skills/agent-stack-setup/scripts/render_tfvars.py render --repo-root . --answers <answers.json> --output terraform.tfvars`.
   - If the renderer rejects placeholders or missing required values, fix the answers before continuing.
   - Leave defaulted variables out of the answers payload unless the user explicitly changed them.

5. Run Terraform with one approval gate.
   - Run `terraform init`.
   - Run `terraform plan` and summarize the outcome.
   - Ask once before `terraform apply`.
   - If apply fails or bootstrap is unhealthy, use `references/verification.md` for targeted recovery and switch to `skills/agent-stack-doctor/SKILL.md` when the issue is no longer setup-specific.

6. Finish with the operator handoff.
   - Surface the important Terraform outputs: `provider_used`, `instance_public_ip`, `ssh_command`, `repo_ssh_command`, `tailscale_note`, `vpn_note`, `dashboard_url`, `dashboard_url_with_token_import`, `openclaw_url`, `hermes_url`, `n8n_url`, `n8n_webhook_url`, `bootstrap_log_command`, `repo_bootstrap_log_command`, `pair_latest_command`, `ui_auth_username`, and `whatsapp_login_command`.
   - Call out any deferred manual step such as Tailscale login, DNS records, public-domain login, Telegram setup, or WhatsApp QR login.

## References

- `references/question-groups.md` for the condensed prompt order and conditional follow-up rules.
- `references/placeholders.md` for placeholder rejection and example-suggestion handling.
- `references/auth-and-schema-gaps.md` for current Anthropic and OpenAI auth guidance, Codex login import steps, and remaining implementation gaps.
- `references/verification.md` for post-apply verification, bootstrap recovery, and handoff commands.

## Showcase and GCP

Use `examples/showcase/README.md` for TODO #7 acceptance. GCP authenticates through ADC/workload identity and requires an approved project with billing, Compute API, quota and compatible SSH/project policy. Preserve ordinary defaults; require explicit pins and use strict doctor plus real API-key tasks and second-peer authenticated browser checks before claiming success.
