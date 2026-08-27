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


# ── Service ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Dependencies (declared in pyproject.toml: fastapi, uvicorn[standard], httpx, shapely)
COPY pyproject.toml ./
COPY app.py ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir .

# Built UI, served by app.py. Kept out of the pip layer so a frontend-only
# change doesn't invalidate the Python install.
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
