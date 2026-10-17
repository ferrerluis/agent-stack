# OpenCode integration acceptance and validation

Verified on 2026-10-17 UTC. Scope: optional OpenCode coding harness, independent
of personal-agent providers, Codex, Claude Code and Muse Code. No Splash changes.
Base: current `main` at `e98070588c784706b95ee048ed96a7e3380b2668`.

## Official contract checked

- [Install and supported platforms](https://opencode.ai/docs/): native CLI,
  Node package and platform installers exist. Windows users are directed toward
  WSL. AgentStack's Ubuntu workspace supports Linux glibc ARM64 and x64; macOS,
  Windows, musl and other architectures are outside this integration's runtime
  matrix. The x64 baseline artifact avoids requiring AVX2.
- [Official pinned V1 release](https://github.com/anomalyco/opencode/releases/tag/v1.19.3)
  and [its installer source](https://github.com/anomalyco/opencode/blob/v1.19.3/install):
  V1 and V2 share the `opencode` name. AgentStack selects an exact V1 tag,
  requires the GitHub release asset's SHA-256 digest, verifies the download and
  checks the executable version. It never runs the mutable install script.
- [Configuration](https://opencode.ai/docs/config/): global JSON/JSONC, provider
  allowlists, model references, update settings and project/inline overrides.
  AgentStack creates only the initial user config, preserving existing native
  JSON, JSONC and legacy config. Model/provider changes after seeding are manual.
- [Authentication](https://opencode.ai/docs/providers/): provider environment
  variables or interactive connection/login; native saved auth lives under
  `~/.local/share/opencode/auth.json`. No authentication is provisioned here.
- [CLI](https://opencode.ai/docs/cli/): TUI, `run`, JSON events, model selection,
  session continuation and export. [Pinned run source](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/cli/cmd/run.ts)
  and native tests establish that headless permission requests are rejected
  unless auto approval is requested. Tool rejection can still produce exit 0.
- [Permissions](https://opencode.ai/docs/permissions/): initial `* = ask` supports
  supervised operation. Project or agent settings can override user defaults.
  Unattended execution needs separately reviewed auth, permissions, timeout and
  independent result verification. No auto approval or server exposure is added.

## Repeatable acceptance matrix

`test_opencode.py` exercises the production installer RUN block, seed Python and
command wrapper rather than separate reimplementations. The native tests are an
explicit opt-in and use only a scripted localhost OpenAI-compatible endpoint.
They demonstrate CLI/tool/config contracts, not real model reasoning or vendor
inference. Fixture API strings never authenticate to an external service.

| Case | Expected result | Evidence / result |
| --- | --- | --- |
| OC01.1 Fresh setup and existing tfvars merge | OpenCode selected; chief model and other tool defaults unchanged | Setup renderer tests: pass |
| OC01.2 Selection combinations | Codex only, each optional combination and all tools coexist | Renderer tests + mocked plans: pass |
| OC01.3 Invalid choices | Missing Codex/workspace/model, duplicates, unknown harness, V2/mutable release and unsafe model rejected | Renderer tests + mocked failure plans: pass |
| OC02.1 Linux installation and repeat run | ARM64 and x64 baseline select exact artifacts and reproduce executable install | Two-pass installer fixtures: pass |
| OC02.2 Installation failure | Bad/missing digest, duplicate asset, wrong tag, failed download and unknown architecture stop before wrapper install | Installer fixtures: pass |
| OC02.3 Official ARM64 artifact | Published digest and binary version agree | Native `1.19.3`: pass; digest below |
| OC03.1 Fresh user config | Explicit model/provider, updates off, permission prompts, owner user and file mode 0600 | Seed tests + real CLI config parse: pass |
| OC03.2 Existing workspace data | JSON/JSONC/legacy config and auth fixture bytes/mode survive repeat seed and changed Terraform model | Seed tests: pass; no real auth read |
| OC03.3 Repair path | Config failure warns while keeping SSH startup reachable | Failure fixture + entrypoint contract: pass |
| OC04.1 Shell/SSH command behavior | Wrapper forwards arguments, disables auto update and propagates native exit code | Executed wrapper fixture: pass; native process runs as UID 1000 |
| OC04.2 Container shell, SSH, restart/rebuild | Binary reachable through real workspace SSH; home/config/session hashes persist | Blocked: no Docker runtime in execution session; unchanged persistent-home and PATH wiring covered locally |
| OC04.3 Disable after enable | Rebuild removes managed binary/seed; retained user files survive | Default-off mocked rendering: pass; real rebuild blocked as OC04.2 |
| OC05.1 Native provider/model config | Seeded config parses; requested provider/model catalog is available | Real CLI `debug config`, `models fixture`: pass |
| OC05.2 Native headless file task | Read fixture, edit only calculator, run immutable tests and independently check result | Scripted localhost model + real read/write/bash tools: pass |
| OC05.3 Session evidence | Native JSON session ID continues and exports as the same session | Real CLI `run --session <id>` and `export <id>`: pass |
| OC05.4 Approval behavior | Headless prompt rejects without hanging or modifying fixture | Native `tool_use` error and unchanged bytes: pass; exit status alone is insufficient |
| OC05.5 Safe native failures | Missing auth, unknown model and invalid config fail without localhost model requests/fallback | Native failures: pass |
| OC05.6 Real provider/model and supervised/unattended use | Actual provider identity, agent-chosen repair, follow-up and reviewed permissions | Blocked: persistent login, credentials and paid inference not authorized; no model quality claim |
| OC06.1 Regressions | Terraform fmt/validate/mocked plans, setup, doctor, controller tests and runtime syntax pass | Pass: 105 Terraform plans, 226 setup passes + 5 native skips, 20 doctor and 861 controller/helper tests; syntax checks passed |
| OC06.2 Cleanup and scope | No test processes/resources/credentials left; source status unchanged by runner | Pass: runner status unchanged; temporary test copies/probe homes removed; caches/evidence retained in task-owned ignored storage |

## Commands and evidence

Run the repository's safe default suite with declared dependencies in a task-owned
virtual environment:

```bash
python3 -m pip install -r scripts/requirements-live.txt
AGENT_STACK_TEST_ROOT="$PWD/.tmp/t" scripts/test-local.sh
```

On pi-main, use disk storage for caches/downloads and a short disk test path if
needed for provider Unix socket length. The test root must meet `AGENTS.md` and
runner storage checks. Preserve existing caches; do not clear other tasks' data.

After independently verifying a native artifact digest:

```bash
OPENCODE_TEST_BINARY=/absolute/path/to/verified/opencode \
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s skills/agent-stack-setup/tests -p test_opencode.py -v
```

Local verified artifact: `opencode-linux-arm64.tar.gz`, SHA-256
`7900818a6a98881edbbadab4b7525940eff3b9750040c200481f9f56cacee405`.
The tests use fresh temporary homes, an explicit provider allowlist, disabled
model fetching/default plugins/LSP downloads/update checks and no inherited
credential environment. The fixture allows only its calculator read/edit and
`python3 checks.py`; all other permissions remain `ask`. A 35-second child
watchdog kills its process group on timeout; the server has a request ceiling.
The independent check verifies correct addition and unchanged `checks.py` hash.

OpenCode acceptance module with native checks enabled: **15 passed, zero skipped**
(10 local cases and 5 native cases), including same-session continuation/export.
The standalone container/helper regression module: **15 passed**, including the
legacy/all-harness template regression.
Full-suite Terraform validation and **105 mocked plans passed**; setup recorded
**226 passes and 5 intentional native skips** (231 total), and **20 doctor tests
passed**. The native cases also passed separately as recorded above. **861 controller/helper tests passed**, with runtime shell/Python syntax and
runner source-status idempotence checks also passing. The final full runner
exited 0 and removed its temporary worktree. GitHub Actions results are attached
to the published PR head; the PR report records that head and run URL. Earlier failed fixture
attempts and long-path provider startup failures are retained separately in
ignored task logs and are not counted as passes.

## Cleanup, deployment limits and website handoff

No Terraform apply/destroy, cloud instance, paid resource, model bill, real persistent
credential creation, real auth inspection, shared infrastructure change, merge
or website publication occurred. Native test servers and homes are disposable.
The verified binary and dependency/provider caches remain in task-owned disk
storage for reproducibility; no credential or transcript is committed.

For the separate morning website handoff: add `opencode` as an independent
optional harness with Codex locked on; export `workspace_opencode_release` and
`workspace_opencode_model`; require an explicit native `provider/model`; explain
separate workspace authentication, first-seed preservation, Linux-only runtime,
V1 pin and supervised-versus-unattended limits. Do not conflate this selector
with Hugging Face/personal-agent model providers or claim live inference passed.
This PR contains no Splash files. Shared integration touchpoints are
`variables.tf`, the runtime/proof maps in `main.tf`, setup renderer and README;
merge independent provider changes with their respective defaults intact.
