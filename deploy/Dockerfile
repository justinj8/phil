# Phil on Railway: the image is only the toolchain, the supervisor and the
# dashboard. The live checkout (core/, strategy/, journal/) is a git clone of
# your fork on the volume, so Phil's hourly commits never rebuild this image.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

# Pinned for reproducible builds; bump deliberately (npm view @anthropic-ai/claude-code dist-tags).
ARG CLAUDE_CODE_VERSION=2.1.274

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PHIL_HOME=/data/phil \
    PHIL_RUNTIME=/data/runtime \
    PHIL_RUNNER=railway \
    PORT=8080

# git: the checkout and its pushes. curl: core/watch.py's transport fallback.
# tini: PID 1, reaps the subprocesses Claude Code leaves behind.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version

# phil runs the supervisor and Claude Code; web runs the public dashboard.
RUN useradd --create-home --uid 1000 --shell /bin/bash phil \
 && useradd --no-create-home --uid 1001 --home-dir /tmp --shell /usr/sbin/nologin web

WORKDIR /app
COPY dashboard /app/dashboard
COPY deploy /app/deploy
RUN chmod +x /app/deploy/entrypoint.sh \
 && python3 -m py_compile /app/dashboard/*.py /app/deploy/*.py

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/entrypoint.sh"]
