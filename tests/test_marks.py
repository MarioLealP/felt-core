"""WP-42.2: her marks in the notebook, on his line or her own reply."""

import json

import felt_core.marks as marks
from felt_core import local_llm
from felt_core.app_history import history_page
from felt_core.config import FeltConfig
from felt_core.db import save_message


def _config(tmp_path) -> FeltConfig:
    return FeltConfig(db_path=str(tmp_path / "felt.db"), local_llm_url="http://felt-llm")


def _decision(**kw) -> str:
    return json.dumps(
        {"mark": True, "line": "his", "type": "circle", "quote": "bin bags", "note": "again?"} | kw
    )


def test_parse_keeps_only_real_quotes():
    line = {"his": "add oat milk and bin bags"}
    assert marks.parse_decision(_decision(), line) == {
        "line": "his",
        "type": "circle",
        "quote": "bin bags",
        "note": "again?",
    }
    assert marks.parse_decision(_decision(quote="cat food"), line) is None
    assert marks.wants_to_mark(_decision()) and not marks.wants_to_mark(_decision(mark=False))
    assert marks.parse_decision(_decision(type="squiggle"), line) is None
    assert marks.parse_decision("not json", line) is None


def test_pacing_line_tells_her_when_she_last_marked():
    assert "never" in marks.pacing_line(None)
    assert "3 exchanges ago" in marks.pacing_line(3)


async def test_mark_is_saved_and_shows_in_history(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "add oat milk and bin bags")
    save_message(config.db_path, "dashboard", "assistant", "Done.")
    seen = {}

    async def fake_chat(messages, **kw):
        seen.setdefault("prompt", messages[-1]["content"])
        return local_llm.LocalReply(text=_decision())

    monkeypatch.setattr(marks.local_llm, "chat", fake_chat)
    mark = await marks.maybe_mark(config, "dashboard")
    assert mark is not None and mark["quote"] == "bin bags"
    assert "never marked" in seen["prompt"] and "rare" in seen["prompt"]
    page = history_page(config.db_path, "dashboard")
    assert page[0]["marks"][0]["type"] == "circle"
    assert page[1]["marks"] == []


async def test_no_second_mark_on_the_same_line_and_she_is_told_how_long_ago(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "add oat milk and bin bags")
    prompts = []

    async def fake_chat(messages, **kw):
        if kw["json_schema"] is marks.WANT_SCHEMA:
            prompts.append(messages[-1]["content"])
        return local_llm.LocalReply(text=_decision())

    monkeypatch.setattr(marks.local_llm, "chat", fake_chat)
    await marks.maybe_mark(config, "dashboard")
    assert await marks.maybe_mark(config, "dashboard") is None  # same line
    save_message(config.db_path, "dashboard", "user", "more bin bags please")
    await marks.maybe_mark(config, "dashboard")
    assert "just before this one" in prompts[-1]


def test_parse_allows_her_own_line_long_quotes_and_strikes():
    lines = {
        "his": "add oat milk and bin bags",
        "mine": "Added. I'm not going to remind you about the bin bags a third time, I promise.",
    }
    struck = marks.parse_decision(
        _decision(line="mine", type="strike-through", quote="I promise", note=""), lines
    )
    assert struck == {"line": "mine", "type": "strike-through", "quote": "I promise", "note": ""}
    sentence = "I'm not going to remind you about the bin bags a third time"
    assert marks.parse_decision(_decision(line="mine", type="underline", quote=sentence), lines)
    # a real quote filed under the wrong line goes to the line it's in
    moved = marks.parse_decision(_decision(line="his", quote="I promise"), lines)
    assert moved is not None and moved["line"] == "mine"
    assert marks.parse_decision(_decision(quote="cat food"), lines) is None


def test_long_quotes_cant_be_circled():
    line = {"his": "remind me to call the dentist tomorrow before lunch please"}
    long = "call the dentist tomorrow before lunch"
    assert marks.parse_decision(_decision(quote=long), line) is None
    assert marks.parse_decision(_decision(quote=long, type="underline"), line)


async def test_she_can_mark_her_own_reply(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "add oat milk and bin bags")
    save_message(config.db_path, "dashboard", "assistant", "Done, bin bags again.")
    seen = {}

    async def fake_chat(messages, **kw):
        seen.setdefault("prompt", messages[-1]["content"])
        return local_llm.LocalReply(text=_decision(line="mine", quote="again", type="underline"))

    monkeypatch.setattr(marks.local_llm, "chat", fake_chat)
    assert await marks.maybe_mark(config, "dashboard") is not None
    assert "Your answer: Done, bin bags again." in seen["prompt"]
    page = history_page(config.db_path, "dashboard")
    assert page[0]["marks"] == [] and page[1]["marks"][0]["quote"] == "again"
    # one mark per exchange: a mark on her reply also closes it
    assert await marks.maybe_mark(config, "dashboard") is None


async def test_no_means_no_second_question(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "good morning")
    calls = []

    async def fake_chat(messages, **kw):
        calls.append(kw["json_schema"])
        return local_llm.LocalReply(text='{"mark": false}')

    monkeypatch.setattr(marks.local_llm, "chat", fake_chat)
    assert await marks.maybe_mark(config, "dashboard") is None
    assert calls == [marks.WANT_SCHEMA]


async def test_felt_llm_down_means_no_mark(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "hi")

    async def down(*a, **kw):
        raise local_llm.LocalLLMUnavailable("down")

    monkeypatch.setattr(marks.local_llm, "chat", down)
    assert await marks.maybe_mark(config, "dashboard") is None
