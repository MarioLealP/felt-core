from felt_core.db.messages import get_history, save_message


async def test_get_history_respects_chronological_order(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await save_message(db_path, "default", "user", "one")
    await save_message(db_path, "default", "assistant", "two")
    await save_message(db_path, "default", "user", "three")

    history = await get_history(db_path, "default", limit=20)

    assert [m["content"] for m in history] == ["one", "two", "three"]


async def test_get_history_limit_keeps_the_most_recent_messages(tmp_path):
    # 2026-09-14 review finding: ORDER BY id ASC LIMIT ? kept returning
    # the OLDEST messages forever once a session passed the limit,
    # permanently hiding anything recent.
    db_path = str(tmp_path / "felt.db")
    for i in range(5):
        await save_message(db_path, "default", "user", f"msg{i}")

    history = await get_history(db_path, "default", limit=3)

    assert [m["content"] for m in history] == ["msg2", "msg3", "msg4"]


async def test_get_history_excludes_legacy_system_observation_rows(tmp_path):
    # WP-02: observations used to be saved into `messages` as fake `user`
    # rows before screen_observations got its own table. Non-destructive
    # fix - hide them from get_history without deleting anything already
    # in a prod DB.
    db_path = str(tmp_path / "felt.db")
    await save_message(db_path, "default", "user", "hey")
    await save_message(
        db_path,
        "default",
        "user",
        "[System Observation: The user's screen currently shows: a spreadsheet]",
    )
    await save_message(db_path, "default", "assistant", "hey yourself")

    history = await get_history(db_path, "default", limit=20)

    assert [m["content"] for m in history] == ["hey", "hey yourself"]
