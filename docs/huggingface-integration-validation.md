# Hugging Face integration acceptance and evidence

Catalog version 1. Base: current main `e9807058` (Meta/Muse PR #5 included).
The methodology follows `meta-integration-tests.md`: explicit cases, fresh local
fixtures, independent identity assertions, regressions, honest blocked live checks
and cleanup. The old Muse budget grants no spending permission here.

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
| HF03 Runtime boundary | .3 Fresh native agent startup/restart | Real OpenClaw/Hermes config acceptance and settings persist | Blocked: native binaries/container runtime unavailable on this executor |
| HF03 Runtime boundary | .4 Account authorization | Existing token scope, credits and provider/model access confirmed | Blocked: no credential/account changes or authenticated model calls authorized |
| HF04 Live agents | .1 OpenClaw 20B/120B response identity | Real agent response, requested/served model and provider, fallbacks empty | Blocked: paid inference prohibited |
| HF04 Live agents | .2 Hermes 20B/120B response identity | Same through real Hermes agent session | Blocked: paid inference prohibited |
| HF04 Live agents | .3 Sequential fresh-file tool/follow-up | Fresh random nonce/numbers, real tool trace, independent sum/hash and same-session follow-up | Blocked: paid inference prohibited |
| HF04 Live agents | .4 Parallel negative contract | Native request captures show false; sequential handling or clear unsupported response | Blocked: paid inference prohibited; vendor explicitly does not support parallel calls |
| HF05 Regression | .1 Terraform fmt/validate/mock tests | Credential-free full suite | Pending |
| HF05 Regression | .2 Setup/doctor/runner tests | Full suite with pinned dependencies | Pending |
| HF05 Regression | .3 Shell/Python syntax, repo idempotence | Clean generated-work cleanup | Pending |
| HF05 Regression | .4 Exact-head CI/review | PR head SHA matches successful CI, findings resolved/disclosed | Pending |

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
for morning review. Final regression/CI evidence is recorded below after execution.

The final focused readback regression also verifies that an unavailable non-HF
primary cannot skip validation of a requested HF fallback. The official Hermes
resolver functions were executed with stubbed registry/secret boundaries against
source blob verified at commit `8259f4ad805f23fc15e1138aded1530ea28da64c`;
exact model IDs, HF_TOKEN lookup and scoped request overrides passed. This is
partial resolver evidence, not a native agent startup or model-turn result.
