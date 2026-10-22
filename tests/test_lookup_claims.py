"""FELT-108: a reply that claims a lookup without a tool call is retried with
a tool required."""

import json

import pytest

from felt_core.brain import orchestrator
from felt_core.brain.lookup_claims import (
    claims_a_lookup,
    claims_a_tool_use,
    claims_an_action,
    refuses_a_tool_job,
)
from felt_core.config import FeltConfig
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


@pytest.mark.parametrize(
    "text",
    [
        "Checking that for you. Just a sec while I pull up the wiki... Okay, so a Chaos Orb",
        "Let me look that up!",
        "One sec, I'll search your documents for it.",
        "Pulling up the wiki now.",
        "Looking it up...",
        "I looked it up and couldn't find the exact launch time for WoW Forever.",
        "I’ve googled it - nothing official yet.",
        "I checked online and it's not out yet.",
        "I did a quick search, nothing public.",
    ],
)
def test_claims(text):
    assert claims_a_lookup(text)


@pytest.mark.parametrize(
    "text",
    [
        "A Chaos Orb rerolls an item's modifiers.",
        "Morning! Coffee first?",
        "",
        "That's the reroll orb. " * 10 + "Let me know if you want more.",
        "I checked out that anime you mentioned, it's great.",
        "You looked it up already, didn't you?",
        "I searched my feelings and I still hate Mondays.",
    ],
)
def test_not_claims(text):
    assert not claims_a_lookup(text)


@pytest.mark.parametrize(
    "text",
    [
        # the two live replies from 2026-10-17 (the shelf light never changed)
        "Consider it done. I'm flipping that switch for you now.",
        "Got it. Turning the shelf light off for you now.",
        "I'll switch the fan on.",
        "Dimming the lights down a bit.",
        "I already flipped that switch for you, but I can do it again.",
        "Done - I switched the shelf light off.",
        # 2026-10-18 e2e: nothing reached Spotify for any of these
        "Playing some Bring Me The Horizon. This is quite the shift in mood.",
        "Paused the music. I'm staying put, just let me know.",
        "Moving on to something a bit different.",
        "Okay, now playing Birdbrain.",
        "Sure, skipping this one.",
        "Throwing on some Daft Punk for you.",
        "I've queued up that song.",
        "Oh, my bad. I'll dial it back down for you.",
        "I've set that reminder for you for the 21st to check on the semantic router.",
        "Done, your alarm is set for 7:30.",
        "I've added it to your calendar for Friday.",
        "Reminder set for tomorrow at 9.",
        "I'm lowering the volume for you. How's that?",
        "Moving on. Honestly, I was wondering how long you'd let that one sit.",
        "It's at fifty-five percent now. Is that the sweet spot?",
        "Adding some Zutomayo to the queue. Good call.",
        "Got it, switching gears. That last one was a bit of a chaotic leap.",
        "Sure thing, I'll add some Zutomayo to the queue.",
        # 2026-10-19 live: a typo'd "30 percente" went to her, she called nothing
        "Setting the chandelier to thirty percent. There, that's a much more civilized level.",
        "I've dimmed the lights to 30%.",
        "Brought the chandelure down to 20 percent for you.",
        # cm-p6a, 2026-10-22: after a web search, nothing set
        "It’s on the fourth of November, three in the afternoon Pacific Time, which is eleven at "
        "night here. I’ve got it set for you, so you’ll be up an hour before the servers open.",
        "It goes live November 4th at 11 PM UTC, so I'll ping you at 10.",
        "I'll remind you an hour before.",
        "Okay, it's set for 10 PM.",
        "Your reminder's set.",
    ],
)
def test_action_claims(text):
    assert claims_an_action(text)
    assert claims_a_tool_use(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'm going to turn in for the night.",
        "That song really turned my mood around.",
        "Your package turned up, by the way.",
        "Morning! Coffee first?",
        "Ha, nice one. " * 15 + "Want me to turn the fan on?",
        "Playing it cool, huh?",
        "You've been playing Endfield all day.",
        "I paused for a second there, sorry.",
        "Skipped breakfast again?",
        "Want me to skip this one?",
        "Playing along, are we?",
        "Bring it on, I'm ready.",
        "Your battery's at 40 percent, by the way.",
        "I think we should be moving on from this topic soon.",
        "Want me to add that to the queue?",
        "I keep switching gears between games lately.",
        "Want me to set a reminder for that?",
        "I set my mind on finishing this game.",
        "Want me to set the lights to 30 percent?",
        "Should I dim them to 20 percent?",
        "Want me to ping you an hour before?",
        "I'll remind you that you said that.",
        "It's set in 1920s Shanghai, which is half the charm.",
        "The sun's set already, huh.",
    ],
)
def test_not_action_claims(text):
    assert not claims_an_action(text)


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


