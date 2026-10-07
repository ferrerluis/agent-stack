# TODO #7 showcase

One fresh VM runs OpenClaw, Hermes and a workspace containing Claude Code and Codex. The repository's ordinary deployment defaults remain unchanged.

Acceptance requires all 24 cells across AWS, DigitalOcean, GCP and Hetzner at the same final revision, followed by independently verified cleanup. Code, local tests, CLI installation and healthy containers each establish only part of that result. Read the selected batch's private receipts for its current status.

Use [the named settings](terraform.tfvars.example) and [private input example](execution-inputs.example.json) in an isolated checkout with new state and test-only SSH keys. `variables.tf` remains the schema and true-default source; examples supply suggestions and required placeholders.

## Inputs

- Approved provider account/project, region/zone, unique resource prefix, runner SSH `/32`, and a verified x86_64 VM with at least 4 GiB RAM. This floor permits smaller acceptance candidates; each chosen machine must pass the whole workload.
- Exact OpenClaw release and image digest, a Hermes image with `@sha256:…`, and exact Codex / Claude Code releases. Avoid `latest` and updates during acceptance.
- Explicit model IDs and OpenAI / Anthropic field-level secret references. Funding-constrained tests allow the selected GPT-6 Luna pair with either pinned Haiku 4.5 or Haiku 5.5; current terms and API metadata must agree before launch.
- Tailscale enrollment and deletion authority, HTTPS/MagicDNS availability, and an authorized second peer for direct browser and SSH checks.
- A workspace password supplied privately through `op`, hashed in memory before Terraform receives it, and test-only public SSH keys. Provider model keys stay in the controller; the showcase passes inactive model placeholders to Terraform and uses a budget-controlled gateway for actual tasks.

AWS uses a new VPC in the existing account. DigitalOcean, GCP and Hetzner require dedicated new test projects. GCP needs billing, Compute API, quota and compatible SSH/project policy. DigitalOcean's `do_project_id` assigns the new Droplet and disk; Terraform leaves the project itself in place.

Hetzner tokens expose project-scoped resources without an account/project identity endpoint. A selected `identity_proof_ref` must bind the token to the approved signed-in console project. The implemented free proof matches an unattached, empty firewall by native ID/name in that console, then confirms its deletion; it creates no SSH authentication grant.

Keep private inputs outside Git with mode 0600. `terraform.tfvars`, state, gateway tokens and `#token=` URLs are sensitive.

## Bounded execution

The approved ceiling is $25 total cloud/model spend, at most two fresh attempts per cloud, and four hours per attempt. The cost-reduced private inputs use 70 minutes and 512 MiB of outbound traffic per boot; 1 GiB remains the default. Incoming image downloads do not consume the outbound allowance, and internal Docker traffic must not consume it.

Run one deployment at a time. Every launch needs an exact committed export, matching local test receipt, native account/project and capacity reads, fresh plan-bound resource/model terms, enough peer lifetime, a durable reservation, and independent guest/controller cleanup deadlines. A price-read receipt or a caller-written readiness file cannot authorize a launch.

```bash
AGENT_STACK_LIVE_TESTS=1 python3 scripts/showcase_live.py \
  --config /absolute/private-inputs.json --provider hetzner \
  --run-id as7-unique-test-name --batch /absolute/private-batch \
  --revision HEAD --phase plan
```

`plan` is read-only with respect to cloud resources and reserves no money. Use a fresh prefix for `--phase run`; a saved planning attempt cannot be reused as a fresh launch. The run phase collects its own checks and prices. Use the same recorded prefix/batch with `--phase cleanup` for recovery; recovery never replays paid tasks or apply.

The driver runs the local suite at the exported revision. When local Docker is unavailable, the first suitable owned VM runs the actual credential-free container suite before model keys reach acceptance work. Deferral is a pending check, rather than a pass.

## Browser peer and cost controls

Prefer a free existing peer that can run the full browser and SSH checks. The Linux rootless peer uses an isolated network/mount namespace, real Tailscale TUN networking and Chromium with proxying disabled. Its enrollment, native identity, MagicDNS resolution, exact process/source binding and fixed cleanup watcher must verify; host DNS and routes stay intact. Select either `runner.local_peer` or [the SSH peer option](peer-ssh.example.json).

