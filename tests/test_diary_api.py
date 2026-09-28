"""WP-26's API surface: the silent event log (POST /v1/events), the
diary dashboard endpoints (GET/PATCH/DELETE /v1/diary, the FELT_DEBUG-gated
POST /v1/diary/write), and the self-memory controls (GET/PATCH/DELETE
/v1/self-memory)."""

import json

from fastapi.testclient import TestClient

import felt_core.api as api
import felt_core.diary as diary
from felt_core.db import (
    get_diary,
    get_events_between,
    get_self_memories,
    insert_diary,
    insert_self_memory,
)


def _client(tmp_path, monkeypatch, debug=False):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    if debug:
        monkeypatch.setenv("FELT_DEBUG", "1")
    else:
        monkeypatch.delenv("FELT_DEBUG", raising=False)
    return TestClient(api.app)  # no lifespan - no background loops


# -- POST /v1/events ----------------------------------------------------------


def test_events_endpoint_logs_and_returns_204(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/events", json={"kind": "music_track", "data": {"title": "Song", "artist": "Artist"}}
    )

    assert response.status_code == 204
    db_path = str(tmp_path / "felt.db")
    rows = get_events_between(db_path, "2000-01-01 00:00:00", "2100-01-01 00:00:00")
    assert len(rows) == 1
    assert rows[0]["kind"] == "music_track"
    assert json.loads(rows[0]["data_json"]) == {"title": "Song", "artist": "Artist"}


def test_events_endpoint_accepts_any_kind_and_caps_oversized_data(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/events", json={"kind": "some_totally_new_kind", "data": {"blob": "x" * 5000}}
    )

    assert response.status_code == 204
    db_path = str(tmp_path / "felt.db")
    rows = get_events_between(db_path, "2000-01-01 00:00:00", "2100-01-01 00:00:00")
    assert len(rows) == 1
    assert len(rows[0]["data_json"]) < 5000


def test_events_endpoint_triggers_the_diary_writer(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    calls: list[str] = []

    async def fake_write(config, date_str):
        calls.append(date_str)
        return True

    monkeypatch.setattr(diary, "write_diary_for_date", fake_write)

    response = client.post(
        "/v1/events", json={"kind": "day_start", "data": {}, "at": "2026-09-23T08:00:00Z"}
    )

    assert response.status_code == 204
    assert calls == ["2026-09-22"]


# -- GET/PATCH/DELETE /v1/diary ----------------------------------------------


def test_diary_list_and_single_date(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db_path = str(tmp_path / "felt.db")
    insert_diary(db_path, "2026-09-20", "a fine day", "[]", "cozy")
    insert_diary(db_path, "2026-09-21", "another fine day", "[]", "chill")

    listing = client.get("/v1/diary")
    assert listing.status_code == 200
    dates = [e["date"] for e in listing.json()["entries"]]
    assert dates == ["2026-09-21", "2026-09-20"]  # newest first

    single = client.get("/v1/diary", params={"date": "2026-09-20"})
    assert [e["date"] for e in single.json()["entries"]] == ["2026-09-20"]

    missing = client.get("/v1/diary", params={"date": "2026-01-01"})
    assert missing.json()["entries"] == []


def test_diary_patch_marks_edited_and_clears_callbacks(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db_path = str(tmp_path / "felt.db")
    insert_diary(
        db_path, "2026-09-20", "original entry", json.dumps([{"text": "x", "uses": 0}]), "cozy"
    )

    response = client.patch("/v1/diary/2026-09-20", json={"entry": "corrected entry"})

    assert response.status_code == 200
    row = get_diary(db_path, "2026-09-20")
    assert row is not None
    assert row["entry"] == "corrected entry"
    assert row["edited"] is True
    assert row["callbacks_json"] == "[]"


def test_diary_patch_404_for_missing_date(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.patch("/v1/diary/2026-01-01", json={"entry": "x"})
    assert response.status_code == 404


def test_diary_delete(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db_path = str(tmp_path / "felt.db")
    insert_diary(db_path, "2026-09-20", "a fine day", "[]", "cozy")

    response = client.delete("/v1/diary/2026-09-20")

    assert response.json() == {"deleted": True}
    assert get_diary(db_path, "2026-09-20") is None


# -- POST /v1/diary/write: FELT_DEBUG-gated ----------------------------------


def test_diary_write_endpoint_404_without_debug(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=False)
    response = client.post("/v1/diary/write", params={"date": "2026-09-20"})
    assert response.status_code == 404


def test_diary_write_endpoint_runs_with_debug_on(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=True)

    async def fake_write(config, date_str):
        return date_str == "2026-09-20"

    monkeypatch.setattr(diary, "write_diary_for_date", fake_write)

    response = client.post("/v1/diary/write", params={"date": "2026-09-20"})

    assert response.status_code == 200
    assert response.json() == {"written": True}


# -- GET/PATCH/DELETE /v1/self-memory -----------------------------------------


def test_self_memory_list_filters_by_status(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db_path = str(tmp_path / "felt.db")
    insert_self_memory(db_path, "opinion", "bread is best", "[]", "2026-09-01")

    response = client.get("/v1/self-memory", params={"status": "active"})

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["text"] == "bread is best"


def test_self_memory_patch_can_pin_edit_and_veto(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db_path = str(tmp_path / "felt.db")
    memory_id = insert_self_memory(db_path, "opinion", "bread is best", "[]", "2026-09-01")

    pin = client.patch(f"/v1/self-memory/{memory_id}", json={"pinned": True})
    assert pin.json() == {"ok": True}
    assert get_self_memories(db_path, status="active")[0]["pinned"] is True

    veto = client.patch(f"/v1/self-memory/{memory_id}", json={"status": "vetoed"})
    assert veto.json() == {"ok": True}
    assert get_self_memories(db_path, status="active") == []
    assert get_self_memories(db_path, status="vetoed")[0]["text"] == "bread is best"


def test_self_memory_patch_404_for_missing_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.patch("/v1/self-memory/999", json={"pinned": True})
    assert response.status_code == 404


def test_self_memory_delete(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    db_path = str(tmp_path / "felt.db")
    memory_id = insert_self_memory(db_path, "opinion", "bread is best", "[]", "2026-09-01")

    response = client.delete(f"/v1/self-memory/{memory_id}")

    assert response.json() == {"deleted": True}
    assert get_self_memories(db_path) == []


def test_duplicate_trigger_while_writing_does_not_start_a_second_write(tmp_path, monkeypatch):
    """A buffered retry / an afk_end right after day_start must not start a
    second cloud write (two entries' worth of calls, two morning greetings)."""
    import asyncio

    from felt_core import diary as diary_mod
    from felt_core.config import FeltConfig

    started: list[str] = []
    release = asyncio.Event()

    async def slow_write(config, date_str):
        started.append(date_str)
        await release.wait()
        return True

    monkeypatch.setattr(diary_mod, "write_diary_for_date", slow_write)
    cfg = FeltConfig(db_path=str(tmp_path / "felt.db"))

    async def run():
        first = asyncio.create_task(diary_mod.on_event(cfg, "day_start", "2026-09-23 08:00:00"))
        await asyncio.sleep(0)
        await diary_mod.on_event(cfg, "day_start", "2026-09-23 08:00:05")
        release.set()
        await first

    asyncio.run(run())
    assert started == ["2026-09-22"]
