# Hugging Face personal-agent models

AgentStack can route OpenClaw and Hermes chat/tool turns through the hosted
Hugging Face Inference Providers router at `https://router.huggingface.co/v1`.
No weights, inference servers, coding harnesses or cloud resources are added.
Existing model and workspace defaults are unchanged.

## Reviewed routes

| AgentStack reference | Backend | Input | Tools | Reasoning |
| --- | --- | --- | --- | --- |
| `huggingface/openai/gpt-oss-20b:groq` | HF → Groq | Text | Sequential | low / medium / high |
| `huggingface/openai/gpt-oss-120b:groq` | HF → Groq | Text | Sequential | low / medium / high |

These are the only accepted Hugging Face routes in this integration. A Hub
repository's existence does not establish hosted availability, chat compatibility
or tool support. Provider pins make the contract reviewable: unqualified IDs,
`:fastest`, `:cheapest`, `:preferred` and other providers are rejected. HF supports
those routing policies, but switching backend can change the tool/reasoning
contract. Extending the catalog requires checking the new pair and its tests.

The public HF `/v1/models` catalog was read without credentials on 2026-10-17.
Both pairs reported `status: live`, `supports_tools: true` and a 131,072-token
context. This is catalog evidence, not an authenticated inference result.
`huggingface-models.json` records that snapshot and a conservative 8,192-token
OpenClaw completion budget. Prices are estimates for runtime accounting, not a
spending authorization or guaranteed current price.

Groq explicitly lists parallel tool calls as unsupported for these two models.
OpenClaw's per-model `params.parallel_tool_calls` is false. Hermes uses a named
`custom:agentstack-huggingface` provider with the fixed HF URL, `key_env: HF_TOKEN`,
and `extra_body.parallel_tool_calls: false`. A separate provider name is deliberate:
current Hermes gives canonical built-in names precedence over named configuration,
so an entry named `huggingface` would silently lose its request overrides.
Sequential multi-step agent tools remain available. Parallel execution has no
passing live claim; requests for multiple independent tasks must use sequential
model turns with these routes.

GPT-OSS reasoning cannot be disabled on this backend. OpenClaw declares the
supported effort vocabulary and maps off/minimal to low and xhigh to high.
Hermes sends medium through its provider-specific request overrides. No reasoning
transcript or hidden chain of thought is collected by the integration. This path
uses chat completions, not the HF beta Responses API or Groq built-in server tools.

## Setup and exports

Use the setup skill or supply these Terraform inputs:

```hcl
model_providers_enabled = ["huggingface"]
default_model           = "huggingface/openai/gpt-oss-20b:groq"
fallback_models         = ["huggingface/openai/gpt-oss-120b:groq"]
# Supply huggingface_api_key through your existing secure tfvars/input workflow.
```

`huggingface_api_key` is a sensitive, optional-by-default string. Selecting HF,
including a managed Hermes-only primary override, requires an existing token.
The renderer and Terraform reject blank, placeholder and unsafe/multiline values;
this is presence/shape validation, not proof of permission, balance or model access.
No token is created, imported from local login stores, or checked through inference.
As with existing provider keys, configured values enter Terraform state and the
protected runtime `.env`. They are not placed in images or workspace harness env.

OpenClaw uses the bundled HF provider with an explicit reviewed model catalog and
an environment SecretRef to `HF_TOKEN`. Hermes removes only the outer
`huggingface/` prefix: `openai/gpt-oss-20b:groq` remains intact. The nested `openai/`
component is a Hub organization, and does not select OpenAI API/Codex auth.
No `GROQ_API_KEY` or `OPENAI_API_KEY` is needed for this HF route.

`hermes_model` can override the shared primary. Supported inherited fallbacks keep
their order; incompatible Codex fallbacks are filtered as before. Existing configs
remain byte-identical in auto/preserve modes; manage changes selected model routes
and the named HF provider while retaining unrelated channels/providers. OpenClaw
follows its existing auto/manage/preserve policy. In managed setup, it verifies
that the CLI-written primary and ordered HF fallbacks exactly match the requested
IDs, including provider suffixes. Other providers' existing canonicalization is
left intact. Configured identity does not prove which model served an API turn.

## Account gating and errors

The existing HF token needs **Make calls to Inference Providers** permission.
HF-routed billing applies; review the account's provider permissions and available
credits before separately authorizing a live test. Do not assume a free allowance:
current HF pricing says free accounts need purchased credits; older agent docs
still describe a historical free tier. This task authorizes no spending.

| Failure | Action |
| --- | --- |
| Missing `huggingface_api_key` / `HF_TOKEN` | Supply an existing permitted token through secure inputs; rerender setup. |
| Unreviewed/malformed route | Choose one of the exact references above; don't substitute a Hub weights URL. |
| Catalog/CLI incompatibility or readback mismatch | Inspect agent version and config modes; require OpenClaw compatible with the documented 2026.10.4 configuration fields and a current Hermes named-provider implementation. Do not accept a silent fallback. |
| HTTP 401 / 403 | Check token validity, Inference Providers scope, model terms/access and account/provider permissions; do not print the token. |
| HTTP 402 or exhausted balance | Stop. Account access/budget approval is needed before purchasing or retrying inference. |
| HTTP 404 / provider unavailable | Recheck the public catalog and provider pin; don't silently switch providers/models. |
| HTTP 400 on tools/reasoning | Check sequential tool settings and low/medium/high effort; preserve native diagnostics privately and redact credentials before reporting. |
| HTTP 429 / timeout | Respect provider limits and bound retries before any separately approved live test. |

## Official references checked 2026-10-17

- [HF router and provider selection](https://huggingface.co/docs/inference-providers/en/index)
- [HF chat completion schema](https://huggingface.co/docs/inference-providers/en/tasks/chat-completion)
- [HF function calling](https://huggingface.co/docs/inference-providers/en/guides/function-calling)
- [HF pricing and billing](https://huggingface.co/docs/inference-providers/en/pricing)
- [HF public router catalog](https://router.huggingface.co/v1/models)
- [OpenClaw Hugging Face configuration](https://docs.openclaw.ai/providers/huggingface)
- [OpenClaw 2026.10.4 compatibility schema](https://github.com/openclaw/openclaw/blob/v2026.10.4/src/config/zod-schema.model-compat.ts)
- [OpenClaw 2026.10.4 parallel-tool parameter handling](https://github.com/openclaw/openclaw/blob/v2026.10.4/src/agents/embedded-agent-runner/extra-params.ts)
- [Hermes provider configuration](https://hermes-agent.nousresearch.com/docs/integrations/providers)
- [Hermes named-provider request/auth fields](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/runtime_provider_custom.py)
- [Groq tool support matrix](https://console.groq.com/docs/tool-use/overview)
- [Groq reasoning contract](https://console.groq.com/docs/reasoning)

## Website handoff

After morning review, Splash can offer Hugging Face under personal-agent model
providers using the two exact references above, an existing-token field, and the
account/tool limitations. Export `huggingface_api_key` plus provider/default/fallback
selections through the schema-driven setup path. Do not offer every Hub model or
imply parallel tool support/free inference. Keep this option separate from coding
harness selectors. No Splash files or unpublished selectors were changed here.
