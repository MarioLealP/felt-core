"""2026-09-24 audit fixes to WP-04's latency metrics: cancelled stages
aren't recorded, polling routes don't swamp `total`, and cloud screen
reactions get their own `screen_reaction` stage (route `local`)."""

import asyncio

from fastapi.testclient import TestClient

import felt_core.brain.screen_reactions as brain
import felt_core.metrics as metrics
from felt_core.api import app
from felt_core.config import FeltConfig
from felt_core.db import get_latency_rows


async def test_a_cancelled_stage_is_not_recorded(tmp_path):
    db_path = str(tmp_path / "felt.db")
    metrics.start_request("req-c", "/v1/chat", db_path)

    async def privacy_like():
        async with metrics.timed("privacy"):
            await asyncio.sleep(10)

    task = asyncio.create_task(privacy_like())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert get_latency_rows(db_path, since_hours=1) == []


async def test_a_stage_that_raises_is_still_recorded(tmp_path):
    db_path = str(tmp_path / "felt.db")
    metrics.start_request("req-e", "/v1/chat", db_path)

    try:
        async with metrics.timed("reply"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert [r["stage"] for r in get_latency_rows(db_path, since_hours=1)] == ["reply"]


def test_polling_routes_do_not_record_a_total(tmp_path, monkeypatch):
    db_path = str(tmp_path / "felt.db")
    monkeypatch.setenv("FELT_DB_PATH", db_path)
    monkeypatch.delenv("FELT_SPOTIFY_REFRESH_TOKEN", raising=False)

    client = TestClient(app)  # no lifespan - no background loops
    assert client.get("/v1/music/now").status_code == 200
    assert client.get("/v1/metrics/latency").status_code == 200

    assert get_latency_rows(db_path, since_hours=1) == []


async def test_screen_reaction_records_a_local_screen_reaction_stage(tmp_path, fake_llm):
    db_path = str(tmp_path / "felt.db")
    config = FeltConfig(db_path=db_path)
    metrics.start_request("req-s", "/v1/react/screen", db_path)
    fake_llm.script = ['{"seen": "a game", "line": "nice move", "emotion": "happy"}']

    await brain.react_to_screen(b"jpeg", "game.exe", "Game", config, session_id="s")

    rows = get_latency_rows(db_path, since_hours=1)
    assert [(r["stage"], r["route"]) for r in rows] == [("screen_reaction", "local")]
