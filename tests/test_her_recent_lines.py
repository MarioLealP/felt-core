"""Mario 10-03: her chat context is the conversation only, plus a note of
the lines she said on her own recently, so she knows them and repeats
herself less (remarks, events and pushes all see the same list)."""

from felt_core.brain import orchestrator
from felt_core.db.kinds import ALARM, APP_SESSION, EMAIL, EVENT, PROACTIVE, REMARK
from felt_core.db.messages import get_her_recent_lines, save_message
from felt_core.db.session import _connect


async def _say(db, content, kind, session=APP_SESSION, role="assistant"):
    return await save_message(db, session, role, content, kind=kind)


async def test_her_recent_lines_are_her_unprompted_lines_oldest_first(tmp_path):
    db = str(tmp_path / "f.db")
    await _say(db, "hi", "chat", role="user")
    await _say(db, "hey", "chat")
    await _say(db, "Nice loot.", REMARK)
    await _say(db, "Good song.", EVENT)
    await _say(db, "Morning!", PROACTIVE)
    await _say(db, "Mail from Ana.", EMAIL)
    await _say(db, "Wake up", ALARM)
    await _say(db, "elsewhere", REMARK, session="other")
    lines = [r["content"] for r in await get_her_recent_lines(db, APP_SESSION)]
    assert lines == ["Nice loot.", "Good song.", "Morning!", "Mail from Ana."]
    assert [r["content"] for r in await get_her_recent_lines(db, APP_SESSION, limit=2)] == [
        "Morning!",
        "Mail from Ana.",
    ]


async def test_old_lines_are_left_out(tmp_path):
    db = str(tmp_path / "f.db")
    old = await _say(db, "Yesterday's remark.", REMARK)
    await _say(db, "Fresh one.", REMARK)
    conn = _connect(db)
    with conn:
        conn.execute("UPDATE messages SET created_at = datetime('now', '-7 hours') WHERE id = ?", (old,))
    conn.close()
    assert [r["content"] for r in await get_her_recent_lines(db, APP_SESSION)] == ["Fresh one."]


async def test_chat_context_is_the_conversation_plus_a_note_of_her_lines(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    await _say(db, "what are you up to", "chat", role="user")
    await _say(db, "not much", "chat")
    for i in range(30):
        await _say(db, f"remark {i}", REMARK)
    seen = {}

    async def fake_local(message, config, session_id, tone, registry, history, speaker=None):
        seen["history"] = history
        return orchestrator.ChatResult(reply="ok")

    class Config:
        db_path = db
        plugins_dir = str(tmp_path / "plugins")

    monkeypatch.setattr(orchestrator, "_chat_local", fake_local)
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: None)
    await orchestrator.chat("hey", Config(), APP_SESSION)  # ty: ignore[invalid-argument-type]
    history = seen["history"]
    assert history[:2] == [
        {"role": "user", "content": "what are you up to"},
        {"role": "assistant", "content": "not much"},
    ]
    note = history[2]
    assert note["role"] == "system"
    assert len(history) == 3
    listed = [line for line in note["content"].splitlines() if line.startswith("- ")]
    assert listed == [f"- remark {i}" for i in range(24, 30)]
