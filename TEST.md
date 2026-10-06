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

The internal [loopback model gateway](scripts/showcase_model_gateway.py) now binds an ephemeral port only on `127.0.0.1`, gives each role a separate random credential and keeps provider keys in controller memory. It activates the original quoted model allocation before listening, records every full spending hold before provider access, streams JSON/SSE bytes incrementally with bounded size/time, and preserves tool-call and terminal/failure events. Four concurrent clients, absolute header/body framing deadlines, fixed paths/auth, no request logging and bounded shutdown constrain the listener. Closing revokes credentials and dispatch, waits for handlers, verifies worker termination and erases cached keys; failure cannot claim verified shutdown. Duplicate/restarted requests retain the original ledger history. Unreviewed beta headers, compressed/chunked request bodies, browser origins and management routes are refused.

Twenty-six tests exercise actual local TCP clients, incremental streaming, private durable accounting, concurrency, hostile framing, separate channel/workspace probes, role revocation and shutdown with fixture upstream responses/keys. One integration test runs the actual workspace helper and fake CLI child processes through the local gateway, verifies fresh persisted answers and matches both role holds. The sandbox blocks loopback sockets; these tests passed with local socket permission outside it. No real provider request or credential was used. Owned-VM transport and pinned native compatibility remain unverified; a proxied fixture reply does not pass a launch gate. See the official [Codex gateway contract](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility).

The showcase saved-plan driver now supplies inactive model-key placeholders to Terraform instead of real OpenAI/Anthropic keys. Provider keys remain with controller-side readers and the gateway; VM routing must be connected before any model work. Cloud/Tailscale credentials and the workspace password hash still have their required private paths, so plans/state remain sensitive. A driver test covers plan/apply/cleanup without fetching model keys for Terraform.

