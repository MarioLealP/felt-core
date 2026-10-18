"""WP-42.3: live updates for the open app, as server-sent events on
GET /v1/app/stream.

Notices only, never data: "a new line" (with its kind, so the app knows
whether Talk or the Margins changed), "her aside pick changed", "a new mark",
"the lists changed" (an item added by voice shows up at once), "an incident
changed" (Claude reported, someone marked it handled), "the controls
changed" (a switch, or her state on the desktop - WP-42.5).
The app refetches through the normal endpoints, so there is one source of
truth and a missed notice or a reconnect can't leave the screen wrong.

It checks the database every POLL_S rather than wiring events through every
writer: a handful of tiny SQLite reads a second, for one user, is nothing,
and every writer (remarks, pushes, mail, alarms, marks, picks) is covered
without touching them.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from felt_core.db import _connect

POLL_S = 1.5
KEEPALIVE_S = 15.0


def _fingerprint(conn, has: set[str], table: str, sql: str) -> str:
    return "|".join(str(v) for v in conn.execute(sql).fetchone()) if table in has else ""


def _state(db_path: str, session_id: str) -> tuple[int, str, int, str, str]:
    """(newest message id, aside picks fingerprint, number of marks,
    incidents fingerprint, controls fingerprint)."""
    conn = _connect(db_path)
    try:
        newest = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        has = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        picks = (
            conn.execute(
                "SELECT COALESCE(GROUP_CONCAT(message_id), '') FROM aside_picks"
            ).fetchone()[0]
            if "aside_picks" in has
            else ""
        )
        marks = (
            conn.execute("SELECT COUNT(*) FROM message_marks").fetchone()[0]
            if "message_marks" in has
            else 0
        )
        incidents = _fingerprint(
            conn,
            has,
            "incidents",
            "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(length(state)"
            " + COALESCE(length(report), 0) + COALESCE(length(followup), 0)), 0) FROM incidents",
        )
        # the switches' version + the desktop's state (not its heartbeat time)
        ctl = (
            conn.execute(
                "SELECT GROUP_CONCAT(key || '=' || CASE WHEN key = 'desktop_state'"
                " THEN json_remove(value, '$.at') ELSE value END, ';') FROM settings"
                " WHERE key IN ('controls_version', 'desktop_state')"
            ).fetchone()[0]
            or ""
            if "settings" in has
            else ""
        )
    finally:
        conn.close()
    return newest, picks, marks, incidents, ctl


def _new_lines(db_path: str, session_id: str, after: int) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, role, kind FROM messages WHERE session_id = ? AND id > ? ORDER BY id",
            (session_id, after),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": i, "role": role, "kind": kind} for i, role, kind in rows]


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


async def notices(
    db_path: str,
    session_id: str,
    gone: Callable[[], Awaitable[bool]],
    *,
    lists_version: Callable[[], Awaitable[str]] | None = None,
    poll_s: float = POLL_S,
    keepalive_s: float = KEEPALIVE_S,
) -> AsyncIterator[str]:
    """The event stream. `gone` is awaited each round; truthy ends it (the
    client went away). `lists_version` says when the shopping list changed -
    it lives in Home Assistant (shopping.py); "" = couldn't tell, so no notice."""
    newest, picks, marks, incidents, ctl = await asyncio.to_thread(_state, db_path, session_id)
    lists = await lists_version() if lists_version else ""
    yield _event("hello", {"newest": newest})
    quiet = 0.0
    while not await gone():
        await asyncio.sleep(poll_s)
        now = await asyncio.to_thread(_state, db_path, session_id)
        now_lists = await lists_version() if lists_version else ""
        sent = False
        if now[0] > newest:
            for line in await asyncio.to_thread(_new_lines, db_path, session_id, newest):
                yield _event("line", line)
            sent = True
        if now[1] != picks:
            yield _event("asides", {})
            sent = True
        if now[2] != marks:
            yield _event("marks", {})
            sent = True
        if now_lists and now_lists != lists:
            if lists:
                yield _event("lists", {})
                sent = True
            lists = now_lists
        if now[3] != incidents:
            yield _event("systems", {})
            sent = True
        if now[4] != ctl:
            yield _event("controls", {})
            sent = True
        newest, picks, marks, incidents, ctl = now
        quiet = 0.0 if sent else quiet + poll_s
        if quiet >= keepalive_s:
            yield ": keepalive\n\n"
            quiet = 0.0
