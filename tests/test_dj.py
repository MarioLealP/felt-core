"""WP-48 DJ mode: learning, profile, picking, and the poll loop."""

import json

import httpx
import pytest

from felt_core import spotify_client as sp
from felt_core.brain.music_fast_path import music_fast_path
from felt_core.config import FeltConfig
from felt_core.db import record_music_play
from felt_core.dj import control, learning, picker, store
from felt_core.dj.profile import build_profile
from felt_core.events import build_prompt


@pytest.fixture(autouse=True)
def _off():
    control.stop("test")
    yield
    control.stop("test")


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), spotify_refresh_token="r")


# --- learning ---------------------------------------------------------------


def test_discovery_starts_at_a_third_and_follows_his_skips():
    assert learning.discovery_share([True, False]) == pytest.approx(1 / 3)
    assert learning.discovery_share([False] * 10) == pytest.approx(0.15)
    assert learning.discovery_share([True] * 10) == pytest.approx(0.6)


def test_outcome_uses_the_history_skip_rule():
    assert learning.outcome(20_000, 200_000) == "skipped"
    assert learning.outcome(150_000, 200_000) == "played"


# --- profile ----------------------------------------------------------------


def test_profile_names_favourites_skips_and_feedback():
    text = build_profile(
        [
            {"artist": "Jamie Paige", "plays": 40, "skips": 2},
            {"artist": "Nobody Likes", "plays": 6, "skips": 5},
        ],
        ["Porter Robinson"],
        [{"title": "Birdbrain", "artist": "Jamie Paige", "liked": 1}],
        [{"title": "X", "artist": "Y", "discovery": 1, "outcome": "skipped"}],
        23,
    )
    assert "Artists he plays most: Jamie Paige, Nobody Likes." in text
    assert "Spotify's top artists for him lately: Porter Robinson." in text
    assert "Artists he usually skips: Nobody Likes." in text
    assert 'He asked for more like: "Birdbrain" by Jamie Paige.' in text
    assert 'Your recent picks he skipped: "X" by Y.' in text
    assert "It's late evening." in text


# --- choosing ---------------------------------------------------------------


def _p(uri, new):
    return {"uri": uri, "title": uri, "artist": "a", "discovery": new}


def test_choose_hits_the_mix_and_fills_from_the_other_kind():
    found = [_p("n1", True), _p("n2", True), _p("k1", False), _p("k2", False)]
    picks = picker.choose(found, 3, 1)
    assert sorted(p["uri"] for p in picks if p["discovery"]) == ["n1"]
    assert len(picks) == 3
    only_known = picker.choose([_p("k1", False), _p("k2", False)], 2, 2)
    assert len(only_known) == 2  # no discoveries found: familiar ones fill in


