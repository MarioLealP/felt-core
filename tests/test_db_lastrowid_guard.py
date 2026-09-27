"""WP-40: `Cursor.lastrowid` is typed `int | None` by the stdlib stub;
`_require_lastrowid` narrows it for the four INSERT call sites that
declare `-> int`. The `None` branch can't happen with today's callers
(always right after an INSERT), so this is the only place it's exercised.
Uses a real `sqlite3.Cursor` (an in-memory connection) rather than a
duck-typed fake, so the test itself stays type-correct against
`_require_lastrowid`'s real `sqlite3.Cursor` parameter."""

import sqlite3

import pytest

from felt_core.db import _require_lastrowid


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    try:
        yield connection
    finally:
        connection.close()


def test_returns_the_real_lastrowid(conn):
    cursor = conn.execute("INSERT INTO t DEFAULT VALUES")
    assert _require_lastrowid(cursor) == 1


def test_raises_when_lastrowid_is_none(conn):
    # `lastrowid` is only ever `None` before any statement has executed on
    # that cursor at all (confirmed live: even a SELECT's cursor reports 0,
    # not None, once it has run) - the real, if narrow, shape of the "no
    # INSERT happened yet" case this guard exists for.
    cursor = conn.cursor()

    with pytest.raises(RuntimeError, match="lastrowid"):
        _require_lastrowid(cursor)
