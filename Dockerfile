# The uv that syncs the image. Global because only an ARG declared ahead of
# every stage is visible to a FROM.
ARG UV_VERSION=0.12.5

# ── Frontend build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# The CARTO basemap key, baked into the bundle by Vite. Declared here rather
# than at the top of the stage so that changing it re-runs this build step
# alone and leaves the `npm ci` layer cached. The empty default matters: a host
# that has no key (or a plain `docker build`) produces unkeyed URLs, and so
# CARTO's watermarked tiles — which is exactly what shipped before this arg
# existed.
ARG VITE_CARTO_API_KEY=""
ENV VITE_CARTO_API_KEY=${VITE_CARTO_API_KEY}
RUN npm run build


# ── uv ────────────────────────────────────────────────────────────────────────
# Only ever a mount source below, so nothing from it reaches the shipped image.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv


# ── Service ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Dependencies, synced from uv.lock ahead of the source so a code change leaves
# this layer cached. A venv rather than the system site-packages, because
# `uv sync` makes its environment match the lock and would remove the base
# image's own pip.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
# Holds the venv to this image's interpreter.
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
# --compile-bytecode: the venv is root-owned and the app runs as appuser, so any
# .pyc not written here is recompiled on every boot.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-cache --compile-bytecode

COPY app.py ./
COPY backend/ ./backend/

# Built UI, served by app.py. Kept out of the dependency layer so a
# frontend-only change doesn't invalidate the Python install.
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# app.py (root) + backend packages (models, config, services, clients, routes)
# must both be importable — mirrors the test config's pythonpath = [".", "backend"].
ENV PYTHONPATH=/app:/app/backend

# Runtime overlay dir (TOWER_FINDER_RUNTIME_DIR default is data/runtime under CWD).
# Mounted as a named volume in compose; create + own it here so the volume
# inherits the right owner.
RUN useradd -r -s /usr/sbin/nologin appuser && \
    mkdir -p /app/data/runtime && \
    chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
