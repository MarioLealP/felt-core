"""WP-29: the poe2_wiki skill turns poe2-rag's search hits into passages."""

import importlib.util
from pathlib import Path

import httpx

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_spec = importlib.util.spec_from_file_location(
    "poe2_wiki_skill", Path(__file__).parents[1] / "plugins/poe2_wiki/skill.py"
)
assert _spec is not None and _spec.loader is not None
skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill)


def _mock(monkeypatch, hits, seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"hits": hits})

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(skill.httpx, "AsyncClient", lambda **kw: orig(transport=transport, **kw))


async def test_passages_are_trimmed_and_labelled(monkeypatch):
    seen = []
    long_text = "word " * 400
    _mock(
        monkeypatch,
        [
            {"title": "Grim Feast", "section": "", "url": "u", "text": "Grim Feast is a buff."},
            {"title": "Minions", "section": "Revive", "url": "u", "text": long_text},
        ],
        seen,
    )
    r = await skill.handle(
        {"query": "what does grim feast do"}, ExecutionContext(config=FeltConfig())
    )
    assert "[Grim Feast] Grim Feast is a buff." in r.text
    assert "[Minions - Revive]" in r.text
    assert len(r.text) < 1000
    assert seen[0].url.path == "/api/search"


async def test_no_hits(monkeypatch):
    _mock(monkeypatch, [], [])
    r = await skill.handle({"query": "nothing"}, ExecutionContext(config=FeltConfig()))
    assert "nothing on that" in r.text
