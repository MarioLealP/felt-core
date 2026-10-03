"""WP-58: the felt-responder's two routes, at felt.clockmost.com/v1/incidents/*.

The responder (a headless Claude in a sandbox on the server) authenticates
with its own token, FELT_RESPONDER_TOKEN, which api.py accepts on these
routes only - so a leaked responder token can see whether incidents wait,
claim and report them, nothing else. The main API token works here too.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from felt_core import responder
from felt_core.config import load_config

router = APIRouter(prefix="/v1/incidents")


@router.get("/waiting")
async def waiting() -> dict:
    """How many incidents wait for Claude, without claiming any."""
    return {"waiting": await asyncio.to_thread(responder.waiting, load_config().db_path)}


@router.post("/handoff")
async def handoff() -> dict:
    """Claims the next incident for Claude; {"incident": null} when there's
    nothing to do (or today's cap is spent)."""
    incident = await asyncio.to_thread(responder.claim_next, load_config().db_path)
    return {"incident": incident}


class Report(BaseModel):
    verdict: Literal["expected", "fixed-itself", "needs-mario", "unknown"]
    summary: str = Field(max_length=4000)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    for_mario: str = Field(default="", max_length=2000)
    proposed_fix: str = Field(default="", max_length=4000)
    confidence: Literal["high", "medium", "low"] = "low"
    couldnt_check: list[str] = Field(default_factory=list, max_length=50)


class ReportIn(BaseModel):
    report: Report
    session_id: str = Field(max_length=100)


@router.post("/{incident_id}/report")
async def report(incident_id: int, body: ReportIn) -> dict:
    saved = await asyncio.to_thread(
        responder.save_report,
        load_config().db_path,
        incident_id,
        body.report.model_dump(),
        body.session_id,
    )
    if not saved:
        raise HTTPException(
            status_code=409, detail="not handed to the responder, or already reported"
        )
    return {"ok": True}
