"""brain._finished_play_from_transition (WP-16 Part 2): the pure
track-change classification music_taste_loop's polling wraps around."""

import felt_core.brain.background as brain

_SONG_A = {"id": "a1", "name": "Song A", "artists": [{"name": "Artist A"}]}
_SONG_B = {"id": "b2", "name": "Song B", "artists": [{"name": "Artist B"}]}


def _snapshot(track, progress_ms, duration_ms=200000, is_playing=True):
    return {
        "track": track,
        "progress_ms": progress_ms,
        "duration_ms": duration_ms,
        "is_playing": is_playing,
    }


def test_no_previous_snapshot_means_nothing_finished():
    assert brain._finished_play_from_transition(None, _snapshot(_SONG_A, 1000)) is None


def test_same_track_still_playing_means_nothing_finished():
    previous = _snapshot(_SONG_A, 30000)
    current = _snapshot(_SONG_A, 45000)
    assert brain._finished_play_from_transition(previous, current) is None


def test_track_change_finishes_the_previous_one():
    previous = _snapshot(_SONG_A, 190000)  # played nearly all of it
    current = _snapshot(_SONG_B, 1000)

    finished = brain._finished_play_from_transition(previous, current)

    assert finished is not None
    assert finished["track_id"] == "a1"
    assert finished["track_name"] == "Song A"
    assert finished["artists"] == "Artist A"
    assert finished["played_ms"] == 190000
    assert finished["skipped"] is False


def test_track_change_before_threshold_is_a_skip():
    previous = _snapshot(_SONG_A, 10000)  # 5% of 200s
    current = _snapshot(_SONG_B, 1000)

    finished = brain._finished_play_from_transition(previous, current)

    assert finished is not None
    assert finished["skipped"] is True


def test_playback_stopping_also_finishes_the_previous_track():
    previous = _snapshot(_SONG_A, 190000)

    finished = brain._finished_play_from_transition(previous, None)

    assert finished is not None
    assert finished["track_id"] == "a1"
    assert finished["skipped"] is False


# -- null ids (audit 2026-09-24: the loop wedged on a local file) --------

_LOCAL_FILE = {
    "id": None,
    "uri": "spotify:local:Artist+L:Album:Local+Song:200",
    "name": "Local Song",
    "artists": [{"name": "Artist L"}],
}
_NO_ID_NO_URI = {"id": None, "name": "Mystery", "artists": []}


def test_null_id_item_is_keyed_by_uri():
    finished = brain._finished_play_from_transition(
        _snapshot(_LOCAL_FILE, 190000), _snapshot(_SONG_B, 1000)
    )

    assert finished is not None
    assert finished["track_id"] == _LOCAL_FILE["uri"]


def test_item_with_neither_id_nor_uri_is_not_recorded():
    assert (
        brain._finished_play_from_transition(
            _snapshot(_NO_ID_NO_URI, 190000), _snapshot(_SONG_B, 1000)
        )
        is None
    )


def test_same_null_id_local_file_still_playing_means_nothing_finished():
    assert (
        brain._finished_play_from_transition(
            _snapshot(_LOCAL_FILE, 1000), _snapshot(_LOCAL_FILE, 16000)
        )
        is None
    )


class _StopLoop(Exception):
    pass


async def test_loop_advances_past_an_item_whose_record_fails(tmp_path, monkeypatch):
    """A failing insert must not wedge the loop: the failed item is tried
    once, then the loop moves on (previously `previous` never advanced, so
    it retried - and failed - every 15 s forever)."""
    from felt_core.config import FeltConfig

    config = FeltConfig(db_path=str(tmp_path / "t.db"), spotify_refresh_token="x")
    polls = iter([_snapshot(_SONG_A, 190000), _snapshot(_SONG_B, 1000), None, None, None])

    async def fake_now_playing(client, cfg):
        return next(polls)

    recorded = []

    def failing_record(db_path, **finished):
        recorded.append(finished["track_id"])
        raise RuntimeError("NOT NULL constraint failed")

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 5:
            raise _StopLoop

    monkeypatch.setattr(brain, "get_currently_playing", fake_now_playing)
    monkeypatch.setattr(brain, "record_music_play", failing_record)
    monkeypatch.setattr(brain.asyncio, "sleep", fake_sleep)

    try:
        await brain.music_taste_loop(config)
    except _StopLoop:
        pass

    # A finished once (A->B), B finished once (B->None); never a retry of A.
    assert recorded == ["a1", "b2"]
