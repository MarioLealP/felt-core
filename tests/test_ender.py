"""The Ender through Home Assistant (2026-10-19): what printer_status says,
which state changes she tells Mario about, and that a restart neither misses
nor repeats one."""

from datetime import UTC, datetime

import httpx
import pytest

from felt_core import db, ender, local_llm, phone
from felt_core.config import FeltConfig


def _config(tmp_path):
    return FeltConfig(
        db_path=str(tmp_path / "felt.db"), ha_url="http://ha", ha_token="t", phone="pocox7"
    )


@pytest.fixture
def ha(monkeypatch):
    """Fake HA: `ha.states` (field -> state) answers GET /api/states/[binary_]sensor.ender_<field>;
    POSTs (the phone notification) are recorded."""

    class HA:
        states: dict[str, str] = {}
        posts: list[dict] = []

    def handler(request):
        if request.method == "POST":
            HA.posts.append({"path": request.url.path})
            return httpx.Response(200, json=[])
        name = request.url.path.rsplit("/", 1)[1]
        field = name.removeprefix("binary_sensor.ender_").removeprefix("sensor.ender_")
        if field not in HA.states:
            return httpx.Response(404)
        return httpx.Response(200, json={"state": HA.states[field]})

    real = httpx.AsyncClient
    for mod in (ender, phone):
        monkeypatch.setattr(
            mod.httpx,
            "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        )
    HA.states = {}
    HA.posts = []
    return HA


@pytest.fixture
def said(monkeypatch):
    """Desk lines; felt-llm down unless a test says otherwise (the plain lines)."""

    async def llm_down(*a, **kw):
        raise local_llm.LocalLLMUnavailable("down")

    monkeypatch.setattr("felt_core.brain.replies.local_reply", llm_down)
    lines: list[str] = []
    monkeypatch.setattr(
        ender, "enqueue_proactive", lambda config, kind, text, **kw: lines.append(text)
    )
    return lines


def test_job_name_and_duration():
    assert ender.job_name("Cube_PLA_11m3s.gcode") == "Cube_PLA_11m3s"
    assert ender.job_name("sub/dir/part.gcode") == "part"
    assert ender.duration(300) == "5m"
    assert ender.duration(4380) == "1h 13m"


@pytest.mark.parametrize(
    ("prev", "cur", "event"),
    [
        (None, "complete", None),  # first run: an old print, not news
        ("printing", "complete", "complete"),
        ("paused", "cancelled", "cancelled"),
        ("printing", "error", "error"),
        ("printing", "paused", "paused"),
        ("paused", "printing", None),  # resumed: he did that himself
        ("standby", "printing", None),
        ("complete", "standby", None),
        ("complete", "complete", None),
    ],
)
def test_event_for(prev, cur, event):
    assert ender.event_for(prev, cur) == event


def test_describe_printing():
    s = ender.snapshot_from(
        {
            "state": "printing",
            "file": "hexwall_tile_07.gcode",
            "progress": "62.4",
            "layer": "84 / 136",
            "time_left": "3960",
            "done_at": datetime(2026, 10, 19, 20, 40, tzinfo=UTC).isoformat(),
            "nozzle": "214.8",
            "nozzle_target": "215.0",
            "bed": "60.1",
            "bed_target": "60.0",
            "message": "",
        }
    )
    assert ender.describe(s) == (
        "Ender — printing (hexwall_tile_07), 62% done, layer 84 / 136, "
        "~1h 6m left (done ~21:40), nozzle 215°C/215°C, bed 60°C/60°C"
    )


def test_describe_idle_and_offline():
    idle = ender.snapshot_from(
        {
            "state": "standby",
            "nozzle": "28.4",
            "nozzle_target": "0.0",
            "bed": "25",
            "layer": "unknown",
        }
    )
    assert ender.describe(idle) == "Ender — standby, nozzle 28°C, bed 25°C"
    assert "offline" in ender.describe(ender.snapshot_from({"state": "unavailable"}))
    assert "offline" in ender.describe(ender.snapshot_from({}))


def test_lines():
    s = ender.snapshot_from({"file": "Cube.gcode", "elapsed": "687", "message": "Filament runout"})
    assert ender.line_for("complete", s) == "The Ender's print is done (Cube) after 11m."
    assert ender.line_for("cancelled", s) == "The Ender's print was cancelled (Cube)."
    assert ender.line_for("error", s) == "The Ender stopped with an error (Cube): Filament runout."
    assert ender.line_for("paused", s) == "The Ender paused (Cube) - Filament runout."
    assert ender.line_for("complete", ender.snapshot_from({})) == "The Ender's print is done."


async def test_check_says_a_finished_print_once(tmp_path, ha, said):
    config = _config(tmp_path)
    ha.states = {"state": "printing", "file": "Cube.gcode"}
    assert await ender.check(config) is None  # first sight: remembered, not said
    ha.states.update(state="complete", elapsed="687")
    assert await ender.check(config) == "The Ender's print is done (Cube) after 11m."
    assert said == ["The Ender's print is done (Cube) after 11m."]
    assert len(ha.posts) == 1  # the phone got it too
    assert await ender.check(config) is None  # no repeat
    assert said == ["The Ender's print is done (Cube) after 11m."]


async def test_offline_is_not_a_transition(tmp_path, ha, said):
    """Moonraker restarting mid-print: unavailable in between must not eat
    or fake the end of the print."""
    config = _config(tmp_path)
    ha.states = {"state": "printing"}
    await ender.check(config)
    ha.states = {"state": "unavailable"}
    assert await ender.check(config) is None
    ha.states = {"state": "complete"}
    assert await ender.check(config) == "The Ender's print is done."


async def test_restart_resumes_from_the_db(tmp_path, ha, said):
    config = _config(tmp_path)
    db.set_setting(config.db_path, "ender_last_state", "printing")  # before the restart
    ha.states = {"state": "cancelled"}
    assert await ender.check(config) == "The Ender's print was cancelled."


async def test_phone_down_still_says_it_at_the_desk(tmp_path, ha, said):
    config = _config(tmp_path)
    config.phone = None  # phone.notify raises PhoneUnavailable
    db.set_setting(config.db_path, "ender_last_state", "printing")
    ha.states = {"state": "complete"}
    assert await ender.check(config) == "The Ender's print is done."
    assert said == ["The Ender's print is done."]


async def test_ha_unconfigured(tmp_path):
    config = FeltConfig(db_path=str(tmp_path / "f.db"), ha_url=None)
    with pytest.raises(ender.EnderUnavailable):
        await ender.snapshot(config)


def test_prompt_carries_the_facts():
    s = ender.snapshot_from({"file": "Cube.gcode", "elapsed": "687", "message": "Filament runout"})
    done = ender.build_prompt("complete", s)
    assert 'finished printing "Cube"' in done and "11m" in done
    assert 'Klipper says: "Filament runout"' in ender.build_prompt("paused", s)
    assert "error" in ender.build_prompt("error", s)
    assert "cancelled" in ender.build_prompt("cancelled", ender.snapshot_from({}))


async def test_she_words_it_herself(tmp_path, ha, said, monkeypatch):
    prompts: list[str] = []

    async def her(messages, config, *, include_facts):
        prompts.append(messages[-1]["content"])
        return '"Your cube is done, go get it!"\n'

    monkeypatch.setattr("felt_core.brain.replies.local_reply", her)
    config = _config(tmp_path)
    db.set_setting(config.db_path, "ender_last_state", "printing")
    ha.states = {"state": "complete", "file": "Cube.gcode"}
    assert await ender.check(config) == "Your cube is done, go get it!"
    assert said == ["Your cube is done, go get it!"]
    assert 'finished printing "Cube"' in prompts[0]


async def test_empty_reply_falls_back(tmp_path, ha, said, monkeypatch):
    async def blank(*a, **kw):
        return "  "

    monkeypatch.setattr("felt_core.brain.replies.local_reply", blank)
    config = _config(tmp_path)
    db.set_setting(config.db_path, "ender_last_state", "printing")
    ha.states = {"state": "cancelled"}
    assert await ender.check(config) == "The Ender's print was cancelled."


async def test_a_runout_pause_says_why(tmp_path, ha, said):
    # 2026-10-22: Klipper's runout sensor paused it - she says so, desk + phone
    config = _config(tmp_path)
    db.set_setting(config.db_path, "ender_last_state", "printing")
    ha.states = {"state": "paused", "file": "Cube.gcode", "filament": "off"}
    line = "The Ender ran out of filament (Cube) - it's paused; load more and hit Resume."
    assert await ender.check(config) == line
    assert said == [line] and len(ha.posts) == 1


async def test_a_plain_pause_with_filament_in(tmp_path, ha, said):
    config = _config(tmp_path)
    db.set_setting(config.db_path, "ender_last_state", "printing")
    ha.states = {"state": "paused", "filament": "on"}
    assert await ender.check(config) == "The Ender paused."


def test_the_runout_prompt():
    prompt = ender.build_prompt("runout", ender.snapshot_from({"file": "Cube.gcode"}))
    assert "ran out of filament" in prompt and "Resume" in prompt
