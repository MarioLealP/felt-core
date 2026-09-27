import pytest

from felt_core import email_watcher
from felt_core.config import FeltConfig
from felt_core.email_watcher import (
    _connect,
    _decode_header,
    _sender_display_name,
    fetch_email_body,
)


def test_decode_header_handles_plain_ascii():
    assert _decode_header("Hello there") == "Hello there"


def test_decode_header_handles_missing_subject():
    assert _decode_header(None) == "(no subject)"


def test_decode_header_decodes_encoded_words():
    # A real MIME encoded-word, as a non-ASCII subject actually arrives.
    assert _decode_header("=?UTF-8?B?T2zDoQ==?=") == "Olá"


def test_sender_display_name_prefers_the_friendly_name():
    assert _sender_display_name('"Jane Doe" <jane@example.com>') == "Jane Doe"


def test_sender_display_name_falls_back_to_the_address():
    assert _sender_display_name("jane@example.com") == "jane@example.com"


def test_sender_display_name_handles_missing_from():
    assert _sender_display_name(None) == "(unknown sender)"


# -- fetch_email_body (WP-40: dropped a spurious uid.encode(), narrowed
# get_payload(decode=True) to isinstance(bytes) instead of assuming it) ---


class _FakeConn:
    def __init__(self, status: str, raw: bytes):
        self._status = status
        self._raw = raw
        self.logged_out = False

    def uid(self, command, uid_arg, message_parts):
        if self._status != "OK":
            return self._status, []
        return self._status, [(b"1 (RFC822 {n}", self._raw)]

    def logout(self):
        self.logged_out = True


def _config():
    return FeltConfig(imap_host="imap.example.com", imap_user="u", imap_password="p")


def test_fetch_email_body_plain_message(monkeypatch):
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\nHello body"
    monkeypatch.setattr(email_watcher, "_connect", lambda cfg: _FakeConn("OK", raw))

    assert fetch_email_body(_config(), "5") == "Hello body"


def test_fetch_email_body_multipart_picks_the_plain_part(monkeypatch):
    raw = (
        b"Content-Type: multipart/mixed; boundary=XYZ\r\n\r\n"
        b"--XYZ\r\nContent-Type: text/plain\r\n\r\nPlain part\r\n"
        b"--XYZ\r\nContent-Type: text/html\r\n\r\n<p>html</p>\r\n"
        b"--XYZ--"
    )
    monkeypatch.setattr(email_watcher, "_connect", lambda cfg: _FakeConn("OK", raw))

    assert fetch_email_body(_config(), "5") == "Plain part"


def test_fetch_email_body_returns_empty_string_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(email_watcher, "_connect", lambda cfg: _FakeConn("NO", b""))

    assert fetch_email_body(_config(), "5") == ""


def test_fetch_email_body_logs_out_even_on_a_bad_status(monkeypatch):
    conn = _FakeConn("NO", b"")
    monkeypatch.setattr(email_watcher, "_connect", lambda cfg: conn)

    fetch_email_body(_config(), "5")

    assert conn.logged_out is True


# -- _connect (WP-40: a clear error instead of an obscure imaplib one when
# `fetch_email_body` is reached with incomplete IMAP config - the only
# caller not already gated by watch()'s own host/user check) -------------


def test_connect_raises_a_clear_error_when_password_missing():
    config = FeltConfig(imap_host="imap.example.com", imap_user="u", imap_password=None)

    with pytest.raises(RuntimeError, match="password"):
        _connect(config)


def test_connect_raises_a_clear_error_when_host_missing():
    config = FeltConfig(imap_host=None, imap_user="u", imap_password="p")

    with pytest.raises(RuntimeError, match="host"):
        _connect(config)
