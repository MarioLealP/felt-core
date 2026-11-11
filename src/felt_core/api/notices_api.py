"""The tray's desktop pop-up routes (WP-42.5).

WP-42.5: the tray's routes for desktop pop-ups (bearer token, like every
desktop client): her alert lines to pop, and "handled" from a pop-up.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from felt_core.config import load_config
from felt_core.core import notices, systems

router = APIRouter()


@router.get("/v1/notices")
async def notice_lines(
    after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=50)] = 20
) -> list[dict]:
    """Her alert lines newer than id `after`, oldest first, for the tray to pop."""
    return await asyncio.to_thread(notices.after, load_config().db_path, after, limit)


@router.get("/v1/notices/latest")
async def latest() -> dict:
    """The newest notice id, so the tray starts from now."""
    return {"id": await asyncio.to_thread(notices.latest, load_config().db_path)}


@router.post("/v1/incidents/{incident_id}/handled")
async def handled(incident_id: int) -> dict:
    """Mark an incident handled from its pop-up (409 when it isn't open)."""
    if not await asyncio.to_thread(systems.mark_handled, load_config().db_path, incident_id):
        raise HTTPException(status_code=409, detail="not open")
    return {"ok": True}
