"""Shared OAuth refresh-token flow (WP-40) - spotify_client.py and
google_calendar.py (gone since 2026-10-19: the calendar is read through
Home Assistant) each had an identical, independently-written
`_get_access_token`: a module-level `{"access_token", "expires_at"}`
cache dict, a 60s expiry safety margin so a token that's about to expire
mid-request isn't handed out as still-good, and a `grant_type=
refresh_token` POST. Factored into one helper here, parameterized by the
token URL and the POST body/headers each provider actually needs (Spotify
sends its client credentials as an HTTP Basic header; Google Calendar
sends them as plain POST fields) - so a third OAuth integration doesn't
have to copy this pattern a third time.

Each caller keeps its own cache dict (still one per provider, still
module-level - a single account each, no need for anything fancier for a
personal assistant) and just calls `refresh_access_token()` instead of
hand-rolling the check-cache/POST/store-in-cache dance itself.
"""

import time

import httpx


async def refresh_access_token(
    client: httpx.AsyncClient,
    cache: dict,
    token_url: str,
    data: dict,
    headers: dict | None = None,
) -> str:
    """Returns a cached access token if it's not within 60s of expiring,
    otherwise POSTs `data` (and `headers`, if given) to `token_url` and
    caches the result. `cache` is the caller's own module-level
    `{"access_token": ..., "expires_at": ...}` dict - mutated in place, so
    every caller keeps a separate cache per provider/account."""
    if cache["access_token"] and time.monotonic() < cache["expires_at"] - 60:
        return cache["access_token"]

    response = await client.post(token_url, data=data, headers=headers)
    response.raise_for_status()
    body = response.json()
    cache["access_token"] = body["access_token"]
    cache["expires_at"] = time.monotonic() + body.get("expires_in", 3600)
    return cache["access_token"]
