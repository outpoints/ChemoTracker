# syntax=docker/dockerfile:1.7
# Reproducible build: base images pinned by digest, dependencies pinned by uv.lock.

# Bump these together with `docker buildx imagetools inspect <image>` to get the new digest.
ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.2@sha256:94a23af2d50e97b87b522d3cea24aaf8a1faedec1344c952767434f69585cbf9

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="ChemoTracker" \
      org.opencontainers.image.description="Self-hosted chemotherapy medicine and symptom tracker" \
      org.opencontainers.image.source="https://github.com/outpoints/ChemoTracker" \
      org.opencontainers.image.licenses="GPL-3.0-only"
ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} app \
 && useradd --system --uid ${APP_UID} --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /data /backups \
 && chown app:app /data /backups
COPY --from=build --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHEMO_DATA_DIR=/data \
    CHEMO_BACKUP_DIR=/backups \
    CHEMO_HOST=0.0.0.0 \
    CHEMO_PORT=8000 \
    TZ=UTC
USER app
WORKDIR /app
VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('CHEMO_PORT', '8000')}/healthz\", timeout=4)"]
CMD ["chemotracker"]
