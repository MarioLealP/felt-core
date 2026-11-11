"""The `facts` table."""

from felt_core.db.session import _connect


def save_fact(db_path: str, content: str) -> None:
    """Save one fact about Mario."""
    conn = _connect(db_path)
    with conn:
        conn.execute("INSERT INTO facts (content) VALUES (?)", (content,))
    conn.close()


def get_all_facts(db_path: str) -> list[str]:
    """Every saved fact, oldest first."""
    conn = _connect(db_path)
    rows = conn.execute("SELECT content FROM facts ORDER BY id ASC").fetchall()
    conn.close()
    return [row[0] for row in rows]
