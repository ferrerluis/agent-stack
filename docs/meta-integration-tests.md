# Meta integration acceptance tests

All cases validate product changes. Scheduling, guideline maintenance and cleanup are workflow requirements, not acceptance cases. The active catalog has 9 cases / 36 sub-tests.

| Case | .1 | .2 | .3 | .4 |
| --- | --- | --- | --- | --- |
| TC01 Setup | Render both Meta references | Reject missing/placeholder keys | Require Codex; validate optional tools | Preserve defaults when new settings omitted |
| TC02 Agent configuration | Install/enable OpenClaw Meta provider and catalog | Map Hermes primary/fallback routes | Respect manage/auto/preserve | Runtime credentials reach intended processes without image/log exposure |
| TC03 Workspace installation | Codex-only works | Selected tools launch; unselected absent | User shell and SSH work | Restart/rebuild preserves files/auth/Codex behavior |
| TC04 OpenClaw + Spark | Confirm configured model and correct response | Tool reads fresh fixture | Return independently verified tool result | Follow-up uses result |
| TC05 OpenClaw + Wonder | Confirm configured model and correct response | Tool reads fresh fixture | Return independently verified tool result | Follow-up uses result |
| TC06 Hermes + Spark | Confirm configured model and correct response | Tool reads fresh fixture | Return independently verified tool result | Follow-up uses result |
| TC07 Hermes + Wonder | Confirm configured model and correct response | Tool reads fresh fixture | Return independently verified tool result | Follow-up uses result |
| TC08 Muse Code | Authenticate/read fixture as workspace user | Fix deliberate bug in intended file | Run fixture tests | Independently verify correctness and permitted edits |
| TC09 Regression | Terraform fmt/validate | Terraform test | Setup/doctor unit suites | Runtime shell/Python syntax |

## Repeatable steps and expected results

- TC01: run `python3 -m unittest discover -s skills/agent-stack-setup/tests -p test_meta_models.py` and the Meta/harness cases in `terraform test`. Valid configurations render exact model refs and default `["codex"]`; invalid keys/selections return nonzero with a named field and no secret values.
- TC02: run the Hermes tests in the same suite and Terraform render assertions; deploy a fresh stack, verify `openclaw models list --provider meta`, inspect provider/model names only, and repeat bootstrap with manage/auto/preserve. Expected: native Meta provider, ordered fallbacks, preserved operator content, no exposed credentials. Local assertions alone do not prove plugin availability.
- TC03: build Codex-only and all-selected workspace images on the temporary DigitalOcean host; run version commands as the workspace user through container exec, interactive shell, and SSH. Create a sentinel/auth fixture, restart and rebuild, then compare contents/permissions and repeat version checks. Expected: only selected optional binaries, unchanged sentinels/auth, existing Codex behavior.
- TC04–TC07: select the named model with empty fallbacks; create a fresh random nonce and numbers in a runtime-visible file. Through the real agent session, ask it to read the file with a tool, calculate their sum, and return the nonce. Verify the tool trace and numeric result independently; ask a follow-up referencing the same values in the same session. Confirm runtime metadata matches the requested model; no raw provider-only calls substitute for agent turns.
- TC08: prepare `calculator.py` with an incorrect add implementation plus immutable tests; run Muse interactively or through `muse serve` as the workspace user with the Meta API key and finite time/API limits, requesting read/fix/test. Use Muse’s normal one-time command approval if Docker blocks its nested sandbox; approve only the exact fixture test command in the fixture directory. Keep Docker protections and persistent permission rules unchanged. Independently run the tests and compare the changed-file set against `calculator.py` only. An exit code alone is insufficient.
- TC09: run `bash scripts/test-local.sh`; map Terraform fmt/validate, Terraform tests, setup/doctor suites, and runtime syntax outcomes to their respective sub-tests. Other existing suites remain normal regression obligations without inflating the catalog.

## Evidence and invalidation

Keep generated results under `.tmp/meta-integration/`, with revision/versions, prerequisites, command, expected result, timestamps, redacted evidence and linked iterations. Use pending/running/passed/failed/blocked/stale. Live sub-tests remain pending or blocked until actually run.

Invalidate only changed dependencies: Hermes-only routing affects TC02.2/TC02.3 and TC06/TC07; shared Meta credentials affect dependent TC02 and TC04–TC08; workspace image/entrypoint affects TC03 and TC08; fixture/assertion changes affect their users. Preserve unrelated passes and historical attempts. A new commit alone does not invalidate all results. Add narrowly scoped regression sub-tests for new bugs and disclose catalog revisions.

## Live-run workflow requirements (outside the test catalog)

Use a fresh DigitalOcean deployment and state. Start with the smallest Basic
Droplet that supports the measured runtime memory needs; build images before
running agents, run cases sequentially, and disable unrelated services. Do not
provision while required credentials are unavailable.

Before paid calls, verify each runtime's output-token and model-step/retry caps,
reserve their worst-case API cost plus VM/storage time and cleanup allowance,
and keep the total below $15. Use a host-side timeout/watchdog so a dropped SSH
session cannot leave a model task running. Refuse an unbounded paid test rather
than treating a process timeout alone as a reliable spending cap. Keep a spend
record using provider usage/prices and actual resource lifetime. Destroy only
this task's resources immediately after testing or failure, and verify absence.

Keep the session's 15-minute reporting task active during testing. It reads the
results record and shows valid-passing parent/sub-test counts without running
competing tests. Reporting, credential handoffs, guideline publication, budget
tracking, and cleanup do not add product acceptance cases.
