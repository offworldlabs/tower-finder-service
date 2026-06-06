# CI/CD for tower-finder-service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CI (lint + test) and CD (build + deploy to the production droplet via a Cloudflare Tunnel) for the standalone `tower-finder-service`, served at `tower-finder.retina.fm`.

**Architecture:** The service runs as its own Docker Compose stack at `/opt/tower-finder-service` on the production droplet (`157.245.214.30`), independent of the `tower-finder` stack. A `cloudflared` sidecar dials outbound to Cloudflare and forwards `tower-finder.retina.fm` → `http://tower-finder-service:8000`, so no host port is published and the existing nginx (which owns 80/443) is untouched. GitHub Actions runs lint + tests on every PR/push, and on push to `main` it SSHes in, hard-resets to `origin/main`, rebuilds, and smoke-tests the public URL.

**Tech Stack:** FastAPI + uvicorn, Docker / Docker Compose, `cloudflare/cloudflared`, GitHub Actions (`appleboy/ssh-action`), ruff, pytest.

**Reference spec:** `docs/superpowers/specs/2026-06-03-cicd-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/routes/towers.py` (modify) | Add cheap `GET /api/health` liveness endpoint on the existing router. |
| `backend/tests/test_health.py` (create) | Test for the health endpoint. |
| `Dockerfile` (create) | Build a runnable image: deps + source, `PYTHONPATH` set, non-root, uvicorn on 8000. |
| `.dockerignore` (create) | Keep build context small; exclude caches, `frontend/`, tests, runtime data. |
| `docker-compose.yml` (create) | `tower-finder-service` + `cloudflared`; healthcheck; named runtime volume; `env_file`. |
| `.env.example` (create) | Document `TUNNEL_TOKEN`, `MAPRAD_API_KEY`, `TOWER_FINDER_RUNTIME_DIR`. |
| `deploy/smoke-test.sh` (create) | Post-deploy curl checks against the public URL. |
| `.github/workflows/ci.yml` (create) | `test` → `deploy` (main only) → `smoke`. |
| `README.md` (modify) | Add a "Deployment" section documenting one-time setup. |

---

## Task 1: Add `/api/health` endpoint

**Files:**
- Modify: `backend/routes/towers.py`
- Test: `backend/tests/test_health.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_health.py`:

```python
"""Tests for the liveness health endpoint."""

import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_health_returns_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_health.py -v`
Expected: FAIL — `assert 404 == 200` (route does not exist yet).

- [ ] **Step 3: Add the route**

In `backend/routes/towers.py`, add this endpoint immediately after the
`router = APIRouter()` line (around line 21), before the `# ── Helpers ──` block:

```python
@router.get("/api/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/tests/test_health.py -v`
Expected: PASS.

- [ ] **Step 5: Verify lint + full unit suite still pass**

Run: `ruff check . && ruff format --check . && pytest -m "not integration" -q`
Expected: no lint errors; all non-integration tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/routes/towers.py backend/tests/test_health.py
git commit -m "20260606 - Add /api/health liveness endpoint."
```

---

## Task 2: Dockerfile and .dockerignore

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

Note on imports: `app.py` is at the repo root and imports `from routes.towers import router`; `routes/towers.py` in turn imports `models` and `services`. The package metadata only exposes `clients/routes/services`, and tests rely on `pythonpath = [".", "backend"]`. The image replicates this with `PYTHONPATH=/app:/app/backend`, so all source imports resolve from the copied tree.

- [ ] **Step 1: Create `.dockerignore`**

```
.git
.venv
__pycache__
*.py[cod]
*.egg-info
.pytest_cache
.ruff_cache
data/runtime
frontend
docs
backend/tests
.github
```

- [ ] **Step 2: Create `Dockerfile`**

```dockerfile
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
```

- [ ] **Step 3: Build the image to verify it builds**

Run: `docker build -t tower-finder-service:plan-check .`
Expected: build completes successfully (ends with `naming to ...tower-finder-service:plan-check`).

- [ ] **Step 4: Run the container and verify the app boots + health responds**

Run:
```bash
docker run --rm -d --name tfs-check -p 8000:8000 tower-finder-service:plan-check
sleep 3
curl -fsS http://localhost:8000/api/health
docker rm -f tfs-check
```
Expected: `{"status":"ok"}` printed; the import chain (`app` → `routes` → `models`/`services`) resolved at runtime.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "20260606 - Add Dockerfile and .dockerignore for the service."
```

