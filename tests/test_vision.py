"""felt-vision: Qwen3-VL-2B describes the screenshot, felt-llm reacts to the
words (vision spike 2026-10-21)."""

import json

import httpx
import pytest

from felt_core import local_llm, vision
from felt_core.brain import screen_reactions
from felt_core.config import FeltConfig


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


_REAL_CLIENT = httpx.AsyncClient


def _mock_vision(monkeypatch, handler):
    def factory(*args, **kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(vision.httpx, "AsyncClient", factory)


def _reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


async def test_describe_sends_the_image_with_its_window(tmp_path, monkeypatch):
    config = _config(tmp_path, vision_url="http://felt-vision:8080/")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return _reply("  Firefox shows the\nWikipedia page   'Lighthouse'. ")

    _mock_vision(monkeypatch, handler)
    text = await vision.describe(b"jpeg", "firefox", "Lighthouse - Wikipedia", config, poe=False)

    assert text == "Firefox shows the Wikipedia page 'Lighthouse'."
    assert seen["url"] == "http://felt-vision:8080/v1/chat/completions"
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "image_url"
    assert 'firefox - "Lighthouse - Wikipedia"' in parts[1]["text"]
    assert seen["body"]["max_tokens"] == 90


async def test_a_tooltip_keeps_its_lines(tmp_path, monkeypatch):
    config = _config(tmp_path, vision_url="http://felt-vision:8080")
    _mock_vision(monkeypatch, lambda r: _reply("Quill Rain\nShortbow\n40% less Attack Damage"))
    text = await vision.describe(b"jpeg", "pathofexile2", "Path of Exile 2", config, poe=True)
    assert text == "Quill Rain\nShortbow\n40% less Attack Damage"


async def test_felt_vision_down_is_unavailable(tmp_path, monkeypatch):
    config = _config(tmp_path, vision_url="http://felt-vision:8080")
    _mock_vision(monkeypatch, lambda r: httpx.Response(503))
    with pytest.raises(local_llm.LocalLLMUnavailable):
        await vision.describe(b"jpeg", "firefox", "x", config, poe=False)
    _mock_vision(monkeypatch, lambda r: _reply("   "))
    with pytest.raises(local_llm.LocalLLMUnavailable, match="nothing"):
        await vision.describe(b"jpeg", "firefox", "x", config, poe=False)


async def test_with_felt_vision_bonsai_gets_words_not_the_image(tmp_path, fake_llm, monkeypatch):
    config = _config(tmp_path, vision_url="http://felt-vision:8080")
    asked = {}

    async def describe(jpeg, app, title, cfg, *, poe):
        asked.update(app=app, title=title, poe=poe)
        return "Hacker News front page; top story 'Eight Bits of Trust'."

    monkeypatch.setattr(vision, "describe", describe)
    fake_llm.script = ['{"seen": "HN", "line": "Crypto drama again?", "emotion": "amused"}']

    result = await screen_reactions.react_to_screen(
        b"jpeg", "firefox", "Hacker News", config, session_id="v", trigger="manual"
    )

    assert result.reply == "Crypto drama again?"
    assert asked == {"app": "firefox", "title": "Hacker News", "poe": False}
    user = fake_llm.calls[0]["messages"][-1]["content"]
    assert isinstance(user, str) and "Eight Bits of Trust" in user
    assert "Keep names and terms exactly" in user and "Look at the screenshot" not in user


async def test_without_felt_vision_the_image_goes_to_bonsai(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ['{"seen": "a game", "line": "nice!", "emotion": "happy"}']
    await screen_reactions.react_to_screen(b"jpeg", "game.exe", "Game", config, trigger="manual")
    user = fake_llm.calls[0]["messages"][-1]["content"]
    assert isinstance(user, list) and user[1]["type"] == "image_url"
