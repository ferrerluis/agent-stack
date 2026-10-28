// Routing metadata only; no prompt or response access is requested.
const muse = "meta-models/Muse-Spark-Open-816B-A42B:baseten";
export default {
  id: "agentstack-hf-tool-content",
  register(api) {
    const runs = new Map();
    api.on("model_call_started", (event) => {
      if (!event.runId) return;
      runs.set(event.runId, event.provider === "huggingface" && event.model === muse);
      if (runs.size > 1024) runs.delete(runs.keys().next().value);
    });
    api.on("before_tool_call", (event, ctx) => {
      if (!runs.get(event.runId ?? ctx.runId) || event.toolName !== "write") return;
      const value = event.params?.content;
      if (value === null || typeof value !== "object") return;
      if (!Array.isArray(value) && ![Object.prototype, null].includes(Object.getPrototypeOf(value))) return;
      return { params: { ...event.params, content: JSON.stringify(value) } };
    });
    api.on("agent_end", (_event, ctx) => runs.delete(ctx.runId));
  },
};
