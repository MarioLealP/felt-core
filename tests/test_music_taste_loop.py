"""brain._finished_play_from_transition (WP-16 Part 2): the pure
track-change classification music_taste_loop's polling wraps around."""

import felt_core.brain as brain

_SONG_A = {"id": "a1", "name": "Song A", "artists": [{"name": "Artist A"}]}
_SONG_B = {"id": "b2", "name": "Song B", "artists": [{"name": "Artist B"}]}


def _snapshot(track, progress_ms, duration_ms=200000, is_playing=True):
    return {"track": track, "progress_ms": progress_ms, "duration_ms": duration_ms, "is_playing": is_playing}


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

    assert finished["track_id"] == "a1"
    assert finished["track_name"] == "Song A"
    assert finished["artists"] == "Artist A"
    assert finished["played_ms"] == 190000
    assert finished["skipped"] is False


def test_track_change_before_threshold_is_a_skip():
    previous = _snapshot(_SONG_A, 10000)  # 5% of 200s
    current = _snapshot(_SONG_B, 1000)

    finished = brain._finished_play_from_transition(previous, current)

    assert finished["skipped"] is True


def test_playback_stopping_also_finishes_the_previous_track():
    previous = _snapshot(_SONG_A, 190000)

    finished = brain._finished_play_from_transition(previous, None)

    assert finished["track_id"] == "a1"
    assert finished["skipped"] is False
