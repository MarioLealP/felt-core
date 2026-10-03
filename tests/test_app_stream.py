"""WP-42.3: the app's live notices (server-sent events)."""

import json

from fastapi.testclient import TestClient

from felt_core.api import app
from felt_core.app_stream import notices
from felt_core.db import save_message
from felt_core.marks import _connect as marks_connect
from felt_core.marks import _ensure as ensure_marks


def _parse(chunk: str) -> tuple[str, dict]:
    name, data = chunk.strip().split("\n")
    return name.removeprefix("event: "), json.loads(data.removeprefix("data: "))


async def test_new_lines_picks_and_marks_become_notices(tmp_path):
    db = str(tmp_path / "felt.db")
    save_message(db, "dashboard", "user", "hi")
    rounds = 0

    async def gone() -> bool:
        return rounds >= 4

    stream = notices(db, "dashboard", gone, poll_s=0, keepalive_s=10)
    assert _parse(await anext(stream)) == ("hello", {"newest": 1})

    # a reply and a remark land before the next check
    save_message(db, "dashboard", "assistant", "Hey.")
    save_message(db, "dashboard", "assistant", "Nice song.", kind="event")
    save_message(db, "other", "assistant", "elsewhere")  # another session: silent
    rounds += 1
    assert _parse(await anext(stream)) == ("line", {"id": 2, "role": "assistant", "kind": "chat"})
    assert _parse(await anext(stream)) == ("line", {"id": 3, "role": "assistant", "kind": "event"})

    conn = marks_connect(db)
    ensure_marks(conn)
    with conn:
        conn.execute(
            "INSERT INTO message_marks (id, message_id, type, quote)"
            " VALUES ('m1', 1, 'circle', 'hi')"
        )
    conn.close()
    rounds += 1
    assert _parse(await anext(stream)) == ("marks", {})


async def test_list_and_incident_changes_become_notices(tmp_path):
    from felt_core import incidents, systems
    from felt_core.db import add_shopping_item, set_shopping_item_checked

    db = str(tmp_path / "felt.db")
    save_message(db, "dashboard", "user", "hi")
    item = add_shopping_item(db, "milk")
    backup = incidents.intake(db, "backup full FAILED")
    assert backup is not None
    rounds = 0

    async def gone() -> bool:
        return rounds >= 5

    stream = notices(db, "dashboard", gone, poll_s=0, keepalive_s=10)
    assert _parse(await anext(stream))[0] == "hello"
    set_shopping_item_checked(db, item, True)  # she crossed it off by voice
    rounds += 1
    assert _parse(await anext(stream)) == ("lists", {})
    systems.mark_handled(db, backup)
    rounds += 1
    assert _parse(await anext(stream)) == ("systems", {})
    from felt_core import controls

    controls.set_control(db, "quiet", True)
    rounds += 1
    assert _parse(await anext(stream)) == ("controls", {})


async def test_a_quiet_stream_sends_keepalives_and_ends_when_the_client_goes(tmp_path):
    db = str(tmp_path / "felt.db")
    calls = 0

    async def gone() -> bool:
        nonlocal calls
        calls += 1
        return calls > 3

    got = [chunk async for chunk in notices(db, "dashboard", gone, poll_s=0.01, keepalive_s=0.02)]
    assert got[0].startswith("event: hello")
    assert got[1:] == [": keepalive\n\n"]


def test_the_stream_is_sso_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", "s3cret")
    with TestClient(app) as c:
        assert c.get("/v1/app/stream").status_code == 401
