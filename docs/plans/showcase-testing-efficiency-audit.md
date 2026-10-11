# AgentStack testing: tool-call bottleneck audit

Status: measured baseline and proposed corrective plan, 2026-10-11. Canonical companion: [revised test plan](showcase-readiness-tests-v2.md).

The largest identifiable waste is repeated verification and supervision, amplified by verifier fingerprints that include unrelated browser code. The acceptance requirements are still nine cases; they have not been reduced to fit the results.

## Counting method

The baseline ends immediately before Luis's request for this audit. It contains **5,816 assistant-issued tool calls across the whole thread**, of which **5,766 occurred after the execution goal began**. Counts below use that goal scope. One assistant call may batch several underlying tool operations; a shell command may execute many tests. These numbers measure the agent's external calls, not individual unit tests, native HTTP requests or shell subprocesses.

The categories are a reproducible primary-purpose classification of recorded tool arguments, with one category per call. Mixed calls retain their additional labels in the content-free audit data. Category boundaries are heuristic: a call that reads, patches and runs a test is work associated with that test, rather than proof that a suite executed three times. Exact total counts and classified category counts should not be confused with exact native stage instrumentation.

| Work associated with | Assistant tool calls | Share |
|---|---:|---:|
| Browser and peer investigation | 846 | 14.7% |
| Cleanup and cost ledger | 316 | 5.5% |
| Cloud launch, preflight and prices | 395 | 6.9% |
| Coding tasks and model verification | 184 | 3.2% |
| Container builds, bootstrap and workspace | 430 | 7.5% |
| Fingerprint and carryover review | 47 | 0.8% |
| Local qualification and unit checks | 1304 | 22.6% |
| Memory and documentation | 972 | 16.9% |
| Other setup and coordination | 54 | 0.9% |
| Polling and coordination | 629 | 10.9% |
| Reboot and persistence | 6 | 0.1% |
| Runtime doctors and activation | 353 | 6.1% |
| Source inspection and editing | 230 | 4.0% |

**2,955 calls (51.3%) included polling, process checks or status coordination**, often batched with other work. This overlaps the categories above and must not be added to them. Only 629 were classified as pure polling/coordination. The recorded argument text mentions `write_stdin` 1,624 times and `clock__curr_time` 1,055 times; these are static code mentions, not exact counts of executed nested operations.

The goal-scope log contains approximately **33.5 million returned text characters**. Local qualification/unit work returned 11.0 million; browser/peer work 4.7 million; memory/documentation 4.1 million. These are logged text volumes, not billed token counts or exact causal attribution. The recorded usage delta is 20,118,871 uncached input tokens and 6,011,222 output tokens; reasoning tokens are included in output and must not be added again. Cached context is separate. Per-test billed token attribution is not available.

## What the native trials actually achieved

There are **51 reserved trial records**, all terminal failures, with cleanup recorded as verified. They comprise 25 DigitalOcean, 21 Hetzner, three AWS and two GCP trials. A reserved trial is not necessarily a successfully created VM or an invoiced compute charge.

| Native stage | Historical passes | Recorded observations |
|---|---:|---:|
| VM provisioning | 37 | 51 |
| Bootstrap | 36 | 36 |
| Container suite | 26 | 36 |
| Resolved installed contents | 12 | 13 |
| Runtime doctors | 13 | 19 |
| Independent doctor evidence | 13 | 13 |
| Codex task | 1 | 4 |
| Claude task | 2 | 5 |
| Authenticated browser before reboot | 0 | 6 |
| Reboot and checks after reboot | 0 | 0 |
| Light VM/container stage | 1 | 1 |

These are historical named-stage results, not current acceptance passes and not independent per-app counts. They show repeated real successes that have not produced a finished acceptance case. Current completion remains **0/9 final cases**. The recent reporting breakdown has two saved-plan subchecks passed out of 44; it is not a replacement for native proof.

| Terminal failure stage | Trials |
|---|---:|
| apply | 14 |
| containers | 10 |
| before_reboot | 6 |
| doctors | 6 |
| openclaw_activation | 4 |
| claude | 3 |
| codex | 3 |
| hermes_activation | 3 |
| hermes | 1 |
| resolved_contents | 1 |

## Why the work repeats

1. **Verifier dependencies are too broad.** `verifier_files()` walks imports across entire modules. Browser code enters deploy, doctor and cleanup fingerprints through `showcase_terraform → showcase_remote → showcase_local_peer → showcase_peer → showcase_browser`. A browser file edit directly changes 20 of 30 named verifier fingerprints, including `apply`, `light`, `resolved_contents`, `doctors` and `verified_cleanup`; prerequisite propagation can affect more. This defeats the intended isolation of the approved layered design. It is an implementation defect, not a reason to discard fingerprinting or stop hashing verifier code.
2. **Local qualification is tied to the full Git revision and full source-file set.** The existing receipt cache explicitly requires the same `source_revision`. Even a documentation-only commit misses the cache. At least 45 distinct sources have complete numbered local qualification receipts, ranging from 863 to 1,047 Python tests plus 64–79 Terraform checks. `test-local.sh` appears in 132 recorded call arguments; that is a mention count, not 132 verified executions. The latest repair subsequently passed 1,053 Python tests and 79 Terraform checks with zero skips.
3. **The agent supervises too much.** Repeated reads of progress, processes, time, cost and the same evidence consume calls without advancing a test. The controller already runs the acceptance stages automatically; routine agent polling adds little beyond the requested 15-minute reports and checks needed for launch or cleanup safety.
4. **Durable memory became an operational log.** Nearly a thousand classified calls involve memory/documentation. Four current memory documents total roughly 314,000 characters and retain large historical checkpoint sections. Updating multiple copies of routine run status creates repeated fetch, metadata, upload and readback work. PARA should contain concise decisions and final outcomes; native trial records should contain routine execution evidence.
5. **The browser fixtures initially omitted actual gateway behavior.** Six native trials stopped at authenticated browser access before coding tasks and reboot. The newest free reproduction uses the actual pinned gateway and actual frontends, and found a normal canvas-lease renewal blocked by the guard. That repair is committed and the free replay passes. Its transport settings and mock Hermes backend still mean it is diagnostic evidence, not DigitalOcean acceptance.

