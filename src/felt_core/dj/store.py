"""DJ mode's memory: every pick she queued and how it went, and Mario's
explicit "more like this" / "not this". Its own tables (db.py is big enough);
created on first use."""

import sqlite3

from felt_core.db import _connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dj_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uri TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    discovery INTEGER NOT NULL,
    picked_at TEXT NOT NULL DEFAULT (datetime('now')),
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS dj_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    liked INTEGER NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _conn(db_path: str) -> sqlite3.Connection:
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def add_pick(db_path: str, uri: str, title: str, artist: str, discovery: bool) -> None:
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO dj_picks (uri, title, artist, discovery) VALUES (?, ?, ?, ?)",
            (uri, title, artist, int(discovery)),
        )
    conn.close()


def set_outcome(db_path: str, uri: str, outcome: str) -> None:
    """`outcome`: 'played' (to the end, or near it) or 'skipped'. The latest
    open pick with this uri gets it."""
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "UPDATE dj_picks SET outcome = ? WHERE id = (SELECT id FROM dj_picks"
            " WHERE uri = ? AND outcome IS NULL ORDER BY id DESC LIMIT 1)",
            (outcome, uri),
        )
    conn.close()


def recent_pick_uris(db_path: str, days: int) -> set[str]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT uri FROM dj_picks WHERE picked_at >= datetime('now', ?)", (f"-{days} days",)
    ).fetchall()
    conn.close()
    return {r["uri"] for r in rows}


def discovery_outcomes(db_path: str, last: int) -> list[bool]:
    """True = kept (played), False = skipped, newest first - her discoveries only."""
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT outcome FROM dj_picks WHERE discovery = 1 AND outcome IS NOT NULL"
        " ORDER BY id DESC LIMIT ?",
        (last,),
    ).fetchall()
    conn.close()
    return [r["outcome"] == "played" for r in rows]


def pick_outcomes(db_path: str, last: int) -> list[dict]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT title, artist, discovery, outcome FROM dj_picks WHERE outcome IS NOT NULL"
        " ORDER BY id DESC LIMIT ?",
        (last,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_feedback(db_path: str, title: str, artist: str, liked: bool) -> None:
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO dj_feedback (title, artist, liked) VALUES (?, ?, ?)",
            (title, artist, int(liked)),
        )
    conn.close()


def feedback(db_path: str, last: int) -> list[dict]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT title, artist, liked FROM dj_feedback ORDER BY id DESC LIMIT ?", (last,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def history_artists(db_path: str) -> list[dict]:
    """Per artist from the listening history (music_taste): plays, skips. A
    track by several artists ("A, B") counts for each."""
    conn = _conn(db_path)
    rows = conn.execute("SELECT artists, skipped FROM music_taste").fetchall()
    conn.close()
    stats: dict[str, dict] = {}
    for r in rows:
        for name in (a.strip() for a in (r["artists"] or "").split(",")):
            if name:
                s = stats.setdefault(name, {"artist": name, "plays": 0, "skips": 0})
                s["plays"] += 1
                s["skips"] += r["skipped"]
    return sorted(stats.values(), key=lambda a: a["plays"] - a["skips"], reverse=True)


def history_tracks(db_path: str) -> dict[str, dict]:
    """track_id -> {title, artist, plays, skips, last} from the listening history."""
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT track_id, track_name AS title, artists AS artist, COUNT(*) AS plays,"
        " SUM(skipped) AS skips, MAX(recorded_at) AS last FROM music_taste GROUP BY track_id"
    ).fetchall()
    conn.close()
    return {r["track_id"]: dict(r) for r in rows}
