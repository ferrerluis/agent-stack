---
name: agent-stack-doctor
description: Diagnose and repair an existing AgentStack deployment. Use when Terraform drift, SSH access, bootstrap, Docker, Tailscale, OpenClaw, Hermes, n8n, Postgres, Caddy, channel auth, or model-provider configuration is failing.
---

# AgentStack Doctor

Use this skill to diagnose and repair an existing AgentStack deployment.
Legacy OpenClaw-only deployments are supported as a compatibility responsibility; this does not make OpenClaw the project identity.

Core rules:
- Diagnose first, repair second.
- Prefer repo-local wrappers and scripts before ad hoc commands.
- Explain the most likely root cause before impactful repair actions such as editing `terraform.tfvars`, re-running `terraform apply`, or changing remote service configuration.
- Re-run the smallest relevant validation after every repair.

## Workflow

1. Establish local facts first.
   - Run `skills/agent-stack-doctor/scripts/collect_diagnostics.sh`.
   - Use `--show-secrets` only when you intentionally need raw token-bearing Terraform outputs.
   - Confirm Terraform output availability, local SSH assets, and repo helper availability.

2. If SSH works, gather remote health evidence.
   - Run `skills/agent-stack-doctor/scripts/check_remote_health.sh`.
   - Use the repo-local SSH wrapper unless you have a reason not to.
   - For enforceable results, add `--strict --json --services openclaw,hermes,workspace --require-claude --check-tailscale --tailscale-mode host` for the showcase profile. Any missing or failed required check returns nonzero; CLI installation does not establish real model execution.

3. Map the evidence to a diagnosis track.
   - Use `references/diagnostic-flow.md` for the ordered repair decision tree.
   - Use `references/ssh-and-runtime.md` for the shared SSH, remote-path, and health-check commands.
   - Use `references/failure-signatures.md` for known symptoms and likely causes.
   - Prefer `/opt/agent-stack`, but fall back to `/opt/openclaw` for legacy installs.
   - If both roots exist without the compatibility symlink, diagnose it as a partial layout migration and recommend rerunning `/usr/local/bin/agent-stack-migrate-layout`.

4. Repair with intent.
   - Low-risk validation commands can run during diagnosis.
   - Before impactful actions, explain the likely root cause and the specific repair you are about to apply.
   - If the fix requires changing deployment inputs, update `terraform.tfvars` and re-run Terraform deliberately rather than making hidden remote-only changes.

5. Close the loop.
   - Confirm the repaired symptom is gone.
   - Surface any follow-up command the user still needs, such as device approval, DNS changes, public-domain login, WhatsApp login, or token refresh.

## References

- `references/diagnostic-flow.md` for the ordered diagnostic tracks.
- `references/ssh-and-runtime.md` for the shared command runbook and remote paths.
- `references/failure-signatures.md` for common failure signatures, gotchas, and the Anthropic auth-key mismatch.
