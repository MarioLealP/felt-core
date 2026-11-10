"""Secret settings: SecretStr in FeltConfig, plain only through config.reveal() (Meru standard)."""

import httpx

from felt_core.config import FeltConfig, reveal

SECRETS = (
    "api_token",
    "responder_token",
    "board_token",
    "ntfy_password",
    "ntfy_publish_password",
    "zabbix_api_token",
    "ha_token",
    "spotify_client_secret",
    "spotify_refresh_token",
    "vault_probe",
)


def _config(**values):
    return FeltConfig(_env_file=None, **values)


def test_secrets_never_print():
    value = "s3cret-do-not-print"
    cfg = _config(**dict.fromkeys(SECRETS, value))
    assert value not in repr(cfg)
    assert value not in str(cfg)
    assert value not in str(cfg.model_dump())
    for name in SECRETS:
        assert value not in f"{getattr(cfg, name)}"


def test_reveal_gives_the_value_and_empty_when_unset():
    cfg = _config(ha_token="t-1")
    assert reveal(cfg.ha_token) == "t-1"
    assert reveal(_config(ha_token=None).ha_token) == ""


def test_unset_and_empty_secrets_are_falsy_so_the_guards_still_work():
    assert not _config(ha_token=None).ha_token
    assert not _config(ha_token="").ha_token
    assert _config(ha_token="t").ha_token


async def test_the_guard_sees_a_masked_secret_in_headers_logins_and_bodies(masked_secret_guard):
    cfg = _config(ha_token="t-1")
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("http://ha.invalid/", headers={"Authorization": f"Bearer {reveal(cfg.ha_token)}"})
        assert masked_secret_guard == []
        await client.get("http://ha.invalid/", headers={"Authorization": f"Bearer {cfg.ha_token}"})
    async with httpx.AsyncClient(transport=transport, auth=("u", str(cfg.ha_token))) as client:
        await client.get("http://ntfy.invalid/")
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("http://spotify.invalid/", data={"refresh_token": str(cfg.ha_token)})
    assert len(masked_secret_guard) == 3
    masked_secret_guard.clear()  # seen - don't fail this test at teardown
