"""The `music_taste` table: plays, skips and replays."""

from sqlmodel import col, select

from felt_core.db.session import session
from felt_core.db.tables import MusicTaste


async def record_music_play(
    db_path: str,
    track_id: str,
    track_name: str,
    artists: str,
    played_ms: int,
    duration_ms: int,
    skipped: bool,
) -> None:
    """Store one play or skip row for a track in the music_taste table."""
    row = MusicTaste(
        track_id=track_id,
        track_name=track_name,
        artists=artists,
        played_ms=played_ms,
        duration_ms=duration_ms,
        skipped=int(skipped),
    )
    async with session(db_path) as s:
        s.add(row)
        await s.commit()


async def get_music_taste_stats(db_path: str, limit: int = 200) -> list[dict]:
    """One row per distinct track_id seen in the last `limit` recorded plays.

    Most recent first, so a long-since-stopped track's old plays don't
    drown out current taste forever - plays/skips counted across that
    window, track_name/artists taken from whichever recorded row is used
    to group it (they don't change track to track).
    """
    recent = (
        select(MusicTaste.track_id, MusicTaste.track_name, MusicTaste.artists, MusicTaste.skipped)
        .order_by(col(MusicTaste.id).desc())
        .limit(limit)
    )
    async with session(db_path) as s:
        rows = list(await s.exec(recent))
    # grouped here, not in SQL: a 5-column aggregate is past sqlmodel.select's typed overloads, and it's <= limit rows
    stats: dict[str, dict] = {}
    for track_id, name, artists, skipped in rows:
        row = stats.setdefault(
            track_id, {"track_id": track_id, "track_name": name, "artists": artists, "plays": 0, "skips": 0}
        )
        row["plays"] += 1
        row["skips"] += skipped
    return [stats[track_id] for track_id in sorted(stats)]  # by track_id, as SQLite's GROUP BY gave them
