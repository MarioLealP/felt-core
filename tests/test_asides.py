"""WP-42.2: her asides in Talk - the remarks since his last message she most
wants answered."""

import asyncio

from fastapi.testclient import TestClient

import felt_core.app_api as app_api
import felt_core.asides as asides
from felt_core import features, local_llm
from felt_core.api import app
from felt_core.app_history import history_page
from felt_core.brain import orchestrator
from felt_core.config import FeltConfig
from felt_core.db import save_event_reaction, save_message, save_observation, set_observation_reply

SSO = {"X-Authentik-Username": "mario"}


def _config(tmp_path) -> FeltConfig:
    return FeltConfig(db_path=str(tmp_path / "felt.db"), local_llm_url="http://felt-llm")


def _remark(db: str, line: str, session: str = "dashboard") -> None:
    """A screen remark, stored the way screen_reactions.py stores one."""
    obs = save_observation(db, session, "on screen: something")
    set_observation_reply(db, obs, line)
    save_message(db, session, "assistant", line, kind="remark")


def _ids(db: str) -> dict[str, int]:
    return {m["content"]: m["id"] for m in _all(db)}


def _all(db: str) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id, content FROM messages").fetchall()
    conn.close()
    return [{"id": i, "content": c} for i, c in rows]


def test_only_remarks_since_his_last_line_and_the_newest_two_by_default(tmp_path):
    db = str(tmp_path / "felt.db")
    _remark(db, "old remark")
    save_message(db, "dashboard", "user", "hi")
    save_message(db, "dashboard", "assistant", "Hey.")  # a reply, not a remark
    assert asides.current_asides(db) == {"asides": [], "more": 0}
    for i in range(5):
        _remark(db, f"remark {i}")
    save_event_reaction(db, "dashboard", "song", None, "good song")
    save_message(db, "dashboard", "assistant", "good song", kind="event")
    _remark(db, "elsewhere", session="other")
    got = asides.current_asides(db)
    assert [a["content"] for a in got["asides"]] == ["remark 4", "good song"]
    assert got["more"] == 4  # remark 0-3
    assert got["asides"][0]["created_at"].endswith("Z")


def test_asides_older_than_six_hours_stop_waiting(tmp_path):
    import sqlite3

    db = str(tmp_path / "felt.db")
    _remark(db, "last night")
    _remark(db, "this morning")
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "UPDATE messages SET created_at = datetime('now', '-7 hours') WHERE content = ?",
            ("last night",),
        )
    conn.close()
    got = asides.current_asides(db)
    assert [a["content"] for a in got["asides"]] == ["this morning"] and got["more"] == 0
    assert "last night" in [m["content"] for m in _all(db)]  # still stored


def test_parse_picks_keeps_valid_distinct_positions():
    assert asides.parse_picks('{"picks": [3, 1]}', 5) == [3, 1]
    assert asides.parse_picks('{"picks": [2, 2, 9, 0]}', 5) == [2]
    assert asides.parse_picks('{"picks": [1, 2, 3]}', 5) == [1, 2]
    assert asides.parse_picks("nope", 5) == []
    assert asides.parse_picks('{"picks": "1"}', 5) == []


