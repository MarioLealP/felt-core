"""WP-42.5: hide and quiet - tap for unlimited, slide for a timer - and the
desktop's heartbeat that makes the controls page right from the phone."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from felt_core import controls, local_llm
from felt_core.api import app

T0 = datetime(2026, 10, 3, 21, 0, tzinfo=UTC)
SSO = {"X-Authentik-Username": "mario"}


def test_tap_is_on_until_turned_off_and_a_slide_is_a_timer(tmp_path):
    db = str(tmp_path / "f.db")
    assert controls.state(db, T0) == {
        "version": 0,
        "hide": {"on": False, "until": None},
        "quiet": {"on": False, "until": None},
    }
    controls.set_control(db, "hide", True, now=T0)  # tap
    later = controls.state(db, T0 + timedelta(days=3))
    assert later["hide"] == {"on": True, "until": None} and later["version"] == 1
    controls.set_control(db, "quiet", True, 30, now=T0)  # slide to 30 min
    assert (
        controls.state(db, T0 + timedelta(minutes=29))["quiet"]["until"] == "2026-10-03T21:30:00Z"
    )
    assert controls.quiet(db, T0 + timedelta(minutes=29))
    assert not controls.quiet(db, T0 + timedelta(minutes=31))  # ran out
    controls.set_control(db, "hide", False, now=T0)
    assert controls.state(db, T0)["version"] == 3
    with pytest.raises(ValueError):
        controls.set_control(db, "mute", True)


def test_the_desktop_counts_while_it_keeps_reporting(tmp_path):
    db = str(tmp_path / "f.db")
    assert controls.desktop(db, T0) is None
    controls.report_desktop(
        db, {"presence": "hidden", "hushed": True, "mood": "sleepy", "animation": True, "x": 1}, T0
    )
    seen = controls.desktop(db, T0 + timedelta(seconds=20))
    assert seen is not None and seen["presence"] == "hidden" and seen["mood"] == "sleepy"
    assert "x" not in seen
    assert controls.desktop(db, T0 + timedelta(minutes=2)) is None  # PC off / tray gone


def test_the_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", "s3cret")
    monkeypatch.delenv("FELT_VOICE_URL", raising=False)

    async def healthy(config):
        return True

    monkeypatch.setattr(local_llm, "is_healthy", healthy)
    token = {"Authorization": "Bearer s3cret"}
    with TestClient(app) as c:
        assert c.get("/v1/app/controls").status_code == 401
        page = c.get("/v1/app/controls", headers=SSO).json()
        assert page["timers"] == [15, 30, 60, 480] and page["desktop"] is None
        assert page["parts"] == {
            "brain": True,
            "model": True,
            "voice": None,
            "desktop": False,
            "animation": False,
        }
        body = {"name": "quiet", "on": True, "minutes": 60}
        assert c.post("/v1/app/controls", headers=SSO, json=body).json()["quiet"]["on"]
        bad = {"name": "quiet", "on": True, "minutes": 7}
        assert c.post("/v1/app/controls", headers=SSO, json=bad).status_code == 422
        # the tray: reads the switches, reports back (token, not SSO)
        assert c.get("/v1/controls").status_code == 401
        assert c.get("/v1/controls", headers=token).json()["quiet"]["on"]
        report = {"presence": "hidden", "hushed": True, "mood": "ok", "animation": True}
        assert c.post("/v1/controls/desktop", headers=token, json=report).json() == {"ok": True}
        page = c.get("/v1/app/controls", headers=SSO).json()
        assert page["parts"]["desktop"] and page["mood"] == "ok"
