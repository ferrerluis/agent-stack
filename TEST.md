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

With Docker, Terraform, SSH and exact CLI versions available:

```bash
./scripts/test-containers.sh --profile showcase \
  --codex-release <x.y.z> --claude-release <x.y.z> --platform linux/amd64
```

This renders the actual workspace templates without providers, builds both legacy Codex-only and combined images, and verifies pinned CLI execution over SSH as UID 1000. It checks persistent home data and host keys across restart/recreation, deliberate missing CLI/password failures, and deletion of uniquely labeled test containers/images/volumes.

It uses a fake test password and a generated test-only SSH key; it retrieves no model credentials and explicitly reports model execution as untested. Missing Docker or unsuccessful cleanup fails the command. Base images/build cache may remain; the command does not prune shared Docker resources.

The unit tests for this command cover its failure and ownership rules. They do not substitute for an actual image build or container run.

## Private Execution Inputs

Use [the reference-only example](examples/showcase/execution-inputs.example.json) to prepare a mode-0600 file outside Git. Validate one ready provider without retrieving secrets or contacting cloud/model APIs:

```bash
python3 scripts/showcase_policy.py --config <private-inputs.json> --provider aws
```

`inputs_complete` means the required fields and approved limits are present. Identity, project isolation, permissions, prices, model availability, minimum VM memory and second-peer access still require live preflight; none is established by an input declaration.

The runner library conservatively reserves the full attempt allowance, blocks concurrent launches and further attempts until independently queried cleanup succeeds, and allows at most two attempts per cloud. A new config or ledger must not be used to restart the same batch's spending allowance. The opt-in cloud execution runner and provider cleanup adapters remain to be implemented.

## Optional Live Smoke Suite

The four-cloud TODO #7 live runner is still being implemented; no live result is claimed. Live smoke testing is intentionally not part of the default runner. If it
is added later, gate it behind an explicit opt-in such as:

```bash
AGENT_STACK_LIVE_TESTS=1 ./scripts/test-live.sh
```

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
