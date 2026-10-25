"""The shopping list in Home Assistant's todo.shopping_list (2026-10-18): the
calls Felt makes, the skill on top, and what happens when HA can't be reached."""

import json

import httpx
import pytest
from conftest import load_plugin_module

from felt_core import shopping
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

MILK = "0e2d6f2e8b764d4ebcef36de5d99d0ab"
EGGS = "1f3e7a3f9c875e5fcdf047ef6eaae1bc"


def _config(tmp_path, **kw):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), ha_url="http://ha", ha_token="t", **kw)


@pytest.fixture
def ha(monkeypatch):
    """Fake HA: records each call; `ha.items` is the list, `ha.status` steers it."""

    class HA:
        calls: list[tuple[str, str, dict | None]] = []
        status = 200
        items: list[dict] = []

    def handler(request):
        body = None if request.method == "GET" else json.loads(request.content)
        HA.calls.append((request.method, request.url.path, body))
        if HA.status != 200:
            return httpx.Response(HA.status)
        if request.method == "GET":
            return httpx.Response(200, json={"state": "1", "last_updated": "t0"})
        if request.url.path.endswith("/get_items"):
            response = {"todo.shopping_list": {"items": HA.items}}
            return httpx.Response(200, json={"changed_states": [], "service_response": response})
        return httpx.Response(200, json=[])

    real = httpx.AsyncClient
    monkeypatch.setattr(
        shopping.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    HA.calls = []
    HA.items = [
        {"summary": "eggs", "uid": EGGS, "status": "completed"},
        {"summary": "milk", "uid": MILK, "status": "needs_action"},
    ]
    return HA


async def test_items_come_from_ha_unchecked_first(tmp_path, ha):
    assert await shopping.items(_config(tmp_path)) == [
        {"id": MILK, "text": "milk", "checked": False},
        {"id": EGGS, "text": "eggs", "checked": True},
    ]
    assert ha.calls == [
        (
            "POST",
            "/api/services/todo/get_items",
            {"entity_id": "todo.shopping_list"},
        )
    ]


async def test_each_change_is_one_todo_service_call(tmp_path, ha):
    config = _config(tmp_path)
    await shopping.add(config, "bread")
    await shopping.set_checked(config, MILK, True)
    await shopping.set_checked(config, MILK, False)
    await shopping.remove(config, EGGS)
    await shopping.clear_checked(config)
    entity = {"entity_id": "todo.shopping_list"}
    assert ha.calls == [
        ("POST", "/api/services/todo/add_item", {**entity, "item": "bread"}),
        ("POST", "/api/services/todo/update_item", {**entity, "item": MILK, "status": "completed"}),
        (
            "POST",
            "/api/services/todo/update_item",
            {**entity, "item": MILK, "status": "needs_action"},
        ),
        ("POST", "/api/services/todo/remove_item", {**entity, "item": EGGS}),
        ("POST", "/api/services/todo/remove_completed_items", entity),
    ]


async def test_unreachable_or_unconfigured_ha_is_one_error(tmp_path, ha):
    with pytest.raises(shopping.ShoppingUnavailable):
        await shopping.items(FeltConfig(db_path=str(tmp_path / "felt.db")))
    ha.status = 500
    with pytest.raises(shopping.ShoppingUnavailable):
        await shopping.add(_config(tmp_path), "bread")


async def test_version_follows_the_entity_and_is_empty_when_ha_is_down(tmp_path, ha):
    assert await shopping.version(_config(tmp_path)) == "1|t0"
    ha.status = 502
    assert await shopping.version(_config(tmp_path)) == ""


async def _say(tmp_path, params):
    skill = load_plugin_module("shopping_list/skill.py")
    return (await skill.handle(params, ExecutionContext(config=_config(tmp_path)))).text


async def test_skill_adds_reads_and_removes(tmp_path, ha):
    assert await _say(tmp_path, {"item": "bread"}) == "Added bread to the shopping list."
    assert ha.calls[-1][2] == {"entity_id": "todo.shopping_list", "item": "bread"}
    assert await _say(tmp_path, {}) == "Shopping list: milk"
    assert await _say(tmp_path, {"action": "remove", "item": "MILK"}) == (
        "Removed milk from the shopping list."
    )
    assert ha.calls[-1][1:] == (
        "/api/services/todo/remove_item",
        {"entity_id": "todo.shopping_list", "item": MILK},
    )
    assert await _say(tmp_path, {"action": "remove", "item": "cheese"}) == (
        "I don't see 'cheese' on the shopping list."
    )


async def test_skill_says_so_when_ha_is_down(tmp_path, ha):
    ha.status = 503
    assert await _say(tmp_path, {"item": "bread"}) == (
        "Couldn't reach the shopping list in Home Assistant right now."
    )


async def test_several_items_are_added_one_by_one(tmp_path, ha):
    # cm-fmy: "put eggs and bread on my shopping list"
    text = await _say(tmp_path, {"action": "add", "items": ["eggs", "bread"]})
    assert text == "Added eggs and bread to the shopping list."
    added = [c[2]["item"] for c in ha.calls if c[1] == "/api/services/todo/add_item"]
    assert added == ["eggs", "bread"]


async def test_items_and_a_lone_item_count_once(tmp_path, ha):
    text = await _say(tmp_path, {"items": ["Eggs", "milk"], "item": "eggs"})
    assert text == "Added Eggs and milk to the shopping list."


async def test_items_alone_is_not_mistaken_for_an_action(tmp_path, ha):
    text = await _say(tmp_path, {"items": ["eggs", "bread", "coffee"]})
    assert text == "Added eggs, bread and coffee to the shopping list."


async def test_several_removed(tmp_path, ha):
    text = await _say(tmp_path, {"action": "remove", "items": ["milk", "cheese"]})
    assert text == (
        "Removed milk from the shopping list. I don't see 'cheese' on the shopping list."
    )
