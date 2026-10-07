# AGENTS

This repo uses shared, portable skills for deployment and repair workflows.

- Use `skills/agent-stack-setup/SKILL.md` when the user wants to set up, configure, or deploy AgentStack.
- Use `skills/agent-stack-doctor/SKILL.md` when the user wants to diagnose or repair an existing AgentStack deployment.

Shared rules:
- Treat `variables.tf` as the schema and true-default source of truth.
- Treat `terraform.tfvars.example` as a suggestion source only; never silently accept placeholder secrets from it.
- Treat `terraform.tfvars`, `*.tfstate`, `gateway_token`, and `#token=` URLs as sensitive.
- Prefer the repo-local SSH helpers in `bin/` when you need to inspect or repair a deployed instance.

Local test and build storage:
- On pi-main, `/tmp` is RAM-backed. Put large downloads, provider caches, builds and disposable test workspaces on a verified disk filesystem; reserve `/tmp` for small, short-lived files.
- The local test runner uses the ignored `.tmp/agent-stack-tests` directory, reuses its shared provider cache and removes completed workspaces. Use that disk directory for manual test/build artifacts too.
- Preserve caches and records referenced by active work. Before changing a cache location, copy and verify it, update the references, and verify their use; do not sweep cloud credentials or setup/recovery records during cleanup.
