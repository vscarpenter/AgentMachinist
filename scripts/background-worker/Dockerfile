# Build explicitly before admitting Tasks:
# docker build -t machinist-worker:pilot scripts/background-worker
# The runtime provides a non-root uid, ephemeral home and only the clone mount.
FROM node:22-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.8.17 AS uv
FROM python:3.12-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv /uv /uvx /usr/local/bin/
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates coreutils build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@0.151.0 \
    && npm cache clean --force

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy
WORKDIR /tmp
CMD ["/bin/sh"]
