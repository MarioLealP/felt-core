from felt_core.db import (
    get_recent_pending_emails,
    get_unresolved_pending_email,
    is_known_email_uid,
    mark_email_resolved,
    save_pending_email,
)


def test_unknown_uid_is_not_known(tmp_path):
    db_path = str(tmp_path / "felt.db")
    assert is_known_email_uid(db_path, "123") is False


def test_saved_uid_becomes_known(tmp_path):
    db_path = str(tmp_path / "felt.db")
    save_pending_email(db_path, "123", "Jane", "Hello")
    assert is_known_email_uid(db_path, "123") is True


def test_get_unresolved_pending_email_returns_the_newest(tmp_path):
    db_path = str(tmp_path / "felt.db")
    save_pending_email(db_path, "1", "Jane", "First")
    save_pending_email(db_path, "2", "John", "Second")
    pending = get_unresolved_pending_email(db_path)
    assert pending["uid"] == "2"
    assert pending["sender"] == "John"
    assert pending["subject"] == "Second"


def test_no_unresolved_email_returns_none(tmp_path):
    db_path = str(tmp_path / "felt.db")
    assert get_unresolved_pending_email(db_path) is None


def test_mark_resolved_removes_it_from_unresolved(tmp_path):
    db_path = str(tmp_path / "felt.db")
    email_id = save_pending_email(db_path, "1", "Jane", "Hello")
    mark_email_resolved(db_path, email_id)
    assert get_unresolved_pending_email(db_path) is None


def test_get_recent_pending_emails_reports_resolved_flag(tmp_path):
    db_path = str(tmp_path / "felt.db")
    email_id = save_pending_email(db_path, "1", "Jane", "Hello")
    mark_email_resolved(db_path, email_id)
    save_pending_email(db_path, "2", "John", "World")

    recent = get_recent_pending_emails(db_path)
    assert recent == [
        {"sender": "John", "subject": "World", "resolved": False},
        {"sender": "Jane", "subject": "Hello", "resolved": True},
    ]
