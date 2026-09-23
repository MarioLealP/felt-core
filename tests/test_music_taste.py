from felt_core.music_taste import is_skip, summarize_taste


def test_is_skip_true_when_stopped_before_threshold():
    assert is_skip(played_ms=20000, duration_ms=200000) is True


def test_is_skip_false_when_played_past_threshold():
    assert is_skip(played_ms=100000, duration_ms=200000) is False


def test_is_skip_false_on_unknown_duration():
    assert is_skip(played_ms=1000, duration_ms=0) is False


def test_summarize_taste_empty_with_no_rows():
    assert summarize_taste([]) == ""


def test_summarize_taste_empty_when_nothing_has_enough_plays():
    rows = [{"track_name": "Song A", "artists": "Artist A", "plays": 1, "skips": 1}]
    assert summarize_taste(rows) == ""


def test_summarize_taste_mentions_a_frequently_skipped_track():
    rows = [
        {"track_name": "Skipped Song", "artists": "Artist A", "plays": 4, "skips": 3},
        {"track_name": "Neutral Song", "artists": "Artist B", "plays": 2, "skips": 0},
    ]
    note = summarize_taste(rows)
    assert "Skipped Song" in note
    assert "skips" in note


def test_summarize_taste_mentions_a_frequently_replayed_track():
    rows = [
        {"track_name": "Loved Song", "artists": "Artist A", "plays": 5, "skips": 0},
        {"track_name": "One-off", "artists": "Artist B", "plays": 1, "skips": 0},
    ]
    note = summarize_taste(rows)
    assert "Loved Song" in note
    assert "all the way through" in note


def test_summarize_taste_does_not_contradict_itself_on_the_same_track():
    # a track that's both the "most skipped" (by ratio) and technically
    # has the most full plays among a tiny dataset shouldn't be reported
    # as both skipped and replayed in the same note.
    rows = [{"track_name": "Only Song", "artists": "Artist A", "plays": 4, "skips": 3}]
    note = summarize_taste(rows)
    assert note.count("Only Song") == 1
