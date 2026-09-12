from felt_core.db import get_pending_alarms, mark_alarm_fired, save_alarm


def test_save_alarm_appears_in_pending(tmp_path):
    db_path = str(tmp_path / "felt.db")
    save_alarm(db_path, "2026-09-17T07:30:00", "wake up")
    pending = get_pending_alarms(db_path)
    assert len(pending) == 1
    assert pending[0]["fire_at"] == "2026-09-17T07:30:00"
    assert pending[0]["label"] == "wake up"


def test_pending_alarms_ordered_by_fire_at(tmp_path):
    db_path = str(tmp_path / "felt.db")
    save_alarm(db_path, "2026-09-18T09:00:00", "later")
    save_alarm(db_path, "2026-09-17T07:30:00", "earlier")
    pending = get_pending_alarms(db_path)
    assert [alarm["label"] for alarm in pending] == ["earlier", "later"]


def test_mark_alarm_fired_removes_it_from_pending(tmp_path):
    db_path = str(tmp_path / "felt.db")
    alarm_id = save_alarm(db_path, "2026-09-17T07:30:00", "wake up")
    mark_alarm_fired(db_path, alarm_id)
    assert get_pending_alarms(db_path) == []
