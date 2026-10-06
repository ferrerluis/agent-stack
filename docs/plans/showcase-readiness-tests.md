# AgentStack showcase readiness: agent-first test plan

Status: Approved for implementation on 2026-10-05; live runs remain gated on checked execution inputs. Based on revision `648522add8c662f8c238c87ef899a7c0b6c8afb7`, reviewed 2026-10-05.

Scope: [TODO #7](https://drive.google.com/file/d/1hCUJTwh9t1Sao1h2QNiYi6Eel0IbtGW3/view). Build design: [Implementation plan](showcase-readiness-implementation.md).

Commands explicitly marked proposed describe interfaces to implement; they are not available in this revision. Runtime CLI flags must be checked against the pinned image/tool versions before live use.

## Required matrix

| Cloud | Terraform launch + SSH | OpenClaw + doctor | Hermes + doctor | Claude Code execution | Codex execution | Tailscale access |
|---|---|---|---|---|---|---|
| AWS | Required | Required | Required | Required | Required | Required |
| DigitalOcean | Required | Required | Required | Required | Required | Required |
| Google Cloud | Required | Required | Required | Required | Required | Required |
| Hetzner | Required | Required | Required | Required | Required | Required |

Success requires 24/24 cells passing. Use one VM per cloud with both runtimes together and one workspace with both coding tools; record that mode explicitly rather than implying separate runtime deployments were tested.

## Inputs collected once before the batch

- Explicit cloud account/project and region/zone; scoped credentials sufficient to create, inspect and remove this run's resources. GCP needs enabled Compute Engine API, quota and compatible organization policy; do not silently enable billing or broaden IAM.
- Approved limits: $25 total cloud/model spend, one fresh-deployment retry per cloud, four hours per attempt, and sequential deployments. Estimate actual regional VM/disk/IP/model costs before each attempt; stop new spend when the remaining allowance cannot cover it and cleanup.
- An already-authorized `op` session for passwords/secrets, plus secret references rather than values. Missing/locked 1Password stops dependent secret operations.
- Explicit pinned OpenClaw/Hermes image digests and Codex/Claude versions, supported model IDs and model credentials. API-key CLI authentication is the selected acceptance mode.
- Tailscale auth for the VM and an existing authorized runner peer or a separately authorized temporary test peer. Provide a cleanup path for newly enrolled nodes; no desktop enrollment prompt may be a hidden test step.
- A runner with the supported Terraform binary, Docker/Compose for container checks, SSH, Python and Tailscale peer access. Container checks may use the first owned VM if local Docker is absent. The first profile uses x86_64 cloud VMs with at least 8 GiB RAM, host Tailscale, no VPN, no public domains and no unconfigured messaging channels.

No credentials need to be collected during this planning review. After preflight, agents run every ordinary test step and report the completed result to Luis.

## Execution input gate

Complete the provider checklist in the implementation plan, including AWS identity/region, DigitalOcean team/project/token, GCP parent/project/billing/identity, and Hetzner project/token. Check permissions, billing, quota, model access and tailnet access before launching a provider. AWS uses a fresh VPC in the existing account; the other clouds use dedicated new projects. Continue other work if one provider is blocked.

The bounded batch is authorized; ordinary test steps need no additional approval. Keep existing resources and data out of the run. Record only references and non-secret identifiers in input checklists; actual secret values stay outside PARA and Git.

## Local tests before any cloud run

Existing command:

```bash
./scripts/test-local.sh
```

Required: init, format, validation, mocked Terraform tests, setup/doctor tests, shell/Python syntax and repository-idempotence checks all pass. No live APIs, deployed-host SSH, personal auth-file reads or resource creation are allowed in this tier.

Add coverage for GCP selection/module/output/volume wiring, inactive-provider initialization, four-cloud input parity where shared inputs are forwarded, Claude installation/auth boundaries, Hermes configuration/readiness, and mode-aware strict doctor checks.

Implemented container command (requires Docker and exact pins):

```bash
./scripts/test-containers.sh --profile showcase \
  --codex-release <x.y.z> --claude-release <x.y.z> --platform linux/amd64
```

Build rendered templates with fake inputs, verify CLI paths/versions under UID 1000, confirm SSH public-key access and persistent home ownership, and test missing-binary/failing-readiness cases. Check legacy Codex-only workspaces still start; container checks must not claim real model execution when using mocks.

The implemented command covers workspace SSH, versions, persistent data/host keys, missing-binary/password failures and ownership-checked cleanup. Native agent image/readiness checks and real model execution remain in the live tier; no container run has passed on the current host because Docker is absent.

Private input structure and approved limits can be checked per provider with `python3 scripts/showcase_policy.py --config <mode-0600-inputs.json> --provider <provider>`. This retrieves no secret and performs no live preflight. The reservation/fresh-plan library, opt-in read-only preflight and inventory/cleanup verifier are implemented; deployment/test execution and resource/node deletion remain pending.

After installing the optional task-local dependencies, run `AGENT_STACK_LIVE_TESTS=1 python3 scripts/showcase_preflight.py --config <mode-0600-inputs.json> --provider <provider> --run-id <as7-unique-id>`. It retrieves selected 1Password fields once, checks cloud identity/project/VM size and selected permissions/quotas, inventories project/prefix resources and finds the registered authorized second peer. Tests use generated signing keys, paginated fixtures and the actual offline AWS SDK without live secrets or requests.

The report always keeps `launch_ready: false` and lists unresolved checks; registered peer status does not prove browser connectivity, and price/model/deletion checks remain pending. Hetzner's project-scoped token cannot return account/project IDs, so that mapping requires separate evidence. Adding a later provider's inputs or repairing pins must preserve spending/retry history; each provider's recorded account/project/region/zone must remain unchanged for retries and cleanup.

## Proposed live-run interface and resource ownership

```bash
AGENT_STACK_LIVE_TESTS=1 ./scripts/test-live.sh \
  --provider aws --profile showcase --run-id <unique-id> \
  --config <private-run-config>
```

Run providers sequentially. The private run configuration records approved accounts/projects, spend/lifetime/retries, credential references and permission to remove this run's resources; it contains no plaintext passwords and is not stored in PARA or committed.

- Copy the exact tested source into a fresh private checkout per provider; never copy existing state, tfvars, SSH keys or auth. Use one state per cloud and a unique project/resource prefix.
- Force fresh data disks; reject all existing-volume inputs. Generate a test-only SSH key, restrict public bootstrap SSH to the runner's allowed CIDR, and keep workspace/application ports out of public cloud firewall rules.
- Tag/label resources with project, run ID, owner and expiry where supported. Track complete owned IDs in a private run record, including networks, disks, addresses, firewalls, SSH keys and temporary tailnet nodes.
- Obtain passwords through `op` immediately before their use without printing or saving them. Inject coding-tool model keys through stdin into a trusted helper for the single invocation; use no CLI secret arguments, shell tracing or build-time secrets.
- Use the existing repo SSH wrapper from the isolated checkout. Extend it narrowly for noninteractive test use and per-run known-hosts files; establish and validate first-contact trust, then reject changed host keys.

## Cases executed on every cloud

| ID | Action and command contract | Pass rule |
|---|---|---|
| L1 | In isolated checkout: `terraform init`, saved `terraform plan -out=<private-plan>`, then `terraform apply -input=false <private-plan>`. | Plan contains only owned temporary resources in the approved account; apply succeeds. Raw plan/state/output JSON remain private. |
| L2 | `bin/agent-stack-ssh -- <bounded loader/systemd/mount probes>`. | SSH uses keys without prompts; cloud-init completed; loader marker names the selected provider; expected data disk mounted at `/opt/agent-stack/data`; installer marker matches the rendered artifact; service active. |
| R1 | Compose inspect plus OpenClaw gateway health, native read-only doctor, and a gateway agent turn. | Healthy container; no required doctor findings; actual selected model produces a valid response to a fresh arithmetic/nonce challenge. |
| R2 | Compose inspect, Hermes authenticated readiness, AgentStack/native doctor where supported, and an API agent turn. | Healthy runtime/config/permissions; all applicable doctor checks green; API model inference returns the expected challenge answer; dashboard auth works. |
| W1 | Run the Codex command below as workspace UID 1000 against a fresh fixture. | Exit 0 before timeout; independently verified file has correct sum and nonce; real model execution recorded. |
| W2 | Run the Claude command below as the same workspace user against a different fresh fixture. | Same functional pass rule; credentials/settings from one tool do not overwrite the other. |
| N1 | From a second peer: `tailscale ping <observed-target>`; host/workspace SSH using actual tailnet address; authenticated browser checks at `https://<observed-dns>` and `https://<observed-dns>:8443`. | Successful peer traffic and key-authenticated SSH; both authenticated apps load at direct HTTPS URLs without SSH tunnels. Online status or a login page alone cannot pass. |
| P1 | Reboot the temporary VM and repeat loader/mount/runtime doctor and tailnet probes; verify workspace output fixtures remain. | Same mounted disk, healthy services and reachable tailnet; persisted user home and tool state survive. |
| D1 | Run documented README steps/commands and compare their results with recorded configuration/versions. | No hidden interactive prerequisites; every promised behavior has supporting evidence. |
| C1 | Destroy only this run's isolated state and remove enrolled temporary peers; independently query owned IDs. | No owned resources/nodes remain; cleanup result explicitly recorded. A cleanup failure fails the run. |

L1/L2 satisfy the launch/SSH matrix column; R1/R2 satisfy the runtime columns; W1/W2 satisfy the coding-tool columns; N1 satisfies Tailscale. P1, D1 and C1 are supporting acceptance checks required for a trustworthy handoff.

## Exact runtime checks to implement

Implemented strict AgentStack checker:

```bash
skills/agent-stack-doctor/scripts/check_remote_health.sh \
  --strict --json --services openclaw,hermes,workspace \
  --require-claude --check-tailscale --tailscale-mode host \
  --ssh-wrapper <isolated-checkout>/bin/agent-stack-ssh
```

Require explicit passing checks and nonzero status on failure, timeout or invalid JSON. The human collection mode remains advisory.

For a pinned OpenClaw version supporting current [doctor lint](https://docs.openclaw.ai/cli/doctor/lint), execute `openclaw doctor --lint --json --severity-min warning` and inspect findings/check coverage, plus `openclaw health --json`. Execute a bounded `openclaw agent --agent main --message <fresh-challenge> --json` through the running gateway, without delivery flags; confirm flags with the pinned version's help and [agent CLI documentation](https://docs.openclaw.ai/agent-send).

For Hermes, query authenticated `/health/detailed` and inspect readiness fields, not just HTTP status; send a non-streaming `/v1/chat/completions` request through the running API with the generated bearer key supplied privately. Its current native `hermes doctor` has no JSON flag: the implemented checker requires zero exit, expected checked sections and a complete passing summary, without `--fix` or optional `--live` probes. Unsupported pinned formats fail until inspected; native doctor API connectivity calls belong in the model allowance. [Hermes API contract](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server), [native doctor](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/doctor.py).

The Hermes check set includes config/model/credential availability, state-directory ownership, running gateway, readiness, usable authenticated dashboard/API and a model turn. Auth rejection is expected only in a deliberate negative test; it cannot be used as service-readiness proof.

## Real workspace tasks

For each tool, create a separate empty fixture directory beneath the workspace user's persisted home with `input.json` containing two random small integers and a nonce not disclosed in the prompt. Use a trusted verifier to require `result.json` with exactly the expected sum and nonce; the runner never writes that result file itself.

Run using Compose exec with explicit workspace user, HOME, PATH and working directory, no TTY, stdin closed after private credential injection, and a 180-second timeout. Resolve `codex` through the existing workspace PATH so the installed fallback/updater path is exercised.

Initial Codex command contract:

```bash
codex exec --sandbox workspace-write --skip-git-repo-check \
  --json --output-last-message final.txt \
  'Read input.json. Write result.json with the sum of lhs and rhs and the unchanged nonce. Then report completion.'
```

Set approval behavior to never prompt through supported configuration for the pinned version and require that its sandbox works; do not bypass it to conceal a container incompatibility. The credential helper sets `CODEX_API_KEY` only for this invocation, as described in [official noninteractive guidance](https://learn.chatgpt.com/docs/non-interactive-mode).

Initial Claude command contract:

```bash
claude -p --permission-mode dontAsk --tools 'Read,Write,Edit' \
  --allowedTools 'Read' 'Write' 'Edit' --output-format json \
  'Read input.json. Write result.json with the sum of lhs and rhs and the unchanged nonce. Then report completion.'
```

Set `ANTHROPIC_API_KEY` only for this invocation, with an explicit supported model if account defaults are ambiguous. [Claude documents print-mode flags/tool permissions](https://code.claude.com/docs/en/cli-reference) and [API-key authentication](https://code.claude.com/docs/en/authentication); confirm these against the selected pinned release before running.

Also require no CLI error/permission-denied result and independently inspect the output file as the workspace user. A version string, login-status message, simulated reply, or an agent's unverified “done” does not pass W1/W2.

## Failure, repair and cleanup rules

- Bound apply/bootstrap, SSH, doctor and model commands separately; proposed maximums are 30 minutes for apply, 5 minutes for readiness, 10 seconds per SSH connection and 180 seconds per model task. Enforce the separately approved total lifetime/spend envelope.
- On failure, retain sanitized evidence, diagnose and make the smallest repository fix. Rerun affected local tests and the failed live cases; if shared runtime/template code changes, revalidate affected cells on every cloud at the final version.
- Handle EXIT/INT/TERM with cleanup; support cleanup resumption after runner crashes. Preserve private state until independent deletion verification succeeds; never delete the only recovery record when cleanup fails.
- Cleanup requires both Terraform-state removal and provider-side confirmation of owned IDs, plus temporary Tailscale-node deletion. Failure or uncertain ownership is escalated; do not delete existing or unowned resources.
- The read-only `scripts/showcase_cleanup.py` component captures IDs before deletion and verifies them afterward against provider/tailnet inventories and a private empty local state. It includes implicit boot disks, default VPC children and primary IPs; recapture cannot discard old IDs. Read failures, renamed/orphan resources, old prefix collisions and runner-peer capture fail. The actual deletion/controller path and Hetzner mapping proof are still pending; fixture/offline-SDK tests do not prove live cleanup.
- A quota/auth/policy problem is blocked or failed evidence, never a pass. Continue independent implementation work; involve Luis for missing authorization, secrets, unavailable account privileges or an expanded budget/scope, not routine test prompts.

## Acceptance record

Record commit, non-secret profile, provider/region/VM size, image digests, CLI/model versions, deployment mode, check IDs/status, functional verifier results, tailnet probes, timings, and cleanup outcome. Save only sanitized evidence in the repo/Drive; keep raw logs containing potential secrets private and temporary.

All 24 matrix cells and supporting P1/D1/C1 checks must pass before the full goal or TODO #7 is complete. Documentation/local tests are useful milestones, but cannot substitute for the live matrix.