The counting evidence supports the repetition diagnosis. It cannot prove an exact number of calls that a different implementation would have saved, and the per-category figures should not be treated as a precise allocation to each of the nine acceptance cases.

## Corrective direction

- Fix the import boundaries so browser changes affect browser verification and real downstream dependencies, while preserving deploy/light/doctor/contents/cleanup proofs whose inputs and verifiers did not change.
- Key complete local receipts to actual execution/test inputs and their verifier, with the commit retained as provenance. Documentation-only commits must not rerun the full suite. Changed execution inputs must still qualify.
- Use the existing autonomous controller and its existing private receipts. Return bounded summaries, keep raw evidence on disk and observe routine progress only for the requested recaps, terminal results or actionable failure.
- Keep builds on paid VMs and preserve actual resolved-content fingerprints; do not add a separate image build/cache pipeline or infer matching images from unchanged recipes.
- Save memory at stable decisions and final outcomes, with links to canonical plans and evidence. Do not repeatedly mirror each intermediate run into four large documents.
- Qualify the corrected workflow once, obtain the three required light passes, then complete all six DigitalOcean cases. A new bug requires a specific failing reproduction before another equivalent paid retry.

## Evidence locations

- Content-free call classifications and aggregate counts: `.tmp/agent-stack-tests/testing-efficiency-audit-20261011/tool-call-classification.json` and `tool-call-audit.json`.
- Native stage counts: `native-trial-audit.json` in that directory.
- Verifier membership and shortest browser import chains: `verifier-dependency-audit.json` and `browser-import-chains.json`.
- Latest source qualification: `.tmp/agent-stack-tests/budget-iterations-20261008/canvas-renewal-repair-receipt.json`, source `977020ae90633fad7828cdcfad7d90788846616d`.
- Last closed DigitalOcean cost/cleanup: `as7-do-1011-iter29-closed-cost.json` in the same budget directory. Bound **$26.13/$35**, remaining **$8.87**, no paid trial active. This is a conservative bound, not an invoice.

The raw session and private runtime records stay outside the committed report. No credentials, tokens, raw tool arguments or response bodies are copied here.

## Refreshed measurement and adopted limits

The subsequent snapshot at 2026-10-11 08:52 UTC contains **6,404 goal-scope assistant calls**, 638 more than the baseline, and **35,141,271 returned text characters**. **3,399 calls (53.1%) include status or polling work**, overlapping the categories below. This is a dated snapshot, not a current lifetime total or exact allocation of billed tokens to tests.

| Work associated with | Calls | Added since baseline |
|---|---:|---:|
| Local qualification and unit checks | 1,404 | 100 |
| Memory and documentation | 1,037 | 65 |
| Browser and peer investigation | 990 | 144 |
| Pure polling and coordination | 794 | 165 |
| Containers, bootstrap and workspace | 456 | 26 |
| Cloud launch, preflight and prices | 421 | 26 |
| Runtime doctors and activation | 379 | 26 |
| Cleanup and cost ledger | 360 | 44 |
| Source inspection and editing | 253 | 23 |
| Coding tasks and model verification | 190 | 6 |
| Fingerprint review | 57 | 10 |
| Other setup and coordination | 56 | 2 |
| Reboot and persistence | 7 | 1 |

The successful native light-trial windows contain 25 calls for Hetzner (20 with status/polling), 17 for AWS (13) and 22 for GCP (19). These exact window counts include requested recaps and concurrent work, exclude preparation and post-trial work, and cannot establish exact per-check attribution or compliance with the six-routine-call target. Supervision remained excessive after the initial repairs. The target is not demonstrated.

The implementation now limits browser-only verifier changes to the two browser stages, retains genuine dependency checks, and reuses intact local qualification for identical execution/test/tool inputs across documentation commits. The controller runs builds, doctors, tasks, access and reboot itself; those stages require no additional routine assistant calls on a clean trial. The adopted budget is one call each for launch, binding, terminal review, independent cleanup/cost closure, fingerprint/acceptance review and concise recording/reporting. Count requested recaps, diagnosis, safety actions and documentation separately and include them in an all-in total; never conceal underlying operations inside a batch.

Observe healthy trials at the requested 15-minute recaps or an actionable change. Keep raw evidence on disk and summaries below 4,000 characters. Bound unproductive diagnosis to 20 new assistant calls before reporting the unresolved issue and changing strategy. Run focused regressions for a reproduced bug and complete qualification once per changed frozen execution candidate. Update durable memory for decisions, completed cases and final outcomes.

After this snapshot, the original full DigitalOcean trial at `4f9e534` passed its native runtime and cleanup checks, with independent evidence and fingerprint review. Three light-provider cases also pass. The remaining documentation review brings the operational result to 8/9 cases and 43/44 subchecks until that review is recorded. The cumulative conservative cloud/model bound is **$29.05/$35**, with no paid trial active; this is not an invoiced charge. The frozen execution candidate qualified with **1,074 Python tests and 79 Terraform checks, zero skips**.

Refreshed aggregate classifications are retained privately under `.tmp/agent-stack-tests/testing-efficiency-audit-20261011/refresh-audit/`. The active goal already includes these measurable outcomes and limits; it was retained rather than reset. Goal completion still requires all nine cases, all 44 subchecks and the saved linked plans.
