"""FastAPI entry point.

Run locally with:
    uvicorn app:app --reload
"""

from fastapi import FastAPI

from routes.towers import router

app = FastAPI(
    title="tower-finder-service",
    description="Ranks broadcast towers near a node from FCC + Maprad data.",
)
app.include_router(router)
