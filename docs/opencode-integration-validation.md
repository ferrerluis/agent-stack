# OpenCode integration acceptance and validation

Verified on 2026-10-17 UTC. Scope: optional **HF-only OpenCode coding harness**,
independent of personal-agent selections, Codex, Claude Code and Muse Code.
No Splash changes. Base: `main` at `e98070588c784706b95ee048ed96a7e3380b2668`.

## Official contracts and evidence boundaries

- [Install/platforms](https://opencode.ai/docs/),
  [pinned V1 release](https://github.com/anomalyco/opencode/releases/tag/v1.19.3)
  and [pinned installer](https://github.com/anomalyco/opencode/blob/v1.19.3/install):
  Ubuntu workspace uses Linux glibc ARM64 or x64 baseline (no AVX2 requirement).
  Windows/WSL, macOS and musl are outside this runtime matrix. Exact V1 release,
  published asset SHA-256 and executable version are checked. The mutable V1/V2
  install script is never executed.
- [OpenCode configuration](https://opencode.ai/docs/config/) and
  [providers/auth](https://opencode.ai/docs/providers/#hugging-face): native
  JSON/JSONC precedence, provider/model allowlists and separate HF authentication.
  The [official provider catalog](https://github.com/anomalyco/models.dev/blob/dev/providers/huggingface/provider.toml)
  records `huggingface`, `HF_TOKEN`, `@ai-sdk/openai-compatible` and
  `https://router.huggingface.co/v1`. No auth is provisioned or inspected here.
- [HF chat API](https://huggingface.co/docs/inference-providers/tasks/chat-completion)
  and [function calling](https://huggingface.co/docs/inference-providers/guides/function-calling):
  OpenAI-compatible chat requests, streaming tools and provider-dependent effort.
  [Public router metadata](https://router.huggingface.co/v1/models) and Hub
  `inferenceProviderMapping` were read without authentication. The compact
  [catalog receipt](../tests/fixtures/opencode-hf-catalog.json) records timestamp,
  source URLs/digests, live Baseten mappings, tools and 1,048,576-token context.
  This establishes public availability, **not authenticated account access**.
- Model cards: [Kimi K3](https://huggingface.co/moonshotai/Kimi-K3),
  [Muse Spark Open](https://huggingface.co/meta-models/Muse-Spark-Open-816B-A42B),
  [DeepSeek V4.1 Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash).
  Kimi always reasons and requires full assistant `reasoning_content` and
  `tool_calls` replay. Muse's Hub weights have manual gating; no gate was accepted
  and no weights downloaded. Hosted eligibility remains untested.
- [Baseten reasoning contract](https://docs.baseten.co/inference/model-apis/reasoning)
  and [Muse public API launch](https://www.baseten.co/blog/muse-spark-open-launch/):
  the selected hosted routes use string efforts. DeepSeek's underlying model card
  describes numeric 1–100; Baseten instead documents `none/high/max`, warns that
  `low` runs at default depth, and supports `high` explicitly. We offer high/max.
  Kimi offers low/high/max, Muse low/medium/high/xhigh. All default to supported
  `high`; none/off are excluded. No universal medium or numeric passthrough.
- Pinned [provider source](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/provider/provider.ts),
  [message transform](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/provider/transform.ts)
  and [provider schema](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/core/src/v1/config/provider.ts):
  explicit `interleaved: reasoning_content`, reasoning capability/options and
  model-specific variants. Native binary tests below exercise the actual parser,
  serializer and file tools, rather than a local serializer reimplementation.
- [CLI](https://opencode.ai/docs/cli/), [permissions](https://opencode.ai/docs/permissions/)
  and [pinned run source](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/cli/cmd/run.ts):
  headless `ask` auto-rejects and can still exit 0. Initial permissions support
  supervised TUI use. Unattended operation requires separately reviewed auth,
  scoped permissions, timeouts and independent verification. No auto approval,
  server, remote attachment or delegation integration is enabled.

## Exact independent model choices

| OpenCode reference | HF request `model` | Offered efforts |
| --- | --- | --- |
| `huggingface/moonshotai/Kimi-K3:baseten` | `moonshotai/Kimi-K3:baseten` | low/high/max |
| `huggingface/meta-models/Muse-Spark-Open-816B-A42B:baseten` | `meta-models/Muse-Spark-Open-816B-A42B:baseten` | low/medium/high/xhigh |
| `huggingface/deepseek-ai/DeepSeek-V4.1-Flash:baseten` | `deepseek-ai/DeepSeek-V4.1-Flash:baseten` | high/max |

Use the public HF ID, including for Muse: its internal Baseten providerId
`meta-models/muse-spark-open` is metadata, not the model sent to the HF router.
The unhosted DeepSeek `-1017` revision, GPT-OSS HF routes, direct vendor providers,
automatic provider selection and fallback are rejected. Personal-agent models
and workspace OpenCode models are separate required choices when enabled.

Startup creates the initial 0600 user config only when native JSON/JSONC/legacy
config is absent. It sets both main and small model explicitly, three reviewed
HF models, a text-only reviewed profile and conservative 8,192-token client output cap, updates off and `* = ask`.
Existing files/auth/sessions are untouched. The launcher checks native HF API-key
presence before inference and fails locally when absent; it does not attempt a
paid request to validate credentials. Native compatible-provider retries on missing
auth were observed in a bounded failed fixture, motivating this explicit guard. The launcher captures native effective
config without logging its potentially sensitive content, rejects conflicting
model/URL/agent/command/reasoning settings, and overlays the reviewed provider
contract in memory. Persistent agent effort overrides must stay at `high`; choose
other efforts through the model variant picker. Unsupported config fails with
repair guidance; SSH and other harnesses remain usable. This is a configuration
contract, not a security boundary against the workspace owner or arbitrary plugins.

## Repeatable acceptance matrix

`test_opencode.py` executes production seed/launcher code and the real installer
RUN block. Native tests use a separately checksum-verified OpenCode executable,
fresh homes and bounded scripted localhost responses. Fixture tokens never
leave loopback. These tests prove runtime contracts, **not model quality or
actual hosted inference**.

| Case | Expected result | Evidence/result |
| --- | --- | --- |
| OC01.1 Fresh setup/existing tfvars | Explicit independent model, chief model unchanged | Renderer tests: pass |
| OC01.2 Enable/disable/tool combinations | Codex mandatory, optional tools independent, default off | Renderer, real template render and mocked plans |
| OC01.3 Invalid input | Missing Codex/workspace/model, duplicates, unknown harness, mutable/V2 release, non-HF/unreviewed/unpinned/unhosted model rejected | Renderer and mocked failure plans |
| OC02.1 Install/idempotency | ARM64 and x64 baseline exact release/digest, repeat install works | Two-pass executed installer fixtures: pass |
| OC02.2 Install failure | Bad/missing digest, duplicate asset, wrong tag, failed download/unsupported architecture stop before wrapper | Installer fixtures: pass |
| OC02.3 Official native artifact | Published SHA-256 and binary version agree | Verified ARM64 1.19.3: pass |
| OC03.1 Fresh user config | Exact main/small model, HF-only provider/models, per-model efforts, ask, mode600/user owner | Production seed + native parse: pass |
| OC03.2 Existing data | JSON/JSONC/legacy/auth/session bytes survive repeat seed/Terraform model change | Production preservation tests: pass |
| OC03.3 Repair path | Seed error warns, SSH startup remains reachable | Failure fixture/entrypoint contract: pass |
| OC04.1 Workspace command | Wrapper quotes arguments, disables updates, propagates failure; native readback only lists three HF models | Executed wrapper + native managed launcher: pass |
| OC04.2 Container SSH/restart/rebuild | Real image launch, SSH PATH, config/session hashes retained | Blocked, not run: no existing container runtime on pi-main |
| OC04.3 Disable after enable | Render omits executable/seed, persistent user data retained | Default-off render: pass; real rebuild blocked as OC04.2 |
| OC05.1 HF serialization | Each exact model ID/provider suffix and high default on wire; all offered variant values serialized | Real native OpenCode + scripted loopback SSE: pass |
| OC05.2 Reasoning/tool history | Assistant reasoning/text, tool IDs/arguments/results retained through read/write/bash and same-session follow-up | Real native transcript capture for all3: pass |
| OC05.3 File repair/evidence | Calculator repaired; immutable independent tests/hash, session export/continuation | Native tools + independent verifier: pass |
| OC05.4 Approval semantics | Headless ask rejects without hanging or modification | Native tool error/unchanged file: pass; exit0 alone is insufficient |
| OC05.5 Safe failures | Missing HF auth, unknown model, invalid config/endpoint, disabled/incompatible effort, agent/command overrides reject without fallback/config leak | Native + production launcher rejection tests: pass |
| OC05.6 Live hosted operation | Account eligibility, HF-to-Baseten forwarding, real reasoning/tools/task quality, supervised/unattended use | Blocked, not run: credentials and paid inference unauthorized |
| OC06.1 Regressions | Terraform/setup/doctor/controller/syntax checks | 109 mocked plans; 231 setup passes + 8 native skips; 20 doctor passes; controller/renderer repair evidence below. Exact-head CI on PR |
| OC06.2 Cleanup | No test resources/credentials left, runner Git status unchanged | Pass: runner removed test copies; native homes/servers cleaned; caches/evidence retained |

## Reproduction and recorded results

Use task-owned dependencies and disk caches under `AGENTS.md` storage rules:

```bash
python3 -m pip install -r scripts/requirements-live.txt
AGENT_STACK_TEST_ROOT="$PWD/.tmp/t" scripts/test-local.sh

OPENCODE_TEST_BINARY=/absolute/path/to/checksum-verified/opencode \
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s skills/agent-stack-setup/tests -p test_opencode.py -v
```

Verified ARM64 archive SHA-256:
`7900818a6a98881edbbadab4b7525940eff3b9750040c200481f9f56cacee405`.
Native tests disable model refresh, default plugins, LSP downloads, project config
and updates, omit inherited credentials and use a closed external proxy with
loopback bypass. A 35-second child watchdog kills process groups on timeout; the
scripted server has a request ceiling. Only calculator read/edit and
`python3 checks.py` are allowed; other tools remain ask.

Revised OpenCode module: **23 passed, zero skipped** (15 offline, 8 native),
rerun after final model-field rejection was added. This includes the default high
transcript contract for all three routes, every offered native effort, HF auth
source matching and missing-auth failure before inference. Fixtures follow the
[pinned native auth schema](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/auth/index.ts).

The full local runner passed **109 mocked Terraform tests**, **231 setup tests
with 8 intentional native skips**, **20 doctor tests**, runtime syntax and Git
status idempotence. Its 861 controller/helper cases found one file-dependency
regression: the standalone renderer read the OpenCode launcher even when disabled,
but a deferred legacy source bundle correctly omitted that optional file. The
read is now conditional. The exact failing deferred-template case passed after
repair, and the complete **15-test container/template-helper module passed**.
The final strict model-field guard was separately covered by the final native
rerun. The PR records broad exact-head CI results and distinguishes environment
skips; the earlier generic-provider head's CI is historical evidence only.
Failed attempts are retained in ignored task logs and are not counted as passes.

## Cleanup, blocked live checks and website handoff

No apply/destroy, paid resource/inference, real credential creation/configuration,
auth inspection, shared infrastructure change, merge or website publication.
Native homes/servers are disposable; verified binary, caches and evidence remain
in task-owned ignored disk storage. No real credential/transcript is committed.

The Docker/boot/SSH blocker was checked on actual **pi-main** using host-level
read-only probes, not inferred from the sandbox: Docker/Podman/containerd tools,
packages, services and system/rootless sockets are absent; no remote Docker
context is configured. Installing a runtime or changing infrastructure is outside
the permitted existing capabilities. The workspace directory is on pi-main;
no alternate executor was used. These checks are **blocked/not run**, not passed.

Morning website handoff: add `opencode` as an independent optional harness with
Codex locked on; require an independent three-choice model picker with the exact
references above; export `workspace_opencode_release` and
`workspace_opencode_model`; do not default/copy the personal-agent selection.
Explain separate HF auth, preservation/repair, Linux V1 scope, supported reasoning
variants, supervised versus unattended operation, and blocked live checks.
No Splash files are changed. Shared merge touchpoints are `variables.tf`,
`main.tf`, setup renderer/question groups, README and tfvars example; preserve the
independent personal-agent schema/defaults from the sibling HF PR.
