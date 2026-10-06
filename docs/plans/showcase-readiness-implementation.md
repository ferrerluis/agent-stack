# AgentStack showcase readiness: implementation plan

Status: Approved for implementation and goal-backed work on 2026-10-05. Reviewed 2026-10-05 against repository revision `648522add8c662f8c238c87ef899a7c0b6c8afb7`.

Scope source: [TODO #7](https://drive.google.com/file/d/1hCUJTwh9t1Sao1h2QNiYi6Eel0IbtGW3/view). Companion: [Agent-first test plan](showcase-readiness-tests.md).

This plan uses the current repository and TODO #7 alone. It does not adopt work or dependencies from the older TODOs.

## Desired result and current gaps

| Required result | What exists at the reviewed revision | What remains |
|---|---|---|
| AWS, DigitalOcean, Hetzner deployment | Three Terraform modules, firewall/storage resources, common cloud-init loader, SSH runtime installer, outputs, and mocked tests. | Run live acceptance on each; fix only failures revealed by those runs. Existing code is not live proof. |
| Google Cloud deployment | No Google provider declaration, module, cloud selection, volume discovery, outputs, or mock coverage. `cloud_provider` accepts only the other three clouds. | Add a Compute Engine deployment through the same loader and installer. |
| OpenClaw works and doctor is green | Container health probe, model/channel bootstrap, persistent state, and diagnostics. | Pin a tested image, verify current configuration compatibility, run native doctor and a real agent turn. |
| Hermes works and doctor is green | Official image, `gateway run`, persistent `/opt/data`, provider-key environment, dashboard/API settings. | Seed the selected model/provider in Hermes configuration; add dashboard authentication, readiness, doctor checks, and a real API agent turn. |
| Both coding tools run in workspace | Codex installation, persisted user home, SSH keys, Codex updater, and Drive skill. | Install Claude Code; define auth for both tools and prove actual unattended execution as the workspace user. |
| Tailscale connectivity | Host and sidecar modes; host SSH/workspace access; OpenClaw Serve route. | Exercise access from a second tailnet peer; make doctor mode-aware; provide direct authenticated HTTPS URLs for both apps. |
| Honest pass/fail results | Local test runner; remote diagnostic collector. | Build strict machine-readable checks and an opt-in live runner with ownership, cleanup, and sanitized results. |
| README describes demonstrated behavior | Three-cloud setup, services, Codex, Tailscale, diagnostics and SSH guidance. | Add GCP, Claude Code, runtime-specific setup, exact tests, and a dated result matrix tied to the tested commit. |

Important findings:

- `check_remote_health.sh` and several remote health commands use `|| true`; success of the collector is not evidence of healthy services. A local fake SSH wrapper returning no services reproduced this: both runtimes were reported missing while the collector exited 0.
- The installer waits for OpenClaw and workspace readiness, but not Hermes. Hermes has no Compose healthcheck and receives no equivalent selected-model configuration from the installer.
- Current [Hermes Docker documentation](https://hermes-agent.nousresearch.com/docs/user-guide/docker) requires an authentication provider for its non-loopback dashboard bind; the template supplies none. This is an inferred compatibility fault, not a reproduced live failure.
- Workspace `workspace.env` contains SSH/updater settings, not model credentials. Host or OpenClaw Codex login does not establish workspace Codex login.
- The repo advertises Terraform >=1.5 while its tests use newer mocking syntax. Confirm and document separate deployment/test minimums against the versions actually exercised.
- Meshnet is explicitly disabled by the NordLynx setup. This revision provides no basis for promising Meshnet connectivity.

## Review evidence

- `./scripts/test-local.sh` passed using checksum-verified Terraform 1.16.5 installed only under `/tmp` on this ARM64 review host.
- Terraform init, formatting and validation passed; 44 mocked Terraform tests passed with no failures.
- All 102 setup and 3 doctor Python tests passed; runtime shell syntax and workspace Codex-control Python syntax passed.
- The runner confirmed unchanged Git status and no cloud-impacting commands. Mocked plan success does not establish live deployment behavior.

Docker is absent on this review host, so image build/runtime behavior remains unverified. No cloud resource, deployed host, real credential, or local Codex auth file was inspected.

## Recommended design

- Retain the existing flat Terraform inputs, provider modules, common loader, SSH installer, service selection and storage layout. Add GCP through that structure; a provider-plugin framework or configuration rewrite is unnecessary for this outcome.
- Use one temporary VM per cloud running `openclaw`, `hermes`, and `workspace` together. The same workspace contains both coding tools; this tests coexistence using four deployments rather than requiring eight runtime-specific deployments.
- Use host Tailscale mode for the demonstrated path. Serve OpenClaw on HTTPS 443 and Hermes on HTTPS 8443 using the observed full tailnet DNS name; verify authenticated browser access from another peer without SSH tunnels.
- Use explicitly selected x86_64 VM sizes with at least 8 GiB RAM for the first acceptance run. Select available sizes/regions before costing; report the tested profile, without asserting existing 2 GiB defaults satisfy this combined workload.
- Pin CLI versions and resolve runtime images to immutable digests for acceptance. Keep both coding tools' automatic updates disabled during testing; preserve the existing optional Codex updater behavior.
- Prefer command-scoped OpenAI and Anthropic API credentials for unattended workspace tests. Keep subscription login an explicit documented option, without silently importing personal auth files.

Additional approved decisions:

- Keep this a named showcase example and preserve deployment defaults.
- Limit total cloud/model spend to $25, one fresh-deployment retry per cloud, four hours per attempt, and one deployment at a time. Delete every owned test resource after its tests.
- Use a new AWS VPC in the existing account and dedicated new DigitalOcean, GCP and Hetzner test projects. Continue independent work and other providers when one cloud is blocked.

## Build sequence

### 1. Establish repeatable local validation

Affected files: `versions.tf`, `scripts/test-local.sh`, `TEST.md`, and existing mocked tests.

- Require Terraform >=1.9 for cross-variable schema validation and >=1.11 for plan-time mocked tests; exercise the local suite with Terraform 1.16.5.
- Preserve the credential-free default suite. Add behavioral tests for the new runtime checks, missing services, failed auth, malformed output, timeouts, and cleanup selection; string-presence tests alone cannot prove those behaviors.
- Add an opt-in container test command on a Docker-capable runner to build the rendered workspace and inspect both CLI installations, UID 1000, SSH, permissions, and preserved home state without real model credentials.

Exit: local suite passes; container checks distinguish installation from real model-backed success.

### 2. Complete and verify the shared runtimes

Affected files: `variables.tf`, `main.tf`, `outputs.tf`, runtime Dockerfile/Compose/env/installer templates, and setup/doctor references.

- Add explicit workspace Claude enablement and pinned release inputs; preserve Codex-only workspace compatibility. Install the native Claude binary into the image, expose it to the unprivileged user in interactive and SSH shells, and disable its automatic updates for the demonstrated profile. The [official installer supports explicit versions](https://code.claude.com/docs/en/setup).
- Keep Claude and Codex user configuration/auth separate. Add a test helper that accepts a secret through stdin and sets it only for the intended CLI process; keys must not enter image layers, Docker build arguments, command-line arguments, test output, or a new Terraform credential input.
- Seed Hermes's supported provider/model configuration from deliberate inputs, not its built-in defaults or an interactive setup wizard. Preserve operator-managed configuration on repeat applies and validate credential availability without printing values.
- Configure a generated or operator-supplied Hermes dashboard username/password/session secret for private access. Retain API bearer authentication and replace wildcard browser CORS with an explicit allowlist or disabled browser access.
- Add Hermes readiness using the authenticated API and inspect its readiness fields; HTTP 200 alone is insufficient. Include Hermes in installer readiness/rollback decisions alongside OpenClaw and workspace. [Hermes distinguishes liveness from readiness](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server).
- Verify OpenClaw's existing bootstrap against the pinned image. Disable unconfigured messaging channels in the test profile rather than requiring Telegram/WhatsApp interaction; test a gateway agent turn without delivery to another person.

Exit: rendered configuration and container tests cover both runtimes and coding tools; a pinned, reproducible profile exists for live acceptance.

### 3. Add Google Cloud through the existing deployment path

Affected files: new `modules/gcp/{main,variables,outputs}.tf`, `versions.tf`, root schema/wiring/outputs, both volume-discovery templates, setup guidance and tests.

- Add `cloud_provider = "gcp"`, Google provider registration and mock configuration. Use application-default credentials or workload identity for Terraform; do not add raw service-account JSON to Terraform variables or state.
- Add validated project, region, zone, machine type, image and data-disk-size inputs. Provision a project-scoped network/subnet, tagged SSH/Tailscale firewall rules, VM, public bootstrap address and separate persistent data disk in the same zone.
- Use the shared cloud-init loader via supported instance metadata on an Ubuntu image; keep the runtime upload/install over SSH unchanged. Verify project policies concerning OS Login, metadata SSH keys, service accounts and external IPs before deployment.
- Set a stable data-disk device name and discover `/dev/disk/by-id/google-<device-name>` in both initial loader and runtime mount helper; never guess `/dev/sdb` or format a disk with an existing filesystem. [Google documents stable device symlinks](https://docs.cloud.google.com/compute/docs/disks/disk-symlinks).
- Update instance/volume/IP selection, explicit dependencies, SSH outputs and rendered template inputs. Make inactive GCP provider initialization harmless when another cloud is selected.
- Add mocked GCP module/root/output tests, invalid-input tests, and rendered loader assertions. Preserve the three existing providers and volume-reuse behavior; live tests always use fresh disks.

Exit: all four provider selections pass mocked plans and use the common runtime contract.

### 4. Make doctor results enforceable

Affected files: `check_remote_health.sh`, runtime diagnostics, doctor skill/references/tests, and setup verification guidance.

- Preserve human diagnostic collection; add strict `--strict --json` checks with explicit service selection. Report each required check as pass/fail and return nonzero for a missing, unhealthy, unreachable, timed-out, or unparseable required check.
- Run runtime-native doctor when available in the pinned version, plus AgentStack-owned container/config/readiness checks for both runtimes. For OpenClaw prefer read-only lint/JSON and inspect findings rather than advisory exit status. [OpenClaw documents this distinction](https://docs.openclaw.ai/cli/doctor/lint).
- Support host and sidecar Tailscale detection, actual DNS/IP discovery and reachable services. A missing sidecar is expected in host mode; an online flag alone does not prove connectivity.
- Define applicable checks from the approved profile before the run. Disabled optional services can be reported as not applicable; no required matrix cell can be skipped, waived, or relabeled green to finish.
- Keep observation separate from repair. For fresh test resources, make repairs in repository inputs/templates and redeploy; never leave undocumented remote changes as the final passing configuration.

Exit: deliberate missing-container, failed-readiness, denied-auth, wrong-mode and unreachable-peer fixtures produce failures; healthy fixtures produce all required checks green.

### 5. Implement autonomous live acceptance

Affected files: new `scripts/test-live.sh`, fixture/verifier helpers, examples and `TEST.md`.

- Implement the companion test plan: separate state/checkouts per cloud, unique owned resources, preflighted credentials/permissions, sequential runs, bounded command timeouts and cleanup on failure or interruption.
- Prepare all input choices before execution. One authorized batch can cover provisioning, SSH, tests, source repairs, narrowly affected reruns and deletion of this batch's temporary resources within an approved spend/retry limit.
- Mock operator prompts and provider actions when testing the runner locally. Real matrix evidence must use real Terraform infrastructure, tailnet access and model-backed commands.
- The private input/reservation policy and read-only preflight are implemented. The preflight retrieves selected 1Password fields once, checks API identity/project/VM size and selected quota/permission/inventory results, and lists remaining gates with `launch_ready: false`. It makes no deployment or model call; Hetzner account/project mapping requires separate proof. Deployment/test execution and independent cleanup adapters remain pending.
- Record 24 required matrix cells, exact commit, non-secret configuration, image digests/CLI versions, checks, durations and cleanup outcomes. Retain sanitized summaries; keep state, plans with secrets, private keys and raw sensitive logs outside PARA and version control.

Exit: all cells pass on the same final implementation; cleanup is confirmed for all owned resources and temporary tailnet nodes.

### 6. Finish the README and handoff

- Add GCP setup and the demonstrated resource profiles, both runtimes, both coding tools, auth modes, host Tailscale access, diagnostics, exact test commands and cleanup procedure.
- Replace unproved “no manual steps” and fixed bootstrap-time claims with behavior demonstrated by the approved profile. Separate supported code from live-tested configurations and name any remaining limitations.
- Publish a dated matrix tied to the tested revision and versions. Mark TODO #7 complete only after both plans, all 24 cells, README checks and cleanup evidence satisfy acceptance.

## Required execution inputs and preflight

Luis authorized implementation and goal creation on 2026-10-05 while gathering these inputs. Each provider's live runs remain gated until its inputs and permissions are checked.

| Provider | Non-secret identifiers and credential references |
|---|---|
| AWS | Existing account ID, region, IAM role/profile or 1Password reference; permission to create, inspect and remove a new VPC, VM, data disk and related networking. |
| DigitalOcean | Account/team, dedicated new test project or project-creation authority, scoped API-token reference for project and resource lifecycle. |
| GCP | Dedicated project or parent organization/folder and project-creation authority, billing account/link authority, region/zone and ADC identity; enabled Compute API, quota and lifecycle permissions. |
| Hetzner | Dedicated new test project and its project-scoped read/write API-token reference. |

Shared prerequisites: OpenAI and Anthropic API-key references and accessible model IDs; an authorized 1Password session; Tailscale enrollment/test-peer access, HTTPS availability and owned-node cleanup authority. Select and pin runtime digests and CLI versions before the matrix.

Verify identity, permissions, billing, quota, model access and tailnet prerequisites before provisioning. Check actual regional VM/disk/IP/model prices before each attempt; stop new spend if the remaining $25 allowance cannot cover the bounded attempt and cleanup. Keep the run configuration private and record references, never credentials, in planning artifacts.

Retrieve passwords through `op` immediately before use without printing or saving them. Terraform state remains private because existing runtime credentials enter it. Continue independent work while inputs are unavailable; ask before expanding spend/retry limits, changing scope or affecting existing resources/data.

## Defined goal

Use [define-goal](/home/ferrerluis/repos/codex-config/skills/define-goal/SKILL.md). A matching full goal was created on 2026-10-05; reuse it rather than creating duplicates. No token budget was requested.

> Make AgentStack satisfy TODO #7: pass local and container suites, then prove all 24 required matrix cells at the final revision across AWS, DigitalOcean, Google Cloud and Hetzner with both runtimes and both workspace coding tools. Require both runtime doctors green, real independently verified API-key CLI tasks as the workspace user, direct authenticated Tailscale URLs verified from another peer, reboot/persistence checks, an evidence-matched README, saved and linked plans, and confirmed cleanup. Stay within $25 total cloud/model spend, one fresh-deployment retry per cloud and four hours per attempt; continue independent implementation while credentials/accounts are unavailable and ask before exceeding limits or affecting existing resources.

Starting implementation does not complete the goal or TODO #7. Both remain incomplete until real acceptance and cleanup pass.

## Canonical repository evidence

All links below point to the reviewed revision, not a moving branch.

- [Provider/schema](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/variables.tf), [root provisioning](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/main.tf), [outputs](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/outputs.tf).
- [Compose/runtime definitions](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/modules/common/templates/runtime/docker-compose.yml.tpl), [workspace image](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/modules/common/templates/runtime/workspace.Dockerfile.tpl), [installer/readiness](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/modules/common/templates/runtime/install-agent-stack.sh.tpl).
- [Remote collector](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/skills/agent-stack-doctor/scripts/check_remote_health.sh), [test contract](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/TEST.md), [README](https://github.com/ferrerluis/agent-stack/blob/648522add8c662f8c238c87ef899a7c0b6c8afb7/README.md).
