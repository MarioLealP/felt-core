"""The semantic router's logic (cm-6b0) with a word-count fake embedder -
the real model's accuracy is measured by tools/e2e_chat.py and the shadow
records, not here."""

import asyncio
import re
import zlib

import numpy as np
import pytest

from felt_core.brain import router
from felt_core.config import FeltConfig

DIM = 256


def fake_embed(texts):
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in re.findall(r"[a-z]+", text.lower()):
            out[i, zlib.crc32(word.encode()) % DIM] += 1.0
        out[i, 0] += 0.01  # never all-zero
    return out


EXAMPLES = {
    "media_control": ["skip this song", "turn the music up"],
    "dj_mode": ["be my dj"],
    "dj_mode/feedback": ["more like this song"],
    "get_weather": ["weather tomorrow", "weather this weekend"],
    "list_calendar_events": ["calendar for the weekend"],
    None: ["hello there", "thanks a lot"],
}


@pytest.fixture
def r():
    return router.Router(fake_embed, bar=0.6, examples=EXAMPLES)


def test_a_clear_command_is_sure(r):
    pick = r.pick("skip this song")
    assert (pick.skill, pick.sure, pick.via) == ("media_control", True, "message")


def test_chat_is_no_tool(r):
    assert r.pick("hello there").skill is None


def test_the_route_qualifier_isnt_reported(r):
    pick = r.pick("more like this song", router.Context(dj_on=True))
    assert (pick.route, pick.skill) == ("dj_mode/feedback", "dj_mode")


def test_dj_feedback_only_counts_while_dj_is_on(r):
    assert r.pick("more like this song", router.Context(dj_on=False)).route != "dj_mode/feedback"


def test_no_music_weighs_against_music_routes(r):
    playing = r.pick("turn the music up", router.Context(music_playing=True))
    silent = r.pick("turn the music up", router.Context(music_playing=False))
    assert silent.score == pytest.approx(playing.score - 0.05)


def test_a_short_follow_up_reads_with_the_previous_message(r):
    alone = r.pick("what about the weekend")
    follow = r.pick("what about the weekend", router.Context(previous="weather tomorrow"))
    assert alone.skill == "list_calendar_events"
    assert (follow.skill, follow.via) == ("get_weather", "follow-up")


def test_sure_chat_isnt_turned_into_a_follow_up(r):
    pick = r.pick("thanks a lot", router.Context(previous="weather tomorrow"))
    assert (pick.skill, pick.via) == (None, "message")


def test_below_the_bar_is_unsure(r):
    assert not r.pick("tell me about octopus brains").sure


# --- shadow mode ----------------------------------------------------------------


@pytest.fixture
def live(tmp_path, monkeypatch):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), router_bar=0.6)
    monkeypatch.setattr(router, "EXAMPLES", EXAMPLES)
    asyncio.run(router.warm(config, embedder=lambda c: fake_embed))
    yield config
    router.reset()


async def test_shadow_records_the_pick_next_to_the_real_route(live):
    task = router.start_shadow("skip this song", router.Context())
    router.finish_shadow(live, task, "skip this song", "media_control", fast=True)
    await router.drain()
    [row] = router.shadow_rows(live.db_path)
    assert (row["pick"], row["actual"], row["fast"], row["sure"]) == (
        "media_control",
        "media_control",
        1,
        1,
    )


async def test_shadow_is_off_without_a_router(tmp_path):
    router.reset()
    assert router.start_shadow("skip", router.Context()) is None
    router.finish_shadow(FeltConfig(db_path=str(tmp_path / "f.db")), None, "skip", None, False)


async def test_a_missing_model_leaves_it_off(tmp_path):
    def broken(config):
        raise FileNotFoundError("no model on disk")

    router.reset()
    await router.warm(FeltConfig(db_path=str(tmp_path / "f.db")), embedder=broken)
    assert router.start_shadow("skip", router.Context()) is None
