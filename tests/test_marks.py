"""WP-42.2: her marks on Mario's lines."""

import json

import felt_core.marks as marks
from felt_core import local_llm
from felt_core.app_history import history_page
from felt_core.config import FeltConfig
from felt_core.db import save_message


def _config(tmp_path) -> FeltConfig:
    return FeltConfig(db_path=str(tmp_path / "felt.db"), local_llm_url="http://felt-llm")


def _decision(**kw) -> str:
    return json.dumps({"mark": True, "type": "circle", "quote": "bin bags", "note": "again?"} | kw)


def test_parse_keeps_only_real_quotes():
    line = "add oat milk and bin bags"
    assert marks.parse_decision(_decision(), line) == {
        "type": "circle",
        "quote": "bin bags",
        "note": "again?",
    }
    assert marks.parse_decision(_decision(quote="cat food"), line) is None
    assert marks.parse_decision(_decision(mark=False), line) is None
    assert marks.parse_decision(_decision(type="squiggle"), line) is None
    assert marks.parse_decision("not json", line) is None


def test_pacing_line_tells_her_when_she_last_marked():
    assert "never" in marks.pacing_line(None)
    assert "3 of his lines ago" in marks.pacing_line(3)


async def test_mark_is_saved_and_shows_in_history(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "add oat milk and bin bags")
    save_message(config.db_path, "dashboard", "assistant", "Done.")
    seen = {}

    async def fake_chat(messages, **kw):
        seen["prompt"] = messages[-1]["content"]
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
        prompts.append(messages[-1]["content"])
        return local_llm.LocalReply(text=_decision())

    monkeypatch.setattr(marks.local_llm, "chat", fake_chat)
    await marks.maybe_mark(config, "dashboard")
    assert await marks.maybe_mark(config, "dashboard") is None  # same line
    save_message(config.db_path, "dashboard", "user", "more bin bags please")
    await marks.maybe_mark(config, "dashboard")
    assert "1 of his lines ago" in prompts[-1]


async def test_felt_llm_down_means_no_mark(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "hi")

    async def down(*a, **kw):
        raise local_llm.LocalLLMUnavailable("down")

    monkeypatch.setattr(marks.local_llm, "chat", down)
    assert await marks.maybe_mark(config, "dashboard") is None