async def test_pick_checks_spotify_and_his_history(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    # he has played "Old Fave" (familiar); "Skipped Thing" mostly skipped
    record_music_play(config.db_path, "id-old", "Old Fave", "Known", 200_000, 200_000, False)
    for _ in range(3):
        record_music_play(config.db_path, "id-skip", "Skipped Thing", "Meh", 1_000, 200_000, True)
    fake_llm.script = [
        json.dumps(
            {
                "songs": [
                    {"title": "Old Fave", "artist": "Known", "new": False},
                    {"title": "Skipped Thing", "artist": "Meh", "new": False},
                    {"title": "Fresh", "artist": "Newbie", "new": True},
                    {"title": "Made Up Song", "artist": "Nobody", "new": True},
                ]
            }
        )
    ]
    tracks = {
        ("Old Fave", "Known"): {"id": "id-old", "uri": "u:old", "name": "Old Fave"},
        ("Skipped Thing", "Meh"): {"id": "id-skip", "uri": "u:skip", "name": "Skipped Thing"},
        ("Fresh", "Newbie"): {"id": "id-new", "uri": "u:new", "name": "Fresh"},
    }

    async def find(client, cfg, title, artist):
        t = tracks.get((title, artist))
        return None if t is None else {**t, "artists": [{"name": artist}]}

    async def nothing(*a, **k):
        return []

    async def not_saved(client, cfg, uris):
        return [False] * len(uris)

    monkeypatch.setattr(sp, "find_track", find)
    monkeypatch.setattr(sp, "top_artists", nothing)
    monkeypatch.setattr(sp, "library_contains", not_saved)
    monkeypatch.setattr(picker.random, "random", lambda: 0.0)  # every slot wants a discovery
    picker._top_cache["at"] = 0.0
    # "Old Fave" was played just now -> too recent; the skipped one is out; made-up song not found
    async with httpx.AsyncClient() as client:  # every Spotify call above is faked
        picks = await picker.pick(client, config, 2, avoid=set())
    assert [(p["uri"], p["discovery"]) for p in picks] == [("u:new", True)]
    assert fake_llm.calls[0]["json_schema"] is not None


# --- the poll loop --------------------------------------------------------------


def _snap(uri, title="t", progress=0, duration=200_000, playing=True):
    return {
        "track": {"uri": uri, "name": title, "artists": [{"name": "a"}]},
        "progress_ms": progress,
        "duration_ms": duration,
        "is_playing": playing,
    }


def _on(config, current, ahead, monkeypatch, spawned):
    for p in [current, *ahead]:
        store.add_pick(config.db_path, p["uri"], p["title"], p["artist"], p["discovery"])
    control.SESSION.on, control.SESSION.current, control.SESSION.ahead = True, current, ahead
    monkeypatch.setattr(
        control, "_spawn", lambda coro: (spawned.append(coro.__name__), coro.close())
    )


def test_her_next_pick_coming_on_records_says_and_refills(tmp_path, monkeypatch):
    config, spawned = _config(tmp_path), []
    a, b, c = _p("u:a", False), _p("u:b", True), _p("u:c", False)
    _on(config, a, [b, c], monkeypatch, spawned)
    control.on_poll(config, _snap("u:a", "u:a", progress=30_000), _snap("u:b", "u:b"))
    assert control.SESSION.current == b and control.SESSION.ahead == [c]
    assert spawned == ["_say", "_refill"]
    assert store.pick_outcomes(config.db_path, 5)[0]["outcome"] == "skipped"  # 15% in


def test_something_else_playing_ends_dj_mode(tmp_path, monkeypatch):
    config, spawned = _config(tmp_path), []
    _on(config, _p("u:a", False), [_p("u:b", False)], monkeypatch, spawned)
    control.on_poll(config, _snap("u:a", "u:a"), _snap("u:mario", "His Pick"))
    assert not control.is_on() and spawned == []


def test_a_long_pause_ends_dj_mode(tmp_path, monkeypatch):
    config, spawned = _config(tmp_path), []
    _on(config, _p("u:a", False), [], monkeypatch, spawned)
    now = [1000.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: now[0])
    paused = _snap("u:a", "u:a", playing=False)
    control.on_poll(config, paused, paused)
    assert control.is_on()
    now[0] += control.PAUSE_ENDS_S + 1
    control.on_poll(config, paused, paused)
    assert not control.is_on()


def test_relinked_uri_is_still_her_pick():
    pick = {"uri": "u:a", "title": "Birdbrain", "artist": "Jamie Paige", "discovery": False}
    snap = {"track": {"uri": "u:other", "name": "Birdbrain", "artists": [{"name": "Jamie Paige"}]}}
    assert control.same_track(pick, snap)


# --- words ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "action"),
    [
        ("be my DJ", "start"),
        ("dj mode", "start"),
        ("can u start dj mode", "start"),
        ("dj mode off", "stop"),
        ("stop the dj", "stop"),
        ("more like this", "more"),
        ("not this one", "less"),
        ("i don't like this song", "less"),
    ],
)
def test_dj_phrases(message, action):
    assert music_fast_path(message) == ("dj_mode", {"action": action})


def test_dj_line_prompt_sells_a_discovery():
    new = build_prompt("dj_track", {"title": "Fresh", "artist": "Newbie", "new": True})
    old = build_prompt("dj_track", {"title": "Fave", "artist": "Known", "new": False})
    assert new is not None and "new to him" in new and '"Fresh" by Newbie' in new
    assert old is not None and "already likes" in old
    assert build_prompt("dj_track", {"artist": "x"}) is None
