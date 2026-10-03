"""WP-42.3: every line lives in `messages`, labelled by `kind` (chat, remark,
event, …), replacing the old test of matching her text against the remark
tables. A database from before gets the columns and its remarks labelled once."""

import sqlite3

from felt_core.db import CHAT, EVENT, REMARK, _connect, get_history, save_message


def _old_database(path: str) -> None:
    """The schema before WP-42.3, with one of each kind of line."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT"
            " NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL"
            " DEFAULT (datetime('now')))"
        )
        conn.execute(
            "CREATE TABLE screen_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL, description TEXT NOT NULL, spoken_reply TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "CREATE TABLE event_reactions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL, event TEXT NOT NULL, dedupe_key TEXT, line TEXT"
            " NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        rows = [
            ("s", "user", "hi"),
            ("s", "assistant", "Hey."),
            ("s", "assistant", "Spreadsheets again?"),
            ("s", "assistant", "Good song."),
            ("other", "assistant", "Spreadsheets again?"),
        ]
        conn.executemany("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)", rows)
        conn.execute(
            "INSERT INTO screen_observations (session_id, description, spoken_reply)"
            " VALUES ('s', 'excel', 'Spreadsheets again?')"
        )
        conn.execute(
            "INSERT INTO event_reactions (session_id, event, line)"
            " VALUES ('s', 'song', 'Good song.')"
        )
    conn.close()


def test_an_old_database_gets_kinds_once(tmp_path):
    db = str(tmp_path / "felt.db")
    _old_database(db)
    conn = _connect(db)
    kinds = dict(conn.execute("SELECT content || '@' || session_id, kind FROM messages"))
    conn.close()
    assert kinds == {
        "hi@s": CHAT,
        "Hey.@s": CHAT,
        "Spreadsheets again?@s": REMARK,
        "Good song.@s": EVENT,
        # same text, but no remark of that session: stays chat
        "Spreadsheets again?@other": CHAT,
    }
    # a second connection doesn't redo it (nor fail on the existing columns)
    _connect(db).close()
    assert [m["content"] for m in get_history(db, "s", exclude_remarks=True)] == ["hi", "Hey."]


def test_save_message_returns_its_id_and_keeps_kind_and_source(tmp_path):
    db = str(tmp_path / "felt.db")
    first = save_message(db, "s", "user", "hi")
    second = save_message(db, "s", "assistant", "Nice track.", kind=EVENT, source_ref="event:song")
    assert second == first + 1
    conn = _connect(db)
    row = conn.execute("SELECT kind, source_ref FROM messages WHERE id = ?", (second,)).fetchone()
    conn.close()
    assert row == (EVENT, "event:song")
