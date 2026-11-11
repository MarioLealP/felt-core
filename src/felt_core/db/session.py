"""felt.db sessions: `session()` (async ORM) and the old sync `_connect()`, both migrate the schema first."""

import asyncio
import sqlite3
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

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


def require_id(row: SQLModel) -> int:
    """A just-inserted row's id, narrowed to `int` - None here would mean the INSERT didn't happen."""
    row_id = getattr(row, "id", None)
    if row_id is None:
        raise RuntimeError("INSERT did not produce an id")
    return row_id


# The schema is migrated once per database per process, not on every
# connection: concurrent first connections (the lifespan's background tasks
# plus a request, on a fresh DB) all ran the migration's writes at once and
# one lost with "database is locked" - flaky tests, and a red CI blocks a
# release. The 30 s timeout lets an ordinary write collision wait it out
# instead of failing at once.
_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def _ensure_schema(db_path: str) -> None:
    if db_path not in _schema_ready:
        with _schema_lock:
            if db_path not in _schema_ready:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                migrate(db_path)
                _schema_ready.add(db_path)


def _connect(db_path: str) -> sqlite3.Connection:
    """The old sync connection - goes once every table has moved to `session()`."""
    _ensure_schema(db_path)
    return sqlite3.connect(db_path, timeout=30)


# One engine per database. NullPool: a connection per session, closed after, like `_connect` - and nothing pooled
# that's tied to an event loop that has since closed (each test runs its own loop).
#
# AUTOCOMMIT: every statement is its own transaction, so no lock is held across an `await`. Otherwise a write keeps
# SQLite's write lock until its COMMIT, which SQLAlchemy sends in a later step - and the loop runs other tasks in
# between. While the old sync `_connect` code still runs ON the loop, a sync write there waited for that lock and
# blocked the very loop that would release it (a 30 s stall, then "database is locked"; tests/test_db_session.py).
# Each query here is one statement; one that needs several atomically has to say so.
_engines: dict[str, AsyncEngine] = {}


def _engine(db_path: str) -> AsyncEngine:
    engine = _engines.get(db_path)
    if engine is None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            poolclass=NullPool,
            connect_args={"timeout": 30},
            isolation_level="AUTOCOMMIT",
        )
        _engines[db_path] = engine
    return engine


@asynccontextmanager
async def session(db_path: str) -> AsyncGenerator[AsyncSession]:
    """An ORM session on felt.db, schema up to date; `await s.commit()` to keep writes."""
    if db_path not in _schema_ready:
        await asyncio.to_thread(_ensure_schema, db_path)
    async with AsyncSession(_engine(db_path), expire_on_commit=False) as s:
        yield s
