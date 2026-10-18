"""Talks to felt-core's /v1/ha/chat with an Authentik access token.

Authentik's client_credentials grant for the ha-felt provider, logged in as
the ha-felt service account with its app password (the one long-lived
credential, kept in this config entry). Tokens live 15 minutes: one is
reused until a minute before it expires, and refreshed once if felt-core
says 401 (revoked, or Authentik rotated its key).
"""

from __future__ import annotations

import time

import aiohttp


class FeltError(Exception):
    """Felt couldn't answer (auth, network or felt-core said no)."""


class FeltClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token_url: str,
        client_id: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._base = url.rstrip("/")
        self._token_url = token_url
        self._form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "username": username,
            "password": password,
            "scope": "profile",
        }
        self._token: str | None = None
        self._expires = 0.0

    async def _access_token(self, fresh: bool = False) -> str:
        if fresh or self._token is None or time.monotonic() > self._expires - 60:
            try:
                async with self._session.post(
                    self._token_url, data=self._form, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200 or "access_token" not in body:
                        raise FeltError(f"Authentik refused the login ({resp.status})")
            except aiohttp.ClientError as e:
                raise FeltError(f"Authentik unreachable ({e!r})") from e
            self._token = body["access_token"]
            self._expires = time.monotonic() + float(body.get("expires_in", 900))
        assert self._token is not None
        return self._token

    async def check(self) -> None:
        """Config flow: can we log in at all?"""
        await self._access_token(fresh=True)

    async def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        for attempt in (0, 1):
            token = await self._access_token(fresh=attempt == 1)
            try:
                async with self._session.request(
                    method,
                    self._base + path,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=90),
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        continue
                    if resp.status != 200:
                        raise FeltError(f"felt-core said {resp.status}")
                    return await resp.json()
            except aiohttp.ClientError as e:
                raise FeltError(f"felt-core unreachable ({e!r})") from e
        raise FeltError("felt-core refused the token")

    async def chat(self, text: str, language: str | None) -> str:
        reply = await self._call("POST", "/v1/ha/chat", {"text": text, "language": language})
        return reply["reply"]

    async def controls(self) -> dict:
        """{quiet, hide, dj, desktop_online, mood}"""
        return await self._call("GET", "/v1/ha/controls")

    async def systems(self) -> dict:
        """{areas: {name: {status, count}}, needs_you, latest: [...]}"""
        return await self._call("GET", "/v1/ha/systems")

    async def set_control(self, name: str, on: bool) -> dict:
        return await self._call("POST", "/v1/ha/controls", {"name": name, "on": on})
