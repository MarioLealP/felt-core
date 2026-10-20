"""Mario 10-03: her chat context is the conversation only, plus a note of
the lines she said on her own recently, so she knows them and repeats
herself less (remarks, events and pushes all see the same list)."""

import asyncio

from felt_core.brain import orchestrator
from felt_core.db import (
    ALARM,
    APP_SESSION,
    EMAIL,
    EVENT,
    PROACTIVE,
    REMARK,
    _connect,
    get_her_recent_lines,
    save_message,
)


def _say(db, content, kind, session=APP_SESSION, role="assistant"):
    return save_message(db, session, role, content, kind=kind)


def test_her_recent_lines_are_her_unprompted_lines_oldest_first(tmp_path):
    db = str(tmp_path / "f.db")
    _say(db, "hi", "chat", role="user")
    _say(db, "hey", "chat")
    _say(db, "Nice loot.", REMARK)
    _say(db, "Good song.", EVENT)
    _say(db, "Morning!", PROACTIVE)
    _say(db, "Mail from Ana.", EMAIL)
    _say(db, "Wake up", ALARM)
    _say(db, "elsewhere", REMARK, session="other")
    lines = [r["content"] for r in get_her_recent_lines(db, APP_SESSION)]
    assert lines == ["Nice loot.", "Good song.", "Morning!", "Mail from Ana."]
    assert [r["content"] for r in get_her_recent_lines(db, APP_SESSION, limit=2)] == [
        "Morning!",
        "Mail from Ana.",
    ]


def test_old_lines_are_left_out(tmp_path):
    db = str(tmp_path / "f.db")
    old = _say(db, "Yesterday's remark.", REMARK)
    _say(db, "Fresh one.", REMARK)
    conn = _connect(db)
    with conn:
        conn.execute(
            "UPDATE messages SET created_at = datetime('now', '-7 hours') WHERE id = ?", (old,)
        )
    conn.close()
    assert [r["content"] for r in get_her_recent_lines(db, APP_SESSION)] == ["Fresh one."]


def test_chat_context_is_the_conversation_plus_a_note_of_her_lines(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    _say(db, "what are you up to", "chat", role="user")
    _say(db, "not much", "chat")
    for i in range(30):
        _say(db, f"remark {i}", REMARK)
    seen = {}

    async def fake_local(message, config, session_id, tone, registry, history, speaker=None):
        seen["history"] = history
        return orchestrator.ChatResult(reply="ok")

    class Config:
        db_path = db
        plugins_dir = str(tmp_path / "plugins")

    monkeypatch.setattr(orchestrator, "_chat_local", fake_local)
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: None)
    asyncio.run(orchestrator.chat("hey", Config(), APP_SESSION))  # ty: ignore[invalid-argument-type]
    history = seen["history"]
    assert history[:2] == [
        {"role": "user", "content": "what are you up to"},
        {"role": "assistant", "content": "not much"},
    ]
    note = history[2]
    assert note["role"] == "system" and len(history) == 3
    listed = [line for line in note["content"].splitlines() if line.startswith("- ")]
    assert listed == [f"- remark {i}" for i in range(24, 30)]
