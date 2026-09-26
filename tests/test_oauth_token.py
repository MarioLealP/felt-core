"""felt_core.oauth_token.refresh_access_token (WP-40) - the OAuth
refresh-token flow factored out of spotify_client.py and
google_calendar.py, which each had an identical, independently-written
copy of this exact cache-check/POST/store dance."""

import time

import httpx

from felt_core.oauth_token import refresh_access_token


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _client(monkeypatch, response_data=None, error=None):
    calls = []

    async def fake_post(self, url, data=None, headers=None):
        calls.append({"url": url, "data": data, "headers": headers})
        if error is not None:
            raise error
        return _FakeResponse(response_data)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


async def test_fetches_and_caches_a_fresh_token(monkeypatch):
    calls = _client(monkeypatch, {"access_token": "tok-1", "expires_in": 3600})
    cache: dict = {"access_token": None, "expires_at": 0.0}

    async with httpx.AsyncClient() as client:
        token = await refresh_access_token(
            client, cache, "https://example.com/token", data={"grant_type": "refresh_token"}
        )

    assert token == "tok-1"
    assert cache["access_token"] == "tok-1"
    assert len(calls) == 1
    assert calls[0]["data"] == {"grant_type": "refresh_token"}


async def test_returns_the_cached_token_without_a_new_request(monkeypatch):
    calls = _client(monkeypatch, {"access_token": "should-not-be-used", "expires_in": 3600})
    cache = {"access_token": "cached-tok", "expires_at": time.monotonic() + 3600}

    async with httpx.AsyncClient() as client:
        token = await refresh_access_token(client, cache, "https://example.com/token", data={})

    assert token == "cached-tok"
    assert calls == []  # no request made - the cache was still fresh


async def test_refreshes_when_within_the_60s_safety_margin_of_expiry(monkeypatch):
    calls = _client(monkeypatch, {"access_token": "refreshed-tok", "expires_in": 3600})
    # 30s left - inside the 60s margin, so a token this close to expiring
    # must never be handed out as still-good.
    cache = {"access_token": "about-to-expire", "expires_at": time.monotonic() + 30}

    async with httpx.AsyncClient() as client:
        token = await refresh_access_token(client, cache, "https://example.com/token", data={})

    assert token == "refreshed-tok"
    assert len(calls) == 1


async def test_defaults_expiry_to_one_hour_when_the_provider_omits_it(monkeypatch):
    _client(monkeypatch, {"access_token": "tok"})  # no expires_in field
    cache: dict = {"access_token": None, "expires_at": 0.0}

    async with httpx.AsyncClient() as client:
        await refresh_access_token(client, cache, "https://example.com/token", data={})

    assert 3599 <= cache["expires_at"] - time.monotonic() <= 3600


async def test_passes_headers_through_when_given(monkeypatch):
    calls = _client(monkeypatch, {"access_token": "tok", "expires_in": 3600})
    cache: dict = {"access_token": None, "expires_at": 0.0}

    async with httpx.AsyncClient() as client:
        await refresh_access_token(
            client,
            cache,
            "https://example.com/token",
            data={"grant_type": "refresh_token"},
            headers={"Authorization": "Basic abc123"},
        )

    assert calls[0]["headers"] == {"Authorization": "Basic abc123"}


async def test_raises_when_the_token_endpoint_errors(monkeypatch):
    _client(monkeypatch, error=httpx.ConnectTimeout("slow"))
    cache: dict = {"access_token": None, "expires_at": 0.0}

    try:
        async with httpx.AsyncClient() as client:
            await refresh_access_token(client, cache, "https://example.com/token", data={})
        raise AssertionError("expected the connect timeout to propagate")
    except httpx.ConnectTimeout:
        pass
