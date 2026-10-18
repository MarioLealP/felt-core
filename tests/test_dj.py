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
    assert "Artists he plays most: Jamie Paige." in text  # the skippy one isn't a favourite
    assert "Spotify's top artists for him lately: Porter Robinson." in text
    assert "Artists he usually skips: Nobody Likes." in text
    assert 'He asked for more like: "Birdbrain" by Jamie Paige.' in text
    assert 'Your recent picks he skipped: "X" by Y.' in text
    assert "It's late evening." in text


# --- picking ----------------------------------------------------------------


def _hist(tid, plays, skips, last="2026-01-01 00:00:00"):
    return {"title": tid, "artist": "a", "plays": plays, "skips": skips, "last": last}


def test_familiar_only_takes_songs_he_plays_through_and_not_lately():
    good = "4uLU6hMCjMI75M1A2tKUQC"
    history = {
        good: _hist("good", 5, 0),
        "spotify:local:::Shadow+Bringers+Ost:400": _hist("local file", 50, 0),
        "skippy": _hist("skippy", 4, 3),
        "lately": _hist("lately", 9, 0, last="2999-01-01 00:00:00"),
        "avoided": _hist("avoided", 9, 0),
    }
    picks = picker.familiar(history, 5, avoid={"spotify:track:avoided"})
    assert [p["uri"] for p in picks] == [f"spotify:track:{good}"]
    assert picks[0]["discovery"] is False


