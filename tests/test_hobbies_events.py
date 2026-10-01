"""WP-30: hobbies in the welcome-back line and the diary."""

from felt_core import diary, events


def test_afk_end_mentions_known_hobbies_only():
    prompt = events.build_prompt(
        "afk_end",
        {
            "duration_s": 2400,
            "hobbies": [
                {"hobby": "reading", "minutes": 12},
                {"hobby": "napping", "minutes": 20},
                {"hobby": "rm -rf", "minutes": 3},
            ],
        },
    )
    assert prompt is not None
    assert "reading a book (about 12 min) and napping (about 20 min)" in prompt
    assert "rm -rf" not in prompt


def test_afk_end_without_hobbies_is_unchanged():
    prompt = events.build_prompt("afk_end", {"duration_s": 2400})
    assert prompt is not None
    assert "While he was gone" not in prompt


def test_diary_hobby_line_is_first_person():
    line = diary._event_line("hobby", {"hobby": "gaming", "minutes": 15})
    assert line == "spent about 15 min playing on my handheld while Mario was away"
    assert diary._event_line("hobby", {"hobby": "unknown"}) is None
