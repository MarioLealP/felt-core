"""The morning brief (WP-45, cm-axv): once a day after 05:00, today + weather +
news + the overnight server, in her words, desk and phone."""

from datetime import UTC, datetime

import pytest

from felt_core import brief, db, features, local_llm, phone
from felt_core.config import FeltConfig
from felt_core.incidents import _connect, _ensure
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill

MORNING = datetime(2026, 10, 23, 7, 30, tzinfo=UTC)  # 08:30 in Lisbon (WEST)


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


def _skill(name, text, calls):
    async def handle(params, ctx):
        calls.append((name, params))
        if text is None:
            raise RuntimeError("down")
        return SkillResult(text=text)

    return LoadedSkill(name=name, description=name, parameters_schema={}, handle=handle)


@pytest.fixture
def world(monkeypatch):
    """Her skills, the model and the phone, faked; `world.said` = desk lines."""

    class World:
        calls: list = []
        said: list = []
        phone: list = []
        prompts: list = []
        reply = "Morning! Dentist at 3, 18 and cloudy, and Porto's metro has news."
        texts = {
            "list_calendar_events": "Dentist at 15:00.",
            "get_weather": "Cloudy, 18 degrees.",
            "news_digest": "Greater Porto:\n- Metro do Porto abre nova linha (JN)",
        }

    World.calls, World.said, World.phone, World.prompts = [], [], [], []
    monkeypatch.setattr(
        brief,
        "load_plugins",
        lambda d: {n: _skill(n, t, World.calls) for n, t in World.texts.items()},
    )

    async def reply(messages, config, **kw):
        World.prompts.append(messages[-1]["content"])
        if World.reply is None:
            raise local_llm.LocalLLMUnavailable("down")
        return World.reply

    monkeypatch.setattr("felt_core.brain.replies.local_reply", reply)
    monkeypatch.setattr(
        brief, "enqueue_proactive", lambda config, kind, text, **kw: World.said.append(text)
    )

    async def notify(config, title, message, tag):
        World.phone.append(message)

    monkeypatch.setattr(phone, "notify", notify)
    return World


def test_due():
    assert brief.due(MORNING, None) == "2026-10-23"
    assert brief.due(MORNING, "2026-10-23") is None  # given already today
    assert brief.due(datetime(2026, 10, 23, 3, 0, tzinfo=UTC), None) is None  # 04:00 Lisbon


async def test_the_brief_goes_out_once(tmp_path, world):
    config = _config(tmp_path)
    assert await brief.maybe_brief(config, MORNING) == world.reply
    assert world.said == [world.reply] and world.phone == [world.reply]
    facts = world.prompts[0]
    assert "Dentist at 15:00" in facts and "Cloudy" in facts and "Metro do Porto" in facts
    assert await brief.maybe_brief(config, MORNING) is None  # once a day
    assert len(world.said) == 1


async def test_a_part_that_fails_is_left_out(tmp_path, world):
    world.texts = {**world.texts, "get_weather": None}
    await brief.maybe_brief(_config(tmp_path), MORNING)
    assert "Weather" not in world.prompts[0] and "Dentist" in world.prompts[0]


async def test_felt_llm_down_says_the_facts(tmp_path, world):
    world.reply = None
    line = await brief.maybe_brief(_config(tmp_path), MORNING)
    assert line is not None
    assert line.startswith("Good morning. Here's today:") and "Dentist at 15:00" in line


async def test_the_diary_greeting_opens_it(tmp_path, world):
    config = _config(tmp_path)
    db.set_setting(config.db_path, brief.GREETING_KEY, "Slept well? You were up late.")
    await brief.maybe_brief(config, MORNING)
    assert 'Open with this morning thought of yours: "Slept well?' in world.prompts[0]
    assert db.get_setting(config.db_path, brief.GREETING_KEY) == ""


async def test_switched_off(tmp_path, world):
    config = _config(tmp_path)
    features.set_enabled(config.db_path, brief.FEATURE, False)
    assert not brief.will_open_with(config, MORNING)
    assert await brief.maybe_brief(config, MORNING) is None and world.said == []


def test_overnight(tmp_path):
    config = _config(tmp_path)
    assert brief.overnight(config.db_path, MORNING) == ""  # a quiet night
    conn = _connect(config.db_path)
    _ensure(conn)
    rows = [
        ("Backup failed", "open", "2026-10-23 02:00:00"),
        ("Authentik outpost down", "resolved", "2026-10-23 01:16:55"),
        ("Old thing", "resolved", "2026-10-21 10:00:00"),  # before last night
    ]
    for title, state, first in rows:
        conn.execute(
            "INSERT INTO incidents (type, key, title, tier, handler, state, first_at, last_at)"
            " VALUES ('t', ?, ?, 'warn', 'felt', ?, ?, ?)",
            (title, title, state, first, first),
        )
    conn.commit()
    conn.close()
    text = brief.overnight(config.db_path, MORNING)
    assert text.startswith("Still wrong: Backup failed. 1 alert(s) overnight")
