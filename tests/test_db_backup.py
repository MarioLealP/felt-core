"""The hourly felt.db copy for restic: complete, consistent, and replaced whole."""

import sqlite3

from felt_core.db.backup import BACKUP_NAME, write_backup
from felt_core.db.session import _connect


def _facts(db_path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT content FROM facts ORDER BY id").fetchall()
    finally:
        conn.close()


def _add_fact(db_path: str, content: str) -> None:
    conn = _connect(db_path)
    conn.execute("INSERT INTO facts (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()


def test_the_copy_holds_everything_and_sits_beside_felt_db(tmp_path):
    db = str(tmp_path / "felt.db")
    _add_fact(db, "likes tea")

    copy = write_backup(db)

    assert copy == tmp_path / BACKUP_NAME
    assert _facts(copy) == [("likes tea",)]
    assert sorted(p.name for p in tmp_path.iterdir()) == [BACKUP_NAME, "felt.db"]


def test_a_new_copy_replaces_the_old_one(tmp_path):
    db = str(tmp_path / "felt.db")
    _add_fact(db, "likes tea")
    write_backup(db)
    _add_fact(db, "hates mornings")

    copy = write_backup(db)

    assert _facts(copy) == [("likes tea",), ("hates mornings",)]


def test_an_unfinished_write_is_not_in_the_copy(tmp_path):
    db = str(tmp_path / "felt.db")
    _add_fact(db, "likes tea")
    writer = _connect(db)
    writer.execute("INSERT INTO facts (content) VALUES ('half written')")  # open transaction, not committed
    try:
        copy = write_backup(db)
    finally:
        writer.rollback()
        writer.close()

    assert _facts(copy) == [("likes tea",)]
