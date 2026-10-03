"""WP-42.5: the tray's routes for desktop pop-ups (bearer token, like every
desktop client): her alert lines to pop, and "handled" from a pop-up."""

import asyncio

from fastapi import APIRouter, HTTPException, Query

from felt_core import notices, systems
from felt_core.config import load_config

router = APIRouter()


@router.get("/v1/notices")
async def notice_lines(
    after: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=50)
) -> list[dict]:
    return await asyncio.to_thread(notices.after, load_config().db_path, after, limit)


@router.get("/v1/notices/latest")
async def latest() -> dict:
    return {"id": await asyncio.to_thread(notices.latest, load_config().db_path)}


@router.post("/v1/incidents/{incident_id}/handled")
async def handled(incident_id: int) -> dict:
    if not await asyncio.to_thread(systems.mark_handled, load_config().db_path, incident_id):
        raise HTTPException(status_code=409, detail="not open")
    return {"ok": True}
