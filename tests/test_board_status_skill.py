"""WP-37: board_status reads the Beads board file and never writes to it."""

import importlib.util
import json
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

ISSUES = [
    {"id": "cm-1", "title": "Home Assistant", "status": "open", "priority": 2, "created_at": "2"},
    {"id": "cm-2", "title": "ntfy hygiene", "status": "open", "priority": 1, "created_at": "3"},
    {"id": "cm-3", "title": "The board", "status": "in_progress", "priority": 2, "created_at": "1"},
    {"id": "cm-4", "title": "freeze fix", "status": "closed", "priority": 1, "created_at": "1"},
    {"id": "cm-5", "title": "Walk cycle", "status": "open", "priority": 4, "labels": ["polish"]},
    {"id": "cm-6", "title": "Email noise", "status": "open", "priority": 3, "created_at": "1"},
    {
        "id": "cm-7",
        "title": "Retire Plane",
        "status": "open",
        "priority": 2,
        "dependencies": [{"issue_id": "cm-7", "depends_on_id": "cm-3", "type": "blocks"}],
    },
    {
        "id": "cm-8",
        "title": "Unblocked now",
        "status": "open",
        "priority": 2,
        "created_at": "9",
        "dependencies": [{"issue_id": "cm-8", "depends_on_id": "cm-4", "type": "blocks"}],
    },
]


def _mock(monkeypatch, seen, issues=ISSUES):
    body = "\n".join(json.dumps(i) for i in issues) + "\n"

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/v1/repos/Benylin/workspace/raw/.beads/issues.jsonl":
            return httpx.Response(200, text=body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(skill.httpx, "AsyncClient", lambda **kw: orig(transport=transport, **kw))


def _ctx(msg=""):
    return ExecutionContext(config=FeltConfig(board_token="t"), original_message=msg)


async def test_overview_counts_columns_and_lists_the_top(monkeypatch):
    seen = []
    _mock(monkeypatch, seen)
    r = await skill.handle({}, _ctx("how's the board?"))
    assert r.text.startswith(
        "Board: In progress 1, Ready 3, Blocked 1, Backlog 1, Polish pile 1, Done 1."
    )
    assert "In progress: The board" in r.text
    assert "Ready: ntfy hygiene; Home Assistant; Unblocked now" in r.text
    assert "Nothing is waiting for Mario's review." in r.text
    assert {req.method for req in seen} == {"GET"}
    assert seen[0].headers["Authorization"] == "token t"


async def test_next_is_the_top_of_ready(monkeypatch):
    _mock(monkeypatch, [])
    r = await skill.handle({"focus": "next"}, _ctx())
    assert r.text == "Next up: ntfy hygiene; Home Assistant; Unblocked now"


async def test_waiting_for_me_means_review(monkeypatch):
    issues = [
        *ISSUES,
        {"id": "cm-9", "title": "M2b certs", "status": "open", "labels": ["needs-review"]},
    ]
    _mock(monkeypatch, [], issues)
    r = await skill.handle({}, _ctx("anything waiting for me?"))
    assert r.text == "Needs review (1): M2b certs"


async def test_parked_is_deferred(monkeypatch):
    _mock(monkeypatch, [], [{"id": "cm-1", "title": "PoE2 pricing", "status": "deferred"}])
    r = await skill.handle({"focus": "parked"}, _ctx())
    assert r.text == "Parked (1): PoE2 pricing"


async def test_no_token_is_reported():
    r = await skill.handle({}, ExecutionContext(config=FeltConfig(board_token=None)))
    assert "isn't connected" in r.text


async def test_unreadable_board_is_reported(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="not json\n")

    orig = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(skill.httpx, "AsyncClient", lambda **kw: orig(transport=transport, **kw))
    r = await skill.handle({}, _ctx())
    assert r.text == "Couldn't read the board right now."


async def test_non_get_is_refused_before_sending():
    request = httpx.Request("PATCH", "https://git.example/api/v1/x")
    with pytest.raises(RuntimeError, match="read-only"):
        await skill._read_only(request)
