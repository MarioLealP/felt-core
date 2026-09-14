from felt_core.db import get_history, save_message


def test_get_history_respects_chronological_order(tmp_path):
    db_path = str(tmp_path / "felt.db")
    save_message(db_path, "default", "user", "one")
    save_message(db_path, "default", "assistant", "two")
    save_message(db_path, "default", "user", "three")

    history = get_history(db_path, "default", limit=20)

    assert [m["content"] for m in history] == ["one", "two", "three"]


def test_get_history_limit_keeps_the_most_recent_messages(tmp_path):
    # 2026-09-14 review finding: ORDER BY id ASC LIMIT ? kept returning
    # the OLDEST messages forever once a session passed the limit,
    # permanently hiding anything recent.
    db_path = str(tmp_path / "felt.db")
    for i in range(5):
        save_message(db_path, "default", "user", f"msg{i}")

    history = get_history(db_path, "default", limit=3)

    assert [m["content"] for m in history] == ["msg2", "msg3", "msg4"]
