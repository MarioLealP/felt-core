"""WP-48 DJ mode: learning, profile, picking, and the poll loop."""

import json
import time

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
    sp._felt_skipped.clear()
    sp._felt_started.clear()
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


def test_a_song_he_asked_for_is_an_interlude(tmp_path, monkeypatch):
    # 2026-10-18: "play daddy daddy do" mid-set skipped past two of her picks -
    # her skips, not his, and the set should carry on after his song
    config, spawned = _config(tmp_path), []
    a, b, c = _p("u:a", False), _p("u:b", True), _p("u:c", False)
    _on(config, a, [b, c], monkeypatch, spawned)
    for uri in ("u:a", "u:b"):
        sp._felt_skipped[uri] = time.monotonic()
    sp._felt_started["u:his"] = time.monotonic()
    control.on_poll(config, _snap("u:a", "u:a", progress=30_000), _snap("u:his", "His"))
    assert control.is_on() and control.SESSION.current is None
    control.on_poll(config, _snap("u:his", "His"), _snap("u:c", "u:c"))
    assert control.SESSION.current == c and control.SESSION.ahead == []
    assert all(o["outcome"] != "skipped" for o in store.pick_outcomes(config.db_path, 5))


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
        ("less llike this", "less"),
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


class _Player:
    """A fake Spotify player: queue adds land behind what's queued, next
    takes the front, and the playing track can show the change late."""

    def __init__(self, queue_status=204, up_next=(), lag=0, stuck=False, relink=None):
        self.calls: list[tuple[str, str]] = []
        self.now = "u:now"
        self.queue = list(up_next)
        self.queue_status, self.lag, self.stuck, self.relink = queue_status, lag, stuck, relink
        self._stale = 0
        self._shown = self.now

    def handler(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v1/me/player/queue" and request.method == "POST":
            if self.queue_status == 204:
                uri = request.url.params["uri"]
                self.queue.append(self.relink.get(uri, uri) if self.relink else uri)
            return httpx.Response(self.queue_status)
        if path == "/v1/me/player/next":
            if not self.stuck and self.queue:
                self.now = self.queue.pop(0)
                self._stale = self.lag
            return httpx.Response(204)
        if path == "/v1/me/player/currently-playing":
            if self._stale:
                self._stale -= 1
            else:
                self._shown = self.now
            name = "Birdbrain" if self._shown.startswith("u:x") else self._shown
            return httpx.Response(200, json={"item": {"uri": self._shown, "name": name}})
        return httpx.Response(204)


async def _start(monkeypatch, tmp_path, player, uri="u:x", name=None):
    async def token(client, config):
        return "tok"

    monkeypatch.setattr(sp, "_get_access_token", token)
    monkeypatch.setattr(sp, "_SKIP_SETTLE_S", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(player.handler)) as client:
        await sp.start_track(client, _config(tmp_path), uri, name)
    return player.calls.count(("POST", "/v1/me/player/next"))


async def test_start_track_queues_then_skips(tmp_path, monkeypatch):
    player = _Player()
    assert await _start(monkeypatch, tmp_path, player) == 1
    assert player.now == "u:x"
    assert sp.felt_started("u:x") and sp.felt_skipped("u:now")


async def test_start_track_skips_past_what_was_queued_before(tmp_path, monkeypatch):
    # Mario, 2026-10-18: "can you play X just adds it to the queue"
    player = _Player(up_next=("u:pick", "u:old"))
    assert await _start(monkeypatch, tmp_path, player) == 3
    assert player.now == "u:x" and sp.felt_skipped("u:pick")


async def test_a_lagging_player_is_not_overshot(tmp_path, monkeypatch):
    # live 2026-10-18: Spotify showed the change late, one skip landed on a DJ pick
    player = _Player(up_next=("u:pick", "u:old"), lag=2)
    assert await _start(monkeypatch, tmp_path, player) == 3
    assert player.now == "u:x"


async def test_a_skip_that_never_shows_stops(tmp_path, monkeypatch):
    player = _Player(up_next=("u:pick",), stuck=True)
    assert await _start(monkeypatch, tmp_path, player) == 1


async def test_a_relinked_track_is_found_by_name(tmp_path, monkeypatch):
    player = _Player(up_next=("u:pick",), relink={"u:x": "u:x-relinked"})
    assert await _start(monkeypatch, tmp_path, player, name="Birdbrain") == 2
    assert player.queue == []


async def test_a_sleeping_player_says_so(tmp_path, monkeypatch):
    player = _Player(queue_status=404)
    with pytest.raises(sp.NoActivePlayer):
        await _start(monkeypatch, tmp_path, player)
    assert player.calls == [("POST", "/v1/me/player/queue")]  # never skips what he has on


async def test_dj_start_on_a_sleeping_player(tmp_path, monkeypatch):
    async def picks(client, config, count, avoid):
        return [_p("u:a", False)]

    async def asleep(client, config, uri, name=None):
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
