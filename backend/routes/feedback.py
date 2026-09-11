"""Fleet feedback ingest, and an admin view of what has come back.

Write side is gated by its own token (TOWER_FINDER_FEEDBACK_TOKEN) because every
node holds it; the read side is admin-only because the rollup names callsigns
and node counts per site.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from core.auth import require_admin, require_feedback_token
from models.feedback import TowerOutcome, TowerOutcomeBatch
from services import tower_feedback

router = APIRouter(prefix="/api/feedback")


@router.post("/tower-outcome", dependencies=[Depends(require_feedback_token)])
async def post_tower_outcome(payload: TowerOutcome | TowerOutcomeBatch):
    """Record what a node (or the archive job) saw on towers we recommended.

    Accepts one row or a batch of up to 100. A node reports one row per
    candidate it tried; the archive job posts windows of aggregates.

    `ignored` counts rows already held for the same (node, run, tower). A
    retried post therefore answers 200 with everything ignored, which is the
    reply a node wants: the run is on record, stop retrying.
    """
    rows = payload if isinstance(payload, list) else [payload]
    # In a thread: SQLite commits fsync, and this service runs one worker, so
    # doing it inline would stall every in-flight tower search behind the disk.
    stored = await run_in_threadpool(tower_feedback.record_many, [row.model_dump() for row in rows])
    return {"stored": stored, "ignored": len(rows) - stored}


@router.get("/summary", dependencies=[Depends(require_admin)])
async def get_feedback_summary(limit: int = Query(50, ge=1, le=500)):
    """Per-tower rollup of stored outcomes, busiest towers first."""
    towers = await run_in_threadpool(tower_feedback.summary, limit)
    return {"towers": towers, "count": len(towers)}
