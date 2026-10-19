"""WP-48: plain music commands skip felt-llm's tool pick."""

import pytest

from felt_core.brain.music_fast_path import music_fast_path


@pytest.mark.parametrize(
    ("message", "skill", "params"),
    [
        ("skip", "media_control", {"action": "next"}),
        ("skip this song please", "media_control", {"action": "next"}),
        ("next song", "media_control", {"action": "next"}),
        ("can you go back", "media_control", {"action": "previous"}),
        ("previous track", "media_control", {"action": "previous"}),
        ("pause", "media_control", {"action": "pause"}),
        ("pause the music", "media_control", {"action": "pause"}),
        ("stop the music", "media_control", {"action": "pause"}),
        ("turn off the music", "media_control", {"action": "pause"}),
        ("resume", "media_control", {"action": "resume"}),
        ("turn the music back on", "media_control", {"action": "resume"}),
        ("louder", "media_control", {"action": "louder"}),
        ("turn it up :)", "media_control", {"action": "louder"}),
        ("turn the music down", "media_control", {"action": "quieter"}),
        ("volume 30", "media_control", {"action": "volume", "level": 30}),
        ("set the volume to 55%", "media_control", {"action": "volume", "level": 55}),
        ("save this song", "media_control", {"action": "save"}),
        ("like this one", "media_control", {"action": "save"}),
        ("save this to my liked songs", "media_control", {"action": "save"}),
        ("what's this song?", "now_playing", {}),
        # Mario's own lines, 2026-10-17 (all three missed the first version)
        ("can u pause the song?", "media_control", {"action": "pause"}),
        ("can you restart the song?", "media_control", {"action": "restart"}),
        ("can you play birdbrain jamie paige", "play_music", {"query": "birdbrain jamie paige"}),
        ("start this song over", "media_control", {"action": "restart"}),
        ("play it from the beginning", "media_control", {"action": "restart"}),
        ("pls play some daft punk", "play_music", {"query": "some daft punk"}),
        ("play the music", "media_control", {"action": "resume"}),
        ("what song is playing", "now_playing", {}),
        # 2026-10-18 e2e: both went to felt-llm, which claimed and did nothing
        ("hold the music a sec", "media_control", {"action": "pause"}),
        (
            "could you throw on some bring me the horizon",
            "play_music",
            {"query": "some bring me the horizon"},
        ),
        ("put on some lofi", "play_music", {"query": "some lofi"}),
        ("it's way too loud", "media_control", {"action": "quieter"}),
        ("the music is too loud", "media_control", {"action": "quieter"}),
        ("ugh it's too quiet", "media_control", {"action": "louder"}),
        ("can you make it a bit quieter", "media_control", {"action": "quieter"}),
        ("make the music louder pls", "media_control", {"action": "louder"}),
        ("ugh this song sucks, next", "media_control", {"action": "next"}),
        ("meh. skip this one!", "media_control", {"action": "next"}),
        # queue, not play (2026-10-18)
        ("queue birdbrain", "play_music", {"query": "birdbrain", "when": "queue"}),
        (
            "can you queue up some daft punk",
            "play_music",
            {"query": "some daft punk", "when": "queue"},
        ),
        ("add birdbrain to the queue pls", "play_music", {"query": "birdbrain", "when": "queue"}),
        (
            "put one more time in my queue",
            "play_music",
            {"query": "one more time", "when": "queue"},
        ),
        ("play birdbrain next", "play_music", {"query": "birdbrain", "when": "queue"}),
        (
            "can you stick some zutomayo in the queue for later?",
            "play_music",
            {"query": "some zutomayo", "when": "queue"},
        ),
    ],
)
def test_commands(message, skill, params):
    assert music_fast_path(message) == (skill, params)


@pytest.mark.parametrize(
    "message",
    [
        "stop",  # the presence hush
        "stop it",  # more likely aimed at her
        "turn it off",
        "play a game with me",
        "play games with me later?",
        "play along",
        "play it cool",
        "put on your hoodie",
        "can you put on the pajamas",
        "throw on a jacket",
        "it's too loud outside",
        "that's too low a price",
        "so what's next?",
        "ok, next question",
        "add milk to the shopping list",
        "add birdbrain",
        "I skipped breakfast",
        "what's this song about? it sounds sad",
        "the volume on my headphones is broken",
        "",
    ],
)
def test_not_commands(message):
    assert music_fast_path(message) is None


def test_turn_off_the_music_is_not_a_home_device():
    # the orchestrator tries music before Home Assistant's matcher
    assert music_fast_path("turn off the music") == ("media_control", {"action": "pause"})
