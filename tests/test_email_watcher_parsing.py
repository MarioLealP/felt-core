from felt_core.email_watcher import _decode_header, _sender_display_name


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
