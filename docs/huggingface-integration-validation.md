# Hugging Face integration acceptance and evidence

Catalog version 1. Base: current main `e9807058` (Meta/Muse PR #5 included).
The methodology follows `meta-integration-tests.md`: explicit cases, fresh local
fixtures, independent identity assertions, regressions, honest blocked live checks
and cleanup. The old Muse budget grants no spending permission here.
The product catalog has 5 cases / 19 sub-tests: 13 passed offline and 6 blocked.
Exact-head CI and review are workflow checks recorded on the draft PR, outside
these product acceptance counts.

## Acceptance matrix

| Case | Sub-test | Expected evidence | Status |
| --- | --- | --- | --- |
| HF01 Setup | .1 Fresh exports for both reviewed routes | Exact nested ID/provider suffix, default Codex-only harness selection, no direct OpenAI/Groq auth | Passed: focused setup tests |
| HF01 Setup | .2 Missing, placeholder, unsafe credentials | Named-field errors without secret echo | Passed: focused setup tests |
| HF01 Setup | .3 Primary/fallback/Hermes override gating | Enabled provider and existing token required; unused overrides ignored | Passed: focused setup tests |
| HF01 Setup | .4 Hub/policy/provider rejection | Reject unreviewed models, missing org and automatic policies | Passed: focused setup tests |
| HF02 Config | .1 OpenClaw registry and env reference | Correct chat URL/API, reviewed IDs, no embedded token, idempotent registration | Passed: executable shell fixture |
| HF02 Config | .2 Hermes nested IDs/fallback order | Exact mapped primary and ordered fallbacks, scoped named provider/auth | Passed: real config helper |
| HF02 Config | .3 Managed updates and config preservation | Unrelated channels/providers retained; existing auto/preserve bytes unchanged | Passed: real config helper |
| HF02 Config | .4 Sequential/reasoning settings | Parallel=false for both agents; OpenClaw effort vocabulary; Hermes medium | Passed: configuration contract assertions |
| HF03 Runtime boundary | .1 Credential presence | Native HF_TOKEN slot checked; absent key fails closed | Passed: executable shell fixture |
| HF03 Runtime boundary | .2 Exact configured-model readback | Accept exact routes; reject suffix loss, substitution and fallback drift | Passed: executable readback fixture |
| HF03 Runtime boundary | .3 Fresh native agent startup/restart | Real OpenClaw/Hermes config acceptance and settings persist | Partial: OpenClaw native foreground startup, clean stop and restart passed; combined case remains blocked on Hermes native runtime/container availability |
| HF03 Runtime boundary | .4 Account authorization | Existing token scope, credits and provider/model access confirmed | Blocked: HF_TOKEN/HUGGINGFACE_HUB_TOKEN absent from the executor environment; scope, credits and access unverified |
| HF04 Live agents | .1 OpenClaw 20B/120B response identity | Real agent response, requested/served model and provider, fallbacks empty | Blocked: paid inference prohibited |
| HF04 Live agents | .2 Hermes 20B/120B response identity | Same through real Hermes agent session | Blocked: paid inference prohibited |
| HF04 Live agents | .3 Sequential fresh-file tool/follow-up | Fresh random nonce/numbers, real tool trace, independent sum/hash and same-session follow-up | Blocked: paid inference prohibited |
| HF04 Live agents | .4 Parallel negative contract | Native request captures show false; sequential handling or clear unsupported response | Blocked: paid inference prohibited; vendor explicitly does not support parallel calls |
| HF05 Regression | .1 Terraform fmt/validate/mock tests | Credential-free plans | Passed: formatting/validation, 96 existing plans and final 9-case HF rerun; see history below |
| HF05 Regression | .2 Setup/doctor/runner tests | Broad regressions plus final focused changes | Passed: 230 broad setup, 20 doctor, 860 runner; final 15 focused setup tests |
| HF05 Regression | .3 Shell/Python syntax, repo idempotence | Clean generated-work cleanup | Passed: syntax, final isolated checkout-idempotence check and removed test workspaces |

15 focused Python tests passed. Individual tests include multiple input/route/mode
subcases; these are not inflated into extra acceptance passes. Config helper/shell
fixtures are local integration checks, not native agent or live API evidence.
Nine new Terraform plan cases cover exports, rejection and unused overrides.

## Repeatable commands and evidence rules

```sh
python3 -m unittest discover -s skills/agent-stack-setup/tests -p test_huggingface_models.py
terraform test -filter=tests/huggingface.tftest.hcl
scripts/test-local.sh
```

Use the repository's pinned test requirements in a task-owned virtual environment.
The full runner copies the worktree to ignored disk storage, runs only mocked
Terraform plans, and checks checkout idempotence/temporary-work cleanup. Logs for
this task are ignored under `.tmp/huggingface/`; no credential values or private
transcripts are retained as evidence. Synthetic `hf_OfflineFixtureOnly` values are
nonfunctional test inputs.

The read-only public catalog observation confirmed both selected routes live with
tool support and 131,072 context. No authentication was sent, no generation endpoint
was called and no weights were downloaded. Catalog status can change after the
observation; offline tests do not assert perpetual availability.

A future separately authorized live run must use exact versioned agents in a
fresh isolated runtime; never point tests at shared/live infrastructure. With
fallbacks empty, create fresh random-number/nonce files and independently verify
real agent tool traces/results and follow-up behavior for each route/agent pair.
Record requested, configured and served identities separately. Establish finite
request/token/step/retry bounds and a fresh authorized budget first; stop at
account gates. Do not create persistent credentials or use the historical Muse
budget. Confirm restart persistence and remove only task-owned fixtures/resources.

Changed dependencies invalidate dependent results: model metadata/parameters
invalidate HF02.4 and related runtime/live cases; credentials invalidate HF01.2,
HF03 and live cases; Hermes-only changes leave unrelated OpenClaw evidence valid.
Keep failed attempts separate from current passes; never relabel blocked tests as
passed because config or vendor docs look correct.

## Cleanup and review state

No cloud apply, VM creation, inference server, paid inference, persistent credential
creation, shared deployment, website publication, or Splash edit occurred. Existing
worktrees and unpublished selectors remain untouched. Leave the draft PR unmerged
for morning review. The published final-head CI outcome is recorded on
[draft PR #6](https://github.com/ferrerluis/agent-stack/pull/6) and its matching
commit checks; a local partial rerun is not represented as a fresh all-green full run.

The final focused readback regression also verifies that an unavailable non-HF
primary cannot skip validation of a requested HF fallback. The official Hermes
resolver functions were executed with stubbed registry/secret boundaries against
source blob verified at commit `8259f4ad805f23fc15e1138aded1530ea28da64c`;
exact model IDs, HF_TOKEN lookup and scoped request overrides passed. This is
partial resolver evidence, not a native agent startup or model-turn result.

## Native OpenClaw follow-up

OpenClaw **2026.10.4** ran with official Node **v24.22.0** in task-owned storage.
The initial scripts-disabled, optional-dependencies-omitted install could validate
configuration but lacked the native filesystem binding. Installing the exact
official optional prebuild `@openclaw/fs-safe-linux-arm64-gnu@0.24.2`, matching
`@openclaw/fs-safe@0.24.2`, closed that gap without a source build or guard bypass.
The official prebuild probe returned `{"status":"ready"}` with
`FS_SAFE_NATIVE_MODE=require`; native config validation then reported no warnings.
See the [official fs-safe installation contract](https://github.com/openclaw/fs-safe)
and [OpenClaw foreground gateway commands](https://docs.openclaw.ai/cli/gateway).

The lifecycle probe used fresh user, mount, PID and network namespaces. It mounted
the task runtime read-only, masked the real `/home` and `/root`, and put all agent
state/cache/workspace in a private 512 MiB tmpfs. The network had only loopback and
no external routes. Synthetic local gateway/HF token strings were the only auth
inputs; discovery, scheduled tasks, browser and updates were disabled. No model
turn, channel message, service installation or inference request was issued.

Two consecutive foreground `gateway run --bind loopback` cycles each:

- returned HTTP 200 with `ready: true` from `/readyz`;
- returned the exact configured primary `huggingface/openai/gpt-oss-20b:groq`
  and fallback `huggingface/openai/gpt-oss-120b:groq` through the native CLI;
- preserved config SHA-256
  `4fd8ca80560b54023801bf7efd2825ac2dbe94b8dac9dddcda8365195255c795`
  and the independent workspace sentinel hash;
- exited with status 0 after SIGTERM and released its private loopback listeners.

The readiness payload also reported CPU/event-loop degradation under local load;
these passes establish startup and configuration persistence, not performance.
The outer namespace exited with status 0, its tmpfs state (including an internally
generated config-journal key) was discarded, and its temporary mount directories
were removed. No host port or persistent service was exposed. Redacted task-only
evidence is in `.tmp/huggingface/native/lifecycle-result.json`.

HF03.3 retains its original combined OpenClaw/Hermes scope: the OpenClaw portion
now passes, while full native Hermes startup/restart remains unverified. The
product catalog therefore remains **13 passed / 6 blocked**, with this partial
result made explicit. Live authorization, served-model identity and real tool
turns remain blocked independently of native startup success.

## Local run history and workflow checks

- The initial broad runner used a deeply nested path. Terraform plugins exceeded
  Linux's Unix socket path limit; existing showcase tests also rejected the long
  temporary path. That attempt failed and is retained separately.
- A short-path focused attempt encountered a transient provider-cache file-busy
  error while initialization overlapped. Initial export assertions also needed
  explicit synthetic dashboard values to make the environment known during a
  mocked plan, and an explicit tuple/list conversion. These test fixtures were
  corrected before publishing the implementation. The final focused Terraform
  run passed **9/9**.
- The short-path broad snapshot passed all **96 existing Terraform plans**, but
  still contained the earlier HF export assertion: **96 passed, 1 failed, 8
  skipped**. Its Python phases passed **230 setup, 20 doctor and 860 runner
  tests**, with no skips. The final HF suite passed **15/15** after the additional
  fallback-readback regression; these overlap the broad setup count and are not
  added together.
- The broad runner's initial Git status snapshot became stale when the feature
  was staged/committed while it ran, so its idempotence row failed. A separate
  final check at the published implementation head passed shell/Python syntax,
  whitespace and checkout idempotence. Both broad test workspaces were removed;
  only task-owned caches and redacted evidence were retained.
- Automatic approval review rejected a request to stop obsolete local test
  processes because it identified sibling-process risk. The stop was not retried;
  that run finished normally. No sibling process was stopped and no approval is
  pending.
- The native lifecycle follow-up retained four unsuccessful probe attempts: a
  retired memory-setting fixture, an undiagnosed gateway bootstrap exit, a cache
  snapshot exceeding the intentionally tiny masked-home tmpfs, and a wrong `ss`
  executable path after readiness had already succeeded. The final probe used
  the supported memory-plugin setting, a cache under the existing private tmpfs
  and the correct executable path. Both complete lifecycle cycles then passed;
  filesystem safety remained required throughout.

The existing GitHub workflow runs the full credential-free suite from a clean
checkout. Before final handoff, compare its successful run head SHA with both the
PR head and pushed branch. Record the exact run URL, SHA, counts and any review
findings in the PR description, leaving this source history intact. Native Hermes
startup/restart, served-model identity and paid tool turns remain blocked even if
that CI is green. No credential file was inspected; the environment presence-only
check emitted booleans, never token values.
