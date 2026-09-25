"""Every local chat call sends num_ctx explicitly (2026-09-25: the chat
model shares the Nitro5's 6 GB card with felt-voice, so the context size,
and with it the KV-cache VRAM, must not depend on Ollama's default)."""

import httpx

from felt_core import brain
from felt_core.config import FeltConfig


class _FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": "hi"}}


def _capture(monkeypatch):
    sent = []

    async def fake_post(self, url, json):
        sent.append(json)
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return sent


async def test_num_ctx_sent_with_num_predict(monkeypatch):
    sent = _capture(monkeypatch)
    config = FeltConfig(_env_file=None)

    await brain._ollama_chat([{"role": "user", "content": "x"}], config.ollama_model, config, max_tokens=80)

    assert sent[0]["model"] == "qwen2.5:3b"
    assert sent[0]["options"] == {"num_ctx": 4096, "num_predict": 80}


async def test_num_ctx_sent_without_max_tokens(monkeypatch):
    sent = _capture(monkeypatch)
    config = FeltConfig(_env_file=None, ollama_num_ctx=2048)

    await brain._ollama_chat([{"role": "user", "content": "x"}], "m", config)

    assert sent[0]["options"] == {"num_ctx": 2048}
