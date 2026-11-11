"""The `settings` table: small key/value state."""

from felt_core.db.session import _connect


def get_setting(db_path: str, key: str, default: str | None = None) -> str | None:
    """Return the stored value for a key, or the default when it is not set."""
    conn = _connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(db_path: str, key: str, value: str) -> None:
    """Store a setting value, replacing any previous one."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.close()
