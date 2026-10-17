# Hugging Face integration acceptance and evidence

Catalog **version 2** replaces the rejected GPT-OSS offerings with Luis's confirmed
Kimi K3, Muse Spark Open and DeepSeek V4.1 Flash shortlist. Base remains current
main `e9807058`; this is the same draft [PR #6](https://github.com/ferrerluis/agent-stack/pull/6).
Old route-specific passes are historical, not evidence for the replacement models.

The product catalog has **4 cases / 19 sub-tests: 13 passed offline, 6 blocked**.
Regression CI/review are separate workflow checks below; their exact-head status
is recorded on the PR so publishing results does not change the tested commit.
The methodology follows `meta-integration-validation.md`: explicit acceptance,
fresh fixtures, independent identity checks, regressions, failed-attempt history,
honest boundaries and cleanup. The old Muse budget grants no spending permission.

## Product acceptance matrix

| Case | Sub-test | Evidence | Status |
| --- | --- | --- | --- |
| HF01 Setup | .1 Fresh exports for each of three exact routes | Nested organization/ID and Baseten suffix retained; coding harness defaults independent | Passed: setup fixture |
| HF01 Setup | .2 Missing/placeholder/unsafe credentials | Named-field failure without token echo | Passed: setup fixture |
| HF01 Setup | .3 Primary/fallback/Hermes override gating | Provider and existing token required; unused overrides ignored | Passed: setup and mocked plans |
| HF01 Setup | .4 Removed/unhosted/automatic routes | Reject GPT-OSS, arbitrary IDs, unpinned policies and DeepSeek -1017 | Passed: setup and mocked plans |
| HF02 Config | .1 OpenClaw registration and env SecretRef | Explicit three-model catalog, HF URL, no embedded token, idempotence | Passed: executable shell fixture and native CLI validation |
| HF02 Config | .2 Hermes nested IDs and fallback order | Exact routes and scoped HF provider/auth slot | Passed: config helper and native named-provider resolver |
| HF02 Config | .3 Managed updates and preservation | Retain unrelated settings; prune stale HF picker entries; auto/preserve bytes unchanged | Passed: real helpers |
| HF02 Config | .4 Distinct effort and sequential policy | Supported labels only; managed high; parallel=false; off unavailable | Passed: contract assertions and native selector/wire probe |
| HF03 Offline runtime boundaries | .1 Credential presence fails closed | HF_TOKEN slot checked; absent key rejected | Passed: executable shell fixture; real credentials absent |
| HF03 Offline runtime boundaries | .2 Exact configured-model readback | Reject suffix loss, substitution or HF fallback drift | Passed: shell regression and native OpenClaw CLI |
| HF03 Offline runtime boundaries | .3 OpenClaw parser/selector/replay | Enabled efforts and legacy-off clamp, 1/2 tool calls, matched results and follow-up | Passed: 42 native mocked-SSE cases, 126 in-memory requests |
| HF03 Offline runtime boundaries | .4 Hermes resolver/history/SDK body | Reasoning, content and tool fields survive normalization/history/JSON replay and SDK merge | Passed: 6 native cases, 18 mocked HTTP requests |
| HF03 Offline runtime boundaries | .5 OpenClaw startup/stop/restart | Fresh config acceptance, readiness, exact routes, hashes, clean exit and listener release | Passed: two isolated foreground cycles with the replacement catalog |
| HF04 Native/live completion | .1 Full Hermes startup/restart | Fresh native service/agent session with persistence | Blocked: full agent/service environment not provisioned; repository Docker runtime unavailable; transport modules are tested separately |
| HF04 Native/live completion | .2 Account authorization | Existing HF token scope, credits, model/provider terms and access | Blocked: HF_TOKEN/HUGGINGFACE_HUB_TOKEN absent; no credential setup or spending authorized |
| HF04 Native/live completion | .3 OpenClaw response identity | Real responses for all three, configured/requested/served IDs and provider, fallbacks empty | Blocked: authenticated paid inference prohibited |
| HF04 Native/live completion | .4 Hermes response identity | Same through real Hermes turns | Blocked: authenticated paid inference prohibited |
| HF04 Native/live completion | .5 Fresh-file tools and follow-up | Random nonce/numbers, actual tool trace, independent sum/hash and same-session follow-up per route/agent | Blocked: paid inference prohibited |
| HF04 Native/live completion | .6 Hosted parallel behavior | Real provider request captures and tool results with bounded execution | Blocked: paid inference prohibited; managed configuration requests sequential calls |

Tests may have multiple subcases; 42/6 fixture cases are not added to the 19
acceptance rows. Synthetic responses prove transport behavior, not model capability,
served identity, provider acceptance or successful authenticated API access.

## Hosted availability and contract evidence

[Public catalog evidence](huggingface-hosted-catalog.json) records unauthenticated
HF router and Hub mapping observations. Each selected Baseten pair was live,
`supports_tools: true`, non-free and 1,048,576 context. Muse's HF ID maps to Baseten's
different internal slug; the request keeps the HF ID. Muse's Hub weights are gated,
while its account's hosted authorization remains unknown. The dated DeepSeek
`-1017` checkpoint has no provider mapping and is rejected rather than substituted.
No generation endpoint or weights download was used for this research.

The hosted contract is distinct from the base model: DeepSeek's card describes
numeric effort 1–100, but Baseten documents high/max for V4.1 Flash (plus a disabled
mode which managed setup does not offer). K3 accepts low/high/max; Muse accepts
low/medium/high/xhigh. Managed high is supported by each. Do not generalize these
vocabularies to other providers or claim arbitrary Hub compatibility.

## Native contract probes

OpenClaw **2026.10.4**, `@openclaw/ai` **2026.10.4**, official Node **v24.22.0**:

```sh
node skills/agent-stack-setup/tests/native/huggingface_openclaw.mjs \
  /path/to/node_modules/@openclaw/ai/dist
```

The probe executes the real catalog thinking selector, extra-parameter wrappers
and managed SSE parser/serializer with an in-memory fetch implementation. Global fallback fetch
throws. It checks each offered effort, unsupported legacy level clamping, one/two
returned calls, exact route, enabled reasoning, matched tool results and a later
user turn. All **42 cases / 126 mocked requests** passed. No real model ran.

A negative probe exposed the off-mode issue: an outbound compatibility mapping
could send low while the parser discarded reasoning based on raw off intent.
The final catalog removes unsupported selector levels with `thinkingLevelMap`,
and setup sets per-model thinking high. The final selector+wire probe verifies
legacy off executes at low for Kimi/Muse and high for DeepSeek, preserving history.
Raw adapter calls bypassing the selector with off/none remain outside this contract.

Hermes source **`4b1adbfe5d274c5dbfaa981fcc4dd68391867701`**, Python 3.14 and its
official pinned SDK/dependencies (`openai==2.24.0`, YAML/dotenv/requests/psutil):

```sh
PYTHON=/path/to/task-venv/bin/python
"$PYTHON" skills/agent-stack-setup/tests/native/huggingface_hermes.py \
  /path/to/pinned/hermes-agent
```

The probe creates a temporary HERMES_HOME and a child environment without operator
credentials. It uses the real named-provider resolver with credential reads disabled,
response normalizer, assistant-history builder, outbound serializer and OpenAI SDK.
An HTTP mock captures final JSON bodies; socket connections are prohibited. One/two
calls and subsequent turns preserve reasoning (including whitespace), content, call
IDs/arguments and matching results through JSON replay. All **6 cases / 18 mocked
requests** passed. The agent object and model responses are fixtures; this does not
certify the full agent loop, SQLite session resume or native service restart.

## Native OpenClaw lifecycle and cleanup

The exact official optional prebuild `@openclaw/fs-safe-linux-arm64-gnu@0.24.2`
matched `@openclaw/fs-safe@0.24.2`; its official probe reported ready with
`FS_SAFE_NATIVE_MODE=require`. No Rust source build or guard bypass was needed.
Native CLI validation accepted the generated three-model config without warnings
and returned the exact primary/fallback order without changing the file.

Two foreground `gateway run --bind loopback` cycles ran in fresh user, mount, PID
and network namespaces. The runtime was read-only; real `/home` and `/root` were
masked; state/cache/workspace and any internally generated journal key lived in a
private tmpfs. Only private loopback existed, with no external route. Synthetic
local auth was used; discovery, scheduled tasks, browser and updates were disabled.
Both cycles returned ready, preserved config and independent sentinel hashes,
exited 0 after SIGTERM and released listeners. CPU/event-loop degradation under
local load was observed; this is lifecycle evidence, not a performance result.
No agent turn/inference, channel message or persistent service was started.
The namespace exited and discarded its state; temporary mount directories were removed.
Task evidence, including hashes, is under `.tmp/huggingface/shortlist/`.

## Regression workflow and repeatable local checks

```sh
python3 -m unittest discover -s skills/agent-stack-setup/tests -p test_huggingface_models.py
terraform test -filter=tests/huggingface.tftest.hcl
terraform fmt -check
scripts/test-local.sh
```

The revised focused suite passed **17 Python tests and 11 mocked Terraform plans**.
The plans cover exports and both removed GPT-OSS/unhosted DeepSeek rejection.
The full credential-free GitHub workflow runs formatting, validation, all mocked
plans, setup/doctor/controller tests, syntax and checkout idempotence. Record its
actual counts/skips, final run URL and SHA on the PR; compare local HEAD, pushed
branch, PR head and successful run head. No new HF test may be called passed from
a skip. Human review remains pending; do not merge, deploy or publish the website.

Native probe dependencies/downloads are task-owned on disk. Only tiny namespace
mount points are under `/tmp`; runtime state is volatile. No cloud apply, VM,
inference server, paid call, persistent credential setup, shared deployment or
Splash edit occurred. Preserve the isolated worktree for review, and remove only
task-owned processes/workspaces. Live access remains blocked even with green CI.

## Failed-attempt history and invalidation

- Version 1 used two GPT-OSS/Groq routes. Its previous CI/native checks remain
  historical and are superseded for current model-specific claims.
- Earlier local tests failed on long Unix socket paths, an overlapping provider
  cache and stale test/checkout snapshots. Those fixtures were corrected, separate
  final runs passed, and completed test workspaces were removed.
- Automatic approval review once rejected stopping obsolete test processes due to
  sibling-process risk. It was not retried; the run finished normally. No sibling
  was stopped and no permission remains pending.
- Initial native startup probes had a retired memory setting, generic bootstrap
  failure, too-small masked-home cache, and wrong `ss` path. The supported setting,
  private cache and correct executable resolved these without relaxing safety.
- Shortlist probe development first used the wrong SDK fetch/body seam and wrong
  bundled selector/path. These harness attempts failed before passing corrected
  native tests. The actual off-mode reasoning-loss failure led to the selector fix.
- The first revised Terraform invocation lacked initialized modules; another was
  prevented from opening provider plugin sockets in the sandbox. Reusing the
  task-only initialized data directory and an authorized mocked-plan execution
  passed all 11. A sandboxed namespace attempt was unavailable; the isolated native
  lifecycle test then ran in the authorized execution environment.

Changes to IDs/providers, effort metadata, history serialization or credentials
invalidate their dependent setup/runtime/live rows. New authenticated evidence must
use fresh isolated sessions, empty fallbacks, independent files/results and bounded
requests/tokens/retries, with a fresh authorized budget. Never substitute synthetic
responses or catalog flags for real tool turns or account access.
