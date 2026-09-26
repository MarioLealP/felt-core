"""2026-09-24 audit fixes to the local (Ollama) reply paths: every local
call goes through brain._local_reply (personality prompt + length cap),
and the local screen path's "recent conversation" excludes her own
earlier screen remarks."""

import felt_core.brain as brain
from felt_core.config import FeltConfig
from felt_core.db import (
    get_history,
    save_fact,
    save_message,
    save_observation,
    set_observation_reply,
)


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


async def test_local_skill_phrasing_uses_the_shared_local_helper(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_fact(config.db_path, "likes pizza")
    captured = {}

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "local reply"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    reply = await brain._phrase_skill_result(
        "find my notes", "found 3 notes", [], "local", config, tone="tired"
    )

    assert reply == "local reply"
    assert captured["messages"][0]["role"] == "system"
    assert brain._PERSONALITY_PROMPT in captured["messages"][0]["content"]
    assert "tired" in captured["messages"][0]["content"]
    assert any(
        "likes pizza" in m["content"] for m in captured["messages"]
    )  # local path keeps facts
    assert captured["kwargs"]["max_tokens"] == brain._REPLY_MAX_TOKENS


def test_get_history_exclude_remarks_drops_spoken_screen_reactions(tmp_path):
    db = str(tmp_path / "felt.db")
    save_message(db, "s", "user", "hey felt")
    save_message(db, "s", "assistant", "hey mario")
    obs = save_observation(db, "s", "a spreadsheet")
    set_observation_reply(db, obs, "spreadsheets again?")
    save_message(db, "s", "assistant", "spreadsheets again?")

    assert [m["content"] for m in get_history(db, "s")] == [
        "hey felt",
        "hey mario",
        "spreadsheets again?",
    ]
    assert [m["content"] for m in get_history(db, "s", exclude_remarks=True)] == [
        "hey felt",
        "hey mario",
    ]


async def test_local_screen_path_history_excludes_her_own_screen_remarks(tmp_path, monkeypatch):
    config = _config(tmp_path, screen_reaction_cooldown_s=0)
    save_message(config.db_path, "s", "user", "real question")
    save_message(config.db_path, "s", "assistant", "real answer")
    obs = save_observation(config.db_path, "s", "earlier thing")
    set_observation_reply(config.db_path, obs, "old screen remark")
    save_message(config.db_path, "s", "assistant", "old screen remark")
    captured = {}

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        captured["messages"] = messages
        return "new remark"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    await brain.process_screen_context("new thing", config, session_id="s")

    contents = [m["content"] for m in captured["messages"]]
    assert "real question" in contents and "real answer" in contents
    assert "old screen remark" not in contents


async def test_local_email_summary_has_personality_and_a_token_cap(tmp_path, monkeypatch):
    from felt_core.db import get_unresolved_pending_email, save_pending_email

    config = _config(tmp_path)  # no Gemini key -> local path
    save_pending_email(config.db_path, "uid-1", "Ana", "Dinner")
    captured = {}

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "Ana wants dinner Friday."

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)
    monkeypatch.setattr(brain, "fetch_email_body", lambda cfg, uid: "Dinner on Friday?")

    summary = await brain._summarize_pending_email(config, "s", "yes")

    assert summary == "Ana wants dinner Friday."
    assert captured["messages"][0]["role"] == "system"
    assert brain._PERSONALITY_PROMPT in captured["messages"][0]["content"]
    assert captured["kwargs"]["max_tokens"] == brain._REPLY_MAX_TOKENS
    assert get_unresolved_pending_email(config.db_path) is None
