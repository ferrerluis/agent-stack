# AgentStack TODOs

This is a durable backlog, not authorization to implement or change production data.

## Index

| # | Title | Status | Dependencies | Canonical design |
|---|---|---|---|---|
| 1 | Shared Mem0 memory across OpenClaw, Hermes, and workspace coding harnesses | Proposed | Mem0 deployment and persistence design; workspace service enabled | TBD |
| 2 | Deploy to a personal Linux device over local access or a mesh network | Proposed; immediate / ASAP | Reachable Linux device; supported deployment and network-access design | TBD |
| 3 | Add an initial Paperclip setup to the workspace | Proposed; immediate / ASAP | Workspace service enabled; Paperclip integration design | TBD |

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

## 2. Deploy to a personal Linux device over local access or a mesh network

Status: Proposed.
Priority: Immediate / as soon as possible.
Canonical design: TBD.

Required behavior:

- Add support for deploying AgentStack to a personally owned Linux device, such as a Raspberry Pi at home.
- Support local access and remote access through a mesh network or Tailscale-style connection.
- Document the supported hardware/OS requirements, access prerequisites, and setup path for each access method.

Acceptance evidence:

- A documented deployment and basic working-stack verification on a supported personal Linux device, including a Raspberry Pi.
- Verification of both the local-access path and the mesh-network access path.

## 3. Add an initial Paperclip setup to the workspace

Status: Proposed.
Priority: Immediate / as soon as possible.
Canonical design: TBD.
Upstream: [paperclipai/paperclip](https://github.com/paperclipai/paperclip).

Required behavior:

- Install and provide an initial usable setup of the open-source Paperclip agent orchestration app in the workspace, alongside the existing coding tools.
- Document how to start and access Paperclip and connect it to the workspace's coding tools through supported integrations.

Dependencies:

- Workspace service enabled.
- Confirm the Paperclip version, runtime requirements, persistent data location, and access configuration during implementation planning.

Acceptance evidence:

- Paperclip starts successfully in the workspace alongside the existing coding tools.
- A basic orchestration smoke test demonstrates the initial setup working with a supported coding tool.
