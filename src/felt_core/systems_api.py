"""WP-42.5: the Systems page's routes, at felt.clockmost.com/v1/app/*.

SSO-gated like the rest of the app (app_api.py): Caddy runs forward-auth on
exactly these paths and felt-core refuses a request without its
X-Authentik-Username header.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from felt_core import systems
from felt_core.app_api import sso_user
from felt_core.config import load_config

router = APIRouter(prefix="/v1/app")


@router.get("/systems")
async def systems_page(
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    _user: str = Depends(sso_user),
) -> dict:
    return await asyncio.to_thread(systems.page, load_config().db_path, before, limit)


@router.get("/incidents/{incident_id}")
async def incident(incident_id: int, _user: str = Depends(sso_user)) -> dict:
    found = await asyncio.to_thread(systems.details, load_config().db_path, incident_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return found


@router.post("/incidents/{incident_id}/handled")
async def handled(incident_id: int, _user: str = Depends(sso_user)) -> dict:
    if not await asyncio.to_thread(systems.mark_handled, load_config().db_path, incident_id):
        raise HTTPException(status_code=409, detail="not open")
    return {"ok": True}
