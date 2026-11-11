"""The diary: /v1/diary."""

from fastapi import (
    APIRouter,
    HTTPException,
)
from pydantic import BaseModel

from felt_core.config import load_config
from felt_core.core import diary
from felt_core.db.diary import delete_diary, get_diary, get_diary_list, get_diary_weeks, update_diary_entry

router = APIRouter()


@router.get("/v1/diary")
async def diary_list_endpoint(limit: int = 30, date: str | None = None) -> dict:
    """LAN-only, like the rest of felt-core's API (felt-dashboard's "Felt's diary" page, WP-26).

    `date=` returns just that one entry (or an empty `entries` list); otherwise the
    most recent `limit` entries, newest first, plus the weekly summaries.
    """
    config = load_config()
    if date is not None:
        row = get_diary(config.db_path, date)
        return {"entries": [row] if row else [], "weeks": []}
    return {
        "entries": get_diary_list(config.db_path, limit=limit),
        "weeks": get_diary_weeks(config.db_path),
    }


class DiaryPatchRequest(BaseModel):
    """The corrected entry text Mario sends for one diary entry."""

    entry: str


@router.patch("/v1/diary/{date}")
async def diary_patch_endpoint(date: str, req: DiaryPatchRequest) -> dict:
    """Mario correcting her memory (WP-26: "Decided ... Mario can correct her memory").

    Marks the row edited. Deliberately CLEARS callbacks_json rather than spending a
    second model call regenerating them from the new text - a hand-edited entry is
    usually a small correction, and a stale callback pointing at wording that's no
    longer there is worse than no callback until the next nightly write naturally
    adds fresh ones. Documented here since the WP left this as an implementer call.
    """
    config = load_config()
    updated = update_diary_entry(config.db_path, date, req.entry, "[]")
    if not updated:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.delete("/v1/diary/{date}")
async def diary_delete_endpoint(date: str) -> dict:
    """Delete one diary entry and report whether a row was removed."""
    config = load_config()
    return {"deleted": delete_diary(config.db_path, date)}


@router.post("/v1/diary/write")
async def diary_write_debug_endpoint(date: str) -> dict:
    """A manual trigger for testing (WP-26).

    gated by FELT_DEBUG the same way /v1/proactive/debug is - not a normal client path.
    """
    config = load_config()
    if not config.debug:
        raise HTTPException(status_code=404, detail="not found")
    written = await diary.write_diary_for_date(config, date)
    return {"written": written}