def _call():
    return {
        "id": "c1",
        "type": "function",
        "function": {"name": "poe2_wiki", "arguments": json.dumps({"query": "chaos orb"})},
    }


# WP-65: the bare "it's done" openings (spike e2e, 2026-10-20)
@pytest.mark.parametrize(
    "text",
    [
        "Set for 7:30.",
        "All set for 7:30 tomorrow, sleep well.",
        "Done. Milk's in there.",
        "Done!",
        "Milk's on there.",
        "Okay, ringing it now.",
        "Ringing your phone - listen for it.",
        "Dimmed to the usual level.",
    ],
)
def test_bare_done_claims(text):
    assert claims_an_action(text)


@pytest.mark.parametrize(
    "text",
    [
        "What's on there?",
        "It's on there already, you added it yesterday.",
        "Done with that game yet?",
        "Set for tomorrow? Sure, what time?",
        "My ears are ringing after that concert.",
        "There's a lot going on there.",
        "The milk's gone off, I bet.",
        "I'm all set for the weekend, honestly.",
        "Fried rice! You've got everything for it.",
    ],
)
def test_not_bare_done_claims(text):
    assert not claims_an_action(text)


def test_curly_apostrophes_count():
    assert claims_an_action("I\u2019ve added an appointment for 4th November.")


# live 2026-10-21: she had the calendar tool and said she didn't
@pytest.mark.parametrize(
    "text",
    [
        "I can\u2019t really make an appointment on your calendar because I don\u2019t have access",
        "I don't have access to your calendar system here, but I can remind you!",
        "Sorry, I'm not able to add events to your calendar.",
        "I cannot set a reminder from here.",
        "I can't turn the lights off, sorry.",
    ],
)
def test_refusals(text):
    assert refuses_a_tool_job(text)
    assert claims_a_tool_use(text)


@pytest.mark.parametrize(
    "text",
    [
        "I can't wait for the event, honestly.",
        "I can't believe you put that on the calendar.",
        "You can't make the appointment? Want me to move it?",
        "I don't have a clue what's on your calendar tomorrow, want me to check?",
        "I can't make it any clearer: go to bed.",
    ],
)
def test_not_refusals(text):
    assert not refuses_a_tool_job(text)


def _registry(handled):
    async def handle(params, ctx):
        handled.append(params)
        return SkillResult(text="[Chaos Orb] Rerolls one random modifier.")

    return {
        "poe2_wiki": LoadedSkill(
            name="poe2_wiki",
            description="wiki",
            parameters_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            handle=handle,
        )
    }


async def test_claimed_lookup_is_retried_with_a_tool_required(tmp_path, monkeypatch, fake_llm):
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry(handled))
    fake_llm.script = [
        "Checking that for you, pulling up the wiki... a Chaos Orb is the reset button.",
        {"text": "", "tool_calls": [_call()]},
        "Per the wiki, a Chaos Orb rerolls one random modifier.",
    ]

    result = await orchestrator.chat("what does a chaos orb do", _config(tmp_path), session_id="s")

    assert handled == [{"query": "chaos orb"}]
    assert [c["tool_choice"] for c in fake_llm.calls] == [None, "required", None]
    assert result.reply == "Per the wiki, a Chaos Orb rerolls one random modifier."


async def test_plain_reply_is_not_retried(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry([]))
    fake_llm.script = ["A Chaos Orb rerolls modifiers."]

    result = await orchestrator.chat("chaos orb?", _config(tmp_path), session_id="s")

    assert len(fake_llm.calls) == 1
    assert result.reply == "A Chaos Orb rerolls modifiers."


async def test_forced_retry_without_a_tool_keeps_the_reply(tmp_path, monkeypatch, fake_llm, caplog):
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry([]))
    fake_llm.script = ["Let me check... it rerolls modifiers.", "still no tool"]

    result = await orchestrator.chat("chaos orb?", _config(tmp_path), session_id="s")

    assert result.reply == "Let me check... it rerolls modifiers."
    assert "forced retry still called no tool" in caplog.text


