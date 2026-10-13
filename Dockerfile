# syntax=docker/dockerfile:1

# --- UI build ---
FROM node:22.23.3-bookworm-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json web/
RUN npm --prefix web ci
COPY web/ web/
# the rail logo is imported from the brand assets, outside web/
COPY assets/brand/ assets/brand/
RUN npm --prefix web run build

# --- runtime ---
FROM python:3.12.15-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Linux hosts: --build-arg CLAUDE_UID=$(id -u) so files the terminal writes in the repo are yours
ARG CLAUDE_UID=1000
# .git is mounted read-only (#185): git in the terminal reads history; commits happen on the host
RUN useradd --create-home --uid "$CLAUDE_UID" --user-group --shell /bin/bash claude \
 && install -d -m 0700 /var/lib/fenrir \
 && install -d -o claude -g claude /home/claude/.claude-config \
 && git config --system --add safe.directory /app

# pinned Claude Code (the installer checks the binary's sha256), root-owned so the terminal
# cannot replace what `claude` runs; updates come with a rebuild
ARG CLAUDE_CODE_VERSION=2.1.296
RUN curl -fsSL https://claude.ai/install.sh -o /tmp/install.sh \
 && HOME=/tmp/claude-install bash /tmp/install.sh "$CLAUDE_CODE_VERSION" \
 && install -m 0755 "$(readlink -f /tmp/claude-install/.local/bin/claude)" /usr/local/bin/claude \
 && rm -rf /tmp/claude-install /tmp/install.sh

# server code and UI live in /opt, outside the repo bind-mounted at /app (read-only, but for
# data/, rules/ and strategies/, which the terminal can write)
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /opt/fenrir
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable
COPY --from=web /build/web/dist/ web-dist/

# no setuid/setgid binaries (su, mount, passwd, ...), even run without no-new-privileges
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

ENV PATH=/opt/venv/bin:$PATH \
    UV_NO_SYNC=1 \
    CLAUDE_CONFIG_DIR=/home/claude/.claude-config \
    DISABLE_AUTOUPDATER=1 \
    FENRIR_BIND_ALL=1 \
    FENRIR_PTY_USER=claude \
    FENRIR_PRIVATE_DIR=/var/lib/fenrir \
    FENRIR_ENV_FILE=/var/lib/fenrir/.env

WORKDIR /app
EXPOSE 8000
CMD ["fenrir", "serve", "--port", "8000", "--static-dir", "/opt/fenrir/web-dist"]
