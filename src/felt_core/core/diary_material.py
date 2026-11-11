"""A day's raw material for the diary writer, gathered and compacted.

What she saw, what happened on the desktop, what they said.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from felt_core.config import FeltConfig
from felt_core.core.self_memory import LISBON
from felt_core.db.events import get_events_between
from felt_core.db.messages import get_message_sessions_between
from felt_core.db.observations import get_screen_observations_between

_MAX_MATERIAL_CHARS = 3000


_MAX_EVENT_DATA_CHARS = 2000


# -- pure helpers used by api.py's POST /v1/events --------------------------


def normalize_at(at: str | None) -> str:
    """Pure: a felt-animation timestamp as this DB's UTC 'YYYY-MM-DD HH:MM:SS' string.

    Pure: whatever felt-animation sent (an ISO8601 string, possibly
    with a 'Z' suffix and no offset - treated as local Europe/Lisbon time
    - or nothing at all) into the UTC 'YYYY-MM-DD HH:MM:SS' string every
    other timestamp column in this DB is stored as, so date-range queries
    stay a plain string comparison.
    """
    if at:
        try:
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LISBON)
            dt = parsed.astimezone(UTC)
        except ValueError:
            dt = datetime.now(UTC)
    else:
        dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def cap_event_data(data: dict) -> str:
    """Pure: `data` as JSON, or a stand-in marker if it's too big.

    Pure: `data` json-encoded, or a stand-in marker if it's too big
    ("cap data size" - WP-26's spec) - never a chopped, invalid-JSON
    string.
    """
    try:
        encoded = json.dumps(data, default=str)
    except TypeError, ValueError:
        return "{}"
    if len(encoded) > _MAX_EVENT_DATA_CHARS:
        return json.dumps({"_truncated": True, "keys": list(data)[:10]})
    return encoded


# -- material gathering (pure formatting + one async gatherer) --------------


def _text(data: dict, key: str) -> str:
    v = data.get(key)
    return " ".join(str(v).split())[:80] if v is not None else ""


def _num(data: dict, key: str) -> float | None:
    try:
        return float(data[key])
    except KeyError, TypeError, ValueError:
        return None


def _game_start(data: dict) -> str | None:
    name = _text(data, "name")
    return f'started playing "{name}"' if name else None


def _game_stop(data: dict) -> str | None:
    name = _text(data, "name")
    if not name:
        return None
    dur = _num(data, "duration_s")
    suffix = f" (about {round(dur / 60)} min)" if dur else ""
    return f'stopped playing "{name}"{suffix}'


def _afk_end(data: dict) -> str:
    dur = _num(data, "duration_s")
    return f"back after about {round(dur / 60)} min away" if dur else "back at the desk"


def _music_track(data: dict) -> str | None:
    title, artist = _text(data, "title"), _text(data, "artist")
    if not title:
        return None
    return f'played "{title}"' + (f" by {artist}" if artist else "")


def _mood(data: dict) -> str | None:
    axes = data.get("axes")
    if isinstance(axes, dict) and axes:
        top = max(axes.items(), key=lambda kv: abs(_as_float(kv[1])), default=None)
        if top:
            return f"mood leaned {top[0]}"
    return None


def _window_fall(data: dict) -> str:
    cause = _text(data, "cause")
    return f"took a tumble ({cause})" if cause else "took a tumble"


def _prop_used(data: dict) -> str | None:
    item = _text(data, "item")
    return f"played with the {item}" if item else None


def _hush(data: dict) -> str:
    minutes = _num(data, "minutes")
    return f"got hushed for {round(minutes)} min" if minutes else "got hushed"


def _hobby(data: dict) -> str | None:
    from felt_core.core.event_prompt import HOBBY_PHRASES

    hobby = _text(data, "hobby")
    if hobby not in HOBBY_PHRASES:
        return None
    minutes = _num(data, "minutes")
    spent = f"spent about {round(minutes)} min " if minutes else "spent some time "
    # The event prompt talks TO her ("your handheld"); the diary is hers.
    return spent + HOBBY_PHRASES[hobby].replace("your", "my") + " while Mario was away"


# day_start is missing on purpose: a trigger marker, not something worth narrating
_EVENT_LINES: dict[str, Callable[[dict], str | None]] = {
    "game_start": _game_start,
    "game_stop": _game_stop,
    "afk_start": lambda _data: "stepped away from the desk",
    "afk_end": _afk_end,
    "music_track": _music_track,
    "mood": _mood,
    "window_fall": _window_fall,
    "prop_used": _prop_used,
    "hush": _hush,
    "ledge_sit": lambda _data: "sat on a ledge for a while",
    "hobby": _hobby,
}


def _event_line(kind: str, data: dict) -> str | None:
    """Pure: one compact, cloud-safe line for a logged event, or None for a kind with nothing worth saying.

    Whitelisted fields only - same discipline as events.py's
    build_prompt, never dumps `data` wholesale.
    """
    line = _EVENT_LINES.get(kind)
    return line(data) if line else None


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except TypeError, ValueError:
        return 0.0


class RawMaterial(BaseModel):
    """One day's raw diary material: screen sightings, desktop events and chat windows."""

    model_config = ConfigDict(frozen=True)

    seen: list[dict]
    events: list[dict]
    chat_windows: list[str]


def compact_material(raw: RawMaterial) -> str:
    """Pure: dedupes and caps raw rows into one compact block for the writer prompt.

    Never touches facts - this whole module never reads the facts
    table at all.
    """
    lines: list[str] = []
    for obs in raw.seen:
        description = obs.get("description") or ""
        if not description:
            continue
        if obs.get("spoken_reply"):
            lines.append(f"saw: {description} - she said: {obs['spoken_reply']}")
        else:
            lines.append(f"saw: {description}")
    for ev in raw.events:
        try:
            data = json.loads(ev["data_json"])
        except json.JSONDecodeError:
            data = {}
        line = _event_line(ev["kind"], data if isinstance(data, dict) else {})
        if line:
            lines.append(line)
    # Dedupe while keeping first-seen order (the standard dict.fromkeys trick).
    lines = list(dict.fromkeys(lines))
    for window in raw.chat_windows:
        lines.append(f"chat: {window}")

    return "\n".join(lines)[:_MAX_MATERIAL_CHARS]


def _lisbon_date_bounds_utc(date_str: str) -> tuple[str, str]:
    """Pure: the [start, end) of one Europe/Lisbon date as UTC strings.

    Pure: the [start, end) of one Europe/Lisbon calendar date, as UTC
    'YYYY-MM-DD HH:MM:SS' strings - what every created_at/at column in
    this DB is stored as, so a plain string comparison works.
    """
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LISBON)
    start = day_start.astimezone(UTC)
    end = (day_start + timedelta(days=1)).astimezone(UTC)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


async def gather_material(config: FeltConfig, date_str: str) -> str:
    """Material for one Europe/Lisbon calendar date.

    Material for one Europe/Lisbon calendar date: screen `seen` text
    plus her spoken replies, logged desktop events, and chat as whole
    session windows.
    """
    start, end = _lisbon_date_bounds_utc(date_str)
    seen = await get_screen_observations_between(config.db_path, start, end)
    events = await get_events_between(config.db_path, start, end)
    sessions = await get_message_sessions_between(config.db_path, start, end)

    chat_windows: list[str] = []
    for session in sessions:
        window_text = "\n".join(f"{m['role']}: {m['content']}" for m in session)
        if not window_text.strip():
            continue
        chat_windows.append(window_text[:500])

    return compact_material(RawMaterial(seen=seen, events=events, chat_windows=chat_windows))
