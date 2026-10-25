"""ha_assist: Home Assistant's own matcher first for plain home/list commands
(2026-10-19, replaced home_fast_path)."""

import json

import httpx
import pytest

from felt_core import ha_assist
from felt_core.config import FeltConfig

CONFIG = FeltConfig(ha_url="http://ha", ha_token="t", ha_assist_device="dev-felt")


@pytest.mark.parametrize(
    ("message", "command"),
    [
        ("turn off the shelf light", "turn off the shelf light"),
        ("Felt, turn off the chandelure", "turn off the chandelure"),
        ("could you please turn the lights back off", "turn the lights back off"),
        ("turn on my shelf light please :)", "turn on my shelf light"),
        ("turn front left on, thanks!", "turn front left on"),
        ("can u switch the fan on?", "switch the fan on"),
        ("is the fan on?", "is the fan on"),
    ],
)
def test_clean_drops_the_pleasantries(message, command):
    assert ha_assist.clean(message) == command


def _ha(monkeypatch, answer, seen):
    def handler(request):
        seen.append(json.loads(request.content))
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json={"response": answer})

    real = httpx.AsyncClient

    def client(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(ha_assist.httpx, "AsyncClient", client)


def _speech(kind, text):
    return {"response_type": kind, "speech": {"plain": {"speech": text}}, "data": {}}


async def test_a_match_is_done_by_home_assistant(monkeypatch):
    seen = []
    _ha(monkeypatch, _speech("action_done", "Turned off the light"), seen)
    assert (
        await ha_assist.handle(CONFIG, "turn off the shelf light please") == "Turned off the light"
    )
    assert seen == [
        {
            "text": "turn off the shelf light",
            "language": "en",
            "agent_id": "conversation.home_assistant",
            "device_id": "dev-felt",  # her room: "lights off" = its lights
        }
    ]


async def test_a_question_is_answered(monkeypatch):
    _ha(monkeypatch, _speech("query_answer", "Yes"), [])
    assert await ha_assist.handle(CONFIG, "is the fan on?") == "Yes"


async def test_no_match_goes_to_her_model(monkeypatch):
    answer = _speech("error", "Sorry, I couldn't understand that")
    answer["data"] = {"code": "no_intent_match"}
    _ha(monkeypatch, answer, [])
    assert await ha_assist.handle(CONFIG, "how are you today?") is None


async def test_home_assistant_down_goes_to_her_model(monkeypatch):
    _ha(monkeypatch, httpx.ConnectError("down"), [])
    assert await ha_assist.handle(CONFIG, "lights off") is None


async def test_long_messages_and_no_home_assistant_are_not_asked(monkeypatch):
    seen = []
    _ha(monkeypatch, _speech("action_done", "x"), seen)
    story = "so yesterday I told my brother to turn off the lights and he just laughed at me"
    assert await ha_assist.handle(CONFIG, story) is None
    assert await ha_assist.handle(FeltConfig(ha_url=None), "lights off") is None
    assert seen == []


async def test_several_things_for_a_list_go_to_her_model(monkeypatch):
    # cm-fmy: HA added "eggs and bread" as one item
    seen = []
    _ha(monkeypatch, _speech("action_done", "Added eggs and bread"), seen)
    for message in ("put eggs and bread on my shopping list", "add eggs, bread to the list"):
        assert await ha_assist.handle(CONFIG, message) is None
    assert seen == []
    assert await ha_assist.handle(CONFIG, "add milk to the shopping list") == "Added eggs and bread"
