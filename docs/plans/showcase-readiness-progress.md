# TODO #7 implementation checkpoint

Status: Goal active; implementation started on 2026-10-05. Live acceptance is incomplete.

Branch: `codex/showcase-readiness`, based on `648522add8c662f8c238c87ef899a7c0b6c8afb7`. This checkpoint is local source work, not a deployed release.

Implemented:

- GCP Compute Engine network/firewalls, VM and separate persistent data disk, provider selection, metadata SSH, stable disk discovery and mocked tests.
- Optional pinned Claude Code installation beside Codex, an immutable OpenClaw digest override, and the named combined showcase example with messaging disabled.
- Hermes fresh-model configuration with existing-config preservation, dashboard password/session authentication, authenticated detailed readiness and installer readiness/rollback wiring.
- Private Tailscale Serve ports 443/8443, observed full DNS outputs, bounded noninteractive SSH collection and hidden token-bearing outputs.
- Strict machine-readable service/doctor/config/tool/route checks, including empty, missing, malformed, incomplete, failed and timed-out cases.
- A UID-1000 API-key task helper that takes a secret only through stdin, bounds execution, requires successful model usage and independently verifies a fresh persisted result file.
- README/test/setup/doctor guidance distinguishes implemented code from demonstrated live behavior.
- A credential-free container command renders actual Terraform workspace templates, checks both legacy and combined pinned CLI images through SSH as UID 1000, verifies home/host-key persistence and negative cases, and removes only ownership-verified test resources.
- OpenClaw first-start gateway seeding with authenticated local mode, existing-config preservation, safe rollback and matching host/sidecar listener configuration. Docker's published gateway port remains on host loopback.
- Hermes native doctor is mandatory in strict health, with a completed passing summary and zero exit. Its config and environment-file privacy checks match the official container's UID/GID and modes, with no raw doctor output saved.
- Optional DigitalOcean project assignment for newly created Droplets/disks; reused operator disks retain their existing project.
- Reference-only execution inputs, per-provider structure checks, fresh-create plan restrictions, private concurrent spending reservations, two-attempt limits and independently queried cleanup receipts. These are runner components; live API preflight/execution/cleanup adapters are not yet implemented.

Validation:

- Terraform 1.16.5: final isolated init, format and validate passed; 52 mocked plan tests passed, zero failed.
- 131 setup, 13 doctor and 23 runner Python tests passed (167 total). Coverage includes ownership refusal, concurrent reservations, timeout child-process cleanup and actual Terraform workspace-template rendering without providers.
- Runtime shell/Python syntax and checkout-idempotence checks passed; no cloud-impacting command ran.
- Deployment floor is Terraform >=1.9; plan-time mocking tests require >=1.11.

No real credential was retrieved or imported, no personal coding-tool auth file was inspected, and no cloud/model spend occurred. Docker is absent on this host; image builds, native runtime/CLI execution and real authentication remain unverified.

Next work: implement the opt-in execution runner and provider identity/permission/price/ownership/cleanup adapters. Once inputs are available, verify identities, permissions, billing/quota, model access, prices, pins and tailnet access; run container checks locally or on the first owned VM, then run providers sequentially with new resources under the approved $25/four-hour/one-retry limits.

Required live proof: all 24 matrix cells, direct authenticated browser access from a second peer, reboot/persistence, exact-revision documentation and independently confirmed resource/node cleanup. Neither the goal nor TODO #7 is complete.

Canonical plans: [implementation](showcase-readiness-implementation.md), [tests](showcase-readiness-tests.md).