async def test_discoveries_are_new_artists_with_real_tracks(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = [
        json.dumps(
            {
                "artists": [
                    {"name": "Jonathan Young", "because": "already his"},
                    {"name": "Made Up Band", "because": "nope"},
                    {"name": "Ghost Data", "because": "like Jonathan Young, game-song metal"},
                ]
            }
        )
    ]

    async def tracks(client, cfg, artist):
        if artist == "Ghost Data":
            return [{"uri": "u:gd", "name": "Wicked", "artists": [{"name": "Ghost Data"}]}]
        return []

    monkeypatch.setattr(sp, "artist_tracks", tracks)
    async with httpx.AsyncClient() as client:  # every Spotify call is faked
        picks = await picker.discoveries(
            client, config, "profile", {"jonathanyoung"}, 1, avoid=set()
        )
    assert picks == [
        {
            "uri": "u:gd",
            "title": "Wicked",
            "artist": "Ghost Data",
            "discovery": True,
            "because": "like Jonathan Young, game-song metal",
        }
    ]
    assert fake_llm.calls[0]["json_schema"] is not None


async def test_pick_fills_with_familiar_when_discoveries_fail(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    # recorded just now -> too recent to come back as a familiar pick
    record_music_play(config.db_path, "id-old", "Old Fave", "Known", 200_000, 200_000, False)
    fake_llm.script = [json.dumps({"artists": []})]

    async def none(*a, **k):
        return []

    monkeypatch.setattr(sp, "top_artists", none)
    monkeypatch.setattr(picker.random, "random", lambda: 0.0)  # every slot wants a discovery
    picker._top_cache["at"] = 0.0
    async with httpx.AsyncClient() as client:
        picks = await picker.pick(client, config, 2, avoid=set())
    assert picks == []  # nothing new found, and the one familiar song was played just now


# --- the poll loop --------------------------------------------------------------


def _p(uri, new):
    return {"uri": uri, "title": uri, "artist": "a", "discovery": new}


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
        # Mario, 2026-10-18 - paused the music instead
        ("less like this", "less"),
        ("less of this pls", "less"),
        ("fewer songs like this", "less"),
        ("no more like that", "less"),
        ("more of this", "more"),
        ("more songs like this one", "more"),
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


# --- starting a song (2026-10-18: the plain play call loads nothing on his client) ----


async def _calls_with(monkeypatch, queue_status, up_next=("u:x",)):
    calls = []

    async def token(client, config):
        return "tok"

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/me/player/queue":
            if request.method == "GET":
                return httpx.Response(200, json={"queue": [{"uri": u} for u in up_next]})
            return httpx.Response(queue_status)
        return httpx.Response(204)

    monkeypatch.setattr(sp, "_get_access_token", token)
    return calls, httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_start_track_queues_then_skips(tmp_path, monkeypatch):
    calls, client = await _calls_with(monkeypatch, 204)
    async with client:
        await sp.start_track(client, _config(tmp_path), "u:x")
    assert calls == [
        ("POST", "/v1/me/player/queue"),
        ("GET", "/v1/me/player/queue"),
        ("POST", "/v1/me/player/next"),
    ]


async def test_start_track_skips_past_what_was_queued_before(tmp_path, monkeypatch):
    # Mario, 2026-10-18: "can you play X just adds it to the queue"
    calls, client = await _calls_with(monkeypatch, 204, up_next=("u:pick", "u:old", "u:x", "u:ctx"))
    async with client:
        await sp.start_track(client, _config(tmp_path), "u:x")
    assert calls.count(("POST", "/v1/me/player/next")) == 3


async def test_a_sleeping_player_says_so(tmp_path, monkeypatch):
    calls, client = await _calls_with(monkeypatch, 404)
    async with client:
        with pytest.raises(sp.NoActivePlayer):
            await sp.start_track(client, _config(tmp_path), "u:x")
    assert calls == [("POST", "/v1/me/player/queue")]  # never skips what he has on


async def test_dj_start_on_a_sleeping_player(tmp_path, monkeypatch):
    async def picks(client, config, count, avoid):
        return [_p("u:a", False)]

    async def asleep(client, config, uri):
        raise sp.NoActivePlayer

    monkeypatch.setattr(picker, "pick", picks)
    monkeypatch.setattr(sp, "start_track", asleep)
    assert await control.start(_config(tmp_path)) == control.NOT_AWAKE
    assert not control.is_on()


def test_a_leftover_from_an_earlier_set_is_skipped_not_the_end(tmp_path, monkeypatch):
    config, spawned = _config(tmp_path), []
    store.add_pick(config.db_path, "u:old", "Old", "a", False)  # an earlier set's pick
    _on(config, _p("u:a", False), [_p("u:b", False)], monkeypatch, spawned)
    control.on_poll(config, _snap("u:a", "u:a"), _snap("u:old", "Old"))
    assert control.is_on() and spawned == ["_skip_leftover"]


# --- search by "<title> by/from <artist>" (live 2026-10-18) -----------------


def _hit(name, *artists):
    return {"uri": f"u:{name}", "name": name, "artists": [{"name": a} for a in artists]}


async def _search(monkeypatch, tmp_path, query, results):
    seen = []

    async def get(client, config, path, **params):
        seen.append(params["q"])
        return {"tracks": {"items": results.get(params["q"], [])}}

    monkeypatch.setattr(sp, "_get", get)
    async with httpx.AsyncClient() as client:
        track = await sp.search_track(client, _config(tmp_path), query)
    return track, seen


async def test_search_uses_title_and_artist_fields(tmp_path, monkeypatch):
    voices = _hit("Voices In My Head", "NIJUUGO*A", "重音テト")
    track, seen = await _search(
        monkeypatch,
        tmp_path,
        "voices in my head from nijuugo*a",
        {'track:"voices in my head" artist:"nijuugo*a"': [voices]},
    )
    assert track == voices and len(seen) == 1


async def test_search_skips_hits_that_dont_look_like_it(tmp_path, monkeypatch):
    wrong = _hit("Last Shot", "VALORANT", "templuv")
    voices = _hit("Voices In My Head", "NIJUUGO*A")
    track, _ = await _search(
        monkeypatch,
        tmp_path,
        "voices in my head by nijuugo*a",
        {"voices in my head nijuugo*a": [wrong, voices]},
    )
    assert track == voices


async def test_search_falls_back_to_the_top_hit(tmp_path, monkeypatch):
    top = _hit("Daddy! Daddy! Do!", "Masayuki Suzuki")
    track, seen = await _search(
        monkeypatch, tmp_path, "daddy daddy do from kaguya", {"daddy daddy do kaguya": [top]}
    )
    assert track == top and len(seen) == 3


async def test_no_match_prefers_the_plain_searchs_top_hit(tmp_path, monkeypatch):
    # "kaikai kitan from jujutsu kaisen": "from" names the anime, not the artist
    cover = _hit("JUJUTSU KAISEN - KAIKAI KITAN", "Kevin Remisch")
    eve = _hit("Kaikai Kitan", "Eve")
    track, _ = await _search(
        monkeypatch,
        tmp_path,
        "kaikai kitan from jujutsu kaisen",
        {
            'track:"kaikai kitan" artist:"jujutsu kaisen"': [cover],
            "kaikai kitan jujutsu kaisen": [eve, cover],
        },
    )
    assert track == eve


async def test_plain_search_drops_some(tmp_path, monkeypatch):
    dp = _hit("One More Time", "Daft Punk")
    track, seen = await _search(monkeypatch, tmp_path, "some daft punk", {"daft punk": [dp]})
    assert track == dp and seen == ["daft punk"]
