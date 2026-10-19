# Hugging Face integration acceptance and evidence

Catalog **version 2** replaces the rejected GPT-OSS offerings with Luis's confirmed
Kimi K3, Muse Spark Open and DeepSeek V4.1 Flash shortlist. The base is main
`e9807058`; this is the same draft [PR #6](https://github.com/ferrerluis/agent-stack/pull/6).
Old route-specific passes are historical, not evidence for the replacement models.

The product catalog has **4 cases / 19 sub-tests: 14 passed offline or with native
synthetic responses, 5 blocked on hosted verification**. The deployed evidence
below was collected on 2026-10-19; it does not establish hosted model capability.
Regression CI/review are separate workflow checks below; their exact-head status
is recorded on the PR so publishing results does not change the tested commit.
The methodology follows `meta-integration-validation.md`: explicit acceptance,
fresh fixtures, independent identity checks, regressions, failed-attempt history,
honest boundaries and cleanup. The old Muse budget grants no spending permission.

## Product acceptance matrix

| Case | Sub-test | Evidence | Status |
| --- | --- | --- | --- |
| HF01 Setup | .1 Fresh exports for each of three exact routes | Nested organization/ID and Baseten suffix retained; all nine coding/personal selection combinations exported independently | Passed: setup fixtures; deployed distinct selections retained after restart |
| HF01 Setup | .2 Missing/placeholder/unsafe credentials | Named-field failure without token echo | Passed: setup fixture |
| HF01 Setup | .3 Primary/fallback/Hermes override gating | Provider and existing token required; unused overrides ignored | Passed: setup and mocked plans |
| HF01 Setup | .4 Removed/unhosted/automatic routes | Reject GPT-OSS, arbitrary IDs, unpinned policies and DeepSeek -1017 | Passed: setup and mocked plans |
| HF02 Config | .1 OpenClaw registration and env SecretRef | Explicit three-model catalog, HF URL, no embedded token, idempotence | Passed: executable shell fixture and native CLI validation |
| HF02 Config | .2 Hermes nested IDs and fallback order | Exact routes and scoped HF provider/auth slot | Passed: config helper and native named-provider resolver |
| HF02 Config | .3 Managed updates and preservation | Retain unrelated settings; prune stale HF picker entries; auto/preserve bytes unchanged | Passed: real helpers |
| HF02 Config | .4 Distinct effort and sequential policy | Supported labels only; managed high; parallel=false; off unavailable | Passed: contract assertions and native selector/wire probe |
| HF03 Offline runtime boundaries | .1 Credential presence fails closed | HF_TOKEN slot checked; absent key rejected | Passed: executable credential-free shell fixture |
| HF03 Offline runtime boundaries | .2 Exact configured-model readback | Reject suffix loss, substitution or HF fallback drift | Passed: shell regression and native OpenClaw CLI |
| HF03 Offline runtime boundaries | .3 OpenClaw parser/selector/replay | Enabled efforts and legacy-off clamp, 1/2 tool calls, matched results and follow-up | Passed: 42 native mocked-SSE cases, 126 in-memory requests |
| HF03 Offline runtime boundaries | .4 Hermes resolver/history/SDK body | Reasoning, content and tool fields survive normalization/history/JSON replay and SDK merge | Passed: 6 native cases, 18 mocked HTTP requests |
| HF03 Offline runtime boundaries | .5 OpenClaw startup/stop/restart | Fresh config acceptance, readiness, exact routes, hashes, clean exit and listener release | Passed: two isolated foreground cycles with the replacement catalog |
| HF04 Native/live completion | .1 Full Hermes startup/restart | Deployed managed service, two native CLI turns in one session, persistent config/sentinel checks across shared restart | Passed: synthetic gateway, native tools and independent file verification; no personal paid calls |
| HF04 Native/live completion | .2 Account authorization | Authorization for every selected model/provider pair | Blocked: temporary account access existed, but a sibling coding tool follow-up returned HTTP403 and stopped the shared paid experiment; personal model access remains unverified |
| HF04 Native/live completion | .3 OpenClaw response identity | Real responses for all three, configured/requested/served IDs and provider, fallbacks empty | Blocked: zero OpenClaw paid calls; synthetic transport evidence below cannot establish served identity |
| HF04 Native/live completion | .4 Hermes response identity | Same through real Hermes turns | Blocked: zero Hermes paid calls; synthetic transport evidence below cannot establish served identity |
| HF04 Native/live completion | .5 Fresh-file tools and follow-up | Random nonce/numbers, actual tool trace, independent sum/hash and same-session follow-up per route/agent | Blocked for hosted execution; both native agents passed a deployed synthetic two-turn case, with the Hermes creation-only caveat below |
| HF04 Native/live completion | .6 Hosted parallel behavior | Real provider request captures and tool results with bounded execution | Blocked: paid execution stopped; managed configuration requests sequential calls |

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

## Deployed native synthetic verification, 2026-10-19

An approved disposable deployment composed the HF product code at `817eca6d` with
the independent OpenCode PR, yielding local composition
`766a6375a02ac73c208451513c6338960742ccd1`. This evidence update changes only this
document; it does not change the product code tested in that composition.

The official pinned OpenClaw image ran **2026.10.4**, Node **24.21.0** and Undici
**8.11.2**. The official pinned Hermes image had an unknown source commit;
installed public source hashes identified its direct CLI dispatch, rather than
assuming the earlier cached source's gateway RPC behavior. Its runtime was Python
**3.14.7**, OpenAI **2.24.0**, httpx **0.28.1** and httpcore **1.0.9**. Its normal
managed service remained separate from the actual CLI model worker.

Both agents executed their full native CLI loops against synthetic responses.
Temporary final-HTTP instrumentation preserved the official
`https://router.huggingface.co/v1` classification and native serialized request
bytes, forwarding only the exact chat-completions endpoint to a bounded loopback
gateway. The remote runtimes held synthetic capabilities, with the real HF token
confined to the coordinator's gateway. The frozen hooks did not replace the SDK,
SSE parser, reasoning replay, native tool loop or security checks. Requests required
high effort, an explicit output cap at most 4,096, sequential tool calls, usage
reporting and a serialized body at most 32KiB. Synthetic ledger accounting is
separate from real provider charges.

| Deployed check | Observed result | Limit |
| --- | --- | --- |
| OpenClaw native session | Two turns in the exact same session; native read/write each turn; independent nonce/sum/delta and input/context/config hashes verified; official trajectory export checked and raw export removed | One synthetic route case; no hosted response or all-model capability claim |
| OpenClaw final HTTP | Six synthetic requests: 11,100 / 11,505 / 12,022 / 12,679 / 13,088 / 13,624 bytes; every prior reasoning carrier replayed | Short authored fixture context; no real high-effort response-size claim |
| Hermes native session | Two turns in the exact same session; four native read_file/write_file results, zero payload errors or stale-write blocks; independent nonce/sum/delta and immutable hashes verified | New output creation each revision, as explained below |
| Hermes final HTTP | Six synthetic requests: 19,094 / 19,694 / 20,435 / 20,928 / 21,532 / 22,292 bytes; every prior reasoning carrier replayed | No real hosted response; full native loop evidence is distinct from earlier parser controls |
| Independent selections | All nine coding/personal setup combinations exported; deployed coding DeepSeek and personal Kimi selections remained distinct, with empty personal fallbacks | Configuration/readback evidence, not model execution for each combination |
| Shared restart | All three managed containers restarted with images and config/sentinel hashes preserved; workspace SSH host identity matched before/after; distinct selections survived | No paid model turn after restart; Hermes image defines no container health check |

Hermes's official launcher resets PYTHONPATH before starting Python with `-P`.
The first direct-controller attempt therefore missed the external sitecustomize
preload. An exclusive temporary `.pth` imported the exact frozen final-HTTP hook
from a verified source file through the launcher's normal site processing. An
import-only official-launcher check preserved safe-path mode and hook identities;
the later full native turns established transport coverage beyond that check.
The temporary `.pth` was removed before the shared restart. No native source,
import-path security policy, SSL verification, SSRF checks or overwrite guard was
relaxed.

The first Hermes turn created and independently verified an output file. A fresh
CLI invocation resuming that session does not inherit the previous process's
in-memory full-file write baseline. The initial follow-up correctly refused to
overwrite the existing output. For the final fresh case, the operator verified
the prior output's regular-file status, ownership and recorded hash, recorded its
retirement, then removed only that fixture output before the unchanged controller
advanced the input. The resumed native agent created and verified a new output at
the same filename. This proves session continuation and creation per revision; it
does **not** prove cross-process overwrite success or recovery after a denied write.

Retained coordinator receipts include `deployed-claw-synthetic-proof.json`,
`deployed-hermes-synthetic-proof.json`, `shared-restart-receipt.json` and
`restart-independent-selections.json`. The Hermes proof SHA256 is
`db6da700d58a64158ea2bcaecef3a310de2e54235675b9c8202f76e92ebd78d0`.
Only sanitized evidence is summarized here; credentials, resource identities,
private filesystem locations and raw reasoning are excluded.

## Hosted stop and cleanup

The shared paid experiment stopped after its second upstream attempt returned
**HTTP403** on a sibling coding tool follow-up. The first request had succeeded;
the recorded usage cost was **$0.0011214**, and the ledger retained the full
**$0.0301056** model reservation after the failure. The HTTP403 cause remains unknown;
its raw vendor body was not retained, and no credential, balance, provider or
reasoning diagnosis is asserted. The gateway disabled paid execution without a
retry. **Zero paid OpenClaw calls, zero paid Hermes calls and zero of nine planned
full paid sessions completed.** Neither synthetic ledger values nor the sibling's
single successful request establish personal-agent hosted capability.

Cleanup receipts verify the temporary `.pth` was removed before restart, the
synthetic leases closed, and local gateway/tunnel/watchdog processes became
inactive. Every owned cloud resource deletion returned HTTP204 and subsequent lookup
returned HTTP404. An independent second inventory at **2026-10-19 04:24:44 UTC** found
all six owned identities absent and zero matching resource names; the unrelated
default VPC remained. Credential and local sensitive-file cleanup completed at
**2026-10-19 04:28:13 UTC**. The temporary HF token's official revocation returned
HTTP202, with an identity check changing from HTTP200 to HTTP401. The exact
temporary cloud API token was deleted and its harmless account check returned
HTTP401; the pre-existing token was untouched. Both temporary credential files,
the test SSH key/capability, raw state and backups, plans, tfvars and private logs
were removed without inspecting raw state. The private directory is empty;
provider caches and sanitized evidence remain. No paid call followed the HTTP403 stop.

## Regression workflow and repeatable local checks

```sh
python3 -m unittest discover -s skills/agent-stack-setup/tests -p test_huggingface_models.py
terraform test -filter=tests/huggingface.tftest.hcl
terraform fmt -check
scripts/test-local.sh
```

The revised focused suite passed **18 Python tests and 11 mocked Terraform plans**.
The plans cover exports and both removed GPT-OSS/unhosted DeepSeek rejection.
The full credential-free GitHub workflow runs formatting, validation, all mocked
plans, setup/doctor/controller tests, syntax and checkout idempotence. Record its
actual counts/skips, final run URL and SHA on the PR; compare local HEAD, pushed
branch, PR head and successful run head. No new HF test may be called passed from
a skip. Human review remains pending; do not merge, deploy or publish the website.

Native probe dependencies/downloads are task-owned on disk. The earlier local
namespace tests used volatile runtime state; the later disposable deployment and
its cleanup are reported separately above. Preserve the isolated worktree and
sanitized evidence for review. This PR remains draft and unmerged; no website or
Splash change was published. Hosted verification remains blocked even with green CI.

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
- The deployed Hermes image differed from the cached gateway-backed source.
  Installed source inspection established direct CLI dispatch; the earlier cached
  gateway assumptions were not applied to that image.
- Deployed Hermes attempt 1 failed before gateway admission because the official
  launcher cleared the external preload path. Attempt 2 reached the gateway but
  the synthetic reply generator misread Hermes's JSON/line-numbered read result.
  Both failed records and launch markers were retained; a corrected fresh case
  passed its first native turn.
- Deployed Hermes attempt 3 resumed successfully at the CLI level but its native
  overwrite guard refused the second write. The output retained the independently
  verified first-turn hash. The verifier rejected the file mismatch despite exit 0;
  it was never reported as a passed session. Attempt 4 used the explicit verified
  fixture-output retirement described above and passed both turns. Attempts 1–3
  remain failed evidence, not retries relabeled as success.

Changes to IDs/providers, effort metadata, history serialization or credentials
invalidate their dependent setup/runtime/live rows. New authenticated evidence must
use fresh isolated sessions, empty fallbacks, independent files/results and bounded
requests/tokens/retries, with a fresh authorized budget. Never substitute synthetic
responses or catalog flags for real tool turns or account access.
