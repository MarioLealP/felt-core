"""The `music_taste` table: plays, skips and replays."""

from felt_core.db.session import _connect


def record_music_play(
    db_path: str,
    track_id: str,
    track_name: str,
    artists: str,
    played_ms: int,
    duration_ms: int,
    skipped: bool,
) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO music_taste (track_id, track_name, artists, played_ms, duration_ms, skipped) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (track_id, track_name, artists, played_ms, duration_ms, int(skipped)),
        )
    conn.close()


def get_music_taste_stats(db_path: str, limit: int = 200) -> list[dict]:
    """One row per distinct track_id seen in the last `limit` recorded
    plays (most recent first, so a long-since-stopped track's old plays
    don't drown out current taste forever) - plays/skips counted across
    that window, track_name/artists taken from whichever recorded row is
    used to group it (they don't change track to track).
    """
    conn = _connect(db_path)
    rows = conn.execute(
        """
        SELECT track_id, track_name, artists, COUNT(*) as plays, SUM(skipped) as skips
        FROM (SELECT * FROM music_taste ORDER BY id DESC LIMIT ?)
        GROUP BY track_id
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "track_id": track_id,
            "track_name": track_name,
            "artists": artists,
            "plays": plays,
            "skips": skips,
        }
        for track_id, track_name, artists, plays, skips in rows
    ]
