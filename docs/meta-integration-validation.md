# Meta integration validation

Initial live validation completed on 2026-10-17 UTC against runtime implementation
`cf11ef5d`. The PR review follow-up below records subsequent fixes and verification.
See [the repeatable acceptance catalog](meta-integration-tests.md) for each
sub-test’s steps and expected result.

## Initial product acceptance results

The initial **9 required cases / 36 sub-tests passed**. No required case was skipped,
blocked, stale or running. The active catalog now includes two additional review
regressions, recorded below.

| Case | Scope | Passed sub-tests |
| --- | --- | --- |
| TC01 | Setup choices, key validation, mandatory Codex, unchanged defaults | 4/4 |
| TC02 | Native provider installation/catalog, Hermes routes, configuration modes, credential handling | 4/4 |
| TC03 | Codex-only and all-selected workspace, shell/SSH, restart/rebuild persistence | 4/4 |
| TC04 | OpenClaw + Muse Spark: response, file tool, independent result, follow-up | 4/4 |
| TC05 | OpenClaw + Muse Wonder: response, file tool, independent result, follow-up | 4/4 |
| TC06 | Hermes + Muse Spark: response, file tool, independent result, follow-up | 4/4 |
| TC07 | Hermes + Muse Wonder: response, file tool, independent result, follow-up | 4/4 |
| TC08 | Muse Code: authenticate/read, fix, run tests, independent verification | 4/4 |
| TC09 | Terraform formatting/validation/tests, setup/doctor checks, runtime syntax | 4/4 |

## Live evidence

The deployment used one DigitalOcean `nyc1` Basic `s-2vcpu-2gb` Droplet,
20 GB storage and 2 GB swap. Agents ran sequentially. This validates DigitalOcean;
other cloud providers and other agent versions were outside the live matrix.

| Runtime | Requested and served model | Independently checked sums, first turn / follow-up |
| --- | --- | --- |
| OpenClaw | `meta/muse-spark-1.3` | 474 / 481 |
| OpenClaw | `meta/muse-wonder-1.0` | 544 / 551 |
| Hermes (`meta-ai`) | `muse-spark-1.3` | 438 / 445 |
| Hermes (`meta-ai`) | `muse-wonder-1.0` | 124 / 131 |

Each run used a fresh file containing random numbers and a nonce. The prompt
provided the path, and the real agent read the file with its tool. Native runtime
and Meta response metadata confirmed the requested model; fallback lists were
empty. The second turn used the same session. The tool trace, nonce and arithmetic
were independently checked.

OpenClaw 2026.10.4 used official `@openclaw/meta-provider@2026.10.4` and its
exported native catalog. Managed reapplication succeeded with the plugin already
installed. The tested Hermes image was
`nousresearch/hermes-agent@sha256:36099926da316742423544e055d4734777105ffe6341873af5459b957d0c2919`.

The workspace launched Codex 0.145.0, Claude Code 2.1.301 and Muse Code
1.4.10-R6427.1 as the normal user, including through SSH. The Codex-only image
started SSH and Codex with optional tools and Muse credentials absent.
Sentinel files and authentication fixtures retained their hashes and permissions
after restart/rebuild. Runtime key checks passed; actual keys were absent from
image metadata/history and container logs.

Muse Code read the intentionally broken calculator, changed only `calculator.py`,
and ran the immutable `checks.py` through its native MSP session over workspace
SSH. Docker’s default protections prevented Muse’s nested Linux sandbox from
starting, so the test client approved only
`PYTHONDONTWRITEBYTECODE=1 python3 checks.py` once in the fixture directory.
Muse’s tool returned exit 0. Independent public tests and 100 random addition
checks passed; `checks.py` was unchanged. No permission rule was saved, Docker
security settings were unchanged, and no privileged container or extra capability
was used. Unattended execution without that command approval is not demonstrated.

## Repository checks

- Terraform formatting and validation passed; **91 Terraform tests passed**.
- **213 setup tests passed** in a clean task-owned environment with the declared
  dependencies, including pinned PyYAML for Hermes YAML configuration tests.
- **20 unchanged doctor tests passed**; runtime shell/Python syntax checks passed.
- Focused Meta checks and affected container/helper/controller regressions passed.
  The original broad runner had 858 tests, four subsequently fixed helper errors
  and three preexisting skips; it is not reported as a fresh all-green 858-test run.

Generated results and historical attempts remain in the worktree’s ignored
`.tmp/meta-integration/results.json` (catalog version 3, bug iteration 16).
The source-side verification summary retains model identity, tool/session checks,
file hashes, independent test outcomes and numeric usage without transcripts or
credentials. Failed historical attempts remain separate from current passes.

## Cost and cleanup

The guarded run completed 29 generation requests. Recorded token usage estimates
API cost at **$0.25730115**; this is not a final invoice. Worst-case API reservations
were $3.985660, plus a conservative $0.15 cloud/storage allowance: **$4.135660**,
within the $15 budget. Exact token counting, request/output limits, no upstream
retries and watchdogs bounded the live work.

The Droplet, volume, firewall, SSH key and test tag were deleted. API checks
confirmed the paid resources absent. DigitalOcean automatically made the first
`nyc1` VPC the regional default and [does not allow deleting that default without
another default VPC](https://docs.digitalocean.com/reference/doctl/reference/vpcs/delete/).
The empty default `meta-muse-1016-vpc` remains with zero members; no replacement
network or unrelated resources were changed. Both temporary API keys were revoked,
local private keys/state/plans were removed, and scheduled test reporting ended.

## PR review follow-up

Two review findings added **TC01.5** and **TC02.5**, taking the active catalog to
**9 required cases / 38 sub-tests**. The changes are limited to setup key
validation and selection of inherited Hermes fallback routes.

- Hermes now inherits only its supported provider-prefixed fallbacks, preserving
  their original order. A supported primary override works with mixed Codex/API
  fallbacks or with no compatible fallback. OpenClaw keeps its complete global
  list; existing auto/preserve configuration and unrelated managed settings stay
  intact.
- Terraform and setup rendering now require `meta_api_key` whenever Muse Code is
  selected, including with a non-Meta chief-of-staff model. A key for Muse alone
  does not enable Meta agent models or change the selected primary. The workspace
  wrapper still gives a user-supplied `META_API_KEY` precedence.

Both bugs were reproduced before the fixes. Verification passed: **18 focused
setup/runtime tests**, **5 new mocked Terraform plans**, **216 complete setup
tests**, and **96 complete Terraform tests**, plus formatting and validation.
The full suite was followed by another focused check using complete provider/auth
fixtures; see PR #5 for CI evidence on the final published head.

Unrelated live results remain valid: the tested Meta fallback lists contain only
supported routes and render identically, and the Muse binary, wrapper, credentials
and Docker settings are unchanged. No new cloud resources, paid model calls,
credential changes, website deployment or periodic reporting were needed.
The previously documented Muse command-approval limitation still applies.
