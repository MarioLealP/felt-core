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
    # Proactive email awareness (see email_watcher.py) — one row per new
    # message the watcher has already pinged Mario about. `uid` is the
    # IMAP UID (unique within a mailbox), used both to avoid re-notifying
    # the same message across restarts and, once resolved, to fetch the
    # full body for summarizing. `resolved` distinguishes "already
    # summarized on confirmation" from "still waiting for a yes/no."
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            notified_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # A running shopping list — voice-editable (the shopping_list skill)
    # and checkable from a phone browser (the /shopping-list mini-app in
    # api.py), both against this same table so either one immediately
    # sees what the other did.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shopping_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            checked INTEGER NOT NULL DEFAULT 0,
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


def delete_alarm(db_path: str, alarm_id: int) -> None:
    """Distinct from mark_alarm_fired: a cancelled alarm never fired, so
    marking it 'fired' would be a lie — this removes the row outright."""
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM alarms WHERE id = ?", (alarm_id,))
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
    # ORDER BY id DESC so LIMIT keeps the MOST RECENT messages (once a
    # session passes the limit, ASC would keep returning the oldest ones
    # forever, permanently hiding anything recent — 2026-09-14 review
    # finding). Reverse back to chronological order before returning.
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


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


def add_shopping_item(db_path: str, text: str) -> int:
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute("INSERT INTO shopping_list (text) VALUES (?)", (text,))
        item_id = cursor.lastrowid
    conn.close()
    return item_id


def get_shopping_items(db_path: str) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT id, text, checked FROM shopping_list ORDER BY checked ASC, id ASC"
    ).fetchall()
    conn.close()
    return [{"id": row[0], "text": row[1], "checked": bool(row[2])} for row in rows]


def set_shopping_item_checked(db_path: str, item_id: int, checked: bool) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("UPDATE shopping_list SET checked = ? WHERE id = ?", (int(checked), item_id))
    conn.close()


def delete_shopping_item(db_path: str, item_id: int) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))
    conn.close()


def clear_checked_shopping_items(db_path: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM shopping_list WHERE checked = 1")
    conn.close()


def is_known_email_uid(db_path: str, uid: str) -> bool:
    conn = _connect(db_path)
    row = conn.execute("SELECT 1 FROM pending_emails WHERE uid = ?", (uid,)).fetchone()
    conn.close()
    return row is not None


def save_pending_email(db_path: str, uid: str, sender: str, subject: str) -> int:
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO pending_emails (uid, sender, subject) VALUES (?, ?, ?)",
            (uid, sender, subject),
        )
        email_id = cursor.lastrowid
    conn.close()
    return email_id


def get_unresolved_pending_email(db_path: str) -> dict | None:
    """The most recent still-unanswered "want me to summarize?" ping, if
    any — there's only ever meant to be one live at a time from Mario's
    point of view, so the newest is the one a bare "yes" refers to.

    Bounded to the last 10 minutes: without this, a "yes" said days after
    ignoring a ping would wrongly reopen it and blurt out a stale email
    summary in response to something unrelated (2026-09-14 review
    finding). 10 minutes is a generous window for "still talking about
    the thing that was just pinged," not a precise cutoff Mario ever
    specified."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT id, uid, sender, subject FROM pending_emails "
        "WHERE resolved = 0 AND notified_at >= datetime('now', '-10 minutes') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"id": row[0], "uid": row[1], "sender": row[2], "subject": row[3]}


def get_recent_pending_emails(db_path: str, limit: int = 5) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT sender, subject, resolved FROM pending_emails ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [{"sender": s, "subject": subj, "resolved": bool(r)} for s, subj, r in rows]


def mark_email_resolved(db_path: str, email_id: int) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("UPDATE pending_emails SET resolved = 1 WHERE id = ?", (email_id,))
    conn.close()


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
