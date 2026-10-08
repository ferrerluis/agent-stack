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

The approved ceiling is $25 total cloud/model spend and four hours per attempt. The user now permits as many test or bug-fix attempts as fit that cash cap. Set `max_attempts` to `null` for this authorization, or retain a positive integer for a stricter count limit. Changing an existing batch's count limit requires an explicit recorded approval; it preserves all original attempts, purchases, cost proofs and other limits. The cost-reduced private inputs use a 105-minute maximum and 512 MiB of outbound traffic per boot; 1 GiB remains the default. Delete resources immediately after success or failure. Incoming image downloads do not consume the outbound allowance, and internal Docker traffic must not consume it.

A proposed 70-minute maximum leaves only 55 minutes before cleanup, less than the container phase's required 3,400-second window even before deployment. The chosen maximum allows the 30-minute apply timeout followed by that window, with 15 minutes reserved for cleanup. Refresh the complete quote for the chosen duration; Hetzner rounds a full 105-minute VM lifetime to two hours.

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

Transfer allowances use the measured provider rate and enforced quota, with two boots, framing/measurement and account margins. DigitalOcean's measured minimum charge is also included; the independent $0.50 cleanup hold stays in place. Future quotes omit the former generic $0.50 transfer floor when the complete measured bound is smaller. This tightens a spending allowance; it does not change provider bills, old reservations or closed-cost proofs.

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

A failed native container check retains its bounded command output in the attempt's owner-private `container-command-error.json`. The credential-free suite supplies the failing profile, a stable reason and bounded command output; arguments, stdin and environment values are excluded. Unknown exceptions stay redacted, normal status receipts contain no raw output, and diagnostics cannot count as acceptance evidence.

Runtime command failures use `runtime-command-error.json`; restoration failures use a separate `runtime-restore-error.json` so cleanup cannot overwrite the first failure. The VM's lifecycle report includes only the action, a fixed phase/reason and an allowlisted program/operation. It retains no arguments, input, environment or raw nested-command output, and unknown exception text stays redacted. Both runtime and standalone agent watchdogs stop and independently verify the timer and service, including a service collected during disarm.

OpenClaw's native `config.get` response contains materialized runtime defaults. The route verifier accepts only the pinned version's nonrouting compaction mode and subagent concurrency/archive defaults; additional keys, alternate models/providers and unexpected values still fail. Applied-revision equality, a single exact model/endpoint, full token bounds and blocked tools remain required. Failures identify the rejected check using fixed labels, without recording configuration or credentials.

The pinned Gateway projects config revisions as `hmac-sha256:v1:` plus a canonical, unpadded base64url SHA-256 digest. The verifier requires that exact format and equality between configured and applied revisions; raw file hashes, malformed tokens and stale revisions cannot establish active configuration.

## Recorded cost investigation, 2026-10-07

Confirmed prepaid purchases were $10.30; cloud bills were unreconciled. The earlier $22.24 figure included conservative allowances. Verified cleanup of six failed trials released $3.90 of unused allowances, for an $18.34 ceiling. The first Hetzner trial's native cleanup further released $0.55, for $19.14. These are conservative bounds, not actual cloud charges, and the request for $30 was withdrawn.

The first Hetzner trial built the workspace and ran all three services healthy, but setup exceeded its apply timeout while a 30-second model-catalog command continued for over 15 minutes. A scoped stop was recorded; the trial remained failed and every resource was confirmed deleted. No paid model request ran. The revised wrapper closes interactive input and bounds both the container command and its Docker client, with a forced-stop grace.

The read-only samples showed roughly 2.5 GiB of busiest observed memory use. Native pricing offered an available CX23 with 4 GiB / two CPUs at $0.0104/hour in fsn1, making it a cheaper candidate for the remaining Hetzner attempt. Full task, container, browser and reboot acceptance remains required for that machine.

A broader regional AWS comparison priced all 73 eligible x86_64, non-accelerator, 4/8 GiB On-Demand candidates after retries. M7a.medium was the lowest price found with full CPU accounted for, at $0.05796/hour; its complete VM/disk/IPv4 unit bound was $0.071293334/hour. DigitalOcean's 18 available regional candidates at or above 4 GiB yielded s-2vcpu-4gb at $0.035714286/hour. Both native scope and quota checks passed; build performance and full acceptance remain pending.

GCP e2-standard-2 remained cheapest among the supported predefined, non-shared E2 sizes checked at or above 4 GiB; its 8 GiB selection costs less than the 4 GiB high-CPU choice. Fresh complete planning quotes forecast $1.29 for AWS, $1.16 for DigitalOcean and $1.77 for GCP at a two-hour maximum, with account/rounding margins, disk/IP, transfer and cleanup reserves included. These forecasts create no resources or spending reservation; every actual launch repeats the checks. Use the free peer and the CX23 candidate to keep the remaining forecast within $25.

AWS, DigitalOcean and GCP used both originally approved attempts. Further trials need explicit approval of the retry count within the existing $25 cap. Hetzner's first attempt used one; all 24 final-revision live cells were still unverified at this checkpoint.

The second originally authorized Hetzner trial used the CX23 at revision `61d3b08`, after all 848 Python tests and 64 Terraform checks passed. Its three-minute readiness window expired with OpenClaw/Hermes starting and the workspace healthy; the timing points to image downloads consuming that window, but the Docker journal was not captured to prove the cause. No paid model request ran, and every owned resource was independently confirmed deleted after roughly 11 minutes. Its $1.14 allowance tightened to a $0.57 closed bound, leaving a $19.71 conservative total.

