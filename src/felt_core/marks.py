"""WP-42.2: her marks — now and then she circles, underlines or strikes
something in their shared notebook, with a few words of handwriting.

She can mark whatever she finds interesting (Mario 10-02), his line or her own
reply, from one word up to a sentence. Candidates are only the exchange that
just happened: older lines have scrolled out of view, so a mark there would
land where nobody sees it ink. One mark per exchange at most.

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

TYPES = ("circle", "underline", "box", "bracket", "strike-through")
# a circle or box around half a sentence wraps into one loop per line, which
# reads as a mess, so the enclosing shapes stay for short phrases
ENCLOSING = ("circle", "box")
MAX_ENCLOSED_WORDS = 5
MAX_WORDS = 20

SCHEMA = {
    "type": "object",
    "properties": {
        "mark": {"type": "boolean"},
        "line": {"type": "string", "enum": ["his", "mine"]},
        "type": {"type": "string", "enum": list(TYPES)},
        "quote": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["mark", "line", "type", "quote", "note"],
    "additionalProperties": False,
}

_INSTRUCTION = (
    "You keep a shared notebook with Mario. Below are his line and your answer to it. "
    "Now, pen in hand, decide whether anything in either deserves a mark: something "
    "to tease him about, something you noticed, something that matters to you, or a "
    "bit of your own answer you'd underline or cross out. Marks are meant to be rare, "
    "a treat, not a habit; most exchanges get none, and marking often would cheapen "
    "them. Answer mark=false unless it's genuinely worth it. If you mark: say which "
    "line (his or mine), quote a word, a phrase or up to one sentence copied EXACTLY "
    "from it, and pick circle or box (5 words at most), underline, bracket or "
    "strike-through; then a note of at most 6 words, lowercase, in your voice, or an "
    "empty note if the mark says it all. No emoji."
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
    """How many of his lines since her last mark (None: she never marked);
    0 means this exchange already carries one."""
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
        return "You already marked this exchange."
    if since == 1:
        return "You marked something in the exchange just before this one."
    return f"Your last mark was {since} exchanges ago."


def latest_exchange(db_path: str, session_id: str) -> dict[str, tuple[int, str]]:
    """His latest line and her reply to it, as {"his": (id, text), "mine": ...};
    "mine" is missing when she hasn't answered (yet)."""
    conn = _connect(db_path)
    try:
        his = conn.execute(
            "SELECT id, content FROM messages WHERE session_id = ? AND role = 'user'"
            " ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if his is None:
            return {}
        mine = conn.execute(
            "SELECT id, content FROM messages WHERE session_id = ? AND role = 'assistant'"
            " AND id > ? ORDER BY id LIMIT 1",
            (session_id, his[0]),
        ).fetchone()
    finally:
        conn.close()
    out = {"his": (his[0], his[1])}
    if mine is not None:
        out["mine"] = (mine[0], mine[1])
    return out


def parse_decision(text: str, lines: dict[str, str]) -> dict | None:
    """The model's JSON -> a mark to keep (with "line": his/mine), or None (no
    mark, bad JSON, an unknown line or type, a quote that isn't really in that
    line, or one too long for its shape)."""
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("mark") is not True:
        return None
    quote = str(d.get("quote", "")).strip()
    kind = d.get("type")
    which = d.get("line")
    words = len(quote.split())
    if not quote or kind not in TYPES or which not in lines or quote not in lines[which]:
        return None
    if words > MAX_WORDS or (kind in ENCLOSING and words > MAX_ENCLOSED_WORDS):
        return None
    note = " ".join(str(d.get("note", "")).split()[:8])
    return {"line": which, "type": kind, "quote": quote, "note": note}


async def maybe_mark(config: FeltConfig, session_id: str) -> dict | None:
    """Called after a reply; may add one mark to his line or her reply."""
    started = time.monotonic()
    exchange = latest_exchange(config.db_path, session_id)
    if not exchange:
        return None
    conn = _connect(config.db_path)
    try:
        _ensure(conn)
        since = _since_last(conn, session_id)
    finally:
        conn.close()
    if since == 0:
        return None  # this exchange already carries a mark
    shown = f"His line: {exchange['his'][1]}"
    if "mine" in exchange:
        shown += f"\nYour answer: {exchange['mine'][1]}"
    try:
        reply = await local_llm.chat(
            [
                {"role": "system", "content": personality_prompt(config=config)},
                {
                    "role": "user",
                    "content": f"{_INSTRUCTION}\n{pacing_line(since)}\n\n{shown}",
                },
            ],
            max_tokens=200,
            temperature=0.7,
            json_schema=SCHEMA,
            config=config,
        )
    except local_llm.LocalLLMUnavailable as exc:
        logger.info("marks: felt-llm unavailable (%s)", exc)
        return None
    mark = parse_decision(reply.text, {k: text for k, (_, text) in exchange.items()})
    if mark is None:
        return None
    message_id = exchange[mark.pop("line")][0]
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
