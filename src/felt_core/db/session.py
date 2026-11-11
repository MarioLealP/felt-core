"""The SQLite connection: opens felt.db and makes sure the schema exists first."""

import sqlite3
import threading
from pathlib import Path

from felt_core.db.schema import _create_schema


def _require_lastrowid(cursor: sqlite3.Cursor) -> int:
    """`Cursor.lastrowid` is typed `int | None` by the stdlib stub because
    it's genuinely `None` for a statement that isn't an INSERT - but every
    caller here only ever calls this right after an INSERT, where sqlite3
    always sets it. Narrows the type at the one real use; if this ever
    fired for real it would mean the INSERT itself silently didn't happen,
    which deserves a loud error, not a `None` propagating into a call site
    that thinks it got an `int`.
    """
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT did not produce a lastrowid")
    return cursor.lastrowid


# The schema is created / migrated once per database per process, not on every
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
                _create_schema(conn)
                _schema_ready.add(db_path)
    return conn
