import assert from "node:assert/strict";
import test from "node:test";
import { HuggingFaceToolContent } from "../../../modules/common/runtime/workspace_hf_tool_compat.mjs";
import openclaw from "../../../modules/common/runtime/openclaw_hf_tool_compat.mjs";

const muse = { providerID: "huggingface", id: "meta-models/Muse-Spark-Open-816B-A42B:baseten" };

test("Muse JSON content reaches native write as text without changing its path", async () => {
  const hooks = await HuggingFaceToolContent();
  await hooks["chat.params"]({ sessionID: "muse", model: muse });
  const args = { filePath: "/owned/output.json", content: { nonce: "fresh", sum: 42 } };
  await hooks["tool.execute.before"]({ sessionID: "muse", tool: "write" }, { args });
  assert.deepEqual(args, { filePath: "/owned/output.json", content: '{"nonce":"fresh","sum":42}' });
});

test("other models/providers, tools and sessions keep their original arguments", async () => {
  const hooks = await HuggingFaceToolContent();
  for (const model of [{ ...muse, id: "moonshotai/Kimi-K3:baseten" }, { ...muse, providerID: "meta" }]) {
    await hooks["chat.params"]({ sessionID: "other", model });
    const args = { content: { sum: 42 }, filePath: "/unchanged" };
    await hooks["tool.execute.before"]({ sessionID: "other", tool: "write" }, { args });
    assert.equal(typeof args.content, "object");
  }
  await hooks["chat.params"]({ sessionID: "muse", model: muse });
  for (const [sessionID, tool] of [["missing", "write"], ["muse", "read"], ["muse", "edit"]]) {
    const args = { content: { sum: 42 } };
    await hooks["tool.execute.before"]({ sessionID, tool }, { args });
    assert.equal(typeof args.content, "object");
  }
});

test("strings and invalid missing/null content are never fabricated", async () => {
  const hooks = await HuggingFaceToolContent();
  await hooks["chat.params"]({ sessionID: "muse", model: muse });
  for (const args of [{ content: "existing text" }, { content: null }, {}]) {
    const before = structuredClone(args);
    await hooks["tool.execute.before"]({ sessionID: "muse", tool: "write" }, { args });
    assert.deepEqual(args, before);
  }
  await hooks["chat.params"]({ sessionID: "muse", model: { ...muse, id: "deepseek-ai/DeepSeek-V4.1-Flash:baseten" } });
  const args = { content: { sum: 42 } };
  await hooks["tool.execute.before"]({ sessionID: "muse", tool: "write" }, { args });
  assert.equal(typeof args.content, "object");
});

test("OpenClaw uses routing metadata and preserves native path/tool checks", () => {
  const hooks = {};
  openclaw.register({ on: (name, hook) => { hooks[name] = hook; } });
  hooks.model_call_started({ runId: "muse", provider: "huggingface", model: muse.id });
  const event = { runId: "muse", toolName: "write", params: { path: "/owned/result", content: { sum: 42 } } };
  assert.deepEqual(hooks.before_tool_call(event, {}), { params: { path: "/owned/result", content: '{"sum":42}' } });
  assert.equal(typeof event.params.content, "object");
  hooks.model_call_started({ runId: "muse", provider: "huggingface", model: "deepseek-ai/DeepSeek-V4.1-Flash:baseten" });
  assert.equal(hooks.before_tool_call(event, {}), undefined);
  hooks.model_call_started({ runId: "muse", provider: "huggingface", model: muse.id });
  assert.equal(hooks.before_tool_call({ ...event, toolName: "exec" }, {}), undefined);
  hooks.agent_end({}, { runId: "muse" });
  assert.equal(hooks.before_tool_call(event, {}), undefined);
});