The installer now downloads selected remote images before applying runtime files or starting health checks, with a ten-minute bound and forced-stop grace. It excludes the locally built workspace image. The existing health requirements and three-minute readiness window remain intact; failed downloads stop before applying runtime files.

At a 105-minute maximum, the next four planning allowances are AWS $1.25, DigitalOcean $1.14, GCP $1.72 and Hetzner $1.12: $5.23 combined, or $24.94 including the current bound. These forecasts preserve the original native price timestamps; every actual launch refreshes its prices and refuses to exceed $25. All four providers used both originally approved attempts. The user subsequently removed the attempt-count limit while preserving the $25 cap; no extra model funding was authorized. All 24 live cells remain unverified.

At revision `2b72471`, all 858 Python tests and 64 Terraform checks passed. Two further Hetzner CX23 trials were confirmed deleted: one was interrupted during bootstrap, and the other passed deployment/SSH with all three services healthy but failed its real container check. The error output was discarded, so its cause remains unproved. Neither invoked paid model tasks. Each closed cost bound was $0.57, bringing the conservative total bound to $20.85; this is not a reconciled cloud bill. The diagnostic retention above addresses that missing evidence before another paid iteration.

Iteration05 at `1e26c8f` passed all 863 Python tests and 64 Terraform checks, then passed native deployment/SSH before failing legacy container SSH readiness. Retained test-container logs show four successful key-authenticated logins and UID 1000; the test then reused its original randomly published port after restart. [Docker documents that allocated ports may change on restart](https://github.com/moby/moby/blob/master/api/docs/v1.24.md). A regression that changes the published port reproduces the stale endpoint for both workspace profiles and passes after refreshing both the port and SSH command. Restart now also verifies the persisted host-key fingerprint, and readiness failures retain the last bounded client error. Native validation of this repair remains pending.

Iteration06 at `03bb3ae` passed all 866 Python tests and 64 Terraform checks. Its native legacy container profile completed, with the published SSH port changing from 32768 to 32770, confirming the endpoint repair. The later container phase failed while disarming its cleanup timer: stopping a transient timer and its never-started service together can return an error after the timer collects that service. A real local systemd reproduction confirmed this behavior; the repair stops and verifies each unit independently, accepts a disappearing unit only after a fresh inactive-state proof, and retains the cleanup program when proof is unavailable. Probe failures now include an allowlisted phase/reason without exposing arbitrary exception text. All twelve cloud trials were confirmed deleted, the conservative total bound was $21.99, no paid model task had run, and all 24 final-revision live cells remained unverified. The repaired timer still requires native cloud validation.

Iteration05 has independently verified cleanup and a $0.57 closed cost bound. All eleven paid trials are cleaned up, with no paid model task invoked. The conservative total bound is $21.42, leaving $3.58 within $25; these figures are not reconciled provider bills. Preserve the original trial history and native cleanup proofs when continuing under the approved unlimited-attempt count.

The timer repair at `42dd31e` passed all 870 Python tests and 64 Terraform checks without skips, including both real local systemd disarm cases. The next candidate also removes the generic transfer floor as described above and requires its own full local receipt before paid execution. All twelve prior cloud trials remain cleaned up at the unchanged $21.99 conservative bound.

Iteration07 at `da18669` passed all 871 Python tests and 64 Terraform checks, then passed native deployment/bootstrap, both real container profiles and the private model channel. The container proof verifies UID 1000, SSH, persistent files and disarmed cleanup at that exact revision. OpenClaw activation subsequently failed before any paid model request; the native cause remains unproved because its failure output was not retained. A duplicate combined timer/service stop in the runtime lifecycle was reproduced with real local systemd, and both unused-service and running-service cases pass after the shared runtime/agent repair. The repair and private diagnostics still need their exact-revision full suite and native validation. All thirteen cloud trials were confirmed deleted; iteration07's receipt-bound cost ceiling is $0.08, bringing the conservative total to $22.07. All 24 final-revision live cells remain unverified.

Iteration08 at `fc84bd2` passed all 878 Python tests and 64 Terraform checks, native deployment/bootstrap, both real container profiles in 156 seconds and the private model channel. OpenClaw activation failed in `prepare` / `native_route` validation before any paid model request. The saved report does not identify the exact rejected field. The pinned upstream snapshot pipeline materializes compaction/subagent defaults that the old verifier rejected; a local regression reproduces this incompatibility. The narrower verifier repair and fixed check labels still require their full local suite and fresh native validation. All fourteen trials have verified cleanup, iteration08 closed to $0.08, and the conservative total bound is $22.15. These bounds are not invoices; all 24 final-revision live cells remain unverified.

Iteration09 at `1c8cf35` passed 881 Python tests and 64 Terraform checks, native deployment/bootstrap, both real container profiles in 93 seconds and the private model channel. OpenClaw activation failed with `openclaw_applied_revision_unverified`; no paid model request ran. The pinned upstream projector returns versioned HMAC tokens, while the old verifier required plain hexadecimal hashes. A local regression reproduces rejection of the supported token format; the repaired proof still requires exact configured/applied equality. This repair needs its exact full suite and fresh native validation. All fifteen trials have verified cleanup; iteration09 closed to $0.08, bringing the conservative total bound to $22.23 and leaving $2.77 under $25. All 24 final-revision live cells remain unverified.
