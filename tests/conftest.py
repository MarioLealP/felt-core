"""pytest-recording (VCR) config — real HTTP calls to public APIs
(poe.ninja, the official PoE trade API, etc.) get recorded to a cassette
once and replayed from disk on every subsequent test run, instead of
hitting the real service every time `pytest` runs. Default record mode
is "none" (pytest-recording's own default): a test whose cassette
doesn't have a matching recorded interaction fails loudly rather than
silently falling back to a real request. To (re)record a cassette after
writing/changing a `@pytest.mark.vcr` test, run once with
`uv run pytest --record-mode=once path/to/test.py`, then commit the
generated `tests/cassettes/...` file.
"""

import pytest


def _scrub_response_cookies(response):
    """`filter_headers` only strips REQUEST headers — vcrpy doesn't wire
    it into response recording at all (checked its source directly,
    2026-09-14). The trade API hands back a Set-Cookie with an anonymous
    per-request POESESSID even for unauthenticated calls; not a real
    credential, but a session-shaped token has no business sitting in a
    committed test fixture, so it needs this explicit response filter."""
    headers = response.get("headers", {})
    for key in list(headers):
        if key.lower() in ("set-cookie", "cookie"):
            del headers[key]
    return response


@pytest.fixture(scope="module")
def vcr_config():
    return {
        # Body matters here — some tests POST different query bodies to
        # the same trade-API URL in one test function, and body-matching
        # keeps each request paired with the right recorded response
        # instead of relying on recorded order alone.
        "match_on": ["method", "scheme", "host", "port", "path", "query", "body"],
        "filter_headers": ["authorization", "cookie"],
        "before_record_response": _scrub_response_cookies,
    }