---

## Task 3: docker-compose.yml and .env.example

**Files:**
- Create: `docker-compose.yml`
- Create: `.env.example`

- [ ] **Step 1: Create `.env.example`**

```dotenv
# Cloudflare Tunnel token (from Zero Trust → Networks → Tunnels).
# Routes tower-finder.retina.fm → http://tower-finder-service:8000.
TUNNEL_TOKEN=

# Optional: required only for non-US (au/ca) queries. US works FCC-only.
MAPRAD_API_KEY=

# Where tower_config.json runtime overlay is read/written (default below).
TOWER_FINDER_RUNTIME_DIR=data/runtime
```

- [ ] **Step 2: Create `docker-compose.yml`**

```yaml
services:
  tower-finder-service:
    build: .
    env_file:
      - .env
    volumes:
      - runtime-data:/app/data/runtime
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "3"

  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment:
      - TUNNEL_TOKEN=${TUNNEL_TOKEN}
    depends_on:
      tower-finder-service:
        condition: service_healthy
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "3"

volumes:
  runtime-data:
```

- [ ] **Step 3: Validate the compose file parses and resolves**

Run:
```bash
cp .env.example .env
docker compose config >/dev/null && echo "compose OK"
rm .env
```
Expected: `compose OK` (no YAML/interpolation errors). `${TUNNEL_TOKEN}` resolves from the temporary `.env`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "20260606 - Add compose stack with cloudflared tunnel sidecar."
```

---

## Task 4: Smoke-test script

**Files:**
- Create: `deploy/smoke-test.sh`

- [ ] **Step 1: Create `deploy/smoke-test.sh`**

```bash
#!/usr/bin/env bash
# Post-deploy smoke test for tower-finder-service.
# Hits the public URL (through the Cloudflare tunnel) to validate the full path.
set -euo pipefail

BASE_URL="${BASE_URL:-https://tower-finder.retina.fm}"
PASS=0
FAIL=0

check_status() {
  local name="$1" url="$2" expected="$3"
  printf "  %-40s " "$name"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 --max-time 60 "$url" 2>/dev/null) \
    || code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 --max-time 60 "$url" 2>/dev/null) \
    || { echo "FAIL (connection)"; FAIL=$((FAIL + 1)); return; }
  if [ "$code" = "$expected" ]; then
    echo "OK ($code)"; PASS=$((PASS + 1))
  else
    echo "FAIL ($code != $expected)"; FAIL=$((FAIL + 1))
  fi
}

echo "── tower-finder-service smoke tests (${BASE_URL}) ──"
check_status "GET /api/health" "${BASE_URL}/api/health" "200"
check_status "GET /api/config" "${BASE_URL}/api/config" "200"
check_status "GET /api/towers (Greenville SC)" "${BASE_URL}/api/towers?lat=34.85&lon=-82.40" "200"

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2: Make it executable and lint it**

Run:
```bash
chmod +x deploy/smoke-test.sh
bash -n deploy/smoke-test.sh && echo "syntax OK"
```
Expected: `syntax OK` (no bash syntax errors).

- [ ] **Step 3: Commit**

```bash
git add deploy/smoke-test.sh
git commit -m "20260606 - Add post-deploy smoke-test script."
```

---

