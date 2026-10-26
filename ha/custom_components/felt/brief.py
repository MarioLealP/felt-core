"""Today's morning brief out of her Margins: the newest line with source
"brief" from today (local time), paging back through the app's /margins
(a busy day of remarks can push the morning's brief a few pages back).
The same rule as the dashboard's felt-brief-card. No Home Assistant imports,
so it is testable on its own."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, tzinfo
from typing import Any

PAGE = 50
PAGES = 6


async def todays_brief(
    fetch: Callable[[str], Awaitable[Any]], today: date, tz: tzinfo
) -> dict | None:
    """{"text", "at"} for today's brief, None when there isn't one (yet).
    `fetch` GETs an app path (pages come oldest first)."""
    before = None
    for _ in range(PAGES):
        path = f"/margins?limit={PAGE}" + (f"&before={before}" if before is not None else "")
        lines = await fetch(path)
        if not isinstance(lines, list) or not lines:
            return None
        for line in reversed(lines):
            created = line.get("created_at")
            if not created:
                continue
            at = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(tz)
            if at.date() != today:
                return None  # past today: it isn't there
            if line.get("source") == "brief":
                return {"text": line.get("content", ""), "at": at}
        before = lines[0]["id"]
    return None