The [workspace task helper](modules/common/runtime/workspace_cli_task.py) has an explicit `--model-gateway` mode. Stdin accepts only the same tool/provider's temporary role credential and a strict loopback/container-host URL. Fresh temporary client homes and an environment allowlist exclude saved logins, inherited provider/cloud routes, proxies, custom headers and preload settings; homes are removed after success, failure or timeout. Codex selects a custom Responses provider with environment auth, HTTP streaming and zero automatic retries; Claude uses a gateway bearer token, disabled user/project settings and an empty explicit MCP configuration. The gateway's original spending allocation governs this mode; the legacy direct-key helper retains its existing local Claude dollar cap. Six new helper tests cover hostile input, parsed configuration, actual fake CLI children, cleanup and sanitized stdin errors. The selected routing hash remains a client setting. The owned-workspace component now independently checks DNS, UID, a nonbillable nonce and original ledger receipts; actual pinned native behavior and provider terminal/model/usage correlation remain unverified. Configuration follows [Codex provider options](https://learn.chatgpt.com/docs/config-file/config-reference), [Codex authentication](https://learn.chatgpt.com/docs/auth), [Claude gateway auth](https://code.claude.com/docs/en/llm-gateway-connect) and [Claude environment controls](https://code.claude.com/docs/en/env-vars).

Actual public HTML from OpenAI's current model page and Claude's pricing/model/versioning pages passed the parser on 2026-10-06; authenticated metadata, keys and upstream requests remain fixtures. Older or unrecognized model-card formats fail until explicitly supported. Owned-VM transport, routing for both runtimes and coding tools, account adjustments and actual pinned protocol verification remain required. OpenAI's [hard spend limits](https://developers.openai.com/api/docs/guides/spend-limits) allow slight overspend during propagation, and Claude's [local cost figures](https://code.claude.com/docs/en/costs) are estimates; neither alone proves the approved ceiling. No model launch gate passes and the public command still has no paid run phase.

A new config or ledger must not restart the same batch's spending allowance, and schema 3 refuses older incomplete-scope ledgers without replacing them. Saved-plan execution, guarded deletion and the internal acceptance coordinator are implemented; production launch-gate measurements and the paid CLI phase remain in development.

The internal [SSH model channel](scripts/showcase_model_transport.py) and [VM relay](scripts/showcase_model_relay.py) connect the gateway to an ephemeral private Docker-bridge listener. The [model session](scripts/showcase_model_session.py) now joins them to the internal coordinator and [workspace route probe](scripts/showcase_model_container.py); runtime configuration, reload and route-probe components are implemented, while budgeted doctors, native task adapters and production launch measurements remain pending.

- Bind the original applied attempt, ledger, source, boot/checksum, container proof and existing SSH pin; inspect effective SSH Match rules before opening one loopback forward and verify its actual listener. Opt-in repo-helper `--pinned` / `--model-forward` modes isolate SSH configuration/auth and preserve ordinary defaults.
- Discover the standard private Docker bridge; bound four connections, buffers, bytes, session/control time and the original work deadline. Close on control EOF, stop, listener failure or deadline, creating no daemon service or provider-key files.
- Independently observe one registered authenticated nonce at the controller gateway without provider access or a model hold. Channel/pump/deadline loss revokes client/auth/dispatch and active streams while retaining prior spending holds.
- Verify remote listener/session closure, zero SSH exit and local worker termination; failed close remains unverified. A nonce proves transport only, and cannot generate a model response or establish native routing.

Twenty-three channel/relay/helper tests use real local sockets and an isolated OpenSSH 10.2p1 client/server with generated test keys. They cover effective/private policy, native option precedence/CRLF allocation, strict pins/filenames, nonce observation, incremental transport, client/buffer/deadline limits, half-close/EOF, failed sessions/output and independently closed listeners/workers.

VM ownership/boot/checksum, Docker identity and upstream model responses remain fixtures; no cloud SSH, actual Docker bridge/container DNS/native route or paid model execution is established. Four added gateway tests cover single-use/malformed/nonbillable probes, allocation/deadline refusal and stream revocation.

Source follows the official [SSH forward interface](https://man.openbsd.org/ssh), [client options](https://man.openbsd.org/ssh_config) and [server bind/Match policy](https://man.openbsd.org/sshd_config). Finish coordinator/public-command wiring, per-container route/receipt matching and reboot reconnection before these components enable paid work.

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

Planning prepares and verifies exact committed source, initializes isolated Terraform data, runs read preflight, injects required cloud/Tailscale secrets and the workspace password hash in memory, and saves a private binary plan plus checked JSON. Real model keys are excluded from Terraform; its inactive placeholders require later controller gateway routing. Planning creates no VM, makes no model generation call and reserves no spend. Plans/state contain sensitive values; keep the entire batch outside Git with mode 0700. `planned` still reports `launch_ready: false`.

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

The model-session suite adds seventeen tests for source/plan/terms conversion, checked container-probe input/UID/DNS/nonce/response limits, actual bounded stdin failure, independent probe observation, private capability handling, original-account receipts, missing/uncertain/forged/replayed holds, scope/boot/deadline refusal, retained allocation across reboot and verified/uncertain shutdown. Two tests use actual loopback gateway HTTP and locked ledger writes; upstream keys and model replies remain fixtures. Three new gateway cases cover distinct single-use channel/workspace probes, individual role sealing and interruption of an in-flight stream without refunding its hold. With the completion/lifecycle/environment/doctor tests below, the full runner passes 514 tests; the unchanged 138 setup and 13 doctor tests retain prior passing results (665 total).

Coordinator tests replace cloud, SSH, Docker, model and reboot operations, including the channel factories; they do not prove actual VM/container/native execution. A received gateway hold currently proves one bounded HTTP response through the selected route, not provider terminal/model/usage or challenge correlation; those checks and measured native runtime adapters remain pending. No paid public run phase is enabled.

The [internal coordinator](scripts/showcase_controller.py) requires a source/plan/terms-bound model session before reservation. It sequences saved-plan apply, bootstrap, required container checks, a verified private model channel, strict doctors, runtime/coding tasks and peer access; it closes the channel before reboot, opens a fresh boot-bound channel afterward and closes it again before final acceptance. Loaded controller sources must match the prepared commit, and a durable per-run record prevents replay after interruption.

Cleanup runs after success, partial apply, test failure and ordinary interruption; no cell is published as passing unless acceptance and independent cleanup both succeed. The existing `--phase cleanup` command also recovers coordinator records, restoring completed acceptance only when its saved evidence and every step receipt remain unchanged, without repeating paid work.

Production price/model/permission/quota/peer/cleanup measurements, a paid public run phase, Hetzner mapping proof and orphan recovery remain unfinished. The coordinator has fixture integration coverage only; every real matrix cell remains unverified.

`scripts/showcase_remote.py` implements controller checks for an active, applied, exact-source attempt in the original spending ledger. It reuses the private key/known-hosts file and repo SSH wrapper, matches owned state to recorded inventory, then re-queries the selected VM's public address through the cloud API before SSH. A changed address, foreign state, incomplete apply, expired work deadline or unverified identity refuses remote work. SSH children receive no cloud credentials.

Bootstrap checks require completed cloud-init, the selected-provider loader marker, active service, the installed artifact checksum from Terraform state, the expected mounted data disk and observed tailnet DNS. AWS matches the NVMe serial to the owned volume ID; the other clouds use their stable device paths. The strict doctor runs from the checked source through stdin and requires every applicable service/native doctor/config/CLI/route check to pass. Rechecks replace stale green evidence on failure; loading the source creates no unrecorded Python cache in the private checkout.

Each coding task runs as UID 1000 with explicit workspace HOME/PATH and a temporary role credential on stdin; upstream provider keys stay in controller memory. Before dispatch, the checked workspace probe requires host.docker.internal to resolve only to the observed private bridge address and matches a registered authenticated nonce independently at the controller, without a model request. The controller saves a durable invocation marker and refuses a repeated paid task, including after an uncertain result. It requires successful model usage and the fresh helper result, then independently inspects regular UID-1000 files for the unchanged input hash, correct sum/nonce and result hash. Reports contain selected status/hash/token/duration fields and original-account request receipts; raw CLI output and keys are never saved. Each completed task revokes its role capability and requires nonempty, fresh, received gateway holds matching the original quote/policy/source/scope/model/boot. Final acceptance and cleanup recovery compare receipts against the original ledger, rather than accepting a client echo; failed probes, missing/uncertain holds and lost channels cannot pass.

`scripts/showcase_agents.py` adds one fresh arithmetic/nonce turn per runtime, with a durable controller marker before dispatch and no automatic paid retry. OpenClaw uses a direct local gateway RPC, requires the pinned CLI's expected-URL/final-response options, and rejects embedded replies, ambiguous status, tools, messaging, fallback, wrong sessions/models and missing usage. Hermes uses authenticated local Runs API calls with one unique idempotency key and an explicit provider/model; it checks the actual served runtime rather than the top-level model echo. The older probe still checks its seeded OpenAI/custom route against the exact OpenAI base URL; it needs a checked budget-route adapter before it can be used by this showcase controller. The controller independently verifies the original answer, model, usage and proof fields. Real provider keys are excluded from this runner's Terraform/VM configuration, and raw responses remain in memory. The default model session refuses both runtime turns with runtime_model_route_required until their native route, doctor and generation adapters are verified; it cannot silently fall back to the direct-origin probe.

Before a turn, the VM arms a transient systemd timer to stop the captured container ID after 210 seconds, covering loss of SSH or the probe process. A verified completed turn disarms the timer; failure attempts to stop and inspect that exact container, retaining the timer when direct containment is uncertain. Uncertain tasks clear prior service/doctor evidence, block repeat paid work, and require cleanup or the approved fresh retry. Commands bound output and elapsed time; task dispatch requires the full 300-second verification/containment window. These behaviors follow the [OpenClaw gateway contract](https://github.com/openclaw/openclaw/blob/main/src/cli/gateway-cli/register.ts), [Hermes Runs API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server), [systemd timer interface](https://github.com/systemd/systemd/blob/v249/man/systemd-run.xml) and [Docker stop behavior](https://docs.docker.com/reference/cli/docker/container/stop/); actual pinned-container compatibility remains unverified.

Reboot requires both successful runtime turns, both verified coding fixtures, a bootstrap baseline, passing second-peer access and a closed model session before reboot. It refuses an uncertain runtime turn instead of restarting possible unfinished paid work. Acceptance needs a new boot ID, the same disk/volume/checksum/DNS, fresh passing doctors and unchanged verified workspace files. Starting reboot clears the current peer-access pass; `network_checks("after_reboot")` must prove SSH and both browser sessions on the new boot. An interrupted reboot can resume verification without issuing another reboot or coding task; missing proof remains uncertain. These methods remain library components, not a public launch command or a complete matrix verdict. Real container/peer/browser compatibility and production launch-gate measurements remain pending.

Local tests run the actual independent file verifier as UID 1000 against temporary files and replace all cloud/SSH/model/container/systemd operations with fixtures. They cover address drift, changed source/foreign state, failed/repeated checks, invalid usage/CLI/path/hash/UID, new-boot enforcement, changed disks and interrupted reboot recovery. Agent tests reject stale/echoed/fenced answers, invalid/nonfinite/duplicate JSON, wrong serving models, incomplete/aborted/refused turns, missing usage, unsupported gateway flags, failed containment and unverified timer disarming. The actual bounded subprocess runner is exercised for excessive stdout/stderr, timeout and secret-safe failures; the Hermes request source is exercised with a fake authenticated API. The actual Terraform builtin provider also confirmed the wrapped runtime checksum state shape using only a disposable local fixture. The separate private-channel suite uses an isolated real local SSH server. Neither suite establishes cloud SSH, Docker container routing, native model execution, actual reboot, browser access or cloud support.

The internal [runtime configuration adapter](scripts/showcase_runtime_routes.py) now builds explicit single-model OpenClaw and Hermes routes and stages only stopped runtimes through checked source and private stdin. OpenClaw uses an explicit replacement catalog and API transport; Hermes uses a dedicated named provider with discovery and fallbacks disabled. These candidates follow the official [OpenClaw provider configuration](https://docs.openclaw.ai/gateway/config-tools/custom-providers) and [Hermes model configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models). Actual pinned images still need native route verification.

Twenty new tests cover all four runtime/provider pairs, retained full-context/output bounds, unsupported alternate routes, strict private packets, real local file locks/atomic renames/fsync, private snapshots, exact byte/mode restoration, crashes, concurrent writers, symlinks/hardlinks, changed files, stale binding and bounded redacted source stdin. Local UID/root ownership is replaced in the file tests; cloud/VM/Docker/SSH, runtime state and reboot use fixtures. Session tests cover fresh staging after reboot without a new allocation, no paid authorization from a staged file, failed-install revocation and uncertain restoration after deadline loss. The existing native SSH and gateway loopback suites retain their separate coverage.

The file adapter saves an original root-private snapshot before replacement, refuses repeated installs and restores only the exact original or installed hash. Session close revokes channel access before restoring files; a failed restore cannot claim verified shutdown. The file adapter alone does not establish a container connection, native provider selection, doctor success or generation. The default coordinator retains its runtime task guard.

The internal [runtime lifecycle adapter](scripts/showcase_runtime_lifecycle.py) adds checked-source stop/install/start/health and [container/native route probes](scripts/showcase_runtime_probe.py). Before changing a file it captures the exact container/image IDs, verifies the immutable image and data mount, saves a private recovery record and arms a captured-container watchdog. It refuses replacement containers and automatic restarts. Failure leaves recovery data and attempts to stop only that container; verified restoration leaves it stopped. Reload and restoration invalidate prior service/doctor/config/readiness results.

OpenClaw must report valid configuration with matching on-disk and applied gateway revisions, the explicit provider/API/model and full bounds. That is active-configuration evidence, not actual model-generation proof. Hermes must resolve the named provider through its native resolver, select the exact model/API/endpoint/capability and avoid pools or overrides. These checks follow the current [OpenClaw config RPC](https://docs.openclaw.ai/gateway/protocol/rpc-talk-config-and-agents) and [Hermes native provider resolution](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/runtime_provider.py); unsupported pinned contracts fail. Each container checks its required UID and installed file hash, sends one nonbillable nonce over the numeric private bridge, and the controller independently requires gateway observation before accepting the route.

Fifteen added lifecycle tests exercise all four runtime/provider pairs, private recovery, original-byte restoration, stale native revisions, wrong UID/model/endpoint/image, replacement containers, timeout, replay, changed files and forged receipts. Real temporary files and the complete inline Python loader run locally; native root/runtime ownership, Docker/systemd/SSH, resolver/RPC and container replies use fixtures. Two tests use actual loopback gateway HTTP and the original locked ledger: generation before controller task start cannot create a hold, retrieve a key or contact a provider, while registered runtime nonces remain nonbillable. Every model session enables this task-start restriction before its gateway listens; the standalone gateway component retains its earlier ungated test mode. Node is unavailable on this host, so the embedded JavaScript has not been executed or syntax-checked with Node.

The internal session can call `activate_runtime` and `runtime_doctor`; the default coordinator does not yet call either or authorize runtime tasks. Terminal model/usage/challenge correlation, complete doctor integration, reboot ordering and pinned verification remain required. No native container, VM, cloud/model operation or live matrix cell was demonstrated by these tests.

The [Hermes environment adapter](scripts/showcase_runtime_environment.py) now stages `.env` under the same stopped-container lock and private recovery journal. It removes only empty/inactive provider placeholders or public endpoint settings, retains app authentication and sets the actual selected custom endpoint. Real/unknown credentials, cloud identities, external auth paths, proxies and shell expansion refuse. Hermes configuration staging also refuses external secret-source configuration. Both the controller and container require the staged environment hash; close restores exact original bytes/mode while stopped, and changed files retain uncertainty. This addresses Hermes's [native `.env` requirement](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/doctor_config.py) without supplying invalid keys to its [other-provider connectivity probes](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/doctor_connectivity.py).

The internal native doctor adapter uses the repository's checked strict parsers, the exact captured container/UID and an empty inherited environment. It runs OpenClaw's read-only lint command or Hermes doctor without fix/live/ack flags, with bounded output/time and a captured-container watchdog. It verifies health, native route and config/environment hashes before and afterward. Failed, malformed, incomplete, timed-out or changed-file results stop only the verified container where possible and retain recovery data. The gateway denies generation and racing task starts during the diagnostic window; failure seals that role. The controller also requires the original request ledger to remain unchanged. A successful doctor grants no paid authority, and restoration clears its current green result. These are candidate native command contracts; no doctor finding is suppressed or substituted.

Eight new environment tests exercise real local private snapshots, locks, atomic writes, crash recovery, exact restoration, changed-file refusal, malformed/foreign settings and snapshot size bounds. Seven added lifecycle/session/gateway tests cover native output parsing, hash/health/identity drift, replay, denied generation, concurrent start refusal, failed-role revocation and unrefunded unexpected holds. Native command/output, UID/root ownership, Docker/systemd/VM/SSH remain fixtures; three gateway tests in this suite now use actual local HTTP and ledger writes. The complete checked Python loader executes locally with compressed public source to stay below SSH argument limits; temporary credentials remain on private stdin. All 486 runner tests pass, with prior unchanged setup/doctor results retained (637 total). Embedded Node remains unexecuted on this host. Native coverage, account/traffic costs and the production launch gate remain unverified.

Hermes's current [doctor connectivity implementation](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/doctor_connectivity.py) includes built-in provider addresses and some model POST probes outside named-route resolution. Verify the selected pin and constrain those paths without fabricating a green doctor result or admitting an unpriced request. Real provider keys remain controller-only; temporary scoped channel capabilities can occur in the owned VM's private runtime file until restoration or resource cleanup.

## Provider Completion Component

The internal [completion verifier](scripts/showcase_model_completion.py) checks the bounded JSON/SSE bytes that the gateway delivers unchanged and incrementally. It requires the selected model, a successful provider terminal, positive token usage within the original request/policy bounds, consistent cache counts and supported text or requested client-tool output. Streaming identity, sequence and content must agree with the terminal. Chat streaming explicitly requests final usage. The contracts follow [OpenAI Responses completion events](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_completed_event.py), [Chat completion chunks](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_chunk.py) and [Claude streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming).

Failed/incomplete/refused/truncated/unknown responses receive no completion proof. Only hashes and selected counts enter the original private ledger; reused provider response IDs refuse, and every full worst-case hold remains charged. Old HTTP-only holds remain readable for cleanup without inferred proof. Session receipts require proof for every request and a final text response; acceptance and recovery recheck those original holds. This still needs fresh provider-answer and native-usage correlation before guarded runtime tasks or the paid public path are enabled.

Twenty-eight new tests cover all three APIs as JSON/SSE, usage/cache/request limits, tool intermediates/final text, identity/order/content, duplicate/malformed/nonfinite/deep/oversized input, refusals/failures/truncation, unchanged incremental delivery, legacy accounting, retained holds and final acceptance. Local HTTP/file/ledger operations execute; keys/provider bytes and native/cloud replies are fixtures. All 514 runner tests pass without skips in 80.333 seconds, with prior unchanged setup/doctor results retained (665 total). Actual provider TLS/generation, pinned native compatibility, coordinator doctor/reboot integration and full account-cost/launch proof remain unverified. No real credential, deployment or spend occurred.

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
