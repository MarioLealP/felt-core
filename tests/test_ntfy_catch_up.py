"""WP-42.5: felt-core catches up on alerts sent while it was down."""

from datetime import UTC, datetime

from felt_core.db import set_setting
from felt_core.ntfy_listener import LAST_ID, sent_at, stream_params


def test_first_run_is_live_only_then_it_catches_up_from_the_last_seen(tmp_path):
    db = str(tmp_path / "f.db")
    assert stream_params(db) == {}
    set_setting(db, LAST_ID, "abc123")
    assert stream_params(db) == {"since": "abc123"}


def test_a_caught_up_alert_keeps_its_own_time():
    assert sent_at({"time": 1791104897}) == datetime.fromtimestamp(1791104897, UTC)
    assert sent_at({}) is None
