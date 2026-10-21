"""Her remark on a new song, decided on the server (Mario 10-03: she lives
on the server, so Spotify reaches her with the desktop off too).

felt-tray used to spot the track change and ask for the line; now
music_taste_loop's own Spotify poll (brain/background.py) does. The line
goes into her log like any event line, and the desktop is asked to say it
(the proactive queue, which only delivers while she's visible there). A
song remark is stale once the song moves on, hence the short TTL.
"""

from __future__ import annotations

import asyncio
import logging

from felt_core import features
from felt_core.config import FeltConfig
from felt_core.db import APP_SESSION

logger = logging.getLogger(__name__)

SAY_WITHIN_S = 90.0
_tasks: set[asyncio.Task] = set()


def _key(snapshot: dict | None) -> str | None:
    if not snapshot or not snapshot.get("is_playing"):
        return None
    track = snapshot.get("track") or {}
    return track.get("id") or track.get("uri") or None


def new_track_details(previous: dict | None, current: dict | None) -> dict | None:
    """Pure: the music_new_track details when one playing track gave way to
    another between two polls; None otherwise - including the first poll
    (the server starting mid-song isn't a change) and a start from silence
    (same rule felt-tray had)."""
    before, now = _key(previous), _key(current)
    if before is None or now is None or before == now:
        return None
    track = (current or {}).get("track") or {}
    artists = [a.get("name", "") for a in track.get("artists") or [] if isinstance(a, dict)]
    return {
        "title": track.get("name", ""),
        "artist": ", ".join(a for a in artists if a),
        "album": (track.get("album") or {}).get("name", ""),
    }


async def remark_on(config: FeltConfig, details: dict) -> None:
    from felt_core.events import react_to_event
    from felt_core.proactive import enqueue_proactive

    try:
        result = await react_to_event("music_new_track", details, config, session_id=APP_SESSION)
    except Exception:
        logger.warning("song remark: no line (%s)", details.get("title"), exc_info=True)
        return
    if result.line:
        enqueue_proactive(
            config, "music", result.line, emotion=result.emotion, ttl_s=SAY_WITHIN_S, record=False
        )


def on_poll(config: FeltConfig, previous: dict | None, current: dict | None) -> None:
    """Called each Spotify poll; starts the remark off the poll loop."""
    details = new_track_details(previous, current)
    if details is None or not features.enabled(config.db_path, "ambient.song_remarks"):
        return
    task = asyncio.get_running_loop().create_task(remark_on(config, details))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
