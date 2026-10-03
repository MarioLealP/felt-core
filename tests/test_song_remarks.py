"""Mario 10-03: her song remarks come from the server's own Spotify poll,
so they happen with the desktop off; the desktop is only asked to say them."""

import asyncio

from felt_core import db, song_remarks
from felt_core.events import EventReaction


def _snap(track_id, playing=True, name="Song", artists=("A", "B"), album="Album"):
    return {
        "is_playing": playing,
        "track": {
            "id": track_id,
            "name": name,
            "artists": [{"name": a} for a in artists],
            "album": {"name": album},
        },
    }


def test_only_a_change_between_two_playing_tracks_counts():
    assert song_remarks.new_track_details(None, _snap("1")) is None  # server started mid-song
    assert song_remarks.new_track_details(_snap("1"), _snap("1")) is None
    assert song_remarks.new_track_details(_snap("1", playing=False), _snap("2")) is None
    assert song_remarks.new_track_details(_snap("1"), None) is None
    assert song_remarks.new_track_details(_snap("1"), _snap("2", name="Next")) == {
        "title": "Next",
        "artist": "A, B",
        "album": "Album",
    }


def test_the_line_is_logged_once_and_handed_to_the_desktop(tmp_path, monkeypatch):
    class Config:
        db_path = str(tmp_path / "f.db")
        voice_url = None

    async def fake_react(event, details, config, session_id):
        assert (event, session_id) == ("music_new_track", db.APP_SESSION)
        # react_to_event logs the line itself, as an event line
        db.save_message(config.db_path, session_id, "assistant", "Oh, this one.", kind=db.EVENT)
        return EventReaction(line="Oh, this one.", emotion="happy")

    import felt_core.events as events

    monkeypatch.setattr(events, "react_to_event", fake_react)
    asyncio.run(song_remarks.remark_on(Config(), {"title": "Next"}))  # ty: ignore[invalid-argument-type]
    item = db.claim_next_proactive(Config.db_path)
    assert item is not None and item["text"] == "Oh, this one." and item["kind"] == "music"
    conn = db._connect(Config.db_path)
    lines = conn.execute("SELECT kind FROM messages").fetchall()
    conn.close()
    assert lines == [(db.EVENT,)]  # not a second, proactive copy


def test_no_line_means_nothing_for_the_desktop(tmp_path, monkeypatch):
    class Config:
        db_path = str(tmp_path / "f.db")
        voice_url = None

    async def quiet(event, details, config, session_id):
        return EventReaction(skipped="cooldown")

    import felt_core.events as events

    monkeypatch.setattr(events, "react_to_event", quiet)
    asyncio.run(song_remarks.remark_on(Config(), {"title": "Next"}))  # ty: ignore[invalid-argument-type]
    assert db.claim_next_proactive(Config.db_path) is None
