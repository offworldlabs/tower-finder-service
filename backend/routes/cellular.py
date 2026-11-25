"""Whether cellular data is switched on here, and what the cell database holds."""

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from services import cell_sites

router = APIRouter(prefix="/api")


@router.get("/cellular")
async def get_cellular():
    """Cell database status: row counts per country and per source, build time, attribution.

    With the flag off this is the 404 every unknown /api path gets, so a
    deployment that has not switched cellular on answers as it did before.
    """
    if not cell_sites.cellular_enabled():
        raise HTTPException(status_code=404)
    # In a thread: the first call per database file scans the whole cells table,
    # and this service runs a single worker.
    return await run_in_threadpool(cell_sites.cellular_status)
