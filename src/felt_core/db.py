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
    # Incoming ntfy messages — a log Felt can be asked about
    # conversationally ("anything wrong recently?"), separate from both
    # the conversation log and the facts table.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ntfy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            message TEXT NOT NULL,
            priority INTEGER,
            received_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Durable facts — distinct from conversation history above. These
    # persist across every session, not just one conversation thread, and
    # are only written via an explicit "remember this" mechanism (the
    # remember_fact skill), never inferred silently — keeps this
    # auditable and correctable rather than a black box.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Small key-value store for user preferences (e.g. temperature unit)
    # that need to persist and be changeable from any client (the
    # dashboard, the tray app) — distinct from `facts`, which is
    # LLM-context memory, not app configuration.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    # Time-of-day alarms (distinct from set_timer, which is duration-only
    # and deliberately not persisted — assistant-skills.md, Mario
    # 2026-09-12: "another one skill i remembered was set an alarm /
    # timer"). Alarms ARE persisted: one set the night before has to
    # survive a felt-core restart between then and when it fires, unlike
    # a short timer.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fire_at TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            fired INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def save_alarm(db_path: str, fire_at_iso: str, label: str) -> int:
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO alarms (fire_at, label) VALUES (?, ?)", (fire_at_iso, label)
        )
        alarm_id = cursor.lastrowid
    conn.close()
    return alarm_id


def get_pending_alarms(db_path: str) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute("SELECT id, fire_at, label FROM alarms WHERE fired = 0 ORDER BY fire_at ASC").fetchall()
    conn.close()
    return [{"id": row[0], "fire_at": row[1], "label": row[2]} for row in rows]


def mark_alarm_fired(db_path: str, alarm_id: int) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("UPDATE alarms SET fired = 1 WHERE id = ?", (alarm_id,))
    conn.close()


def get_setting(db_path: str, key: str, default: str | None = None) -> str | None:
    conn = _connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(db_path: str, key: str, value: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.close()


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


def save_fact(db_path: str, content: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("INSERT INTO facts (content) VALUES (?)", (content,))
    conn.close()


def get_all_facts(db_path: str) -> list[str]:
    conn = _connect(db_path)
    rows = conn.execute("SELECT content FROM facts ORDER BY id ASC").fetchall()
    conn.close()
    return [row[0] for row in rows]


def save_ntfy_event(db_path: str, title: str | None, message: str, priority: int | None) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO ntfy_events (title, message, priority) VALUES (?, ?, ?)",
            (title, message, priority),
        )
    conn.close()


def get_recent_ntfy_events(db_path: str, limit: int = 10) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT title, message, priority, received_at FROM ntfy_events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {"title": title, "message": message, "priority": priority, "received_at": received_at}
        for title, message, priority, received_at in rows
    ]
