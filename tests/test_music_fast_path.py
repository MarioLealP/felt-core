"""WP-48: plain music commands skip felt-llm's tool pick."""

import pytest

from felt_core.brain.home_fast_path import home_fast_path
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
        ("what song is playing", "now_playing", {}),
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
        "play some Daft Punk",  # play_music by name, via the model
        "I skipped breakfast",
        "what's this song about? it sounds sad",
        "the volume on my headphones is broken",
        "",
    ],
)
def test_not_commands(message):
    assert music_fast_path(message) is None


def test_turn_off_the_music_is_not_a_home_device():
    # the orchestrator tries music first; home alone would take it as a device
    assert home_fast_path("turn off the music") is not None
    assert music_fast_path("turn off the music") == ("media_control", {"action": "pause"})
