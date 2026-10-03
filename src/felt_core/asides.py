"""WP-42.2: her asides in the app's Talk page.

Her unprompted remarks (screen reactions, event lines) stay out of the Talk
transcript (Mario: "keep actual chat clean"), but the ones since his last
message can wait at the bottom of Talk for him to answer: at most two
(Mario: "just 1 or 2 is enough"), the rest counted as "+N earlier in the
margins".

Which two is HER pick (Mario: "go with 2"). Dry runs on her real remarks
(10-03, plan doc): asked one remark at a time ("worth bringing up?") she
flipped between runs, because each call gets a freshly sampled persona; asked
once to compare ("which one or two would you most like him to answer?") she
gave the same picks every run and never a music or guest line. So: one
comparative call in the background after each new remark, never in the remark
prompt itself. Two or fewer remarks need no call; a remark arriving mid-pick
reruns it once at the end instead of stacking calls on a tight box.

Asides older than six hours stop waiting (Mario 10-03: yes to "should old
asides expire?"), so a morning doesn't open on last night's remarks. Only the
Talk view expires: the remarks stay in the database for the diary, her
memory and the Margins page.

When he answers one, the link is kept (`answered_remarks`) so the transcript
can show her remark above his reply.
"""

import asyncio
import json
import logging
import sqlite3

from felt_core import local_llm
from felt_core.config import FeltConfig
from felt_core.db import APP_SESSION, ASIDE_KINDS, _connect

logger = logging.getLogger(__name__)

SHOWN = 2
MAX_AGE = "-6 hours"  # SQLite datetime modifier
# a whole day away can leave hundreds; she compares the newest of them
PICK_WINDOW = 30
_LINE_CHARS = 300

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2}
    },
    "required": ["picks"],
    "additionalProperties": False,
}

_ASK = (
    "While Mario was busy you made these remarks to him, unprompted:\n{lines}\n\n"
    "He may not have caught them. When he next opens your notebook, one or two of them "
    "wait there for him to answer; the rest stay in the margins. Which one or two would "
    "you most like him to answer? Give their numbers."
)

_IS_REMARK_SQL = f"kind IN ({', '.join(repr(k) for k in ASIDE_KINDS)})"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS aside_picks ("
        " message_id INTEGER PRIMARY KEY, after_user_id INTEGER NOT NULL,"
        " picked_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS answered_remarks ("
        " user_message_id INTEGER PRIMARY KEY, remark_id INTEGER NOT NULL)"
    )


def _last_user_id(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(id) FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
    ).fetchone()
    return row[0] or 0


def _remarks_since(conn: sqlite3.Connection, session_id: str, after_id: int) -> list[tuple]:
    """(id, content, created_at) of her remarks after message `after_id` and
    younger than MAX_AGE, oldest first."""
    return conn.execute(
        "SELECT id, content, created_at FROM messages WHERE session_id = ? AND id > ?"
        " AND created_at >= datetime('now', ?) AND dismissed_at IS NULL AND "
        + _IS_REMARK_SQL
        + " ORDER BY id",
        (session_id, after_id, MAX_AGE),
    ).fetchall()


def _row(r: tuple) -> dict:
    return {"id": r[0], "content": r[1], "created_at": r[2].replace(" ", "T") + "Z"}


def current_asides(db_path: str, session_id: str = APP_SESSION) -> dict:
    """What Talk shows under the transcript: {"asides": [up to 2 of her remarks
    since his last message, oldest first], "more": how many others}. Her pick
    when she has made one for this batch; otherwise (two or fewer, or a pick
    still running) the newest."""
    conn = _connect(db_path)
    try:
        _ensure(conn)
        after = _last_user_id(conn, session_id)
        remarks = _remarks_since(conn, session_id, after)
        picked = {
            r[0]
            for r in conn.execute(
                "SELECT message_id FROM aside_picks WHERE after_user_id = ?", (after,)
            )
        }
    finally:
        conn.close()
    shown = [r for r in remarks if r[0] in picked] or remarks[-SHOWN:]
    return {"asides": [_row(r) for r in shown], "more": len(remarks) - len(shown)}


