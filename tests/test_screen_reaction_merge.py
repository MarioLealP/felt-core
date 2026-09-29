"""WP-40: process_screen_context and react_to_screen used to each inline
their own copy of the identical "are we on cooldown" check
(seconds_since_last_spoken_reaction + logging + early return). Merged into
one shared felt_core.brain.screen_reactions._on_cooldown - this proves the
merge didn't change either entry point's observable behavior: the pure
helper's own truth table, and both entry points producing the identical
silent/non-silent outcome from the identical state, with zero model calls
while on cooldown."""

import json

import felt_core.brain.screen_reactions as screen_reactions
from felt_core.config import FeltConfig
from felt_core.db import save_observation, set_observation_reply

# -- _on_cooldown: the pure(ish) truth table, isolated from either caller ---


def test_on_cooldown_true_when_a_recent_spoken_reaction_exists(tmp_path):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), screen_reaction_cooldown_s=90)
    obs = save_observation(config.db_path, "s", "a game")
    set_observation_reply(config.db_path, obs, "nice")

    assert screen_reactions._on_cooldown(config, "s") is True


def test_on_cooldown_false_with_no_prior_spoken_reaction(tmp_path):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), screen_reaction_cooldown_s=90)

    assert screen_reactions._on_cooldown(config, "s") is False


def test_on_cooldown_false_once_the_window_has_a_zero_cooldown(tmp_path):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), screen_reaction_cooldown_s=0)
    obs = save_observation(config.db_path, "s", "a game")
    set_observation_reply(config.db_path, obs, "nice")

    assert screen_reactions._on_cooldown(config, "s") is False


def test_on_cooldown_is_per_session(tmp_path):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), screen_reaction_cooldown_s=90)
    obs = save_observation(config.db_path, "session-a", "a game")
    set_observation_reply(config.db_path, obs, "nice")

    assert screen_reactions._on_cooldown(config, "session-a") is True
    assert screen_reactions._on_cooldown(config, "session-b") is False


# -- both entry points, driven by the same shared helper, behave alike -----


def _config(tmp_path, **kwargs):
    kwargs.setdefault("screen_reaction_cooldown_s", 90)
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


async def test_both_entry_points_go_silent_on_the_same_cooldown_state(tmp_path, fake_llm):
    config = _config(tmp_path)
    session_id = "shared-cooldown-test"
    fake_llm.default = json.dumps(
        {"seen": "x", "line": "should not be reached", "emotion": "happy"}
    )

    # Prime the cooldown exactly once, via whichever entry point - both read
    # the same screen_observations table through the same helper.
    obs = save_observation(config.db_path, session_id, "priming")
    set_observation_reply(config.db_path, obs, "already reacted")

    screen_result = await screen_reactions.process_screen_context(
        "new thing", config, session_id=session_id
    )
    image_result = await screen_reactions.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id=session_id
    )

    assert screen_result.reply == ""
    assert screen_result.animation_tag is None
    assert image_result.reply == ""
    assert image_result.animation_tag is None
    assert fake_llm.calls == []  # neither entry point made a single model call


async def test_both_entry_points_react_once_cooldown_has_passed(tmp_path, fake_llm):
    config = _config(tmp_path, screen_reaction_cooldown_s=0)

    fake_llm.script = ["a local-style reaction"]
    screen_result = await screen_reactions.process_screen_context(
        "new thing", config, session_id="s1"
    )
    assert screen_result.reply == "a local-style reaction"

    fake_llm.script = [json.dumps({"seen": "x", "line": "nice!", "emotion": "happy"})]
    image_result = await screen_reactions.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="s2"
    )
    assert image_result.reply == "nice!"

    assert len(fake_llm.calls) == 2
