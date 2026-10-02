"""WP-37: board_status reads the Plane board and never writes to it."""

import importlib.util
from pathlib import Path

import httpx
import pytest

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_spec = importlib.util.spec_from_file_location(
    "board_status_skill", Path(__file__).parents[1] / "plugins/board_status/skill.py"
)
assert _spec is not None and _spec.loader is not None
skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill)

STATES = [
    {"id": "s-done", "name": "Done", "sequence": 90},
    {"id": "s-ready", "name": "Ready", "sequence": 40},
    {"id": "s-prog", "name": "In progress", "sequence": 50},
    {"id": "s-review", "name": "Needs review", "sequence": 60},
    {"id": "s-polish", "name": "Polish pile", "sequence": 10},
]
ISSUES = [
    {"name": "WP-47 Home Assistant", "state": "s-ready", "sort_order": 20},
    {"name": "WP-41 ntfy hygiene", "state": "s-ready", "sort_order": 10},
    {"name": "WP-37 The board", "state": "s-prog", "sort_order": 10},
    {"name": "WP-01 freeze fix", "state": "s-done", "sort_order": 10},
    {"name": "Walk cycle", "state": "s-polish", "sort_order": 10},
]


def _page(results):
    return {"results": results, "next_page_results": False, "next_cursor": None}


def _mock(monkeypatch, seen, issues=ISSUES):
    def handler(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("/projects/"):
            return httpx.Response(200, json=_page([{"id": "p1", "identifier": "FELT"}]))
        if path.endswith("/states/"):
            return httpx.Response(200, json=_page(STATES))
        if path.endswith("/issues/"):
            return httpx.Response(200, json=_page(issues))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(skill.httpx, "AsyncClient", lambda **kw: orig(transport=transport, **kw))


def _ctx(msg=""):
    return ExecutionContext(config=FeltConfig(plane_api_token="t"), original_message=msg)


async def test_overview_lists_columns_in_board_order(monkeypatch):
    seen = []
    _mock(monkeypatch, seen)
    r = await skill.handle({}, _ctx("how's the board?"))
    assert r.text.startswith("Board: Polish pile 1, Ready 2, In progress 1, Done 1.")
    assert "In progress: WP-37 The board" in r.text
    assert "Ready: WP-41 ntfy hygiene; WP-47 Home Assistant" in r.text
    assert "Nothing is waiting for Mario's review." in r.text
    assert {req.method for req in seen} == {"GET"}
    assert seen[0].headers["X-API-Key"] == "t"


async def test_next_is_the_top_of_ready(monkeypatch):
    _mock(monkeypatch, [])
    r = await skill.handle({"focus": "next"}, _ctx())
    assert r.text == "Next up: WP-41 ntfy hygiene; WP-47 Home Assistant"


async def test_waiting_for_me_means_review(monkeypatch):
    _mock(monkeypatch, [])
    r = await skill.handle({}, _ctx("anything waiting for me?"))
    assert r.text == "Nothing in Needs review."


async def test_no_token_is_reported():
    r = await skill.handle({}, ExecutionContext(config=FeltConfig(plane_api_token=None)))
    assert "isn't connected" in r.text


async def test_non_get_is_refused_before_sending():
    request = httpx.Request("PATCH", "https://plane.example/api/v1/x")
    with pytest.raises(RuntimeError, match="read-only"):
        await skill._read_only(request)
