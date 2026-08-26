"""FastAPI entry point.

Run locally with:
    uvicorn app:app --reload

That serves the API only. For the UI, either build it once
(``cd frontend && npm ci && npm run build``) so the block at the bottom of this
file picks up ``frontend/dist``, or run ``npm run dev`` alongside — Vite proxies
/api straight back here.
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from routes.towers import router

app = FastAPI(
    title="tower-finder-service",
    description="Ranks broadcast towers near a node from FCC + Maprad data.",
)
app.include_router(router)


# ── Frontend ──────────────────────────────────────────────────────────────────
#
# Mounted AFTER the API router so /api/* keeps winning; the catch-all below
# would otherwise swallow it. Absent in a bare `uvicorn app:app` checkout where
# nobody has run a frontend build — the API still serves fine, so this stays
# optional rather than a hard startup requirement.

_DIST = Path(os.getenv("TOWER_FINDER_FRONTEND_DIST", Path(__file__).parent / "frontend" / "dist"))

if (_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        """Serve the built UI, falling back to index.html for client routes."""
        candidate = (_DIST / full_path).resolve()
        # resolve() + is_relative_to keeps "../" out of the served tree.
        if full_path and candidate.is_file() and candidate.is_relative_to(_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
