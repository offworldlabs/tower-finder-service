"""FastAPI entry point.

Run locally with:
    uvicorn app:app --reload

That serves the API only. For the UI, either build it once
(``cd frontend && npm ci && npm run build``) so ``create_app`` picks up
``frontend/dist``, or run ``npm run dev`` alongside, which proxies /api
straight back here.
"""

import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from routes.towers import router
from services.region_lookup import warm_borders

logger = logging.getLogger(__name__)

# The API's first path segment, for the catch-all below. Empty disables that
# guard rather than claiming every path.
_API_SEGMENT = router.prefix.strip("/")

# Absent from Python 3.12's table, and python:3.12-slim ships no
# /etc/mime.types, so without this the bundled Inter faces leave the image
# as application/octet-stream.
mimetypes.add_type("font/woff2", ".woff2")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Parse the US/CA/AU border polygons before anything is served. The first
    # classify_region() call otherwise does it inside a request handler, on the
    # event loop, once per process — ~1.5s during which every other request on
    # this worker is stalled. In a thread so the loop stays free even here.
    try:
        await run_in_threadpool(warm_borders)
    except Exception:
        # A missing or corrupt borders file should cost /api/towers its region
        # detection, not the whole service its boot: classify_region still
        # loads on demand and surfaces the real error on the request that
        # needs it. Raising here would crash-loop the container instead.
        logger.exception("Warming the border polygons failed; region lookup will load on demand")
    yield


def create_app() -> FastAPI:
    """A factory, so a test can build one against a different frontend dist."""
    dist = Path(os.getenv("TOWER_FINDER_FRONTEND_DIST", Path(__file__).parent / "frontend" / "dist"))
    application = FastAPI(
        title="tower-finder-service",
        description="Ranks broadcast towers near a node from FCC + Maprad data.",
        lifespan=lifespan,
    )
    application.include_router(router)

    # Mounted AFTER the API router so /api/* keeps winning; the catch-all below
    # would otherwise swallow it. Absent in a bare checkout where nobody has run
    # a frontend build, so the API still serves without one.
    if (dist / "index.html").is_file():
        application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @application.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str):
            """Serve the built UI, falling back to index.html for client routes."""
            # Before every other exit, so an unmatched endpoint is a 404 a caller
            # can branch on however it is spelled, rather than a 200 that fails
            # to parse. Segment, not prefix, so a doubled slash still counts.
            # GET only, since so is this route; other methods get a 405.
            if _API_SEGMENT and full_path.lstrip("/").split("/", 1)[0] == _API_SEGMENT:
                raise HTTPException(status_code=404)
            try:
                candidate = (dist / full_path).resolve()
            except ValueError:
                # A NUL byte reaches resolve() as ValueError, where is_file()
                # would have swallowed it. Serve the shell rather than a 500.
                return FileResponse(dist / "index.html")
            # resolve() + is_relative_to keeps "../" out of the served tree.
            if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return application


app = create_app()
