"""felt.db's Alembic migrations: a new database and a pre-Alembic one end up with the same schema."""

import logging
import sqlite3

from felt_core.db.migrate import BASELINE, _legacy_catch_up, migrate, schema_differences


def _schema(db_path: str) -> dict[str, tuple]:
    """Every table's columns, indexes and SQLite's own flags - what the hand-written DDL produced."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version'"
            )
        ]
        return {
            table: (
                sorted(conn.execute(f"PRAGMA table_info({table})").fetchall()),
                sorted(row[1:] for row in conn.execute(f"PRAGMA index_list({table})")),
                "AUTOINCREMENT" in conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0],
            )
            for table in tables
        }
    finally:
        conn.close()


def _version(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()


def test_new_database_matches_the_models(tmp_path):
    db = str(tmp_path / "felt.db")
    migrate(db)
    assert schema_differences(db) == []
    assert _version(db) == BASELINE


def test_old_database_is_stamped_with_the_same_schema_as_a_new_one(tmp_path, caplog):
    old, new = str(tmp_path / "old.db"), str(tmp_path / "new.db")
    _legacy_catch_up(old)
    conn = sqlite3.connect(old)
    conn.execute("INSERT INTO facts (content) VALUES ('likes tea')")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.INFO, logger="felt_core.db.migrate"):
        migrate(old)
    migrate(new)

    assert "stamped at" in caplog.text
    assert "differs" not in caplog.text
    assert _version(old) == BASELINE
    assert _schema(old) == _schema(new)
    conn = sqlite3.connect(old)
    assert conn.execute("SELECT content FROM facts").fetchall() == [("likes tea",)]
    conn.close()


def test_migrating_twice_changes_nothing(tmp_path):
    db = str(tmp_path / "felt.db")
    migrate(db)
    before = _schema(db)
    migrate(db)
    assert _schema(db) == before
    assert _version(db) == BASELINE


def test_an_old_database_that_drifted_is_logged(tmp_path, caplog):
    db = str(tmp_path / "felt.db")
    _legacy_catch_up(db)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE stray (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.ERROR, logger="felt_core.db.migrate"):
        migrate(db)

    assert "differs from the baseline models" in caplog.text
    assert _version(db) == BASELINE
