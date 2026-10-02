"""WP-42.2: her marks — now and then she circles or underlines something Mario
wrote in their shared notebook, with a few words of handwriting.

She decides (felt-llm, json schema), after her reply is already out, so it
never slows a turn. No timer or quota (Mario 10-02: "Dont do timer, let her
decide while she knows its not meant to be often"): she's told marks are rare
and how many of his lines ago her last one was, and paces herself. The quote
must be copied exactly from his message, or it's thrown away.
The app draws marks with Rough Notation and plays each only once (WP-42.1).
"""

import json
import logging
import sqlite3
import time
import uuid

from felt_core import local_llm
from felt_core.brain.personality import personality_prompt
from felt_core.config import FeltConfig
from felt_core.db import _connect

logger = logging.getLogger(__name__)

TYPES = ("circle", "underline", "box", "bracket")

SCHEMA = {
    "type": "object",
    "properties": {
        "mark": {"type": "boolean"},
        "type": {"type": "string", "enum": list(TYPES)},
        "quote": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["mark", "type", "quote", "note"],
    "additionalProperties": False,
}

_INSTRUCTION = (
    "You keep a shared notebook with Mario. He just wrote the line below and you "
    "already answered. Now, pen in hand, decide whether ONE word or short phrase of "
    "HIS line deserves a mark in the margin: something to tease him about, something "
    "you noticed, something that matters to you. Marks are meant to be rare, a treat, "
    "not a habit; most lines get none, and marking often would cheapen them. Answer "
    "mark=false unless it's genuinely worth it. If you mark: quote 1-4 words copied EXACTLY from "
    "his line, pick circle, underline, box or bracket, and write a note of at most 6 "
    "words, lowercase, in your voice. No emoji."
)


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS message_marks ("
        " id TEXT PRIMARY KEY, message_id INTEGER NOT NULL, type TEXT NOT NULL,"
        " quote TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS message_marks_msg ON message_marks (message_id)")


def marks_for(db_path: str, message_ids: list[int]) -> dict[int, list[dict]]:
    """message id -> its marks, for the app's history page."""
    if not message_ids:
        return {}
    conn = _connect(db_path)
    try:
        _ensure(conn)
        q = ",".join("?" * len(message_ids))
        rows = conn.execute(
            "SELECT id, message_id, type, quote, note FROM message_marks"
            f" WHERE message_id IN ({q})",
            message_ids,
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, list[dict]] = {}
    for mid, msg, kind, quote, note in rows:
        out.setdefault(msg, []).append({"id": mid, "type": kind, "quote": quote, "note": note})
    return out


def _since_last(conn: sqlite3.Connection, session_id: str) -> int | None:
    """How many of his lines since her last mark (None: she never marked)."""
    last = conn.execute(
        "SELECT mm.message_id FROM message_marks mm"
        " JOIN messages m ON m.id = mm.message_id WHERE m.session_id = ?"
        " ORDER BY mm.created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if last is None:
        return None
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user' AND id > ?",
        (session_id, last[0]),
    ).fetchone()[0]


def pacing_line(since: int | None) -> str:
    if since is None:
        return "You have never marked one of his lines yet."
    if since == 0:
        return "You already marked his very last line."
    return f"Your last mark was {since} of his lines ago."


def latest_user_message(db_path: str, session_id: str) -> tuple[int, str] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, content FROM messages WHERE session_id = ? AND role = 'user'"
            " ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


def parse_decision(text: str, line: str) -> dict | None:
    """The model's JSON -> a mark to keep, or None (no mark, bad JSON, a quote
    that isn't really in his line, or an unknown type)."""
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("mark") is not True:
        return None
    quote = str(d.get("quote", "")).strip()
    kind = d.get("type")
    if not quote or kind not in TYPES or quote not in line or len(quote.split()) > 4:
        return None
    note = " ".join(str(d.get("note", "")).split()[:8])
    return {"type": kind, "quote": quote, "note": note}


async def maybe_mark(config: FeltConfig, session_id: str) -> dict | None:
    """Called after a reply; may add one mark to his latest message."""
    started = time.monotonic()
    latest = latest_user_message(config.db_path, session_id)
    if latest is None:
        return None
    message_id, line = latest
    conn = _connect(config.db_path)
    try:
        _ensure(conn)
        since = _since_last(conn, session_id)
    finally:
        conn.close()
    if since == 0:
        return None  # this line already carries a mark
    try:
        reply = await local_llm.chat(
            [
                {"role": "system", "content": personality_prompt(config=config)},
                {
                    "role": "user",
                    "content": f"{_INSTRUCTION}\n{pacing_line(since)}\n\nHis line: {line}",
                },
            ],
            max_tokens=120,
            temperature=0.7,
            json_schema=SCHEMA,
            config=config,
        )
    except local_llm.LocalLLMUnavailable as exc:
        logger.info("marks: felt-llm unavailable (%s)", exc)
        return None
    mark = parse_decision(reply.text, line)
    if mark is None:
        return None
    mark["id"] = f"mk-{uuid.uuid4().hex[:12]}"
    conn = _connect(config.db_path)
    try:
        _ensure(conn)
        with conn:
            conn.execute(
                "INSERT INTO message_marks (id, message_id, type, quote, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (mark["id"], message_id, mark["type"], mark["quote"], mark["note"]),
            )
    finally:
        conn.close()
    logger.info(
        "marks: %s on message %d (%.1fs)", mark["type"], message_id, time.monotonic() - started
    )
    return mark
