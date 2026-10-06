# TODO #7 showcase

One fresh VM runs OpenClaw, Hermes and a workspace containing Claude Code and Codex. This is the live acceptance profile; the repository's normal deployment defaults remain unchanged.

Code and local tests do not establish live support. The four-cloud matrix is pending credentials, container verification, real agent tasks, peer browser access, reboot checks and cleanup.

Use the shared [settings](terraform.tfvars.example) only in an isolated checkout with new state and test-only SSH keys. Before launching, supply:

- Provider, approved account/project, region/zone, unique resource name, runner SSH `/32`, and a verified x86_64 VM with at least 8 GiB RAM.
- Exact OpenClaw release plus `openclaw_image_digest`, Hermes image with `@sha256:…`, and `workspace_codex_release` / `workspace_claude_release`. No `latest` tags or CLI updates during acceptance.
- Supported `default_model` and optional `hermes_model`, OpenAI and Anthropic secret references, Tailscale enrollment and deletion authority, and a second authorized tailnet peer.
- A workspace password supplied privately through `op`, hashed in memory before Terraform receives `workspace_password_hash`, and test-only public SSH keys. Coding-tool model keys go through the task helper's stdin; they are separate from Terraform runtime credentials.

AWS uses a new VPC in the existing account. DigitalOcean, GCP and Hetzner require dedicated new test projects; GCP also needs billing, Compute API, quota and compatible project policy. Run one deployment at a time, within $25 total spend, one fresh-deployment retry per provider and four hours per attempt; confirm deletion of all owned resources and tailnet nodes.

Supply `do_project_id` for DigitalOcean so the new Droplet and disk join that project. Terraform does not create or delete the project, and project assignment leaves reused disks in their original project; the showcase requires a new disk.

The [private input example](execution-inputs.example.json) records accounts/projects and 1Password field references. Copy it outside Git with mode 0600, replace placeholders, and run `python3 scripts/showcase_policy.py --config <private-inputs.json> --provider <provider>`. This checks field structure and limits; live permissions, prices and account/model/peer access remain separate checks.

For the existing-peer option, put the fields from [the SSH peer example](peer-ssh.example.json) under `runner.peer_ssh` in that private input file. Supply the authorized peer's reachable IP, Linux SSH user/port, independently verified Ed25519 public host key and a 1Password private-key reference. The peer must already have Python, OpenSSH, Tailscale, Playwright and Chromium; this component installs nothing and enrolls no peer. The temporary-peer alternative still needs an explicit choice and implementation.

The [container command](../../TEST.md) accepts exact Codex and Claude versions and checks both workspace variants without model credentials. The opt-in [read-only preflight](../../TEST.md#read-only-cloud-preflight) checks one selected cloud, the registered second tailnet peer and authenticated metadata for all four selected model IDs. Use project/workspace-specific API keys that need no extra selection header. It reads only the required 1Password fields, creates nothing and generates no model response; model execution/pricing gates remain unverified and `read_checks_passed` always has `launch_ready: false`.

AWS and DigitalOcean account identities are checked through their APIs. GCP uses the explicitly supplied service-account field for an in-memory signed token exchange, then checks the target project, billing, permissions, CPU quota and inventories across zones. Hetzner's token scope can be checked for existing resources and VM availability, but account/project mapping needs separate proof because its Cloud API does not return those IDs. Prices, complete quotas/lifecycle permissions, model execution and peer browser access remain pending.

The [read-only inventory and cleanup verifier](../../TEST.md#read-only-cleanup-verification) now records implicit boot disks, VPC children, IPs and tailnet nodes, preserves them through partial cleanup and requires both empty Terraform state and independent API absence checks. It creates or deletes nothing; Hetzner receipts still need separate scope-mapping proof.

The [saved-plan driver and recovery command](../../TEST.md#saved-plans-and-guarded-recovery) prepare checked private plans and support ownership-checked destruction of recorded active attempts. DigitalOcean planning measures bundled VM/boot-disk, data-volume and outbound unit prices; AWS planning checks the standard Ubuntu AMI and measures plain USD On-Demand VM, both gp3 disks and one public IPv4 address. GCP planning checks E2 CPU/RAM and the standard Ubuntu image, then reads CPU, memory, both balanced disks and external IPv4 rates from the authenticated Billing Catalog API, assuming no free allowance. Hetzner planning prices the VM/boot disk, data disk and Primary IPs in the actual EUR or USD account currency including VAT, and checks type/image availability over the full attempt. EUR prices still need a USD spending bound. Private receipts bind the checked source, plan and selected scope. These partial prices do not pass the price gate; complete cost bounds remain pending. Planning reserves no spend; production launch measurements, the paid public entrypoint, Hetzner identity proof and all real acceptance checks remain pending. Adding another cloud's inputs or repairing pins preserves spending/retry history and original cleanup scope.

The [peer access component](../../TEST.md#second-peer-browser-component) now collects the owned VM's browser credentials and SSH host keys in memory, sends checked source through key-based SSH to the selected peer, and checks host port 22, workspace port 2222 and both authenticated browser sessions. Reboot requires fresh peer checks afterward; these paths have fixture coverage only and remain unverified on a real peer.

The internal model-request gate preserves the quoted model allocation and records worst-case request holds before credentials or HTTP, sharing the original spending ledger across restarts. The internal terms reader now obtains supported model/context/cache bounds from fixed official pages and selected model GETs, preserving canonical IDs and including published long-context/regional premiums. Public page parsing passed; authenticated reads remain fixtures, the public command does not yet call this reader, and routing all four tools through a credential-isolating gateway remains pending. The $25 live-run gate is still unverified.

The [internal coordinator](../../TEST.md#remote-acceptance-component) now sequences deployment, acceptance, reboot and automatic cleanup, with a first-owned-VM container-test path when local Docker is absent. Deferred tests cannot enable doctors/model tasks or count as a pass; production launch-gate measurements and the paid public run phase remain unfinished, so the public command still supports planning and cleanup only.

The [private preparation command](../../TEST.md#private-attempt-preparation) exports an exact commit, creates a new test SSH key and saves non-secret profile settings without retrieving credentials or making cloud/model requests. It refuses operator state/auth, changed source/settings/keys and added overrides. Preparation does not reserve spend or establish deployment readiness.

After apply, `openclaw_url` and `hermes_url` use the observed full tailnet DNS name on HTTPS ports 443 and 8443. Retrieve `dashboard_url_with_token_import` and `hermes_dashboard_password` privately with `terraform output -raw`; ordinary apply output hides these values.

Run the strict checks from the isolated checkout:

```bash
./skills/agent-stack-doctor/scripts/check_remote_health.sh \
  --strict --json --services openclaw,hermes,workspace \
  --require-claude --check-tailscale --tailscale-mode host
```

The strict result includes OpenClaw lint and Hermes's native doctor. Hermes must return zero with a complete passing summary; AgentStack also checks its private configuration and authenticated detailed readiness. Unsupported native-report formats fail pending validation of the pinned image.

For a single authorized workspace model task, pipe the key from `op` through SSH into `docker compose exec -T --user 1000 workspace python3 /usr/local/libexec/agent-stack-workspace-cli-task --cli codex --model <model-id>` (or `--cli claude`). Supply the workspace `HOME` when using Docker exec; SSH already sets it. The helper emits sanitized status, token counts and the persisted fixture path; it never emits the key or raw CLI output.

Every required matrix cell remains pending until measured. Use the [implementation plan](../../docs/plans/showcase-readiness-implementation.md) and [test plan](../../docs/plans/showcase-readiness-tests.md) for the complete acceptance and cleanup rules.
