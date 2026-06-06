FROM python:3.12-slim

WORKDIR /app

# Dependencies (declared in pyproject.toml: fastapi, uvicorn[standard], httpx)
COPY pyproject.toml ./
COPY app.py ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir .

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
