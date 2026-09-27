from felt_core.ntfy_listener import _basic_auth


def test_basic_auth_pairs_user_and_password():
    assert _basic_auth("mario", "secret") == ("mario", "secret")


def test_basic_auth_none_when_neither_set():
    assert _basic_auth(None, None) is None


def test_basic_auth_none_when_user_set_but_password_missing():
    # WP-40 audit fix: a user without a password used to build a
    # half-`None` auth tuple that httpx would reject at connection time -
    # not authenticating at all is the correct fallback, same as "ntfy
    # not configured".
    assert _basic_auth("mario", None) is None


def test_basic_auth_none_when_password_set_but_user_missing():
    assert _basic_auth(None, "secret") is None