async def test_forced_retry_error_keeps_the_reply(tmp_path, monkeypatch, fake_llm, caplog):
    from felt_core import local_llm

    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry([]))
    fake_llm.script = [
        "Let me check... it rerolls modifiers.",
        local_llm.LocalLLMUnavailable("400"),
    ]

    result = await orchestrator.chat("chaos orb?", _config(tmp_path), session_id="s")

    assert result.reply == "Let me check... it rerolls modifiers."
    assert "forced retry failed" in caplog.text


async def test_a_skill_whose_service_is_down_says_so(tmp_path, monkeypatch):
    # e2e 2026-10-18: poe2-rag/drive search/weather/searxng down -> the whole chat 500'd
    import httpx

    from felt_core.brain import orchestrator

    async def down(name, params, registry, ctx):
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(orchestrator, "run_skill", down)
    result, output = await orchestrator._run_skill_call(
        ("poe2_wiki", {"query": "chaos orb"}), {}, _config(tmp_path), "what does the chaos orb do"
    )
    assert result is None
    assert "couldn't reach" in output and "poe2_wiki" in output


# cm-p6a: tools chained in one message (search, then set the reminder)
def _two_skills(handled):
    def skill(name, text):
        async def handle(params, ctx):
            handled.append((name, params))
            return SkillResult(text=text)

        return LoadedSkill(
            name=name,
            description=name,
            parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            handle=handle,
        )

    return {
        "web_search": skill("web_search", "WoW Forever launches Nov 4, 23:00 Lisbon."),
        "set_reminder": skill("set_reminder", "Reminder set for Nov 4, 22:00."),
    }


def _tool(name, q="x", call_id="c1"):
    call = {"id": call_id, "type": "function"}
    call["function"] = {"name": name, "arguments": json.dumps({"q": q})}
    return {"text": "", "tool_calls": [call]}


async def test_a_second_tool_call_runs(tmp_path, monkeypatch, fake_llm):
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _two_skills(handled))
    fake_llm.script = [
        _tool("web_search", "wow forever launch time"),
        _tool("set_reminder", "nov 4 22:00", "c2"),
        "It launches at 11 tonight - you'll get a nudge at 10.",
    ]

    result = await orchestrator.chat("google it and remind me", _config(tmp_path), session_id="s")

    assert [name for name, _ in handled] == ["web_search", "set_reminder"]
    assert result.reply == "It launches at 11 tonight - you'll get a nudge at 10."
    tool_results = [m for m in fake_llm.calls[2]["messages"] if m["role"] == "tool"]
    assert len(tool_results) == 2 and "Reminder set" in tool_results[1]["content"]


async def test_a_claimed_reminder_after_a_search_is_retried(tmp_path, monkeypatch, fake_llm):
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _two_skills(handled))
    fake_llm.script = [
        _tool("web_search"),
        "It's at eleven at night here. I've got it set for you.",
        _tool("set_reminder", "nov 4 22:00", "c2"),
        "Eleven tonight; I'll nudge you at ten.",
    ]

    result = await orchestrator.chat("google it and remind me", _config(tmp_path), session_id="s")

    assert [name for name, _ in handled] == ["web_search", "set_reminder"]
    assert [c["tool_choice"] for c in fake_llm.calls] == [None, None, "required", None]
    assert result.reply == "Eleven tonight; I'll nudge you at ten."


async def test_a_repeated_call_ends_the_chain(tmp_path, monkeypatch, fake_llm, caplog):
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _two_skills(handled))
    fake_llm.script = [_tool("web_search", "same"), _tool("web_search", "same", "c2"), "phrased"]

    result = await orchestrator.chat("google it", _config(tmp_path), session_id="s")

    assert [name for name, _ in handled] == ["web_search"]
    assert "repeated web_search" in caplog.text
    assert result.reply == "phrased"


async def test_the_chain_stops_at_the_cap(tmp_path, monkeypatch, fake_llm):
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _two_skills(handled))
    cap = orchestrator._MAX_TOOL_STEPS
    fake_llm.script = [_tool("web_search", f"q{i}", f"c{i}") for i in range(cap + 1)] + ["phrased"]

    result = await orchestrator.chat("google it", _config(tmp_path), session_id="s")

    assert len(handled) == cap
    assert result.reply == "phrased"
    # the cut-short chain's phrasing is told what was really done
    assert "Only these steps were done" in fake_llm.calls[-1]["messages"][-1]["content"]
