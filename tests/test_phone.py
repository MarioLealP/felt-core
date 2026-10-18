"""The phone through the Home Assistant app (2026-10-18): Clock timers and
alarms, find-my-phone, battery - and the fallbacks when it can't be
reached."""

import json

import httpx
import pytest
from conftest import load_plugin_module

from felt_core import phone
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext


def _config(tmp_path):
    return FeltConfig(
        db_path=str(tmp_path / "felt.db"), ha_url="http://ha", ha_token="t", phone="pocox7"
    )


@pytest.fixture
def ha(monkeypatch):
    """Fake HA: records each call; `ha.status` / `ha.states` steer it."""

    class HA:
        calls: list[tuple[str, str, dict | None]] = []
        status = 200
        states = {
            "sensor.pocox7_battery_level": "20",
            "sensor.pocox7_battery_state": "discharging",
            "sensor.pocox7_next_alarm": "2026-10-19T05:30:00+00:00",
        }

    def handler(request):
        body = None if request.method == "GET" else json.loads(request.content)
        HA.calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            entity = request.url.path.rsplit("/", 1)[1]
            if entity not in HA.states:
                return httpx.Response(404)
            return httpx.Response(200, json={"state": HA.states[entity]})
        return httpx.Response(HA.status, json=[])

    real = httpx.AsyncClient
    monkeypatch.setattr(
        phone.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    HA.calls = []
    return HA


def test_timer_extras():
    assert phone.timer_extras(600, "pasta, al dente: 9 min") == [
        "android.intent.extra.alarm.LENGTH:600:int",
        "android.intent.extra.alarm.SKIP_UI:true:boolean",
        "android.intent.extra.alarm.MESSAGE:pasta al dente 9 min",  # , and : split extras
    ]


def test_alarm_extras_without_label():
    assert phone.alarm_extras(7, 30) == [
        "android.intent.extra.alarm.HOUR:7:int",
        "android.intent.extra.alarm.MINUTES:30:int",
        "android.intent.extra.alarm.SKIP_UI:true:boolean",
    ]


async def test_start_timer_calls_the_companion_app(tmp_path, ha):
    await phone.start_timer(_config(tmp_path), 60, "Felt test")
    method, path, body = ha.calls[0]
    assert (method, path) == ("POST", "/api/services/notify/mobile_app_pocox7")
    assert body["message"] == "command_activity"
    assert body["data"]["intent_action"] == "android.intent.action.SET_TIMER"
    assert "LENGTH:60:int" in body["data"]["intent_extras"]


async def test_ha_refusing_is_phone_unavailable(tmp_path, ha):
    ha.status = 400  # e.g. the app re-registered under a new name
    with pytest.raises(phone.PhoneUnavailable):
        await phone.ring(_config(tmp_path))


async def test_unconfigured_is_phone_unavailable(tmp_path):
    with pytest.raises(phone.PhoneUnavailable):
        await phone.ring(FeltConfig(db_path=str(tmp_path / "f.db"), ha_url=None))


async def test_battery(tmp_path, ha):
    assert await phone.battery(_config(tmp_path)) == (20, False)
    ha.states["sensor.pocox7_battery_state"] = "charging"
    assert await phone.battery(_config(tmp_path)) == (20, True)


async def test_battery_while_the_phone_is_offline(tmp_path, ha):
    ha.states["sensor.pocox7_battery_level"] = "unavailable"
    with pytest.raises(phone.PhoneUnavailable):
        await phone.battery(_config(tmp_path))


# --- skills -------------------------------------------------------------


def _ctx(tmp_path, message=""):
    return ExecutionContext(config=_config(tmp_path), original_message=message)


async def test_timer_skill_sets_it_on_the_phone(tmp_path, ha):
    skill = load_plugin_module("set_timer/skill.py")
    result = await skill.handle({"minutes": 10, "label": "pasta"}, _ctx(tmp_path))
    assert "on your phone" in result.text
    assert "LENGTH:600:int" in ha.calls[0][2]["data"]["intent_extras"]


async def test_timer_skill_falls_back_to_a_push(tmp_path, ha, monkeypatch):
    skill = load_plugin_module("set_timer/skill.py")
    ha.status = 500
    started = []
    monkeypatch.setattr(
        skill.asyncio, "create_task", lambda coro: (started.append(1), coro.close())
    )
    result = await skill.handle({"minutes": 10}, _ctx(tmp_path))
    assert started == [1] and "ping you" in result.text


async def test_alarm_skill_sets_the_next_occurrence_on_the_phone(tmp_path, ha):
    skill = load_plugin_module("set_alarm/skill.py")
    result = await skill.handle({"time": "7:30am", "label": "gym"}, _ctx(tmp_path))
    assert "on your phone" in result.text
    extras = ha.calls[0][2]["data"]["intent_extras"]
    assert "HOUR:7:int" in extras and "MINUTES:30:int" in extras


async def test_alarm_skill_falls_back_when_the_phone_is_unreachable(tmp_path, ha, monkeypatch):
    skill = load_plugin_module("set_alarm/skill.py")
    ha.status = 500
    scheduled = []
    monkeypatch.setattr(skill, "schedule_alarm", lambda c, at, label: scheduled.append(at))
    result = await skill.handle({"time": "7:30am"}, _ctx(tmp_path))
    assert len(scheduled) == 1 and "on your phone" not in result.text


async def test_find_phone_rings_on_the_alarm_stream(tmp_path, ha):
    skill = load_plugin_module("find_phone/skill.py")
    result = await skill.handle({}, _ctx(tmp_path))
    assert result.text == "Ringing your phone."
    assert ha.calls[0][2]["data"]["channel"] == "alarm_stream"


async def test_phone_battery_skill(tmp_path, ha):
    skill = load_plugin_module("get_phone_battery/skill.py")
    result = await skill.handle({}, _ctx(tmp_path))
    assert result.text == "Phone battery: 20% (not charging)"


async def test_list_alarms_says_the_phones_next_one(tmp_path, ha):
    skill = load_plugin_module("list_alarms/skill.py")
    result = await skill.handle({}, _ctx(tmp_path))
    assert result.text == "Alarms: next on your phone: Monday 6:30 AM"  # Lisbon time


async def test_list_alarms_without_one(tmp_path, ha):
    skill = load_plugin_module("list_alarms/skill.py")
    ha.states["sensor.pocox7_next_alarm"] = "unavailable"
    result = await skill.handle({}, _ctx(tmp_path))
    assert result.text == "Alarms: none on your phone"
