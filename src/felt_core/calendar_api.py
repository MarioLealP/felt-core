"""The desktop's agenda (2026-10-15): read-only upcoming events for the tray,
which hands them to the bar's calendar over D-Bus. Felt keeps the one Google
login (server-side) and the reminders (calendar_headsup.py); the desktop only
shows. Minimal fields on purpose: no descriptions, no attendees."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, HTTPException

from felt_core import google_calendar
from felt_core.config import load_config

router = APIRouter()

_CACHE_S = 60.0
_cache: dict[int, tuple[float, list[dict]]] = {}
_lock = asyncio.Lock()


def _when(part: dict) -> tuple[str | None, bool]:
    """(ISO start/end, all_day) from a Google `start`/`end` object."""
    if "dateTime" in part:
        return part["dateTime"], False
    return part.get("date"), True


def _link(event: dict) -> str | None:
    """The meeting link, if any (Meet, or a video entry point)."""
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for ep in (event.get("conferenceData") or {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return None


def agenda_items(events_by_calendar: list[tuple[str, list[dict]]]) -> list[dict]:
    """Pure: Google events -> the bar's items, sorted by start. Cancelled
    events and ones Mario declined are left out; all-day events stay."""
    keyed: list[tuple[tuple[str, bool, str], dict]] = []
    for calendar, events in events_by_calendar:
        for e in events:
            if e.get("status") == "cancelled":
                continue
            if any(
                a.get("self") and a.get("responseStatus") == "declined"
                for a in e.get("attendees", [])
            ):
                continue
            start, all_day = _when(e.get("start", {}))
            end, _ = _when(e.get("end", {}))
            if not start:
                continue
            item = {
                "title": e.get("summary") or "(untitled)",
                "calendar": calendar,
                "start": start,
                "end": end,
                "all_day": all_day,
                "location": e.get("location"),
                "link": _link(e),
            }
            # by day, all-day first on its day, then by time
            keyed.append(((start[:10], not all_day, start), item))
    keyed.sort(key=lambda k: k[0])
    return [item for _, item in keyed]


@router.get("/v1/calendar/upcoming")
async def upcoming(days: int = 14) -> dict:
    days = max(1, min(days, 31))
    async with _lock:
        hit = _cache.get(days)
        if hit and time.monotonic() - hit[0] < _CACHE_S:
            return {"events": hit[1]}
        config = load_config()
        if not config.google_calendar_refresh_token:
            raise HTTPException(status_code=503, detail="calendar not connected")
        now = datetime.now(UTC)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with httpx.AsyncClient(timeout=20) as client:
            cals = await google_calendar.list_calendars(client, config)
            per_cal = await asyncio.gather(
                *(
                    google_calendar.list_events(
                        client, config, c["id"], start, now + timedelta(days=days)
                    )
                    for c in cals
                )
            )
        items = agenda_items(
            [(c.get("summary", ""), ev) for c, ev in zip(cals, per_cal, strict=True)]
        )
        _cache[days] = (time.monotonic(), items)
        return {"events": items}
