"""Conversation persistence — SQLite, decided from day one
(assistant-core.md), built now.

Plain sqlite3 (stdlib, no new dependency) — a single-user LAN service
doesn't need an async driver, and the ops here are small and fast enough
that the brief blocking calls aren't worth the extra complexity yet.
"""

import sqlite3
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def save_message(db_path: str, session_id: str, role: str, content: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )
    conn.close()


def get_history(db_path: str, session_id: str, limit: int = 20) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in rows]
