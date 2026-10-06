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

Validation:

- Terraform 1.16.5: isolated init, format and validate passed; 49 mocked plan tests passed, zero failed.
- 124 setup and 9 doctor Python tests passed. Doctor tests passed again after requiring that selected native checks are not skipped or cancelled.
- Runtime shell/Python syntax and checkout-idempotence checks passed; no cloud-impacting command ran.
- Deployment floor is Terraform >=1.9; plan-time mocking tests require >=1.11.

No real credential was retrieved or imported, no personal coding-tool auth file was inspected, and no cloud/model spend occurred. Docker is absent on this host; image builds, native runtime/CLI execution and real authentication remain unverified.

Next work: container acceptance command, private run-configuration/preflight and bounded live-runner/ownership/cleanup helpers. Once inputs are available, verify identities, permissions, billing/quota, model access, prices, pins and tailnet access, then run providers sequentially with new resources under the approved $25/four-hour/one-retry limits.

Required live proof: all 24 matrix cells, direct authenticated browser access from a second peer, reboot/persistence, exact-revision documentation and independently confirmed resource/node cleanup. Neither the goal nor TODO #7 is complete.

Canonical plans: [implementation](showcase-readiness-implementation.md), [tests](showcase-readiness-tests.md).
