import httpx
from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

skill = load_plugin_module("web_search/skill.py")


def _mock(monkeypatch, seen, results):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": results})

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(skill.httpx, "AsyncClient", lambda **kw: orig(transport=transport, **kw))


async def test_asks_the_searxng_from_the_settings(monkeypatch):
    seen = []
    _mock(monkeypatch, seen, [{"title": "Porto", "content": "A city in Portugal."}])
    config = FeltConfig(web_search_url="http://searxng.example/search")
    ctx = ExecutionContext(config=config, original_message="")
    result = await skill.handle({"query": "porto"}, ctx)
    assert str(seen[0].url) == "http://searxng.example/search?q=porto&format=json"
    assert result.text == "- Porto: A city in Portugal."


async def test_no_results(monkeypatch):
    _mock(monkeypatch, [], [])
    ctx = ExecutionContext(config=FeltConfig(), original_message="")
    result = await skill.handle({"query": "nothing"}, ctx)
    assert result.text == "No search results found."
