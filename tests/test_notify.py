from felt_core.config import FeltConfig
from felt_core.notify import notify


class _ExplodingClient:
    async def post(self, url, content, headers):
        raise AssertionError("must not call ntfy when publish config is incomplete")


def _config(
    *,
    ntfy_url: str | None = "https://ntfy.example.com",
    ntfy_publish_topic: str | None = "felt-alerts",
    ntfy_publish_user: str | None = "felt",
    ntfy_publish_password: str | None = "secret",
) -> FeltConfig:
    return FeltConfig(
        ntfy_url=ntfy_url,
        ntfy_publish_topic=ntfy_publish_topic,
        ntfy_publish_user=ntfy_publish_user,
        ntfy_publish_password=ntfy_publish_password,
    )


async def test_returns_false_when_url_missing():
    config = _config(ntfy_url=None)
    assert await notify(config, "hi") is False


async def test_returns_false_when_publish_topic_missing():
    config = _config(ntfy_publish_topic=None)
    assert await notify(config, "hi") is False


async def test_returns_false_when_publish_user_missing():
    config = _config(ntfy_publish_user=None)
    assert await notify(config, "hi") is False


async def test_returns_false_when_publish_password_missing(monkeypatch):
    # WP-40 audit fix: a publish user set without a password used to slip
    # past this guard and build a half-`None` httpx auth tuple, which
    # would fail at the real request instead of failing closed here.
    config = _config(ntfy_publish_password=None)
    monkeypatch.setattr("httpx.AsyncClient.post", _ExplodingClient().post)

    assert await notify(config, "hi") is False
