"""Google Calendar — Felt's own dedicated OAuth client (2026-09-14,
Mario: "add event creation ... get all calendars, since has my main
account and my professional calendar + public holidays").

Deliberately Felt's own Google Cloud OAuth client rather than reusing
Noctalia's — checked first (2026-09-14): Noctalia's own stored refresh
token only carries `calendar.readonly` scope (confirmed against
Google's tokeninfo endpoint), and its hosted OAuth broker
(api.noctalia.dev) only ever seems to request that scope for its own
read-only calendar widget use case, so it couldn't have granted write
access anyway. Felt's client requests the full `calendar` scope
directly, holds its own client_id/client_secret in config, and isn't
coupled to a third-party broker's behavior for something this
central.

Access tokens are refreshed on demand and cached in-process (module-
level, single account — no need for anything fancier for a personal
assistant with one Google account) via the shared oauth_token.
refresh_access_token helper (WP-40) - same pattern spotify_client.py
uses.
"""

from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from felt_core.config import FeltConfig
from felt_core.oauth_token import refresh_access_token

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API_BASE = "https://www.googleapis.com/calendar/v3"
TZ = ZoneInfo("Europe/Lisbon")

_token_cache: dict = {"access_token": None, "expires_at": 0.0}


def events_url(calendar_id: str) -> str:
    """Pure: calendar IDs routinely contain characters URL-unsafe as a
    plain f-string path segment — the public-holidays calendar's real ID
    is "pt.portuguese#holiday@group.v.calendar.google.com", whose "#"
    was silently parsed as a URL fragment separator by everything after
    it (confirmed live, 2026-09-14) until this was added."""
    return f"{_API_BASE}/calendars/{quote(calendar_id, safe='')}/events"


async def _get_access_token(client: httpx.AsyncClient, config: FeltConfig) -> str:
    return await refresh_access_token(
        client,
        _token_cache,
        _TOKEN_URL,
        data={
            "client_id": config.google_calendar_client_id,
            "client_secret": config.google_calendar_client_secret,
            "refresh_token": config.google_calendar_refresh_token,
            "grant_type": "refresh_token",
        },
    )


async def list_calendars(client: httpx.AsyncClient, config: FeltConfig) -> list[dict]:
    token = await _get_access_token(client, config)
    response = await client.get(
        f"{_API_BASE}/users/me/calendarList",
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json().get("items", [])


async def list_events(
    client: httpx.AsyncClient,
    config: FeltConfig,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict]:
    token = await _get_access_token(client, config)
    response = await client.get(
        events_url(calendar_id),
        headers={"Authorization": f"Bearer {token}"},
        params={
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
        },
    )
    response.raise_for_status()
    return response.json().get("items", [])


async def get_event(
    client: httpx.AsyncClient, config: FeltConfig, calendar_id: str, event_id: str
) -> dict | None:
    """One event by id, or None if it's gone (404/410). A deleted event
    can also come back with `status: "cancelled"` - callers check that."""
    token = await _get_access_token(client, config)
    response = await client.get(
        f"{events_url(calendar_id)}/{quote(event_id, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code in (404, 410):
        return None
    response.raise_for_status()
    return response.json()


async def create_event(
    client: httpx.AsyncClient,
    config: FeltConfig,
    calendar_id: str,
    summary: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
    *,
    recurrence: list[str] | None = None,
    popup_minutes: int | None = None,
    transparent: bool = False,
    private: dict[str, str] | None = None,
) -> dict:
    """`recurrence`: RRULE lines (needs the start's timeZone, always sent);
    `popup_minutes`: the phone's own notification that many minutes before,
    instead of the calendar's default; `transparent`: doesn't block the
    time as busy; `private`: extendedProperties.private (Felt's own tags)."""
    token = await _get_access_token(client, config)
    body: dict = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)},
    }
    if description:
        body["description"] = description
    if recurrence:
        body["recurrence"] = recurrence
    if popup_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": popup_minutes}],
        }
    if transparent:
        body["transparency"] = "transparent"
    if private:
        body["extendedProperties"] = {"private": private}
    response = await client.post(
        events_url(calendar_id),
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    response.raise_for_status()
    return response.json()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Pure: the [start, end) of `day` as tz-aware Europe/Lisbon
    datetimes, suitable for timeMin/timeMax."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    return start, start + timedelta(days=1)


def resolve_calendar(calendars: list[dict], name: str | None) -> dict | None:
    """Pure. No name -> the primary calendar. A name -> substring match
    against each calendar's display name (same convention as every
    other name-matching skill in this project), preferring an exact
    match over a partial one."""
    if not name:
        return next((c for c in calendars if c.get("primary")), None)

    query = name.strip().lower()
    exact = [c for c in calendars if c.get("summary", "").lower() == query]
    if exact:
        return exact[0]
    partial = [c for c in calendars if query in c.get("summary", "").lower()]
    return partial[0] if partial else None


def format_event_line(event: dict) -> str:
    """Pure: one human-readable line for an event — a timed event shows
    its local start time, an all-day event (Google represents these
    with a "date" field instead of "dateTime" — this is how public
    holiday calendars report their entries) is marked as such instead."""
    start = event.get("start", {})
    title = event.get("summary", "(untitled)")
    if "dateTime" in start:
        when = datetime.fromisoformat(start["dateTime"]).astimezone(TZ)
        return f"{title} at {when.strftime('%H:%M')}"
    return f"{title} (all day)"


def summarize_events(events_by_calendar: list[tuple[str, list[dict]]]) -> str:
    """Pure: every (calendar_name, events) pair flattened into one
    speech-friendly summary, dropping calendars with nothing to show
    rather than saying "X has 0 events" for each one."""
    lines = []
    for calendar_name, events in events_by_calendar:
        for event in events:
            lines.append(f"{format_event_line(event)} ({calendar_name})")

    if not lines:
        return "Nothing on your calendar."
    return "You have: " + "; ".join(lines) + "."
