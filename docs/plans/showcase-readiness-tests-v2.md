# TODO #7: revised testing and execution plan

Status: revised plan prepared under Luis's request, 2026-10-11. Evidence: [tool-call bottleneck audit](showcase-testing-efficiency-audit.md). This replaces the execution strategy for the new goal; it preserves the current [nine acceptance cases and 44 reporting subchecks](showcase-readiness-tests.md#recaps-every-15-minutes).

## Result and bounds

Complete all six DigitalOcean cases and the required light pass on each of Hetzner, AWS and GCP. Stay within **$35 total conservative cloud/model spend**; the current bound is $26.13 with $8.87 remaining. Keep existing accounts/projects/defaults unchanged, use only new owned resources, build containers on the paid test VMs and verify cleanup. Do not introduce a separate image build/cache pipeline.

The current browser repair at `977020a` has a passing full qualification receipt: **1,053 Python tests plus 79 Terraform checks, zero skips**. Its free actual-gateway browser replay passes. No final cloud acceptance case is complete, and no paid trial is active. These are starting facts, not a claim that the revised workflow below already exists.

## 1. Repair proof isolation before further paid trials

The current import walker includes browser code in 20 of 30 verifier fingerprints. Separate stage adapters/import boundaries and make the executed verifier dependency set explicit. Keep hashes of all code that can decide a check's verdict, safety rules and genuine prerequisites; do not simply exclude a shared file or stop hashing harness code.

Required regression evidence:

- A browser-only change directly changes the `before_reboot` and `after_reboot` verifier fingerprints and their genuine dependents. It does not change deploy, light, resolved-content, native-doctor or cleanup verifier fingerprints.
- A provider Terraform/input change invalidates that provider's affected checks; another provider's unchanged normalized inputs remain eligible to carry.
- A doctor verifier change invalidates doctor proof and required downstream proofs. Changing an unrelated verifier does not invalidate it.
- An image, installed package, host or Hermes startup repair change invalidates checks that use those resolved contents, even if a recipe or mutable image tag is unchanged.
- Changed, missing, forged or wrongly bound evidence cannot carry. Every new attempt requires new native cleanup evidence.
- A newly introduced verdict-changing dependency must be covered; an incomplete dependency declaration must fail validation rather than silently preserve old proof.

Use the retained DO29 manifests only to review their recorded contents. A newly built VM always requires fresh actual image/host/repair observations; recipes do not prove content equality.

## 2. Stop rerunning the complete suite for unrelated commits

Extend the existing local receipt mechanism rather than adding another pipeline. Record a fingerprint of the executable deployment inputs, test inputs, fixtures, verifier, relevant dependency/tool versions and local test command. Keep the Git revision as provenance.

- Documentation-only edits and generated run names may reuse intact qualification of identical execution/test inputs. Validate the changed documentation's links and factual claims separately.
- Execution, policy, test, fixture, dependency or verifier changes require affected tests and complete qualification before a new release candidate launches.
- Run focused regression tests while repairing a reproduced bug. Run the complete required local suite once after the execution candidate is frozen; do not repeat it for unchanged inputs.
- Never reuse a receipt with changed output, failed tests, missing evidence or skips where checks are required.

Verify same-input reuse, docs-only reuse, actual code/test/verifier change refusal and tampered receipt refusal. Do not weaken the final qualification requirement to make a cache hit.

## 3. Bound agent supervision and output

Use the existing controller's automatic stages, watchdog and private run/cleanup records. Produce a small state summary at stage start, completion and failure; retain raw evidence privately on disk. Do not build a separate monitoring system.

Routine target per error-free cloud trial: **at most six assistant orchestration calls**, excluding Luis's requested 15-minute recaps:

1. Launch the qualified immutable candidate with fresh scope, account, price and budget gates.
2. Bind and verify the owned controller/watchdog.
3. Receive and inspect the terminal result.
4. Independently confirm exact native cleanup and reconcile the original cost hold.
5. Evaluate fingerprints and current acceptance/subcheck evidence.
6. Save the concise result and report it.

Batch independent reads; keep mutations, dependencies, approvals and cleanup sequential. Do not game the call target by hiding extra actions or returning large combined logs. Record assistant calls separately from underlying tool operations and native requests. Routine console summaries should stay under 4,000 characters, with artifact pointers for details.

