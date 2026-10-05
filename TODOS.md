# AgentStack TODOs

This is a durable backlog, not authorization to implement or change production data.

## Index

| # | Title | Status | Dependencies | Canonical design |
|---|---|---|---|---|
| 1 | Shared Mem0 memory across OpenClaw, Hermes, and workspace coding harnesses | Proposed | Mem0 deployment and persistence design; workspace service enabled | TBD |

## 1. Shared Mem0 memory across OpenClaw, Hermes, and workspace coding harnesses — Proposed

Canonical design: TBD.

Why:

OpenClaw, Hermes, and coding harnesses running in the optional workspace container, including Codex, need a single durable memory system rather than isolated per-tool stores.

Required behavior:

- Make Mem0 available to both OpenClaw and Hermes through their respective supported memory-integration paths.
- Make a Mem0 CLI available in the workspace container for interactive use by Codex and other coding harnesses.
- Configure every supported client to use the same persistent Mem0 backend, namespace/tenant policy, and access boundary so writes made by one client are visible to the others as intended.
- Keep credentials out of Terraform state, images, logs, and repository documentation; define backup, retention, and restore behavior for the shared memory store.
- Add documentation and an end-to-end verification proving a memory written through each supported path can be read through the others within the approved access scope.

Open decisions:

- Choose the Mem0 deployment form, version pinning, backing database/vector store, and persistent-volume layout.
- Define the identity, tenant, namespace, and authorization model so shared does not mean globally readable.
- Confirm the supported OpenClaw and Hermes integration paths, including the least-privilege configuration required for workspace CLI users.
- Decide whether the Mem0 CLI is a standalone binary, an isolated Python environment, or another reproducibly versioned package in the workspace image.

Dependencies:

- An approved canonical Mem0 design.
- Workspace service enabled for CLI consumers.
- Secret-delivery approach that does not expose credentials to untrusted workspace users or Terraform state.

Acceptance evidence:

- Documented architecture, pinned versions, configuration contract, and operational backup/restore procedure.
- OpenClaw, Hermes, and workspace image tests pass without embedding secrets.
- A scoped E2E probe demonstrates intentional cross-client read/write sharing and rejects access outside the chosen tenant/namespace boundary.