A paid replacement peer requires its own new name, measured reservation and fixed lifetime. Cleanup of a trial preserves an existing peer; peer cleanup separately removes its owned process group, namespace and tailnet resources.

Use the lowest compatible available candidate with fresh native pricing. Hourly prices, disks, public IPs, account adjustments, whole-hour rounding, transfer/minimum reserves and cleanup allowances belong in the complete quote. Monthly prices are caps; deleted resources accrue usage until deletion. An allowance is not a provider bill.

Purchased model credits count once as cash. Actual requests require durable holds, exact model/origin binding, output/request limits, and unchanged global prepaid caps. Haiku 5.5's two prompt-length tiers are bounded at their highest input, output and cache rates, including the documented regional premium. Disable extra reasoning only when its model metadata supports the chosen settings.

After native absence proof, `Reservations.reconcile_cleanup` can tighten an original allowance using its original quote, scope, timestamps and cleanup receipt. It preserves account margins, transfer/minimum reserves and whole-hour rounding, removes completed cleanup contingencies, and retains retry history. Missing or edited proof refuses reconciliation; late deletion can increase the bound.

## Required live evidence

For every cloud, require all of the following before declaring its six cells verified:

- Applied owned deployment and SSH access; exact selected images, releases and source files.
- Both runtime doctors green for OpenClaw and Hermes, plus strict workspace/CLI and host Tailscale checks.
- Independently verified real agent turns and unattended API-key tasks from both coding tools as the workspace user. Installation/version output cannot substitute for actual tasks.
- Direct authenticated OpenClaw and Hermes URLs from a second native tailnet peer, without a proxy or forwarded browser origin.
- Reboot, persisted workspace data/configuration and repeated required doctor/browser checks.
- Cleanup of every owned cloud resource, temporary key and tailnet node, independently checked through the native APIs.

Use the shared [setup skill](../../skills/agent-stack-setup/SKILL.md) and [doctor skill](../../skills/agent-stack-doctor/SKILL.md) for deployment/repair. Prefer the repo-local `bin/` SSH helpers for inspection. The driver stores private plans, inventories, source hashes, task proofs, browser evidence, reservations and cleanup receipts together in the selected batch.

## Recorded cost investigation, 2026-10-07

Confirmed prepaid purchases were $10.30; cloud bills were unreconciled. The earlier $22.24 figure included conservative allowances. Verified cleanup of six failed trials released $3.90 of unused allowances, for an $18.34 ceiling. The first Hetzner trial's native cleanup further released $0.55, for $19.14. These are conservative bounds, not actual cloud charges, and the request for $30 was withdrawn.

The first Hetzner trial built the workspace and ran all three services healthy, but setup exceeded its apply timeout while a 30-second model-catalog command continued for over 15 minutes. A scoped stop was recorded; the trial remained failed and every resource was confirmed deleted. No paid model request ran. The revised wrapper closes interactive input and bounds both the container command and its Docker client, with a forced-stop grace.

The read-only samples showed roughly 2.5 GiB of busiest observed memory use. Native pricing offered an available CX23 with 4 GiB / two CPUs at $0.0104/hour in fsn1, making it a cheaper candidate for the remaining Hetzner attempt. Full task, container, browser and reboot acceptance remains required for that machine.

A broader regional AWS comparison priced all 49 eligible x86_64, non-accelerator, 8 GiB On-Demand candidates after retries. R7a.medium was the lowest fixed full-CPU price found in that set; native availability and quota checks passed, and the fresh VM/disk/IPv4 bound was $0.089413334/hour. One-core build performance and smaller candidates remain separate checks. The selected GCP size was cheapest among supported predefined E2 candidates checked; DigitalOcean's earlier comparison covered all available regional 8 GiB candidates.

AWS, DigitalOcean and GCP used both originally approved attempts. Further trials need explicit approval of the retry count within the existing $25 cap. Hetzner's first attempt used one; all 24 final-revision live cells were still unverified at this checkpoint.
