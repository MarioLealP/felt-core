"""The async session holds no SQLite lock across an await (the old sync code still runs on the event loop)."""

import sqlite3

from sqlmodel import insert

from felt_core.db.session import session
from felt_core.db.tables import Setting


async def test_a_sync_write_between_an_async_write_and_its_commit_does_not_wait(tmp_path):
    db = str(tmp_path / "felt.db")
    async with session(db) as s:
        await s.exec(insert(Setting).values(key="async", value="1"))
        # SQLAlchemy commits in a later step, and the loop runs other tasks in between - like the old sync code,
        # which writes ON the loop. Had the insert left the write lock held until then, this would wait for a commit
        # the blocked loop can't run ("database is locked" after the timeout; a 30 s stall in production).
        conn = sqlite3.connect(db, timeout=0.2)
        try:
            with conn:
                conn.execute("INSERT INTO settings (key, value) VALUES ('sync', '1')")
        finally:
            conn.close()
        await s.commit()
