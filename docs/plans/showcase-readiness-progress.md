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
- Reference-only execution inputs, per-provider structure checks, fresh-create plan restrictions, private concurrent spending reservations, two-attempt limits and cleanup-receipt policy. Later provider inputs and repaired pins preserve spending/retry history; retries and cleanup remain tied to the original account/project/region/zone. Schema 3 refuses older incomplete-scope ledgers without resetting them.
- Opt-in read-only preflight for all four clouds and the registered authorized second peer. It retrieves selected 1Password fields once, performs bounded requests without redirects/personal-profile discovery, checks API identity/project/VM size and selected quota/permission/inventory results, and lists unresolved checks with `launch_ready: false`. GCP uses an in-memory signed token exchange and inventories VMs/disks across zones; DO verifies team scope and account-level prefix collisions. Hetzner account/project mapping needs separate proof.
- Read-only private ownership inventory and independent cleanup verification, including AWS boot disks/default VPC children, GCP boot disks, Hetzner primary IPs and tailnet nodes. Recapture preserves prior IDs; renamed/orphan resources and query failures prevent a receipt. Verification requires empty Terraform state and successful cloud/tailnet absence queries, preserves the runner peer and marks the original ledger attempt cleaned without refunding spend. Actual deletion/controller execution and Hetzner scope-mapping proof remain pending.
- Private preparation exports one exact committed revision, rejects tracked operator state/auth and links, generates a fresh test SSH key and non-secret named-profile settings, and records source/settings/key hashes. Recovery checks refuse modified files/inputs/keys and added overrides. Child processes use a private umask and discard inherited credentials/proxies/SSH agents/Terraform overrides; work deadlines leave cleanup recovery available. Preparation makes no live request or spending reservation.
- Optional sensitive workspace SHA-512 crypt hash input, with Terraform/setup validation and literal-dollar preservation in Compose. Startup accepts legacy passwords or hashes, rejects both together and removes their environment before SSH. A single-read stdin-only `op`/OpenSSL helper keeps the plaintext password out of Terraform/files; tests use fake passwords only. Container checks now exercise the legacy password and combined hash paths when Docker is available.

Validation:

- Terraform 1.16.5: isolated cached-provider init and format passed; validate and all 55 mocked plan tests passed outside the sandbox, zero failed. The sandbox prevented local provider startup; the same isolated source and previously verified provider binaries succeeded with local transport available. No live cloud request or resource creation occurred.
- 132 setup, 13 doctor and 89 runner Python tests passed (234 total). Coverage includes real Git export/key generation/OpenSSL hashing with fake inputs, both entrypoint authentication paths, changed source/settings/key/override refusal, cleanup after deadline expiry, precise prefix ownership, concurrent reservations, original account/project/region/zone binding, empty-state enforcement, full private capture/verify/ledger flow, implicit and renamed/orphan resources, timeout child-process cleanup, actual Terraform workspace-template rendering, generated GCP JWT signature/refresh checks and offline actual AWS SDK request validation. No real provider/model request is made by these tests.
- Runtime shell/Python syntax and checkout-idempotence checks passed; no cloud-impacting command ran.
- Deployment floor is Terraform >=1.9; plan-time mocking tests require >=1.11.

No real credential was retrieved or imported, no personal coding-tool auth file was inspected, and no cloud/model spend occurred. Docker is absent on this host; image builds, native runtime/CLI execution and real authentication remain unverified.

Next work: connect private preparation to the opt-in deployment/test/deletion controller, remaining price/permission/quota gates and Hetzner scope-mapping proof. Integrate in-memory secret injection, inventory capture before every deletion, private recovery after interrupted attempts and independent cleanup verification. Once inputs are available, resolve the read-only preflight's unverified checks; verify model access, prices, pins and actual tailnet connectivity, run container checks locally or on the first owned VM, then run providers sequentially under the approved $25/four-hour/one-retry limits.

Required live proof: all 24 matrix cells, direct authenticated browser access from a second peer, reboot/persistence, exact-revision documentation and independently confirmed resource/node cleanup. Neither the goal nor TODO #7 is complete.

Canonical plans: [implementation](showcase-readiness-implementation.md), [tests](showcase-readiness-tests.md).
