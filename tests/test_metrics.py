import asyncio

from fastapi.testclient import TestClient

import felt_core.metrics as metrics
from felt_core.api import app
from felt_core.db import get_latency_rows


async def test_timed_records_into_db_and_server_timing(tmp_path):
    db_path = str(tmp_path / "felt.db")
    metrics.start_request("req-1", "/v1/test", db_path, session_id="s1")

    async with metrics.timed("mystage", route="local"):
        # > one Windows monotonic tick (~15.6 ms): 0.01 s could measure 0.0
        # there, depending on which test ran just before (seen 2026-09-28).
        await asyncio.sleep(0.05)

    header = metrics.server_timing_header()
    assert header is not None
    assert "mystage" in header
    assert 'desc="local"' in header

    rows = get_latency_rows(db_path, since_hours=1)
    assert len(rows) == 1
    assert rows[0]["stage"] == "mystage"
    assert rows[0]["route"] == "local"
    assert rows[0]["ms"] > 0


async def test_timed_is_a_no_op_outside_a_request(tmp_path):
    # No start_request() call - nothing should raise, nothing recorded.
    metrics._current.set(None)
    async with metrics.timed("orphan_stage"):
        await asyncio.sleep(0)
    assert metrics.server_timing_header() is None


def test_server_timing_header_present_on_chat_request(tmp_path, monkeypatch):
    db_path = str(tmp_path / "felt.db")
    monkeypatch.setenv("FELT_DB_PATH", db_path)

    import felt_core.brain.orchestrator as orchestrator
    import felt_core.brain.replies as replies

    async def fake_decide_tool(message, registry, cfg):
        return None

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "a reply"

    monkeypatch.setattr(orchestrator, "decide_tool", fake_decide_tool)
    monkeypatch.setattr(replies, "_ollama_chat", fake_ollama_chat)

    with TestClient(app) as client:
        response = client.post("/v1/chat", json={"message": "hi", "session_id": "wp04-test"})

    assert response.status_code == 200
    server_timing = response.headers.get("server-timing")
    assert server_timing is not None
    assert "router" in server_timing
    assert "total" in server_timing

    rows = get_latency_rows(db_path, since_hours=1, endpoint="/v1/chat")
    stages = {r["stage"] for r in rows}
    assert "router" in stages
    assert "total" in stages


def test_compute_stats_percentiles_and_max():
    rows = [{"stage": "router", "ms": ms} for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]]

    stats = metrics.compute_stats(rows)

    assert stats["router"]["n"] == 10
    assert stats["router"]["p50"] == 55.0
    assert stats["router"]["p95"] == 95.5
    assert stats["router"]["max"] == 100


def test_compute_stats_route_breakdown():
    rows = [
        {"stage": "reply", "ms": 500, "route": "cloud"},
        {"stage": "reply", "ms": 600, "route": "cloud"},
        {"stage": "reply", "ms": 3000, "route": "local"},
    ]

    stats = metrics.compute_stats(rows)

    assert stats["reply"]["n"] == 3
    assert stats["reply"]["by_route"]["cloud"]["n"] == 2
    assert stats["reply"]["by_route"]["local"]["n"] == 1
    assert stats["reply"]["by_route"]["local"]["max"] == 3000
