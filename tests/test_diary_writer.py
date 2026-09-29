"""WP-26: the nightly writer's trigger (first day_start/afk_end after
05:00 local, once per date, skipped with no material), its single
felt-llm call writing both the diary and self-memory halves, and
Sunday's weekly compression."""

import json
from datetime import date, timedelta

import felt_core.db as db
import felt_core.diary as diary
from felt_core.config import FeltConfig
from felt_core.local_llm import LocalLLMUnavailable


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


_DAILY_JSON = json.dumps(
    {
        "entry": "we played endfield together, it was great.",
        "callbacks": ["endfield hype"],
        "mood_word": "cozy",
        "self_updates": [
            {"kind": "like", "text": "endfield's colours are gorgeous", "keywords": ["endfield"]}
        ],
        "greeting": "morning! still thinking about endfield.",
    }
)


# -- the trigger: pure ------------------------------------------------------


def test_target_date_for_trigger_ignores_unknown_kinds():
    assert diary._target_date_for_trigger("mash", "2026-09-23 08:00:00") is None


def test_target_date_for_trigger_requires_after_5am_local():
    # Europe/Lisbon is UTC+1 under DST in September: 08:00 UTC = 09:00
    # local, after 5am -> triggers, writing the day before.
    assert diary._target_date_for_trigger("day_start", "2026-09-23 08:00:00") == "2026-09-22"
    # 03:00 UTC = 04:00 local - before 5am, no trigger yet.
    assert diary._target_date_for_trigger("day_start", "2026-09-23 03:00:00") is None


def test_target_date_for_trigger_also_fires_on_afk_end():
    assert diary._target_date_for_trigger("afk_end", "2026-09-23 08:00:00") == "2026-09-22"


# -- write_diary_for_date ----------------------------------------------------


async def test_write_diary_for_date_skips_when_no_material(tmp_path):
    config = _config(tmp_path)
    written = await diary.write_diary_for_date(config, "2026-09-22")
    assert written is False
    assert db.get_diary(config.db_path, "2026-09-22") is None


async def test_write_diary_for_date_writes_both_diary_and_self_memory_once(
    tmp_path, monkeypatch, fake_llm
):
    config = _config(tmp_path)
    db.save_event(
        config.db_path, "game_start", json.dumps({"name": "endfield.exe"}), "2026-09-22 12:00:00"
    )

    fake_llm.default = _DAILY_JSON
    enqueued = []

    monkeypatch.setattr(
        diary, "enqueue_proactive", lambda cfg, kind, text, **kw: enqueued.append((kind, text))
    )

    first = await diary.write_diary_for_date(config, "2026-09-22")
    second = await diary.write_diary_for_date(config, "2026-09-22")

    assert first is True
    assert second is False  # already has a row - never overwrites
    assert len(fake_llm.calls) == 1  # one call, no weekly compression (not a Sunday-ended week)

    row = db.get_diary(config.db_path, "2026-09-22")
    assert row is not None
    assert row["entry"].startswith("we played endfield")
    assert row["mood_word"] == "cozy"
    assert json.loads(row["callbacks_json"]) == [{"text": "endfield hype", "uses": 0}]
    assert enqueued == [("diary", "morning! still thinking about endfield.")]

    memories = db.get_self_memories(config.db_path, status="active")
    assert any("endfield" in m["text"] for m in memories)


async def test_write_diary_for_date_skips_when_felt_llm_is_unavailable(tmp_path, fake_llm):
    config = _config(tmp_path)
    db.save_event(
        config.db_path, "game_start", json.dumps({"name": "endfield.exe"}), "2026-09-22 12:00:00"
    )

    fake_llm.default = LocalLLMUnavailable("down")  # type: ignore[assignment]

    written = await diary.write_diary_for_date(config, "2026-09-22")

    assert written is False
    assert db.get_diary(config.db_path, "2026-09-22") is None


async def test_write_diary_for_date_skips_on_unparsable_response(tmp_path, fake_llm):
    config = _config(tmp_path)
    db.save_event(
        config.db_path, "game_start", json.dumps({"name": "endfield.exe"}), "2026-09-22 12:00:00"
    )

    fake_llm.default = "not json at all"

    written = await diary.write_diary_for_date(config, "2026-09-22")

    assert written is False
    assert db.get_diary(config.db_path, "2026-09-22") is None


# -- on_event: the trigger wired to the writer -------------------------------


async def test_on_event_writes_yesterday_once_then_never_again(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    db.save_event(
        config.db_path, "game_start", json.dumps({"name": "endfield.exe"}), "2026-09-22 12:00:00"
    )

    fake_llm.default = _DAILY_JSON
    monkeypatch.setattr(diary, "enqueue_proactive", lambda *a, **kw: None)

    await diary.on_event(config, "day_start", "2026-09-23 08:00:00")
    assert db.get_diary(config.db_path, "2026-09-22") is not None

    fake_llm.calls.clear()
    await diary.on_event(config, "day_start", "2026-09-23 09:00:00")

    assert fake_llm.calls == []  # the date already has a row - the writer never runs again


# -- Sunday weekly compression ------------------------------------------------


def _next_sunday_on_or_after(d: date) -> date:
    while d.weekday() != 6:
        d += timedelta(days=1)
    return d


async def test_sunday_write_triggers_weekly_compression(tmp_path, fake_llm):
    config = _config(tmp_path)
    sunday = _next_sunday_on_or_after(date(2026, 9, 1))
    for i in range(1, 7):
        d = (sunday - timedelta(days=i)).isoformat()
        db.insert_diary(config.db_path, d, f"entry for {d}", "[]", "fine")
    db.save_event(
        config.db_path,
        "game_start",
        json.dumps({"name": "x.exe"}),
        f"{sunday.isoformat()} 12:00:00",
    )

    fake_llm.script = [
        json.dumps(
            {
                "entry": "sunday funday.",
                "callbacks": [],
                "mood_word": "happy",
                "self_updates": [],
                "greeting": "",
            }
        ),
        json.dumps({"summary": "a good week overall.", "callbacks": ["callback one"]}),
    ]

    written = await diary.write_diary_for_date(config, sunday.isoformat())

    assert written is True
    assert len(fake_llm.calls) == 2  # the daily write, then the weekly compression
    week_row = db.get_latest_diary_week(config.db_path)
    assert week_row is not None
    assert week_row["summary"] == "a good week overall."


def test_non_sunday_never_compresses(tmp_path):
    config = _config(tmp_path)
    monday = _next_sunday_on_or_after(date(2026, 9, 1)) + timedelta(days=1)
    assert monday.weekday() == 0
    # Directly exercises the pure gate rather than the whole writer path.
    import asyncio

    asyncio.run(diary._maybe_compress_week(config, monday.isoformat()))
    assert db.get_latest_diary_week(config.db_path) is None
