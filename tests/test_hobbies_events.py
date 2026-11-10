"""WP-30: hobbies in the welcome-back line and the diary."""

import felt_core.core.diary_material as diary_material
import felt_core.core.event_prompt as event_prompt


def test_afk_end_mentions_known_hobbies_only():
    prompt = event_prompt.build_prompt(
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
    prompt = event_prompt.build_prompt("afk_end", {"duration_s": 2400})
    assert prompt is not None
    assert "While he was gone" not in prompt


def test_diary_hobby_line_is_first_person():
    line = diary_material._event_line("hobby", {"hobby": "gaming", "minutes": 15})
    assert line == "spent about 15 min playing on my handheld while Mario was away"
    assert diary_material._event_line("hobby", {"hobby": "unknown"}) is None
