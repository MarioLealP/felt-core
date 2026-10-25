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
# 2026-10-19: the Shelf plug shown as a light (HA's switch_as_x) - it switches, can't dim
ONOFF = {"light.shelf"}
# 2026-10-25: the Chandelure and the Yeelights take colours (HA: color_temp, hs, rgb)
COLOUR = {"light.chandelure", "light.front_left"}
DIM_STATES = {k: v for k, v in STATES.items() if k != "switch.shelf"} | {
    "light.shelf": ("Shelf", "on")
}


def _mock(monkeypatch, calls, states=STATES):
    def handler(request):
        path = request.url.path
        if path.startswith("/api/states/"):
            eid = path.removeprefix("/api/states/")
            name, state = states[eid]
            attributes = {"friendly_name": name}
            if eid.startswith("light."):
                attributes["supported_color_modes"] = (
                    ["onoff"]
                    if eid in ONOFF
                    else ["color_temp", "hs", "rgb"]
                    if eid in COLOUR
                    else ["brightness"]
                )
            return httpx.Response(200, json={"state": state, "attributes": attributes})
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


async def _dim(monkeypatch, device, calls, **params):
    _mock(monkeypatch, calls, DIM_STATES)
    allowed = ",".join(e for e in DIM_STATES if not e.startswith("lock."))
    config = FeltConfig(ha_url="http://ha", ha_token="t", ha_entities=allowed)
    return (await skill.handle({"device": device, **params}, ExecutionContext(config=config))).text


async def test_dim_without_a_number_uses_the_usual_level(monkeypatch):
    calls = []
    text = await _dim(monkeypatch, "chandelure", calls, action="dim")
    assert calls == [
        ("/api/services/light/turn_on", {"entity_id": ["light.chandelure"], "brightness_pct": 30})
    ]
    assert text == "Chandelure dimmed to 30%."


async def test_dim_to_the_number_he_said(monkeypatch):
    # 2026-10-19 live: "chandelure to 30 percente please" - HA's matcher missed the typo
    calls = []
    await _dim(monkeypatch, "chandelure", calls, action="dim", brightness=45)
    assert calls[0][1]["brightness_pct"] == 45


async def test_a_brightness_alone_means_dim(monkeypatch):
    calls = []
    await _dim(monkeypatch, "chandelure", calls, brightness=200)
    assert calls[0][1]["brightness_pct"] == 100


async def test_dim_the_lights_skips_the_ones_that_cant(monkeypatch):
    calls = []
    text = await _dim(monkeypatch, "the lights", calls, action="dim")
    [(path, body)] = calls
    assert path == "/api/services/light/turn_on" and "light.shelf" not in body["entity_id"]
    assert len(body["entity_id"]) == 3 and "Shelf can't dim" in text


async def test_a_plug_cant_dim(monkeypatch):
    calls = []
    text = await _dim(monkeypatch, "shelf", calls, action="dim")
    assert calls == [] and "can't be dimmed" in text


# --- colour (cm-7gf) ---


async def test_a_colour_by_name(monkeypatch):
    calls = []
    text = await _dim(monkeypatch, "chandelure", calls, color="Blue")
    assert calls == [
        ("/api/services/light/turn_on", {"entity_id": ["light.chandelure"], "color_name": "blue"})
    ]
    assert text == "Chandelure set to blue."


async def test_a_two_word_colour_is_one_ha_name(monkeypatch):
    calls = []
    await _dim(monkeypatch, "chandelure", calls, color="light blue")
    assert calls[0][1]["color_name"] == "lightblue"


async def test_warm_white_is_a_colour_temperature(monkeypatch):
    calls = []
    await _dim(monkeypatch, "chandelure", calls, color="warm white")
    assert calls[0][1] == {"entity_id": ["light.chandelure"], "color_temp_kelvin": 2700}


async def test_a_colour_with_a_brightness(monkeypatch):
    calls = []
    await _dim(monkeypatch, "chandelure", calls, color="red", brightness=40)
    assert calls[0][1] == {
        "entity_id": ["light.chandelure"],
        "color_name": "red",
        "brightness_pct": 40,
    }


async def test_the_lights_colour_skips_the_ones_that_cant(monkeypatch):
    calls = []
    text = await _dim(monkeypatch, "the lights", calls, color="blue", action="on")
    [(_, body)] = calls
    assert sorted(body["entity_id"]) == ["light.chandelure", "light.front_left"]
    assert text.startswith("2 lights set to blue") and "Shelf" in text


async def test_a_light_that_only_dims_cant_change_colour(monkeypatch):
    calls = []
    text = await _dim(monkeypatch, "backright", calls, color="blue")
    assert calls == [] and "can't change colour" in text


async def test_an_unknown_colour_says_so(monkeypatch):
    calls = []
    # HA itself answers 200 to an unknown name (checked live 2026-10-25)
    text = await _dim(monkeypatch, "chandelure", calls, color="sparkly")
    assert calls == [] and text == "Home Assistant doesn't know the colour 'sparkly'."
