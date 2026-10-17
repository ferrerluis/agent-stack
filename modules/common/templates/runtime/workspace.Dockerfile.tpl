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

%{ if workspace_muse_enabled }
ARG MUSE_RELEASE=${workspace_muse_release}
ENV MUSE_NO_AUTO_UPDATE=1
RUN set -eu; \
    case "$(uname -m)" in x86_64) platform=x86_linux ;; aarch64|arm64) platform=aarch64_linux ;; *) exit 1 ;; esac; \
    curl -fsSL --proto '=https' --proto-redir '=https' \
      "https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=$MUSE_RELEASE&file=manifest.json" -o /tmp/muse-manifest.json; \
    jq -e --arg version "$MUSE_RELEASE" '.version == $version and .checksum_algorithm == "sha256"' /tmp/muse-manifest.json >/dev/null; \
    artifact="$(jq -er --arg platform "$platform" '.artifacts[$platform].url | select(startswith("https://lookaside.facebook.com/"))' /tmp/muse-manifest.json)"; \
    checksum="$(jq -er --arg platform "$platform" '.artifacts[$platform].checksum | select(test("^[0-9a-f]{64}$"))' /tmp/muse-manifest.json)"; \
    install -d -m 0755 /usr/local/libexec; \
    curl -fsSL --proto '=https' --proto-redir '=https' "$artifact" -o /usr/local/libexec/muse; \
    printf '%s  %s\n' "$checksum" /usr/local/libexec/muse | sha256sum -c -; \
    chmod 0755 /usr/local/libexec/muse; \
    printf '%s\n' '#!/bin/sh' 'if [ -z "$${META_API_KEY:-}" ] && [ -r /etc/agent-stack-muse.env ]; then . /etc/agent-stack-muse.env; fi' 'export MUSE_NO_AUTO_UPDATE=1' 'exec /usr/local/libexec/muse "$@"' >/usr/local/bin/muse; \
    chmod 0755 /usr/local/bin/muse; \
    muse --version; \
    rm -f /tmp/muse-manifest.json
%{ endif }

%{ if workspace_opencode_enabled }
# Pin V1 release assets rather than piping the mutable V1/V2 installer to bash.
# Ubuntu uses glibc; the x64 baseline also supports CPUs without AVX2.
ARG OPENCODE_RELEASE=${workspace_opencode_release}
RUN set -eu; \
    case "$(uname -m)" in x86_64) platform=linux-x64-baseline ;; aarch64|arm64) platform=linux-arm64 ;; *) echo "Unsupported OpenCode architecture" >&2; exit 1 ;; esac; \
    asset="opencode-$platform.tar.gz"; \
    curl -fsSL --proto '=https' --proto-redir '=https' \
      "https://api.github.com/repos/anomalyco/opencode/releases/tags/v$OPENCODE_RELEASE" -o /tmp/opencode-release.json; \
    jq -e --arg tag "v$OPENCODE_RELEASE" '.tag_name == $tag and .draft == false and .prerelease == false' /tmp/opencode-release.json >/dev/null; \
    checksum="$(jq -er --arg asset "$asset" '[.assets[] | select(.name == $asset)] | select(length == 1) | .[0].digest | select(test("^sha256:[0-9a-f]{64}$")) | ltrimstr("sha256:")' /tmp/opencode-release.json)"; \
    curl -fsSL --proto '=https' --proto-redir '=https' \
      "https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_RELEASE/$asset" -o /tmp/opencode.tar.gz; \
    printf '%s  %s\n' "$checksum" /tmp/opencode.tar.gz | sha256sum -c -; \
    install -d -m 0755 /usr/local/libexec; \
    tar -xzf /tmp/opencode.tar.gz -C /usr/local/libexec opencode; \
    chmod 0755 /usr/local/libexec/opencode; \
    test "$(/usr/local/libexec/opencode --version)" = "$OPENCODE_RELEASE"; \
    printf '%s\n' '#!/bin/sh' 'export OPENCODE_DISABLE_AUTOUPDATE=1' 'exec /usr/local/libexec/opencode "$@"' >/usr/local/bin/opencode; \
    chmod 0755 /usr/local/bin/opencode; \
    rm -f /tmp/opencode-release.json /tmp/opencode.tar.gz
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
