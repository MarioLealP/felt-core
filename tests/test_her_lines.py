"""WP-42.3: lines she starts on her own (pushes, mail, alarms) are stored as
her lines in Mario's conversation, labelled by kind, so they reach the
Margins and her own history even when the desktop never caught them."""

from datetime import datetime, timedelta

from felt_core import emails, local_llm
from felt_core.alarms import _fire_at
from felt_core.asides import current_asides
from felt_core.config import FeltConfig
from felt_core.db import (
    ALARM,
    APP_SESSION,
    EMAIL,
    PROACTIVE,
    _connect,
    get_history,
    save_alarm,
)
from felt_core.proactive import enqueue_proactive


def _config(tmp_path) -> FeltConfig:
    # no ntfy_* set: notify() is a no-network no-op
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


def _lines(db: str) -> list[tuple]:
    conn = _connect(db)
    rows = conn.execute(
        "SELECT session_id, role, content, kind, source_ref FROM messages ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def test_a_push_is_a_line_even_if_it_expires_undelivered(tmp_path):
    config = _config(tmp_path)
    item = enqueue_proactive(config, "calendar_15", "Dentist in 15.", ttl_s=-10)
    assert _lines(config.db_path) == [
        (APP_SESSION, "assistant", "Dentist in 15.", PROACTIVE, f"proactive:calendar_15:{item}")
    ]
    # it can wait in Talk to be answered, and she remembers saying it
    assert current_asides(config.db_path)["asides"][0]["content"] == "Dentist in 15."
    assert get_history(config.db_path, APP_SESSION)[-1]["content"] == "Dentist in 15."


async def test_new_mail_is_a_line_she_remembers(tmp_path, fake_llm):
    config = _config(tmp_path)
    mail_id = emails.add(config.db_path, "<1@x>", "Ana <ana@x.pt>", "Dinner", "hi", False)
    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]
    await emails.tick(config)
    await emails.tick(config)  # same mail: not said twice
    assert _lines(config.db_path) == [
        (
            APP_SESSION,
            "assistant",
            'Btw, you\'ve got mail from Ana: "Dinner".',
            EMAIL,
            f"email:{mail_id}",
        )
    ]


async def test_an_alarm_is_a_line_but_never_an_aside(tmp_path):
    config = _config(tmp_path)
    alarm_id = save_alarm(config.db_path, datetime.now().isoformat(), "stretch")
    await _fire_at(config, alarm_id, datetime.now() - timedelta(seconds=1), "stretch")
    assert _lines(config.db_path) == [
        (APP_SESSION, "assistant", "Alarm: stretch", ALARM, f"alarm:{alarm_id}")
    ]
    assert current_asides(config.db_path)["asides"] == []
