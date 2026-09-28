"""WP-26's smallest-viable diary_recall skill - answers straight from
stored diary/diary_weeks rows, no model call of its own."""

from datetime import UTC, datetime, timedelta

from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.db import insert_diary, insert_or_update_diary_week
from felt_core.plugin import ExecutionContext
from felt_core.self_memory import LISBON

skill = load_plugin_module("diary_recall/skill.py")


def _ctx(tmp_path, message=""):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"))
    return ExecutionContext(config=config, original_message=message)


async def test_recall_yesterday_returns_the_stored_entry(tmp_path):
    ctx = _ctx(tmp_path, "what did I do yesterday")
    yesterday = (datetime.now(UTC).astimezone(LISBON).date() - timedelta(days=1)).isoformat()
    insert_diary(ctx.config.db_path, yesterday, "we played endfield all night.", "[]", "cozy")

    result = await skill.handle({}, ctx)

    assert result.text == "we played endfield all night."


async def test_recall_yesterday_with_no_entry_says_so(tmp_path):
    ctx = _ctx(tmp_path, "what did I do yesterday")
    result = await skill.handle({}, ctx)
    assert "haven't written" in result.text


async def test_recall_week_returns_the_latest_weekly_summary(tmp_path):
    ctx = _ctx(tmp_path, "how was my week")
    insert_or_update_diary_week(ctx.config.db_path, "2026-W38", "a good week overall.", "[]")

    result = await skill.handle({"range": "week"}, ctx)

    assert "a good week overall." in result.text


async def test_recall_week_with_no_summary_says_so(tmp_path):
    ctx = _ctx(tmp_path, "how was my week")
    result = await skill.handle({"range": "week"}, ctx)
    assert "haven't compressed" in result.text
