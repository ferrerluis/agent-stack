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

## Private Execution Inputs

Use [the reference-only example](examples/showcase/execution-inputs.example.json) to prepare a mode-0600 file outside Git. Validate one ready provider without retrieving secrets or contacting cloud/model APIs:

```bash
python3 scripts/showcase_policy.py --config <private-inputs.json> --provider aws
```

`inputs_complete` means the required fields and approved limits are present. Identity, project isolation, permissions, prices, model availability, minimum VM memory and second-peer access still require live preflight; none is established by an input declaration.

The runner library conservatively reserves the full attempt allowance, blocks concurrent launches and further attempts until independently queried cleanup succeeds, and allows at most two attempts per cloud. Later provider inputs and repaired pins retain prior spending/retry history; each provider stays bound to its original account/project/region/zone. A new config or ledger must not be used to restart the same batch's spending allowance. Ledger schema 3 requires this complete scope; an older ledger is refused and must be preserved, not replaced to reset the budget. Saved-plan execution and guarded deletion are implemented as library components; measured launch readiness and the complete paid test controller remain in development.

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

The library applies only that saved plan after measured readiness and an active reservation in the original ledger. Source, settings, key, plan and provider-lock hashes are checked again; a durable record permits one apply invocation. Deadlines and timeouts bound child processes, interrupted creation remains uncertain, and GCP tokens refresh before commands only when they cover the full bounded command duration. The public CLI exposes no launch phase or readiness override; price/model/permission/container/peer measurements and complete test orchestration remain pending.

Recover a recorded active attempt in that same private batch:

```bash
AGENT_STACK_LIVE_TESTS=1 ./scripts/test-live.sh --phase cleanup \
  --provider aws --profile showcase --run-id <original-as7-id> \
  --config <mode-0600-private-inputs.json> --batch <original-private-batch-directory>
```

Before destruction, the driver captures cloud/node IDs, matches state resources and attachments to the original plan and observed inventory, and checks a saved destroy plan permits deletion only. It then applies that exact destroy plan, requires empty state, removes only previously captured owned node IDs using the [Tailscale device API](https://tailscale.com/docs/features/access-control/device-management/how-to/remove), and independently checks cloud/node absence. The runner peer is preserved. Failed queries, unplanned resources, foreign attachments, changed plans or leftover resources keep cleanup unverified and block another launch.

An expired reservation whose durable controller record proves apply was never invoked can be closed only after two independent absence inventories and confirmation that no state appeared; it receives `never_applied_verified_absent` proof without inventing an empty state file. Applied attempts with missing state retain captured IDs and require recovery. Destroyed attempts use `destroyed_empty_local_state` proof. Both retain their full spending reservation and attempt count.

Cleanup accepts repaired credential references while preserving the original source/pins, accounts/projects/regions/zones, tailnet and limits. Hetzner still needs separate verified account/project-token mapping; its apply and cleanup paths refuse declared-only identity. Orphan deletion outside Terraform state is not implemented. Tests exercise the entrypoint and plan/apply/destroy/receipt flow with fake commands/provider responses only; no real launch or deletion has been verified.

## Read-only Cloud Preflight

Install `scripts/requirements-live.txt` in a task-owned Python virtual environment, then use that environment's Python:

```bash
AGENT_STACK_LIVE_TESTS=1 python3 scripts/showcase_preflight.py \
  --config <mode-0600-private-inputs.json> --provider aws \
  --run-id <as7-unique-id>
```

This command retrieves only the selected cloud's fields and the tailnet read key through `op`. Shared references are read once in the process. It makes read requests and a GCP OAuth exchange/permission probe; it creates no cloud resource, enrolls no peer and makes no model call. It does not discover personal AWS profiles or GCP ADC files. Reports contain fixed error codes and selected check results, never credentials or raw provider responses.

Checks cover exact AWS/DO/GCP identity where available, dedicated-project occupancy, regional x86 VM memory/availability, selected quota/permission checks, prefix collisions, and a registered authorized second peer. GCP inventories VMs and disks across zones; DO checks team scope and account-level resources outside project membership. Missing pages, unreachable inventories, HTTP errors and malformed responses fail. Hetzner's API proves token scope and resource/size reads but cannot independently return the declared account/project ID.

`read_checks_passed` is deliberately distinct from deployment readiness: `launch_ready` remains false, and each provider lists unverified permissions, quotas or capacity. Current prices, model access, enrollment/deletion authority, actual peer connectivity/browser authentication and live matrix results remain unverified. This preflight must not be used to bypass the remaining launch gates.

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

Hetzner capture is implemented, but a verified receipt still requires separate account/project-token mapping evidence: its API does not return those IDs. The complete launch/test controller, that proof path and real cleanup remain pending. The separate saved-plan driver implements guarded destruction; this standalone inventory command remains read-only. Tests use provider fixtures and offline SDK responses, including the full capture/verify/ledger flow; they cannot establish live deletion.

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
