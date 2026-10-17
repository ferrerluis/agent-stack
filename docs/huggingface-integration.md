# Hugging Face personal-agent models

AgentStack routes OpenClaw and Hermes through the hosted Hugging Face Inference
Providers chat endpoint, `https://router.huggingface.co/v1`. This adds no weights,
inference servers or cloud resources. Personal-agent selections remain independent
of workspace coding-harness selections. Existing non-HF defaults are unchanged.

## Reviewed offerings

| Family | Exact AgentStack reference | Hosted backend | Managed reasoning |
| --- | --- | --- | --- |
| Kimi | `huggingface/moonshotai/Kimi-K3:baseten` | HF → Baseten | high; supported labels low/high/max |
| Muse Spark Open | `huggingface/meta-models/Muse-Spark-Open-816B-A42B:baseten` | HF → Baseten | high; low/medium/high/xhigh |
| DeepSeek Flash | `huggingface/deepseek-ai/DeepSeek-V4.1-Flash:baseten` | HF → Baseten | high; high/max |

These are Luis's selected offerings. GPT-OSS HF choices have been removed and are
rejected by managed setup. A family label can be short, but exports and readback
always retain the exact ID, organization and `:baseten` suffix above. The provider
pin prevents a policy change from silently selecting a different backend contract.
Do not send Baseten's internal Muse slug `meta-models/muse-spark-open` to the HF
router: HF maps the public Hub ID to that provider ID itself.

The unauthenticated official HF router catalog reported all three pairs live,
with tools, text/image input and 1,048,576 context on 2026-10-17. The managed catalog
currently enables **text input only**, with an 8,192-token completion cap. Images,
audio and video are not certified by this integration. Prices are snapshot estimates
for runtime accounting, not spending authorization. The redacted public observation
is retained in [hosted catalog evidence](huggingface-hosted-catalog.json).

A Hub checkpoint alone does not establish hosted API availability or agent
compatibility. `deepseek-ai/DeepSeek-V4.1-Flash-1017` exists on the Hub but had an
empty Inference Provider mapping at observation time; it is rejected. The selected
undated route is never aliased to that revision. Arbitrary models, unpinned IDs,
other providers and `:fastest`/`:cheapest`/`:preferred` are also rejected. Adding a
pair requires reviewing its hosted contract and tests.

## Reasoning and tools

K3 always thinks and requires the full assistant reasoning/tool-call history on
later turns. Muse also always reasons. DeepSeek's underlying model card describes
numeric effort 1–100, while **Baseten's hosted API documents string effort**
`high`/`max` (and a non-thinking mode). This integration uses the hosted contract;
it does not claim numeric effort is forwarded. Baseten has additional disable/alias
settings; managed setup keeps reasoning enabled for all three.

OpenClaw declares each model's distinct effort vocabulary. Unsupported thinking
levels, including off, are unavailable through `thinkingLevelMap`; its native
selector clamps legacy values into the enabled vocabulary. Managed per-model
`params.thinking` is high. This is necessary: on OpenClaw 2026.10.4, mapping an
outbound off request to an enabled effort alone can still discard returned
reasoning before the next tool turn. The native acceptance probe covers both the
selector and actual SSE replay path. Do not bypass that selector with raw off/none
transport calls or claim those calls preserve K3's required history.

Hermes uses `custom:agentstack-huggingface`, the fixed HF URL, `key_env: HF_TOKEN`,
chat completions and `extra_body.reasoning_effort: high`. High is independently
documented for each selected hosted model. The separate name matters: canonical
built-in provider names can take precedence over named entries and lose request
overrides. Native resolver, history-builder and SDK-body checks preserve the tested
`reasoning_content`, content, tool IDs/arguments and matching results across turns.
These are synthetic transport checks, not live agent/model results.

Both agents request `parallel_tool_calls: false`. The managed policy uses sequential
tool turns until separately authorized live checks establish parallel behavior.
The offline probes also verify correct replay of two returned tool calls and their
results if a response contains a batch. This proves serialization, not hosted
parallel execution or a vendor prohibition. Live tool behavior remains unverified.
No real model reasoning transcript is collected by these tests.

## Setup and exports

Use the setup skill or supply:

```hcl
model_providers_enabled = ["huggingface"]
default_model           = "huggingface/moonshotai/Kimi-K3:baseten"
fallback_models = [
  "huggingface/meta-models/Muse-Spark-Open-816B-A42B:baseten",
  "huggingface/deepseek-ai/DeepSeek-V4.1-Flash:baseten",
]
# Supply huggingface_api_key through your existing secure input workflow.
```

