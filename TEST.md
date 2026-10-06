# AgentStack Test Contract

This project has three test tiers. The default tier must be safe for local agents:
it must not create, mutate, inspect, or destroy live cloud resources.

## Default Local Suite

Deployment requires Terraform >=1.9 for cross-variable validation. The local test suite requires >=1.11 for plan-time overrides and has been exercised with 1.16.5. Run from the repo root:

```bash
./scripts/test-local.sh
```

The runner copies tracked and untracked non-ignored files to a temporary worktree
under `/tmp/agent-stack-tests`, then runs all checks there. This keeps `.terraform/`,
provider lock files, generated SSH config, `__pycache__`, and other command side
effects out of the real checkout.

Default-suite commands:

- `terraform init -backend=false`
- `terraform fmt -check -recursive`
- `terraform validate`
- `terraform test`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s skills/agent-stack-setup/tests`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s skills/agent-stack-doctor/tests`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests`
- `bash -n` over runtime shell templates
- before/after `git status --short --untracked-files=all` comparison

Forbidden in the default suite:

- `terraform apply`, `terraform destroy`, `terraform import`, `terraform taint`, or Terraform state mutation
- provider-backed `terraform plan` outside Terraform's mocked test framework
- cloud CLIs or APIs such as `aws`, `doctl`, `hcloud`, `gcloud`, or direct provider API calls
- SSH to deployed instances
- provider migration against real hosts
- inspecting, importing, printing, or summarizing real `~/.codex/auth.json`
- printing secrets, Terraform state, `terraform.tfvars`, `*.tfstate`, `#token=` URLs, or provider tokens

Allowed side effects:

- temporary files under `/tmp/agent-stack-tests`
- Terraform plugin/cache data inside the temporary test worktree
- no changes to repo-tracked or untracked source files after the runner exits

## Mocked Terraform Suite

The default runner executes native Terraform tests from `tests/*.tftest.hcl`.
These tests use `command = plan`, `mock_provider`, mock data files under
`tests/mocks/`, and `expect_failures` for validation cases.

Coverage expectations:

- AWS, DigitalOcean, Hetzner and GCP root provider selection all plan without credentials
- provider child modules plan with mocked providers and stable fake IDs/IPs
- root outputs cover private Tailscale URLs, public-domain URLs, disabled services, and token-import URLs with explicit test tokens
- validation failures cover invalid cloud provider, root SSH user, missing Tailscale key, missing public-domain values, missing external Postgres values, DigitalOcean existing-volume name requirements, invalid model providers, empty default model, and invalid enabled services
- tests set `ssh_public_key`, `repo_ssh_private_key_path`, and `generate_repo_ssh_config = false` so no repo-local key generation or SSH config writes occur

Do not replace mocked `terraform test` coverage with live provider `terraform plan`.
The whole point of this tier is to catch graph, variable, output, and module-wiring
breakage without credentials or cloud APIs.

## Python And Template Suite

Python unit tests cover local helper behavior and static contracts that Terraform
cannot inspect well:

- `render_tfvars.py` output, placeholder rejection, merge behavior, and defaults from `variables.tf`
- Codex auth import behavior against temp fixtures only
- DigitalOcean volume selection
- provider migration helper help text and safety invariants
- cloud-init loader scope, runtime template contents, private staging permissions, layout migration, and Terraform runtime provisioning contracts
- module input parity across AWS, DigitalOcean, and Hetzner shared runtime inputs
- Hermes config preservation, authenticated detailed readiness, installer readiness and safe rollback contracts
- OpenClaw first-start gateway auth, private file ownership, preservation and rollback
- workspace coding-task verification and command-scoped secret handling, using fake CLI results only
- observed tailnet DNS collection without fabricated URLs
- strict doctor failure behavior for absent services, empty/malformed/incomplete reports, failed SSH, timeouts and invalid/private routes
- doctor scripts and legacy compatibility checks
- private input references, new-resource plan restrictions, concurrent spending reservations, retry limits and independent cleanup receipts

