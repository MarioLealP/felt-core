"""WP-47: smart_home_control finds devices by name or kind and only touches the allowlist."""

import importlib.util
import json
from pathlib import Path

import httpx

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_spec = importlib.util.spec_from_file_location(
    "smart_home_control_skill", Path(__file__).parents[1] / "plugins/smart_home_control/skill.py"
)
assert _spec is not None and _spec.loader is not None
skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill)

# Mario's real devices (2026-10-17), as Home Assistant names them
STATES = {
    "switch.shelf": ("Shelf", "on"),
    "switch.extraplug": ("ExtraPlug", "off"),
    "light.chandelure": ("Chandelure", "on"),
    "light.front_left": ("FrontLeft", "on"),
    "light.back_right": ("BackRight", "off"),
    "fan.core_300s_series": ("Core 300S Series", "on"),
    "lock.front_door": ("Front Door", "locked"),  # in HA, NOT on the allowlist
}
ALLOWED = [e for e in STATES if not e.startswith("lock.")]


def _mock(monkeypatch, calls):
    def handler(request):
        path = request.url.path
        if path.startswith("/api/states/"):
            eid = path.removeprefix("/api/states/")
            name, state = STATES[eid]
            return httpx.Response(200, json={"state": state, "attributes": {"friendly_name": name}})
        if path.startswith("/api/services/"):
            calls.append((path, json.loads(request.content)))
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(skill.httpx, "AsyncClient", lambda **kw: orig(transport=transport, **kw))


def _ctx():
    config = FeltConfig(ha_url="http://ha", ha_token="t", ha_entities=",".join(ALLOWED))
    return ExecutionContext(config=config)


async def _ask(monkeypatch, device, action=None, calls=None):
    _mock(monkeypatch, calls if calls is not None else [])
    params = {"device": device} | ({"action": action} if action else {})
    return (await skill.handle(params, _ctx())).text


async def test_the_fan_by_kind(monkeypatch):
    assert await _ask(monkeypatch, "the fan") == "Core 300S Series is currently on."


async def test_spaced_name_matches_joined_ha_name(monkeypatch):
    calls = []
    assert await _ask(monkeypatch, "front left", "off", calls) == "FrontLeft turned off."
    assert calls == [("/api/services/light/turn_off", {"entity_id": ["light.front_left"]})]


async def test_the_lights_means_all_of_them(monkeypatch):
    calls = []
    assert await _ask(monkeypatch, "the lights", "off", calls) == "all 3 turned off."
    assert calls == [
        (
            "/api/services/light/turn_off",
            {"entity_id": ["light.chandelure", "light.front_left", "light.back_right"]},
        )
    ]


async def test_group_status_names_the_ones_on(monkeypatch):
    assert await _ask(monkeypatch, "lights") == "2 of 3 are on: Chandelure, FrontLeft."


async def test_one_light_is_ambiguous(monkeypatch):
    text = await _ask(monkeypatch, "light", "on")
    assert text.startswith("Multiple devices match 'light'") and text.endswith("Which one?")


async def test_name_beside_a_kind_word(monkeypatch):
    assert await _ask(monkeypatch, "shelf plug") == "Shelf is currently on."


async def test_off_the_allowlist_is_refused(monkeypatch):
    calls = []
    assert await _ask(monkeypatch, "front door", "on", calls) == (
        "I don't see a device matching 'front door'."
    )
    assert calls == []


async def test_unknown_device(monkeypatch):
    assert await _ask(monkeypatch, "bedroom tv") == "I don't see a device matching 'bedroom tv'."
