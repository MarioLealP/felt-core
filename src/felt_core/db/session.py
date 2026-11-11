"""The SQLite connection: opens felt.db and brings its schema up to date first (db/migrate.py)."""

import sqlite3
import threading
from pathlib import Path

from felt_core.db.migrate import migrate


def _require_lastrowid(cursor: sqlite3.Cursor) -> int:
    """Narrows `Cursor.lastrowid` to `int` at the one real use.

    It's typed `int | None` by the stdlib stub because it's genuinely `None`
    for a statement that isn't an INSERT - but every caller here only ever
    calls this right after an INSERT, where sqlite3 always sets it. If this
    ever fired for real it would mean the INSERT itself silently didn't
    happen, which deserves a loud error, not a `None` propagating into a
    call site that thinks it got an `int`.
    """
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT did not produce a lastrowid")
    return cursor.lastrowid


# The schema is migrated once per database per process, not on every
# connection: concurrent first connections (the lifespan's background tasks
# plus a request, on a fresh DB) all ran the migration's writes at once and
# one lost with "database is locked" - flaky tests, and a red CI blocks a
# release. The 30 s timeout lets an ordinary write collision wait it out
# instead of failing at once.
_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    if db_path not in _schema_ready:
        with _schema_lock:
            if db_path not in _schema_ready:
                migrate(db_path)
                _schema_ready.add(db_path)
    return conn