Keep these tests local-only. They may create temporary files, but they must not
read real Codex auth, Terraform state, provider tokens, or cloud resources.

## Optional Container Suite

With Docker, Terraform, SSH, OpenSSL and exact CLI versions available:

```bash
./scripts/test-containers.sh --profile showcase \
  --codex-release <x.y.z> --claude-release <x.y.z> --platform linux/amd64
```

This renders the actual workspace templates without providers, builds both legacy Codex-only and combined images, and verifies pinned CLI execution over SSH as UID 1000. It checks persistent home data and host keys across restart/recreation, deliberate missing CLI/password failures, and deletion of uniquely labeled test containers/images/volumes.

It uses a fake plaintext password for the legacy workspace, a salted hash of a fake password for the combined workspace, and a generated test-only SSH key. It retrieves no model credentials and explicitly reports model execution as untested. Missing Docker or unsuccessful cleanup fails the command. Base images/build cache may remain; the command does not prune shared Docker resources.

The unit tests for this command cover its failure and ownership rules. They do not substitute for an actual image build or container run.

The [owned-VM probe](scripts/showcase_container_probe.py) uses both workspace contexts rendered locally from the checked commit, so the VM needs no Terraform or cloud/model credentials for these tests. It builds and tests the same legacy/combined profiles with fake passwords and generated test keys, checks cleanup independently, and binds selected proof to the source, pins, request and boot.

The VM probe requires a 3400-second controller window, bounds profile work to 2700 seconds and cleanup to 240 seconds, and arms a 3000-second systemd timer before Docker work. The timer targets only fresh expected resource names with the exact test label; name/unit collisions, failed listings, incomplete cleanup or unverified timer disarming cannot pass, and uncertain failure retains its cleanup program.

An attempt permits one probe invocation, and shortened deadlines or a changed boot/bundle invalidate its result. Thirteen probe tests and twenty-five coordinator/integration tests include real local template rendering, while Docker, systemd, SSH, cloud and model operations use fixtures; no actual container build or VM timer has run.

## Private Execution Inputs

Use [the reference-only example](examples/showcase/execution-inputs.example.json) to prepare a mode-0600 file outside Git. Validate one ready provider without retrieving secrets or contacting cloud/model APIs:

```bash
python3 scripts/showcase_policy.py --config <private-inputs.json> --provider aws
```

`inputs_complete` means the required fields and approved limits are present. Identity, project isolation, permissions, prices, model availability, minimum VM memory and second-peer access still require live preflight; none is established by an input declaration.

The runner library conservatively reserves the full attempt allowance, blocks concurrent launches and further attempts until independently queried cleanup succeeds, and allows at most two attempts per cloud. Later provider inputs and repaired pins retain prior spending/retry history; each provider stays bound to its original account/project/region/zone.

New reservations also retain the original quote and its hash, with an exact model-dollar allocation. The internal [model-request gate](scripts/showcase_model_spend.py) atomically records each worst-case hold in the same private locked ledger before retrieving a key or making one fixed-origin HTTP POST. It bounds request counts per role and generated tokens, charges the entire supported context at the supplied upper rate including cache writes, rejects aliases/premium routes/built-in paid tools/multimodal or stored-context requests, and never refunds a hold after errors, interruption or success. Request hashes and selected status enter the ledger; keys, prompts and raw provider responses do not. Restart, duplicate bodies, changed allocations/policies, cleanup races and expired work refuse further requests. Existing schema-3 rows remain valid for cleanup; absent model allocations cannot be inferred or reset.

The internal [model terms reader](scripts/showcase_model_prices.py) now derives whole-context and text/cache prices from fixed official pages and selected authenticated model GETs, without generation. It preserves canonical dated or dateless IDs, refuses moving aliases and unsupported billing contracts, includes documented long-context/regional premiums, and checks Claude's published limits against the API. Five-minute receipts bind the selected roles, original source, scope and binary plan; the internal saved-plan method writes mode-0600 `model-prices.json` and reuses keys only in memory. A policy conversion preserves the whole-context bound and checks explicit role request/output limits. The public planning command does not yet call this component.

