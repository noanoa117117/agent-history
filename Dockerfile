FROM ubuntu:24.04

ARG DEV_UID=1000
ARG DEV_GID=1000
ARG DEV_USER=amida
ARG CLAUDE_CODE_VERSION=2.1.220
ARG NODE_VERSION=24.18.1
ARG CODEX_VERSION=0.146.0

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/amida \
    CODEX_HOME=/home/amida/.codex \
    PATH=/home/amida/.local/bin:/home/amida/.codex/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        curl \
        git \
        gh \
        less \
        procps \
        python3 \
        ripgrep \
        sqlite3 \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node.js from the official binary distribution, pinned by ARG and verified
# against the release SHASUMS. Distro packages move independently of the image,
# so pinning here is what makes the build reproducible.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    grep " ${tarball}\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "${tarball}" -C /usr/local --strip-components=1 --no-same-owner \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md; \
    rm -f "${tarball}" SHASUMS256.txt; \
    node --version; \
    npm --version

# Codex CLI at build time, installed globally as root into /usr/local. Writing
# to a root-owned prefix before the USER switch is what keeps the unprivileged
# user out of npm's global-install permission problems: it only ever reads.
# The npm package is a small launcher that execs a platform-specific static
# binary, so no credentials and no per-user npm prefix are involved.
RUN set -eux; \
    npm install --global --no-fund --no-audit "@openai/codex@${CODEX_VERSION}"; \
    npm cache clean --force; \
    rm -rf /root/.npm; \
    command -v codex; \
    codex --version

RUN set -eux; \
    if ! getent group "$DEV_GID" >/dev/null; then \
        groupadd --gid "$DEV_GID" "$DEV_USER"; \
    fi; \
    group_name="$(getent group "$DEV_GID" | cut -d: -f1)"; \
    if ! getent passwd "$DEV_UID" >/dev/null; then \
        useradd --uid "$DEV_UID" --gid "$group_name" --create-home --shell /bin/bash "$DEV_USER"; \
    fi; \
    mkdir -p \
        /workspace/agent-history \
        /workspace/agent-history/data \
        /home/amida/.claude \
        /home/amida/.codex \
        /home/amida/.config/gh \
        /home/amida/.local; \
    chown -R "$DEV_UID:$DEV_GID" \
        /workspace/agent-history \
        /home/amida

COPY --chmod=0755 container/agent-history-git-status /usr/local/bin/agent-history-git-status
COPY --chmod=0755 container/agent-history-git-log /usr/local/bin/agent-history-git-log
COPY --chmod=0755 container/agent-history-pull /usr/local/bin/agent-history-pull
COPY --chmod=0755 container/agent-history-push /usr/local/bin/agent-history-push
COPY --chmod=0755 container/agent-history-purge-check /usr/local/bin/agent-history-purge-check
COPY --chmod=0755 container/agent-history-test /usr/local/bin/agent-history-test
COPY --chmod=0755 container/agent-history-worker-run /usr/local/bin/agent-history-worker-run
COPY --chmod=0755 container/run-timeboxed /usr/local/bin/run-timeboxed

USER $DEV_UID:$DEV_GID
WORKDIR /workspace/agent-history

# Claude Code's official native installer keeps the executable in the user's home.
# Passing the version selects the requested release without embedding credentials.
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "$CLAUDE_CODE_VERSION"

CMD ["sleep", "infinity"]