async def test_her_pick_is_stored_and_shown(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_message(config.db_path, "dashboard", "user", "brb")
    for line in ["nice track", "I have standards, Mario.", "someone's here", "more notes?"]:
        _remark(config.db_path, line)
    seen = {}

    async def fake_chat(messages, **kw):
        seen["ask"] = messages[-1]["content"]
        return local_llm.LocalReply(text='{"picks": [2]}')

    monkeypatch.setattr(asides.local_llm, "chat", fake_chat)
    ids = await asides.pick(config)
    assert ids == [_ids(config.db_path)["I have standards, Mario."]]
    assert "2. (" in seen["ask"] and "I have standards, Mario." in seen["ask"]
    got = asides.current_asides(config.db_path)
    assert [a["content"] for a in got["asides"]] == ["I have standards, Mario."]
    assert got["more"] == 3
    # his next line starts a new batch: the old pick no longer applies
    save_message(config.db_path, "dashboard", "user", "back")
    assert asides.current_asides(config.db_path) == {"asides": [], "more": 0}
    # and the next pick clears the old one out of the table
    for line in ["a", "b", "c"]:
        _remark(config.db_path, line)
    await asides.pick(config)
    import sqlite3

    conn = sqlite3.connect(config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM aside_picks").fetchone()[0] == 1
    conn.close()


async def test_two_or_fewer_need_no_call(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _remark(config.db_path, "one")
    _remark(config.db_path, "two")

    async def boom(*a, **kw):
        raise AssertionError("no call expected")

    monkeypatch.setattr(asides.local_llm, "chat", boom)
    assert await asides.pick(config) == []
    assert len(asides.current_asides(config.db_path)["asides"]) == 2


async def test_a_remark_mid_pick_reruns_it_once(tmp_path, monkeypatch):
    config = _config(tmp_path)
    features.set_enabled(config.db_path, "ambient.asides", True)  # off by default
    started, release = asyncio.Event(), asyncio.Event()
    runs = []

    async def slow_pick(config, session_id):
        runs.append(session_id)
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(asides, "pick", slow_pick)
    asides.schedule_pick(config, "other")  # not the app's session: nothing
    asides.schedule_pick(config, "dashboard")
    await started.wait()
    asides.schedule_pick(config, "dashboard")
    asides.schedule_pick(config, "dashboard")
    release.set()
    while "dashboard" in asides._running:
        await asyncio.sleep(0)
    assert runs == ["dashboard", "dashboard"]


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", "s3cret")
    return TestClient(app)


def test_asides_route_is_sso_gated(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        _remark(str(tmp_path / "felt.db"), "psst")
        assert c.get("/v1/app/asides").status_code == 401
        got = c.get("/v1/app/asides", headers=SSO).json()
        assert [a["content"] for a in got["asides"]] == ["psst"] and got["more"] == 0


def test_answering_an_aside_tells_her_and_links_it(tmp_path, monkeypatch):
    db = str(tmp_path / "felt.db")
    seen = {}

    async def fake_chat(text, config, session_id, answering=None):
        seen["answering"] = answering
        save_message(config.db_path, session_id, "user", text)
        save_message(config.db_path, session_id, "assistant", "Good.")

    monkeypatch.setattr(app_api, "chat", fake_chat)
    with _client(tmp_path, monkeypatch) as c:
        _remark(db, "You're redesigning my entire world, aren't you?")
        remark_id = c.get("/v1/app/asides", headers=SSO).json()["asides"][0]["id"]
        r = c.post(
            "/v1/app/chat", json={"message": "a bit, yeah", "answering": remark_id}, headers=SSO
        )
        assert r.status_code == 200
        assert seen["answering"] == "You're redesigning my entire world, aren't you?"
        mine = r.json()["messages"][0]
        assert mine["answering"]["id"] == remark_id
        assert history_page(db, "dashboard")[0]["answering"]["content"].startswith("You're")
        # answered: his line closed the batch
        assert c.get("/v1/app/asides", headers=SSO).json()["asides"] == []
        # only her remarks can be answered this way
        reply_id = r.json()["messages"][1]["id"]
        bad = c.post("/v1/app/chat", json={"message": "x", "answering": reply_id}, headers=SSO)
        assert bad.status_code == 422


async def test_chat_puts_the_answered_remark_in_her_context(tmp_path, monkeypatch):
    config = _config(tmp_path)
    seen = {}

    async def fake_local(message, config, session_id, tone, registry, history, speaker=None):
        seen["history"] = history

    monkeypatch.setattr(orchestrator, "_chat_local", fake_local)
    await orchestrator.chat("a bit", config, "dashboard", answering="Too specific?")
    assert seen["history"][-1] == {
        "role": "system",
        "content": 'Mario is answering this remark you made earlier: "Too specific?"',
    }