While a run is healthy, its controller proceeds without repeated agent reads of time/process/progress between the requested recaps. Observe additional state only on a changed stage, actionable failure, safety/cleanup concern or terminal result. A quiet snapshot is not progress; a specific confirmed live handle is a verified wait, not a reason to restart.

Keep the 15-minute table for all nine cases and 44 subchecks. Show current passes, old passes needing re-proof, failures, running and pending checks. Report acceptance totals, changes, active resources and conservative cost bounds; provide no unsupported completion ETA.

## 4. Diagnose a specific failure before another paid retry

Use the smallest faithful reproduction and actual pinned components where protocol or version behavior matters. The passing actual OpenClaw gateway/frontend replay is the browser starting point; simulated replies alone cannot qualify a protocol fix.

Bound each diagnosis cycle to **20 new assistant calls**. If that produces no concrete failing reproduction or evidence that changes the next action, report the exact unresolved issue and change the strategy rather than repeating the same paid attempt. This bound never prevents authorized cleanup or other required safety work, and it is not permission to declare an unfinished goal complete.

One failed cloud attempt may still yield reusable current subcheck evidence if its inputs, verifier, dependencies, intact evidence and original native cleanup match. Preserve those results and all original cost history. Do not count historical, unmatched or simulated evidence as a current pass.

## 5. Execute the unchanged acceptance matrix

After the workflow repairs and one complete frozen-source qualification:

- **Hetzner, AWS and GCP, one owned VM at a time:** verify VM creation, OpenClaw/Hermes/workspace running, all three stable without restart or replacement for at least 120 seconds, actual resolved contents and native cleanup. Do not run doctors, paid coding tasks, private browser or reboot checks on these light providers.
- **DigitalOcean, one owned VM:** run the full credential-free container suite and resolved-content/workspace checks; both native doctors with independent evidence; authenticated direct Tailscale browser access to both apps; independently correlated Codex and Claude API-key file tasks as the workspace user; safe channel closure; reboot/disk/file persistence; post-reboot contents, doctors and authenticated access; independently verified cleanup.
- Use the cheapest currently available compatible machine allowed by the approved account permissions and fresh native quote. Do not broaden IAM or change existing accounts/projects to select a cheaper unsupported size.
- Verify the README against the final evidence and save/link the implementation and test plans. Documentation is a separate acceptance case and cannot substitute for native checks.

If a light pass is already reusable under corrected fingerprints, retain it rather than creating another VM. The ordering above obtains required provider evidence without coupling light providers to a DigitalOcean app-level bug; it does not reduce DigitalOcean's full scope.

## 6. Keep memory concise

Native receipts and the compact current progress snapshot are the operational source of truth. Save durable decisions, the adopted plan and the final outcome in PARA, with links from TODO #7; preserve existing historical records. Do not append each poll or routine failure to four full memory documents. Update memory when the strategy changes, a required case completes or the final result is established, using the required fresh-read/write/readback workflow once for the relevant artifact.

## Replacement execution goal

Complete approved AgentStack TODO #7 under this revised plan: repair and regression-test stage-specific verifier dependencies and execution-input-based local qualification reuse; then prove all nine acceptance cases, comprising six full DigitalOcean cases and one light deployment/stability/resolved-content/cleanup pass each on Hetzner, AWS and GCP. Preserve actual image/host/Hermes repair fingerprints, verifier and dependency evidence, original cost history, existing defaults/accounts/projects and the $35 total conservative cloud/model cap. Build on paid VMs, with no separate build/cache pipeline. Qualify each changed frozen execution candidate, preserve unrelated valid proofs, target no more than six routine assistant orchestration calls per error-free cloud trial excluding requested 15-minute recaps, and bound unproductive diagnosis cycles to 20 calls before reporting a concrete blocker or changed strategy. Completion requires evidence-matched documentation, saved linked plans, all nine cases verified and native cleanup; local mocks and unmatched historical results do not count.

The goal slot was checked after writing this plan and is empty. Luis explicitly requested a new goal, so the replacement can be set in this task without marking the earlier unfinished work complete or creating another task. The native acceptance history remains authoritative; a new goal does not reset spend or turn old results into current passes.
