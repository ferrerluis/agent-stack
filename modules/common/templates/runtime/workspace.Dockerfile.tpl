FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# The persisted workspace home and host-side diagnostics bridge use UID/GID
# 1000. Create the configured workspace user at that stable identity even when
# the upstream Ubuntu image already reserves UID 1000 for its default user.
RUN existing_user="$(getent passwd 1000 | cut -d: -f1)" \
  && if [ -n "$existing_user" ]; then \
    existing_group="$(id -gn "$existing_user")"; \
    usermod --login "${workspace_username}" --home "/home/${workspace_username}" --move-home "$existing_user"; \
    if [ "$existing_group" != "${workspace_username}" ]; then \
      groupmod --new-name "${workspace_username}" "$existing_group"; \
    fi; \
  else \
    groupadd --gid 1000 "${workspace_username}"; \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash "${workspace_username}"; \
  fi \
  && usermod --groups "" "${workspace_username}"

# Keep the workspace image reproducible and compatible with the Codex app server.
# Bump deliberately when upgrading Codex.
ARG CODEX_RELEASE=${workspace_codex_release}

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    openssh-client \
    openssh-server \
    python3 \
    python3-pip \
    python3-venv \
    procps \
    util-linux \
    bubblewrap \
    tini \
  && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh \
  && CODEX_NON_INTERACTIVE=1 CODEX_RELEASE="$CODEX_RELEASE" CODEX_INSTALL_DIR=/usr/local/bin CODEX_HOME=/opt/codex sh /tmp/install-codex.sh \
  && rm -f /tmp/install-codex.sh

%{ if workspace_claude_enabled }
# Copy the resolved binary out of root's private installation tree so UID 1000
# can execute it. Auth is supplied at runtime, never during this image build.
ARG CLAUDE_RELEASE=${workspace_claude_release}
ENV DISABLE_AUTOUPDATER=1
RUN curl -fsSL https://claude.ai/install.sh -o /tmp/install-claude.sh \
  && bash /tmp/install-claude.sh "$CLAUDE_RELEASE" \
  && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
  && rm -rf /tmp/install-claude.sh /root/.local/bin/claude /root/.local/share/claude
%{ endif }

RUN install -d -m 0755 /usr/local/libexec

COPY workspace-entrypoint.sh /usr/local/bin/workspace-entrypoint
COPY para-memory-drive/SKILL.md /usr/local/share/agent-stack/skills/para-memory-drive/SKILL.md
COPY workspace-codex-update.sh /usr/local/libexec/agent-stack-workspace-codex-update
COPY workspace-codex-control.py /usr/local/libexec/agent-stack-workspace-codex-control
COPY workspace-cli-task.py /usr/local/libexec/agent-stack-workspace-cli-task

RUN chmod 0755 /usr/local/bin/workspace-entrypoint \
  && chmod 0644 /usr/local/share/agent-stack/skills/para-memory-drive/SKILL.md \
  && chmod 0755 /usr/local/libexec/agent-stack-workspace-codex-update \
  && chmod 0755 /usr/local/libexec/agent-stack-workspace-codex-control \
  && chmod 0755 /usr/local/libexec/agent-stack-workspace-cli-task \
  && mkdir -p /run/sshd

EXPOSE 22

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/workspace-entrypoint"]