def parse_picks(text: str, count: int) -> list[int]:
    """The model's JSON -> up to two valid 1-based positions, deduplicated."""
    try:
        picks = json.loads(text).get("picks", [])
    except (ValueError, TypeError, AttributeError):
        return []
    out: list[int] = []
    for p in picks if isinstance(picks, list) else []:
        if isinstance(p, int) and 1 <= p <= count and p not in out:
            out.append(p)
    return out[:SHOWN]


async def pick(config: FeltConfig, session_id: str = APP_SESSION) -> list[int]:
    """Ask her which of her remarks since his last message she'd most like him
    to answer, and store it. Returns the picked message ids."""
    conn = _connect(config.db_path)
    try:
        _ensure(conn)
        after = _last_user_id(conn, session_id)
        remarks = _remarks_since(conn, session_id, after)[-PICK_WINDOW:]
    finally:
        conn.close()
    if len(remarks) <= SHOWN:
        return []  # nothing to choose: Talk shows them all
    # here, not at the top: felt_core.brain loads screen_reactions, which
    # imports this module to schedule picks
    from felt_core.brain.personality import personality_prompt

    lines = "\n".join(
        f"{i}. ({r[2][11:16]}) {r[1][:_LINE_CHARS]}" for i, r in enumerate(remarks, 1)
    )
    reply = await local_llm.chat(
        [
            {"role": "system", "content": personality_prompt(config=config)},
            {"role": "user", "content": _ASK.format(lines=lines)},
        ],
        max_tokens=40,
        temperature=0.2,
        json_schema=PICK_SCHEMA,
        config=config,
    )
    ids = [remarks[p - 1][0] for p in parse_picks(reply.text, len(remarks))]
    if not ids:
        return []
    conn = _connect(config.db_path)
    try:
        with conn:
            # earlier batches are never read again
            conn.execute("DELETE FROM aside_picks")
            conn.executemany(
                "INSERT OR REPLACE INTO aside_picks (message_id, after_user_id) VALUES (?, ?)",
                [(i, after) for i in ids],
            )
    finally:
        conn.close()
    logger.info("asides: picked %s of %d remarks", ids, len(remarks))
    return ids


_running: dict[str, asyncio.Task] = {}
_again: set[str] = set()


async def _pick_loop(config: FeltConfig, session_id: str) -> None:
    try:
        while True:
            _again.discard(session_id)
            try:
                await pick(config, session_id)
            except local_llm.LocalLLMUnavailable as exc:
                logger.info("asides: felt-llm unavailable (%s)", exc)
            except Exception:
                logger.exception("asides: pick failed")
            if session_id not in _again:
                return
    finally:
        _running.pop(session_id, None)


def schedule_pick(config: FeltConfig, session_id: str) -> None:
    """After a remark is saved: re-pick in the background. Only the app's
    session has asides; a remark landing mid-pick reruns it once afterwards."""
    if session_id != APP_SESSION:
        return
    if session_id in _running:
        _again.add(session_id)
        return
    _running[session_id] = asyncio.create_task(_pick_loop(config, session_id))


def remark_text(db_path: str, remark_id: int, session_id: str = APP_SESSION) -> str | None:
    """The text of one of her remarks in this session, or None if that id
    isn't one (so a client can't pass off any message as her remark)."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT content FROM messages WHERE id = ? AND session_id = ? AND " + _IS_REMARK_SQL,
            (remark_id, session_id),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def link_answer(db_path: str, user_message_id: int, remark_id: int) -> None:
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO answered_remarks (user_message_id, remark_id)"
                " VALUES (?, ?)",
                (user_message_id, remark_id),
            )
    finally:
        conn.close()


def answered(db_path: str, user_message_ids: list[int]) -> dict[int, dict]:
    """user message id -> the remark it answers (id, content, created_at)."""
    if not user_message_ids:
        return {}
    conn = _connect(db_path)
    try:
        _ensure(conn)
        q = ",".join("?" * len(user_message_ids))
        rows = conn.execute(
            "SELECT a.user_message_id, m.id, m.content, m.created_at FROM answered_remarks a"
            f" JOIN messages m ON m.id = a.remark_id WHERE a.user_message_id IN ({q})",
            user_message_ids,
        ).fetchall()
    finally:
        conn.close()
    return {u: _row((i, c, t)) for u, i, c, t in rows}
