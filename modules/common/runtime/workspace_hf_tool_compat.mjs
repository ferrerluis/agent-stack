// Muse may return JSON file contents as an object despite a string tool schema.
// Normalize only that content, before native validation; paths and permissions
// remain entirely under OpenCode's normal tool implementation.
const muse = "meta-models/Muse-Spark-Open-816B-A42B:baseten";

export const HuggingFaceToolContent = async () => {
  const sessions = new Map();
  return {
    "chat.params": async ({ sessionID, model }) => {
      sessions.set(sessionID, model.providerID === "huggingface" && model.id === muse);
      if (sessions.size > 1024) sessions.delete(sessions.keys().next().value);
    },
    "tool.execute.before": async ({ sessionID, tool }, { args }) => {
      if (!sessions.get(sessionID) || tool !== "write" || !args) return;
      const value = args.content;
      if (value === null || typeof value !== "object") return;
      if (!Array.isArray(value) && ![Object.prototype, null].includes(Object.getPrototypeOf(value))) return;
      args.content = JSON.stringify(value);
    },
    dispose: async () => sessions.clear(),
  };
};
