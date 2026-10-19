"""Mario's calendars, through Home Assistant (felt-ha-hub, 2026-10-19: Mario,
"we can do full move"). HA's Google integration holds the one Google login;
Felt reads and writes with her non-admin HA user. Before this she had her own
Google OAuth client (google_calendar.py, 2026-09-14), now gone.

Every calendar HA has counts (Mario: "All of them") - all-day ones like
holidays and birthdays never get heads-ups anyway. Reminders (set_reminder)
live in their own calendar, `ha_calendar_reminders`: being in it is what
makes an event a reminder, and its own notification setting (0 min) makes
the phone ring on time - no per-event tags or overrides, which HA can't set.

Events come back in Google's shape (`start`/`end` as {"dateTime"} or
{"date"}, `summary`, `location`, `description`, `status`) from HA's REST
`/api/calendars/<entity>`, which - unlike the `calendar.get_events` service -
also gives `uid`, `recurrence_id` and `rrule`. `id` is added: the occurrence's
recurrence_id, else the uid. HA's Google integration already drops events
Mario declined. HA syncs Google on a timer, so `get_event` asks it to
refresh first. Creating goes through HA's websocket (`calendar/event/create`),
the only way to pass a repeat rule.
"""

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import websockets

from felt_core.config import FeltConfig

TZ = ZoneInfo("Europe/Lisbon")
# HA's CalendarEntityFeature.CREATE_EVENT
_CREATE_EVENT = 1
_URL = re.compile(r"https?://\S+")


class CalendarUnavailable(Exception):
    """HA isn't configured, didn't answer, or refused."""


def connected(config: FeltConfig) -> bool:
    return bool(config.ha_url and config.ha_token)


def _headers(config: FeltConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.ha_token}"}


def _event(raw: dict) -> dict:
    """Pure: HA's event + `id` (this occurrence's, for a repeating one)."""
    return {**raw, "id": raw.get("recurrence_id") or raw.get("uid") or ""}


async def list_calendars(client: httpx.AsyncClient, config: FeltConfig) -> list[dict]:
    """Every calendar HA has, as {"id": entity_id, "summary": name}; the
    default one (`ha_calendar_default`) is marked "primary"."""
    response = await client.get(f"{config.ha_url}/api/calendars", headers=_headers(config))
    response.raise_for_status()
    return [
        {
            "id": c["entity_id"],
            "summary": c.get("name") or c["entity_id"],
            "primary": c["entity_id"] == config.ha_calendar_default,
        }
        for c in response.json()
    ]


async def list_events(
    client: httpx.AsyncClient,
    config: FeltConfig,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict]:
    response = await client.get(
        f"{config.ha_url}/api/calendars/{calendar_id}",
        headers=_headers(config),
        params={"start": time_min.isoformat(), "end": time_max.isoformat()},
    )
    response.raise_for_status()
    return [_event(e) for e in response.json()]


async def refresh(client: httpx.AsyncClient, config: FeltConfig, calendar_id: str) -> None:
    """Have HA re-sync the calendar from Google now, not on its timer."""
    response = await client.post(
        f"{config.ha_url}/api/services/homeassistant/update_entity",
        headers=_headers(config),
        json={"entity_id": calendar_id},
    )
    response.raise_for_status()


async def get_event(
    client: httpx.AsyncClient,
    config: FeltConfig,
    calendar_id: str,
    event_id: str,
    around: datetime,
) -> dict | None:
    """The event as Google has it now (refreshed), looked up within a day of
    `around` (its start when last seen), or None if it's gone - or moved
    further than that, which the next poll picks up anyway."""
    await refresh(client, config, calendar_id)
    window = timedelta(days=1)
    for event in await list_events(client, config, calendar_id, around - window, around + window):
        if event["id"] == event_id:
            return event
    return None


async def writable(client: httpx.AsyncClient, config: FeltConfig, calendar_id: str) -> bool:
    """Whether HA can add events to it (the holidays calendar can't)."""
    response = await client.get(
        f"{config.ha_url}/api/states/{calendar_id}", headers=_headers(config)
    )
    response.raise_for_status()
    features = response.json().get("attributes", {}).get("supported_features") or 0
    return bool(features & _CREATE_EVENT)


async def _ws_call(config: FeltConfig, message: dict) -> dict:
    """One command over HA's websocket; tools/e2e_chat.py replaces this."""
    url = re.sub(r"^http", "ws", config.ha_url or "") + "/api/websocket"
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": config.ha_token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise CalendarUnavailable("HA refused the token")
            await ws.send(json.dumps({"id": 1, **message}))
            result = json.loads(await ws.recv())
    except (OSError, websockets.WebSocketException) as e:
        raise CalendarUnavailable(str(e)) from e
    if not result.get("success"):
        raise CalendarUnavailable(str(result.get("error")))
    return result


async def create_event(
    config: FeltConfig,
    calendar_id: str,
    summary: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
    *,
    rrule: str | None = None,
) -> None:
    """`rrule`: an iCal repeat rule without the "RRULE:" prefix, e.g.
    "FREQ=WEEKLY"."""
    event: dict = {"summary": summary, "dtstart": start.isoformat(), "dtend": end.isoformat()}
    if description:
        event["description"] = description
    if rrule:
        event["rrule"] = rrule
    await _ws_call(
        config, {"type": "calendar/event/create", "entity_id": calendar_id, "event": event}
    )


def link(event: dict) -> str | None:
    """Pure: the first web link in the event's place or description (HA
    doesn't pass on Google's Meet link field)."""
    for text in (event.get("location"), event.get("description")):
        m = _URL.search(text or "")
        if m:
            return m[0].rstrip(".,;)>\"'")
    return None


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Pure: the [start, end) of `day` as tz-aware Europe/Lisbon datetimes."""
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
    its local start time, an all-day event (a "date" instead of a
    "dateTime" - how the holiday calendar reports its entries) is
    marked as such instead."""
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
