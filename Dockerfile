# syntax=docker/dockerfile:1.7
# Reproducible build: base images pinned by digest, every Python package pinned by version and
# checksum in uv.lock (`--frozen` refuses to run if the lock and pyproject.toml disagree).
# The app itself is copied as plain source, so no build tool is downloaded at build time.

# Bump these together with `docker buildx imagetools inspect <image>` to get the new digest.
# Python 3.12.15 with Debian's October 2026 fixes: OpenSSL 3.0.22 and ca-certificates without the emSign ECC
# root whose intermediate keys leaked. uv 0.13.5 drops that root from its own store and blocks those keys.
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

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
COPY --from=build --chown=root:root /app/src /app/src
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
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
CMD ["python", "-m", "chemotracker"]
