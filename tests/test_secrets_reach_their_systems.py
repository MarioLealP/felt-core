"""Each secret reaches its system as the real value, not SecretStr's "**********".

The conftest guard (masked_secret_guard) checks every request; these tests drive the calls no other test sends with
a secret set. Hosts are `.invalid`, so nothing leaves the machine: the guard sees the request, then the lookup fails.
"""

import asyncio
import base64
import json

import httpx2
import pytest
from conftest import load_plugin_module

from felt_core import ha_calendar, leave_by, notify, ntfy_listener, spotify_client
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

SECRET = "s3cret-value"


def _config(tmp_path, **values):
    return FeltConfig(_env_file=None, db_path=str(tmp_path / "felt.db"), **values)


def _sent_value_in(request: httpx2.Request) -> bool:
    return SECRET in request.headers.get("authorization", "") or SECRET in request.content.decode()


def _sent_value_in_basic(request: httpx2.Request) -> bool:
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    return scheme == "Basic" and base64.b64decode(encoded).decode().endswith(f":{SECRET}")


async def test_zabbix_token_in_the_header(tmp_path):
    skill = load_plugin_module("get_container_status/skill.py")
    config = _config(tmp_path, zabbix_url="http://zabbix.invalid", zabbix_api_token=SECRET)
    seen = []
    transport = httpx2.MockTransport(
        lambda r: seen.append(_sent_value_in(r)) or httpx2.Response(200, json={"result": []})
    )
    async with httpx2.AsyncClient(transport=transport) as client:
        await skill._zabbix_call(client, config, "host.get", {})
    assert seen == [True]


async def test_ha_token_for_the_travel_time(tmp_path):
    config = _config(tmp_path, ha_url="http://ha.invalid", ha_token=SECRET)
    assert await leave_by.drive_minutes(config, "Porto") is None  # .invalid: no answer, and nothing masked sent


async def test_ntfy_publish_login(tmp_path):
    config = _config(
        tmp_path,
        ntfy_url="http://ntfy.invalid",
        ntfy_publish_topic="t",
        ntfy_publish_user="felt",
        ntfy_publish_password=SECRET,
    )
    with pytest.raises(httpx2.ConnectError):
        await notify.notify(config, "hi")


async def test_ntfy_listen_login(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep  # ntfy_listener.asyncio is this same module: patching it patches asyncio.sleep too
    monkeypatch.setattr(ntfy_listener.asyncio, "sleep", lambda *_: real_sleep(10))  # one attempt, then wait
    seen = []
    real_client = httpx2.AsyncClient

    def client(**kwargs):
        # a SecretStr in the auth tuple fails while encoding, before any request: record what is actually sent
        # 401 takes the error path (log, sleep, retry); a clean 200 would reconnect in a loop that never yields
        transport = httpx2.MockTransport(lambda r: seen.append(_sent_value_in_basic(r)) or httpx2.Response(401))
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(ntfy_listener.httpx2, "AsyncClient", client)
    config = _config(tmp_path, ntfy_url="http://ntfy.invalid", ntfy_topic="t", ntfy_user="felt", ntfy_password=SECRET)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(ntfy_listener.listen(config), 2)
    assert seen[:1] == [True]


async def test_spotify_refresh_token_in_the_body(tmp_path, monkeypatch):
    monkeypatch.setattr(spotify_client, "_token_cache", {"access_token": None, "expires_at": 0.0})
    config = _config(tmp_path, spotify_client_id="id", spotify_client_secret=SECRET, spotify_refresh_token=SECRET)
    seen = []

    def answer(request):
        seen.append(SECRET in request.content.decode())
        return httpx2.Response(200, json={"access_token": "a", "expires_in": 3600})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as client:
        await spotify_client._get_access_token(client, config)
    assert seen == [True]


async def test_ha_token_on_the_calendar_websocket(tmp_path, monkeypatch):
    sent = []

    class FakeSocket:
        def __init__(self):
            self.replies = iter([{"type": "auth_required"}, {"type": "auth_ok"}, {"success": True, "result": {}}])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def recv(self):
            return json.dumps(next(self.replies))

        async def send(self, text):
            sent.append(json.loads(text))

    monkeypatch.setattr(ha_calendar.websockets, "connect", lambda *_, **__: FakeSocket())
    config = _config(tmp_path, ha_url="http://ha.invalid", ha_token=SECRET)
    await ha_calendar._ws_call(config, {"type": "ping"})
    assert sent[0] == {"type": "auth", "access_token": SECRET}


def test_skill_context_carries_the_settings(tmp_path):
    # the skills read secrets through ctx.config - the same FeltConfig
    config = _config(tmp_path, zabbix_api_token=SECRET)
    assert ExecutionContext(config=config, original_message="").config.zabbix_api_token == config.zabbix_api_token