`huggingface_api_key` is sensitive and empty by default. Selecting HF, including a
managed Hermes-only override, requires an existing token. Terraform/setup reject
missing, placeholder and unsafe values without echoing them. This checks presence
and shape, not permissions, balance or access. No credential is generated, imported
from login stores or authenticated through inference. As with existing provider
keys, supplied values enter Terraform state and the protected runtime `.env`.
They do not enter build images or coding-harness environments.

OpenClaw receives the explicit catalog and an environment SecretRef to `HF_TOKEN`.
Hermes removes only the outer `huggingface/` prefix and retains the full inner ID.
No direct Moonshot, Meta, DeepSeek, Baseten, Groq or OpenAI API key is needed for
this HF-routed path. `hermes_model` can override the shared personal-agent primary;
supported inherited fallbacks retain their order. Existing auto/preserve configs
remain unchanged; manage retains unrelated settings while updating selected routes.
OpenClaw verifies exact configured primary and ordered HF fallbacks, including when
another provider's primary is unusable. Configured identity does not prove served
identity or successful inference.

## Account gating and errors

An existing HF token needs **Make calls to Inference Providers** permission plus
sufficient account credits and provider/model access. All selected hosted entries
reported `is_free: false`. Muse's Hub weights are manually gated; Kimi/DeepSeek
weights are ungated. Those weight-access flags neither establish nor waive hosted
account/model terms. Check actual account authorization separately. Do not assume a
free allowance or transfer direct-vendor account/top-up rules to HF-routed billing.
This task authorizes no spending or credential setup.

| Failure | Action |
| --- | --- |
| Missing `huggingface_api_key` / `HF_TOKEN` | Supply an existing permitted token through secure inputs and rerender. |
| Removed, unhosted, unpinned or malformed route | Select an exact reference above; do not substitute a weights URL or dated checkpoint. |
| Config/schema/readback mismatch | Check supported agent version and config modes; reject suffix loss or fallback substitution. |
| HTTP 401 / 403 | Check existing token scope, model terms and account/provider permission without printing credentials. |
| HTTP 402 / exhausted credits | Stop; obtain fresh account/budget authorization before purchasing or retrying. |
| HTTP 404 / provider unavailable | Recheck the public mapping and pin; do not silently switch models/backends. |
| HTTP 400 on reasoning/tools | Check the selected Baseten vocabulary, enabled reasoning/history and sequential policy. Numeric DeepSeek-card effort is not this hosted contract. |
| HTTP 429 / timeout | Bound retries and respect provider limits before any separately approved live test. |

## Official references checked 2026-10-17

- [HF routing](https://huggingface.co/docs/inference-providers/en/index), [chat schema](https://huggingface.co/docs/inference-providers/en/tasks/chat-completion), [billing](https://huggingface.co/docs/inference-providers/en/pricing), [public catalog](https://router.huggingface.co/v1/models)
- [Kimi K3 model card](https://huggingface.co/moonshotai/Kimi-K3) and [official API contract](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)
- [Muse Spark Open model card](https://huggingface.co/meta-models/Muse-Spark-Open-816B-A42B) and [Baseten hosted launch](https://www.baseten.co/blog/muse-spark-open-launch/)
- [DeepSeek V4.1 Flash model card](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash)
- [Baseten hosted reasoning vocabulary](https://docs.baseten.co/inference/model-apis/reasoning)
- [OpenClaw HF configuration](https://docs.openclaw.ai/providers/huggingface) and [2026.10.4 compatibility schema](https://github.com/openclaw/openclaw/blob/v2026.10.4/src/config/zod-schema.model-compat.ts)
- [Hermes provider configuration](https://hermes-agent.nousresearch.com/docs/integrations/providers) and [native transport source tested](https://github.com/NousResearch/hermes-agent/blob/4b1adbfe5d274c5dbfaa981fcc4dd68391867701/agent/transports/chat_completions.py)

## Website handoff

After review, show three personal-agent families: Kimi, Muse Spark Open and DeepSeek
Flash. Export their exact references and an existing-token input through the setup
schema. Explain account gating, enabled reasoning and the sequential policy. Keep
OpenCode's independent model selection separate even when it uses the same HF
shortlist. Do not offer GPT-OSS, the unhosted -1017 revision, all Hub models or free
inference. No Splash files, unpublished selectors or website deployment were changed.