## Task 5: GitHub Actions workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml
      - run: pip install -e ".[dev]"
      - name: Lint (ruff)
        run: ruff check .
      - name: Format check (ruff)
        run: ruff format --check .
      - name: Unit tests
        run: pytest -m "not integration" -q

  deploy:
    runs-on: ubuntu-latest
    needs: [test]
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    concurrency:
      group: tf-svc-deploy
      cancel-in-progress: false
    steps:
      - name: Deploy to production via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.DEPLOY_HOST }}
          username: root
          key: ${{ secrets.DEPLOY_SSH_KEY }}
          command_timeout: 20m
          script: |
            cd /opt/tower-finder-service
            # Hard-reset rather than `git pull`: pull aborts silently on a
            # divergent branch (a local commit on the server), the build then
            # caches every layer because the source "didn't change", and the
            # new code never ships. The deploy server must mirror origin/main.
            git fetch origin main
            git reset --hard origin/main
            docker compose up -d --build
            # Wait for the service to become healthy (up to 60s).
            for i in $(seq 1 12); do
              if docker compose exec -T tower-finder-service \
                python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" 2>/dev/null; then
                echo "Deploy healthy after ~$((i * 5))s"
                exit 0
              fi
              echo "Waiting for health check... attempt $i/12"
              sleep 5
            done
            echo "Health check failed after 60s"
            docker compose logs --tail=50
            exit 1

  smoke:
    runs-on: ubuntu-latest
    needs: [deploy]
    steps:
      - uses: actions/checkout@v4
      - name: Smoke test public URL
        run: bash deploy/smoke-test.sh
```

- [ ] **Step 2: Validate the workflow YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('yaml OK')"`
Expected: `yaml OK`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "20260606 - Add CI/CD workflow: test, deploy, smoke."
```

---

## Task 6: README deployment section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Append a Deployment section to `README.md`**

Add this section at the end of `README.md` (after the existing `## Origin` section):

````markdown
## Deployment

CI/CD runs via GitHub Actions (`.github/workflows/ci.yml`):

- **Every PR / push to `main`**: `ruff check`, `ruff format --check`, and
  `pytest -m "not integration"`.
- **Push to `main`**: after tests pass, SSHes to the production droplet,
  hard-resets to `origin/main`, rebuilds the stack, waits for health, then
  smoke-tests `https://tower-finder.retina.fm`.

The service runs as its own Docker Compose stack at `/opt/tower-finder-service`,
exposed at `tower-finder.retina.fm` through a Cloudflare Tunnel (`cloudflared`
sidecar) — no host port is published and the existing `tower-finder` stack is
untouched.

### One-time setup

**1. Deploy SSH key (run locally):**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tower_finder_service_deploy -C "tfs-deploy" -N ""
ssh root@157.245.214.30 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys" \
  < ~/.ssh/tower_finder_service_deploy.pub
```

Then add GitHub Actions repository secrets (Settings → Secrets and variables → Actions):
- `DEPLOY_HOST` = `157.245.214.30`
- `DEPLOY_SSH_KEY` = contents of `~/.ssh/tower_finder_service_deploy` (the private key)

**2. Cloudflare Tunnel (Zero Trust dashboard):**

- Networks → Tunnels → Create a tunnel (type `cloudflared`); copy the token.
- Add a public hostname: `tower-finder.retina.fm` → service
  `http://tower-finder-service:8000` (this auto-creates the DNS record).

**3. Droplet bootstrap (run on the droplet as root):**

```bash
git clone https://github.com/offworldlabs/tower-finder-service.git /opt/tower-finder-service
cd /opt/tower-finder-service
cp .env.example .env
# Edit .env: set TUNNEL_TOKEN (required) and MAPRAD_API_KEY (optional, non-US only).
docker compose up -d --build
```

After this, every push to `main` redeploys automatically.

### Rollback (manual)

No automated rollback in v1. To roll back, SSH to the droplet and reset to a
known-good commit:

```bash
cd /opt/tower-finder-service
git reset --hard <good-commit-sha>
docker compose up -d --build
```
````

- [ ] **Step 2: Verify the README still renders as valid Markdown (no broken fences)**

Run: `grep -c '^```' README.md`
Expected: an even number (all code fences balanced).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "20260606 - Document CI/CD deployment in README."
```

---

## Manual steps (outside this repo — owner action required)

These are documented in the README but require account access the implementer
may not have; they must be done before the first `main` deploy succeeds:

1. Generate + install the deploy SSH key; add `DEPLOY_HOST` and `DEPLOY_SSH_KEY`
   GitHub secrets.
2. Create the Cloudflare Tunnel + `tower-finder.retina.fm` public hostname; get the token.
3. Clone to `/opt/tower-finder-service` on the droplet and create `.env` with `TUNNEL_TOKEN`.

Until these exist, the `test` job will pass on PRs, but the `deploy`/`smoke`
jobs (push to `main` only) will fail at the SSH/curl step.
