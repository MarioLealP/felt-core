"""WP-16 Part 2: light listening-taste memory - "Mario often skips X /
replays Y" - extracted from db.py so the classification/summary logic is
unit-testable without a real SQLite file, same split as date_resolution.py.
"""

_SKIP_THRESHOLD = 0.30
_MIN_PLAYS_TO_MENTION = 2


def is_skip(played_ms: int, duration_ms: int) -> bool:
    """A track counts as skipped if it changed before _SKIP_THRESHOLD of
    its duration played. An unknown/zero duration can't be classified as a
    skip (there's nothing to compare against), so it's counted as a full
    play rather than silently dropped."""
    if duration_ms <= 0:
        return False
    return played_ms < duration_ms * _SKIP_THRESHOLD


def summarize_taste(rows: list[dict]) -> str:
    """Pure: `rows` is one dict per distinct track (as
    db.get_music_taste_stats returns) - {"track_name", "artists", "plays",
    "skips"}. Picks the most-skipped and most-replayed tracks that have
    enough history to say anything about (_MIN_PLAYS_TO_MENTION+), and
    returns a short note to fold into the prompt, or "" if nothing has
    enough history yet.
    """
    eligible = [r for r in rows if r["plays"] >= _MIN_PLAYS_TO_MENTION]
    if not eligible:
        return ""

    most_skipped = max(eligible, key=lambda r: r["skips"] / r["plays"])
    most_replayed = max(eligible, key=lambda r: r["plays"] - r["skips"])

    notes = []
    skip_note_track = None
    if most_skipped["skips"] / most_skipped["plays"] >= 0.5:
        notes.append(f'often skips "{most_skipped["track_name"]}" by {most_skipped["artists"]}')
        skip_note_track = most_skipped["track_name"]

    # Only suppressed when the skip note above was actually emitted for
    # this same track (a contradiction) - "most replayed of one eligible
    # track" trivially equals "most skipped of one eligible track" too,
    # so this must NOT fire just because they happen to be the same row.
    if (
        most_replayed["track_name"] != skip_note_track
        and most_replayed["plays"] - most_replayed["skips"] >= _MIN_PLAYS_TO_MENTION
    ):
        notes.append(
            f'often plays "{most_replayed["track_name"]}" by {most_replayed["artists"]} all the way through'
        )

    if not notes:
        return ""
    return "Mario " + "; and ".join(notes) + "."
