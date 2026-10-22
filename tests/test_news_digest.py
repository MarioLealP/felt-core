"""news_digest (2026-10-22): Mario's news, read from his HA news sensors."""

from datetime import datetime

import httpx
import pytest
from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.ha_calendar import TZ
from felt_core.plugin import ExecutionContext

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=TZ)
STORY = {
    "title": "Metro do Porto abre nova linha",
    "source": "JN",
    "published": datetime(2026, 10, 22, 9, 30, tzinfo=TZ).timestamp(),
    "digest": "A linha Rosa abre em dezembro.",
}


@pytest.fixture
def skill():
    return load_plugin_module("news_digest/skill.py")


@pytest.fixture
def ha(monkeypatch, skill):
    """Fake HA: `ha.sensors` (topic -> stories) answers /api/states/sensor.news_<topic>."""

    class HA:
        sensors: dict[str, list] = {}
        asked: list[str] = []

    def handler(request):
        topic = request.url.path.rsplit("/", 1)[1].removeprefix("sensor.news_")
        HA.asked.append(topic)
        if topic not in HA.sensors:
            return httpx.Response(404)
        return httpx.Response(200, json={"attributes": {"stories": HA.sensors[topic]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        skill.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    HA.sensors, HA.asked = {}, []
    return HA


def _ctx(tmp_path, message=""):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), ha_url="http://ha", ha_token="t")
    return ExecutionContext(config=config, original_message=message)


def test_when(skill):
    assert skill.when(STORY["published"], NOW) == "today 09:30"
    last_night = datetime(2026, 10, 21, 20, 5, tzinfo=TZ).timestamp()
    assert skill.when(last_night, NOW) == "yesterday 20:05"
    monday = datetime(2026, 10, 19, 8, 0, tzinfo=TZ).timestamp()
    assert skill.when(monday, NOW) == "Monday 19 October"
    assert skill.when(None, NOW) == ""


def test_topic_lines(skill):
    assert skill.topic_lines("Greater Porto", [STORY], NOW) == [
        "Greater Porto:",
        "- Metro do Porto abre nova linha (JN, today 09:30): A linha Rosa abre em dezembro.",
    ]
    assert skill.topic_lines("Tech", [], NOW) == ["Tech: nothing new."]
    # no digest yet: the feed's own summary
    story = {"title": "T", "source": "BBC", "summary": "lead"}
    assert skill.topic_lines("World", [story], NOW)[1] == "- T (BBC): lead"


async def test_one_topic(skill, ha, tmp_path):
    ha.sensors = {"porto": [STORY], "world": []}
    text = (await skill.handle({"topic": "porto"}, _ctx(tmp_path))).text
    assert ha.asked == ["porto"]
    assert "Metro do Porto abre nova linha" in text and "World" not in text


async def test_all_topics_skip_missing_sensors(skill, ha, tmp_path):
    ha.sensors = {"porto": [STORY, {**STORY, "title": "Second story"}], "world": []}
    text = (await skill.handle({}, _ctx(tmp_path))).text
    assert "Second story" not in text  # all topics: the top story of each
    assert ha.asked == list(skill.TOPICS)
    assert "Greater Porto:" in text and "World: nothing new." in text and "Tech" not in text


async def test_no_sensors_at_all(skill, ha, tmp_path):
    assert "empty" in (await skill.handle({}, _ctx(tmp_path))).text


async def test_ha_not_connected(skill, tmp_path):
    ctx = ExecutionContext(config=FeltConfig(db_path=str(tmp_path / "f.db")), original_message="")
    assert "isn't connected" in (await skill.handle({}, ctx)).text


def test_reply_language(skill):
    assert skill.reply_language("what's the news today?") == "English"
    assert skill.reply_language("anything new in Porto?") == "English"
    assert skill.reply_language("há novidades no Porto?") == "Portuguese from Portugal"
    assert skill.reply_language("o que se passa hoje") == "Portuguese from Portugal"


async def test_the_language_comes_first(skill, ha, tmp_path):
    ha.sensors = {"porto": [STORY]}
    text = (await skill.handle({"topic": "porto"}, _ctx(tmp_path, "anything new in Porto?"))).text
    assert text.startswith("(Answer Mario in English - translate the stories")
    assert "quick round" not in text
    assert "Today is " in text
