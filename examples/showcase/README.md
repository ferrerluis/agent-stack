# TODO #7 showcase

One fresh VM runs OpenClaw, Hermes and a workspace containing Claude Code and Codex. This is the live acceptance profile; the repository's normal deployment defaults remain unchanged.

Code and local tests do not establish live support. The four-cloud matrix is pending credentials, container verification, real agent tasks, peer browser access, reboot checks and cleanup.

Use the shared [settings](terraform.tfvars.example) only in an isolated checkout with new state and test-only SSH keys. Before launching, supply:

- Provider, approved account/project, region/zone, unique resource name, runner SSH `/32`, and a verified x86_64 VM with at least 8 GiB RAM.
- Exact OpenClaw release plus `openclaw_image_digest`, Hermes image with `@sha256:…`, and `workspace_codex_release` / `workspace_claude_release`. No `latest` tags or CLI updates during acceptance.
- Supported `default_model` and optional `hermes_model`, OpenAI and Anthropic secret references, Tailscale enrollment and deletion authority, and a second authorized tailnet peer.
- A workspace password supplied privately through `op` and test-only public SSH keys. Coding-tool model keys go through the task helper's stdin; they are separate from Terraform runtime credentials.

AWS uses a new VPC in the existing account. DigitalOcean, GCP and Hetzner require dedicated new test projects; GCP also needs billing, Compute API, quota and compatible project policy. Run one deployment at a time, within $25 total spend, one fresh-deployment retry per provider and four hours per attempt; confirm deletion of all owned resources and tailnet nodes.

After apply, `openclaw_url` and `hermes_url` use the observed full tailnet DNS name on HTTPS ports 443 and 8443. Retrieve `dashboard_url_with_token_import` and `hermes_dashboard_password` privately with `terraform output -raw`; ordinary apply output hides these values.

Run the strict checks from the isolated checkout:

```bash
./skills/agent-stack-doctor/scripts/check_remote_health.sh \
  --strict --json --services openclaw,hermes,workspace \
  --require-claude --check-tailscale --tailscale-mode host
```

For a single authorized workspace model task, pipe the key from `op` through SSH into `docker compose exec -T --user 1000 workspace python3 /usr/local/libexec/agent-stack-workspace-cli-task --cli codex --model <model-id>` (or `--cli claude`). Supply the workspace `HOME` when using Docker exec; SSH already sets it. The helper emits sanitized status, token counts and the persisted fixture path; it never emits the key or raw CLI output.

Every required matrix cell remains pending until measured. Use the [implementation plan](../../docs/plans/showcase-readiness-implementation.md) and [test plan](../../docs/plans/showcase-readiness-tests.md) for the complete acceptance and cleanup rules.
