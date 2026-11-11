"""A consistent copy of felt.db beside it, refreshed every hour, for the server's restic backup.

restic copies the volume's files raw while felt-core may be mid-write, so the live felt.db in a snapshot can be torn;
`felt.backup.db` never is: SQLite's online backup copies every page under one read lock, into a temp file that is
then renamed over the old copy. Restore: stop felt-core, put `felt.backup.db` in place as `felt.db`, start it.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_NAME = "felt.backup.db"
EVERY_S = 3600


def write_backup(db_path: str) -> Path:
    """Copy felt.db to `felt.backup.db` in the same folder, consistently, and return the copy's path."""
    target = Path(db_path).with_name(BACKUP_NAME)
    partial = target.with_name(f"{BACKUP_NAME}.partial")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(db_path, timeout=30)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    partial.replace(target)
    return target


async def backup_loop(db_path: str) -> None:
    """Refresh the copy now and every hour; a failed copy is logged and tried again next hour."""
    while True:
        try:
            await asyncio.to_thread(write_backup, db_path)
        except Exception:
            logger.exception("felt.db backup copy failed")
        await asyncio.sleep(EVERY_S)