Actual public HTML from OpenAI's current model page and Claude's pricing/model/versioning pages passed the parser on 2026-10-06; authenticated metadata, keys and upstream requests remain fixtures. Older or unrecognized model-card formats fail until explicitly supported. Credential-isolating gateway lifecycle, routing for both runtimes and coding tools, account adjustments and actual pinned protocol verification remain required. OpenAI's [hard spend limits](https://developers.openai.com/api/docs/guides/spend-limits) allow slight overspend during propagation, and Claude's [local cost figures](https://code.claude.com/docs/en/costs) are estimates; neither alone proves the approved ceiling. No model launch gate passes and the public command still has no paid run phase.

A new config or ledger must not restart the same batch's spending allowance, and schema 3 refuses older incomplete-scope ledgers without replacing them. Saved-plan execution, guarded deletion and the internal acceptance coordinator are implemented; production launch-gate measurements and the paid CLI phase remain in development.

## Private Attempt Preparation

With Git and `ssh-keygen` available, prepare one exact committed revision without secrets, live API requests or spending reservations:

```bash
python3 scripts/showcase_attempt.py --config <mode-0600-private-inputs.json> \
  --provider aws --run-id <as7-unique-id> \
  --batch <mode-0700-private-batch-directory> --revision <full-commit-sha>
```

This exports committed source into `<batch>/attempts/<run-id>/repo`, creates a fresh test SSH key, saves non-secret showcase settings and records source/settings/key hashes. It never copies the operator's uncommitted changes, tfvars, state, keys or auth. Tracked state/auth files, links, changed showcase access settings and an existing run directory are refused. Partial preparation retains its private record for recovery; `prepared` always reports `launch_ready: false`.

The preparation library verifies the selected revision/files, settings, provider binding and generated key before later commands. Added overrides, changed source/settings or swapped keys fail. Its child environment discards inherited credentials, proxies, SSH agents and Terraform command overrides; bounded subprocesses kill child processes on timeout. Expiry prevents new work but never prevents bounded cleanup recovery.

For workspace password authentication, the saved-plan driver uses the stdin-only hash helper for the single authorized `op` read immediately before hashing, then passes only `TF_VAR_workspace_password_hash` in memory. Supply no plaintext `workspace_password` to Terraform in the showcase. The hash remains sensitive in state/runtime files; the workspace accepts either the legacy password or the hash, rejects both together, and removes the password/hash environment before starting SSH. Cleanup reuses the private hash without retrieving the original password or model/enrollment keys. This path has been tested with fake secrets only.

## Saved Plans And Guarded Recovery

With the selected provider inputs, task-local live dependencies and an authorized `op` session available:

```bash
AGENT_STACK_LIVE_TESTS=1 ./scripts/test-live.sh --phase plan \
  --provider aws --profile showcase --run-id <as7-unique-id> \
  --config <mode-0600-private-inputs.json> --batch <private-batch-directory> \
  --revision <full-commit-sha>
```

Planning prepares and verifies exact committed source, initializes isolated Terraform data, runs read preflight, injects selected secrets in memory and saves a private binary plan plus checked JSON. It creates no VM, makes no model call and reserves no spend. Plans/state contain sensitive values; keep the entire batch outside Git with mode 0700. `planned` still reports `launch_ready: false`.

DigitalOcean planning also saves mode-0600 `resource-prices.json` and reports selected resource unit prices. The [reader](scripts/showcase_prices.py) supports bundled CPU sizes and checks the exact saved plan, loaded source, region, project, disk quantity, disabled backups and fresh API size availability. It reads the authenticated [sizes endpoint](https://docs.digitalocean.com/reference/api/reference/sizes/) plus fixed public [volume](https://www.digitalocean.com/pricing/volumes) and [Droplet billing](https://docs.digitalocean.com/products/droplets/details/pricing/) pages without redirects/proxies; page/request hashes and the original commit/binary-plan hash bind the receipt. Rates round upward and never assume access to the team's pooled transfer allowance.

AWS planning also uses a [resource-unit-price reader](scripts/showcase_aws_prices.py) with the fixed official [Price List Query API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-price-list-query-api.html). The original saved plan binds account/region/size/AMI, both gp3 disk quantities and one public IPv4 address; an EC2 image read requires Canonical's standard x86 Ubuntu image without marketplace product codes. Bounded queries require a single plain USD On-Demand product and rate per selection, reject changed scope/tiers/future terms, and hash selected source records into a private receipt. Monthly disk rates round upward using 672 hours; known extra disk performance is refused and burstable CPU-credit costs stay pending. No inherited personal profile, proxy, endpoint or SDK retry is accepted.

GCP planning uses an [E2 reader](scripts/showcase_gcp_prices.py) and the fixed official [Billing Catalog API](https://docs.cloud.google.com/billing/docs/reference/rest/v1/services.skus/list). It checks the original project/region/zone/machine type, boot/data balanced-disk quantities and one ephemeral external IP. Machine and standard Ubuntu image reads verify x86 CPU/RAM and selected image/license fields; custom/shared-core/other machine families, extra disks, GPUs, Spot settings and foreign disk/network references fail. All bounded catalog pages are checked for missing/duplicate/ambiguous selected SKUs, scope, latest USD rates, units and conversions. The highest published tier charges every unit without assuming account-level free usage, and monthly disk rates round upward using 672 hours. Selected SKU/page/image/machine hashes enter a private receipt; no new credential reference, ADC discovery or API enablement is performed. The selected project must already support authenticated catalog reads.

Hetzner planning uses a [price reader](scripts/showcase_hetzner_prices.py) for the original selected VM, bundled boot disk, data-volume quantity, one Primary IPv4 and free Primary IPv6. It preserves the API's EUR or USD currency and uses the greater of published gross prices or net prices plus VAT. The current [Cloud API schema](https://docs.hetzner.cloud/reference/cloud) supplies flat location IDs/names/availability and per-location deprecation; both type and standard x86 Ubuntu image must remain available for the full four-hour attempt from read completion. Fixed public [billing](https://docs.hetzner.com/cloud/billing/faq/) and [volume](https://docs.hetzner.com/cloud/volumes/overview/) pages verify whole-hour server billing, free IPv6 and hourly volume billing. Selected API/document hashes bind the private receipt; backups, extra VM features, foreign attachments and ambiguous prices fail. Monthly prices divide by 672 conservatively, and no included traffic is credited.

These are partial unit prices: `complete_quote: false` and `launch_gate: unverified` remain explicit. Model request costs, traffic quantity and any missing traffic rates/units, cleanup deadline enforcement, disk/network/scheduling defaults, CPU credits where applicable, resolved-image plan binding, account adjustments/taxes and remaining resource charges still need measured bounds. Hetzner additionally needs verified account/project-token mapping and automatic Primary IP creation defaults; EUR quotes need a USD spend bound including conversion fees. Unsupported plan/size/page/API contracts fail before launch. No self-declared quote or readiness override is accepted by the public command.

The library applies only that saved plan after measured readiness and an active reservation in the original ledger, rechecking source/settings/key/plan/provider-lock hashes and allowing one apply invocation. Deadlines bound child processes, interrupted creation remains uncertain, and GCP tokens refresh only when they cover the whole command.

When local Docker is absent, the approved first-owned-VM path can record `container_tests: deferred_owned_vm` with a source/pin-bound rendered bundle; every other launch gate must still pass. Deferred container checks remain pending and block native doctors, model tasks, peer checks and reboot until the owned VM produces verified container proof; the public CLI still exposes no launch phase or readiness override.

Recover a recorded active attempt in that same private batch:

```bash
AGENT_STACK_LIVE_TESTS=1 ./scripts/test-live.sh --phase cleanup \
  --provider aws --profile showcase --run-id <original-as7-id> \
  --config <mode-0600-private-inputs.json> --batch <original-private-batch-directory>
```

Before destruction, the driver captures cloud/node IDs, matches state resources and attachments to the original plan and observed inventory, and checks a saved destroy plan permits deletion only. It then applies that exact destroy plan, requires empty state, removes only previously captured owned node IDs using the [Tailscale device API](https://tailscale.com/docs/features/access-control/device-management/how-to/remove), and independently checks cloud/node absence. The runner peer is preserved. Failed queries, unplanned resources, foreign attachments, changed plans or leftover resources keep cleanup unverified and block another launch.

An expired reservation whose durable controller record proves apply was never invoked can be closed only after two independent absence inventories and confirmation that no state appeared; it receives `never_applied_verified_absent` proof without inventing an empty state file. Applied attempts with missing state retain captured IDs and require recovery. Destroyed attempts use `destroyed_empty_local_state` proof. Both retain their full spending reservation and attempt count.

Cleanup accepts repaired credential references while preserving the original source/pins, accounts/projects/regions/zones, tailnet and limits. Hetzner still needs separate verified account/project-token mapping; its apply and cleanup paths refuse declared-only identity. Orphan deletion outside Terraform state is not implemented. Tests exercise the entrypoint and plan/apply/destroy/receipt flow with fake commands/provider responses only; no real launch or deletion has been verified.

## Remote Acceptance Component

The [internal coordinator](scripts/showcase_controller.py) sequences one reserved saved-plan apply, bootstrap, required container checks, strict doctors, both runtime turns, both coding tasks, peer access, reboot and fresh peer access. Loaded controller sources must match the prepared commit, and a durable per-run record prevents replay after interruption.

Cleanup runs after success, partial apply, test failure and ordinary interruption; no cell is published as passing unless acceptance and independent cleanup both succeed. The existing `--phase cleanup` command also recovers coordinator records, restoring completed acceptance only when its saved evidence and every step receipt remain unchanged, without repeating paid work.

Production price/model/permission/quota/peer/cleanup measurements, a paid public run phase, Hetzner mapping proof and orphan recovery remain unfinished. The coordinator has fixture integration coverage only; every real matrix cell remains unverified.

`scripts/showcase_remote.py` implements controller checks for an active, applied, exact-source attempt in the original spending ledger. It reuses the private key/known-hosts file and repo SSH wrapper, matches owned state to recorded inventory, then re-queries the selected VM's public address through the cloud API before SSH. A changed address, foreign state, incomplete apply, expired work deadline or unverified identity refuses remote work. SSH children receive no cloud credentials.

Bootstrap checks require completed cloud-init, the selected-provider loader marker, active service, the installed artifact checksum from Terraform state, the expected mounted data disk and observed tailnet DNS. AWS matches the NVMe serial to the owned volume ID; the other clouds use their stable device paths. The strict doctor runs from the checked source through stdin and requires every applicable service/native doctor/config/CLI/route check to pass. Rechecks replace stale green evidence on failure; loading the source creates no unrecorded Python cache in the private checkout.

Each coding task runs as UID 1000 with explicit workspace HOME/PATH and its API key only on stdin. The controller saves a durable invocation marker before secret retrieval and refuses a repeated paid task, including after an uncertain result. It requires successful model usage and the fresh helper result, then independently inspects regular UID-1000 files for the unchanged input hash, correct sum/nonce and result hash. Reports contain only selected status/hash/token/duration fields; raw CLI output and keys are never saved.

`scripts/showcase_agents.py` adds one fresh arithmetic/nonce turn per runtime, with a durable controller marker before dispatch and no automatic paid retry. OpenClaw uses a direct local gateway RPC, requires the pinned CLI's expected-URL/final-response options, and rejects embedded replies, ambiguous status, tools, messaging, fallback, wrong sessions/models and missing usage. Hermes uses authenticated local Runs API calls with one unique idempotency key and an explicit provider/model; it checks the actual served runtime rather than the top-level model echo. Its seeded OpenAI/custom route must use the exact OpenAI base URL. The controller independently verifies the original answer, model, usage and proof fields. API keys remain inside the runtime container, and raw responses remain in memory.

Before a turn, the VM arms a transient systemd timer to stop the captured container ID after 210 seconds, covering loss of SSH or the probe process. A verified completed turn disarms the timer; failure attempts to stop and inspect that exact container, retaining the timer when direct containment is uncertain. Uncertain tasks clear prior service/doctor evidence, block repeat paid work, and require cleanup or the approved fresh retry. Commands bound output and elapsed time; task dispatch requires the full 300-second verification/containment window. These behaviors follow the [OpenClaw gateway contract](https://github.com/openclaw/openclaw/blob/main/src/cli/gateway-cli/register.ts), [Hermes Runs API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server), [systemd timer interface](https://github.com/systemd/systemd/blob/v249/man/systemd-run.xml) and [Docker stop behavior](https://docs.docker.com/reference/cli/docker/container/stop/); actual pinned-container compatibility remains unverified.

Reboot requires both successful runtime turns, both verified coding fixtures, a bootstrap baseline and passing second-peer access before reboot. It refuses an uncertain runtime turn instead of restarting possible unfinished paid work. Acceptance needs a new boot ID, the same disk/volume/checksum/DNS, fresh passing doctors and unchanged verified workspace files. Starting reboot clears the current peer-access pass; `network_checks("after_reboot")` must prove SSH and both browser sessions on the new boot. An interrupted reboot can resume verification without issuing another reboot or coding task; missing proof remains uncertain. These methods remain library components, not a public launch command or a complete matrix verdict. Real container/peer/browser compatibility and production launch-gate measurements remain pending.

Local tests run the actual independent file verifier as UID 1000 against temporary files and replace all cloud/SSH/model/container/systemd operations with fixtures. They cover address drift, changed source/foreign state, failed/repeated checks, invalid usage/CLI/path/hash/UID, new-boot enforcement, changed disks and interrupted reboot recovery. Agent tests reject stale/echoed/fenced answers, invalid/nonfinite/duplicate JSON, wrong serving models, incomplete/aborted/refused turns, missing usage, unsupported gateway flags, failed containment and unverified timer disarming. The actual bounded subprocess runner is exercised for excessive stdout/stderr, timeout and secret-safe failures; the Hermes request source is exercised with a fake authenticated API. The actual Terraform builtin provider also confirmed the wrapped runtime checksum state shape using only a disposable local fixture. None of this establishes real SSH, model execution, reboot, browser access or cloud support.

## Second-peer Browser Component

`scripts/showcase_browser.py` is a private peer-side component, separate from the public plan/cleanup CLI. It requires the authorized runner's own online Tailscale DNS, one distinct online owned target, a successful ping to its private IP and DNS resolving only to that peer's observed IPs. Relay traffic is accepted; application access uses the direct observed HTTPS URL with normal certificate validation. Fresh Chromium contexts use no proxy, block service workers/downloads, pin hostname resolution to the checked private address and permit only exact-origin reads plus one Hermes password sign-in. OpenClaw WebSocket messages are forwarded unchanged to the real gateway only for connect and an explicit read allowlist; paid/management writes are blocked.

OpenClaw uses the owned gateway's one-time `dashboard --json` browser handoff, keeping its fragment private while replacing the origin and gateway URL. A matching authenticated operator hello, successful read response and registered, connected, visibly rendered app shell are required. Hermes requires anonymous session rejection, the actual basic login form and rendered Sessions view, a verified unexpired session for the expected user and enabled authentication/healthy gateway status. Login pages, public status responses, WebSocket upgrades and blank apps cannot pass. The [test plan](docs/plans/showcase-readiness-tests.md#runtime-checks-and-pinned-compatibility) links the upstream contracts.

Private inputs arrive on stdin; selected proof binds the controller request and excludes passwords, pairing fragments, raw responses, screenshots and saved browser auth state. The standalone browser command has a 150-second process deadline and context/browser cleanup on failure. Twenty-one tests replace Tailscale/DNS/browser operations with fixtures and verify peer/routing/authentication/rendering rules, rejected writes, delayed real-read handling, cleanup and secret-safe reports. No actual browser or peer was used.

`scripts/showcase_peer.py` adds the existing-peer transport. Add [these optional fields](examples/showcase/peer-ssh.example.json) under `runner.peer_ssh` in the private input file only when that option is selected: explicit reachable IP, Linux username/port, independently verified Ed25519 public host key and an `op://` private-key field reference. The selected Linux peer must already have Python, OpenSSH, Tailscale, Playwright with WebSocket forwarding and Chromium. No dependency installation, peer enrollment, personal SSH profile, SSH agent, password prompt or tunnel is used.

The controller rechecks the authorized runner registration and owned VM boot, then collects only the OpenClaw handoff, Hermes dashboard credentials and host/workspace public SSH keys through its verified owned-VM connection. It sends hash-checked source in the SSH command and a bounded private request on stdin; secrets never enter command arguments or evidence. The peer's SSH key is read through `op` once immediately before the connection. Keys and host-key pins live in sealed Linux memory files and are closed after use; a locked 1Password session returns a fixed error for human authorization.

From that peer, direct key-based SSH must reach the target's Tailscale IP on host port 22 as `admin` and workspace port 2222 as `user` with UID 1000 and the persisted home. Each response must match a fresh controller nonce, the expected boot ID and a source address belonging to the checked peer. Both authenticated browser sessions must pass on the same request. The wrapper has a 240-second process deadline and verifies the actual browser portion took less than 150 seconds; its SSH subprocesses have separate deadlines and controlled environments. It closes browser contexts and memory files on failure, and saves only selected proof fields and their hash.

`showcase_remote.network_checks("before_reboot"|"after_reboot")` requires strict doctors, the correct boot stage and a full 750-second work window before starting. It invalidates the previous pass before live collection, rechecks the VM boot afterward and marks failures uncertain; stale request IDs and changed boots cannot retain green evidence. Reboot clears the current access pass, requiring a new request and boot-bound proof afterward. Fourteen peer tests and nine added controller tests cover transport, binding, deadlines, revoked access and secret handling. Actual `ssh-keygen` parsed a generated test key through a sealed memory file, and the inline checked-source loader ran locally; every network/SSH/browser/container operation used a fixture.

Pinned browser/UI/protocol/container compatibility and production launch-gate measurements remain pending; the internal coordinator now sequences these components. No N1, P1 or real matrix cell has passed, and the temporary-peer alternative is not implemented or authorized.

## Read-only Cloud Preflight

Install `scripts/requirements-live.txt` in a task-owned Python virtual environment, then use that environment's Python:

```bash
AGENT_STACK_LIVE_TESTS=1 python3 scripts/showcase_preflight.py \
  --config <mode-0600-private-inputs.json> --provider aws \
  --run-id <as7-unique-id>
```

This command retrieves only the selected cloud's fields, the tailnet read key and the selected OpenAI/Anthropic model-key fields through `op`. Shared references are read once in the process. It makes read requests and a GCP OAuth exchange/permission probe; it creates no cloud resource, enrolls no peer and generates no model response. It does not discover personal AWS profiles or GCP ADC files. Reports contain fixed error codes and selected check results, never credentials or raw provider responses.

Checks cover exact AWS/DO/GCP identity where available, dedicated-project occupancy, regional x86 VM memory/availability, selected quota/permission checks, prefix collisions, and a registered authorized second peer. GCP inventories VMs and disks across zones; DO checks team scope and account-level resources outside project membership. Missing pages, unreachable inventories, HTTP errors and malformed responses fail. Hetzner's API proves token scope and resource/size reads but cannot independently return the declared account/project ID.

Model checks use authenticated [OpenAI model retrieval](https://developers.openai.com/api/reference/resources/models/methods/retrieve) and [Anthropic model retrieval](https://platform.claude.com/docs/en/api/models/retrieve) for all four configured roles. They require exact returned IDs, reject aliases resolving to another ID and known shutdown before the full attempt can end, and deduplicate identical provider/model requests. Model requests use fixed HTTPS origins with no redirect, proxy or retry; malformed/oversized responses and denied reads fail without raw error text. Use project/workspace-specific keys that need no extra account-selection header. Planning does not call this model check; the future run measurer can reuse the same in-memory credential session.

`read_checks_passed` is deliberately distinct from deployment readiness: `launch_ready` remains false, and each provider lists unverified permissions, quotas or capacity. `models.launch_gate` remains `unverified`: model visibility does not establish generation permissions, funded billing/rate limits, runtime/CLI compatibility or model prices. Enrollment/deletion authority, actual peer connectivity/browser authentication and live matrix results also remain unverified. This preflight must not bypass the remaining launch gates.

Default unit tests use generated RSA keys and provider fixtures; when the optional AWS SDK is installed, an offline SDK test validates actual request shapes and rejects inherited personal-profile/endpoint settings. These tests perform no secret retrieval or provider request and do not establish real authentication or cloud support.

## Read-only Cleanup Verification

The inventory/verification component retrieves selected fields through `op` and makes read requests only. It requires an active attempt in the same private spending ledger, the original account/project/region/zone, and a fresh-prefix preflight before capture. Capture records observed IDs before deletion; it does not authorize deletion or replace the isolated Terraform state.

```bash
AGENT_STACK_LIVE_TESTS=1 python3 scripts/showcase_cleanup.py \
  --config <private-inputs.json> --provider aws --run-id <as7-unique-id> \
  --ledger <private-batch-directory> --inventory <private-run-directory>/inventory.json \
  --phase capture
```

After the execution controller has destroyed only the owned resources and removed only the owned tailnet nodes, independently verify their absence:

```bash
AGENT_STACK_LIVE_TESTS=1 python3 scripts/showcase_cleanup.py \
  --config <private-inputs.json> --provider aws --run-id <as7-unique-id> \
  --ledger <private-batch-directory> --inventory <private-run-directory>/inventory.json \
  --phase verify --state <isolated-checkout>/terraform.tfstate
```

Directories must have mode 0700, input/state/inventory files mode 0600. Capture includes AWS boot disks and default VPC children, GCP boot disks and Hetzner primary IPv4/IPv6 IDs. Recapture preserves previously observed IDs and nodes after partial deletion. Renamed IDs, orphan disks, project-membership remnants and unknown resources in the dedicated Hetzner project prevent a cleanup receipt. The original runner peer cannot be captured as an owned node; preflight rejects an already-existing node using the attempt's prefix.

Verification requires an empty private local Terraform state plus successful independent cloud and tailnet queries. Only documented AWS resource-not-found errors count as absence; permission errors, timeouts, incomplete inventories or malformed responses fail. A terminated AWS instance is a retained tombstone, with its disks checked separately. A successful receipt is saved privately and marks the original attempt cleaned without refunding its spending reservation. Missing state after a pre-create failure needs controller proof; this standalone command does not assume that missing state means no resources were created.

Hetzner capture is implemented, but a verified receipt still requires separate account/project-token mapping evidence: its API does not return those IDs. Production launch gates, the paid public run phase, that proof path and real cleanup remain pending. The separate saved-plan driver implements guarded destruction; this standalone inventory command remains read-only. Tests use provider fixtures and offline SDK responses, including the full capture/verify/ledger flow; they cannot establish live deletion.

## Optional Live Smoke Suite

The full four-cloud TODO #7 live runner is still being implemented; no live result is claimed. The available `--phase plan` and `--phase cleanup` commands require the explicit opt-in shown above. Paid test orchestration is not yet exposed, and live testing is intentionally outside the default runner.

Live tests must:

- require explicit credentials and an explicit provider
- create namespaced temporary resources only
- never reuse existing volumes
- verify SSH, cloud-init loader readiness, `/opt/agent-stack`, Docker Compose health, and one UI endpoint
- clean up resources and report any cleanup failure loudly
- document expected cloud cost and duration before running

## Evidence Report

Every default local run prints:

```markdown
## Test Evidence

Workspace dirty before run:
Workspace dirty after run:
Cloud-impacting commands run: none

| Phase | Status | Evidence |
| --- | --- | --- |
```

Only mark a phase `pass` when the evidence is machine-checkable from commands
or local tests. Missing cloud credentials are irrelevant to the default suite
and must not block it.
