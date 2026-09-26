import asyncio
import time

import felt_core.brain.orchestrator as orchestrator
import felt_core.brain.replies as brain
from felt_core.config import FeltConfig


def _config(tmp_path, **kwargs):
    return FeltConfig(
        db_path=str(tmp_path / "felt.db"),
        plugins_dir=str(tmp_path / "no-plugins-here"),
        **kwargs,
    )


async def test_no_skill_chat_runs_router_and_privacy_concurrently(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def slow_decide_tool(message, registry, cfg):
        await asyncio.sleep(0.3)
        return None

    async def slow_is_safe_for_cloud(text, cfg):
        await asyncio.sleep(0.3)
        return False

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "a local reply"

    monkeypatch.setattr(orchestrator, "decide_tool", slow_decide_tool)
    monkeypatch.setattr(brain, "is_safe_for_cloud", slow_is_safe_for_cloud)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    start = time.monotonic()
    result = await orchestrator.chat("hello", config, session_id="wp03-test")
    elapsed = time.monotonic() - start

    assert result.reply == "a local reply"
    # Concurrent (~0.3s) vs serial (~0.6s) - 0.5s is a safe boundary between
    # the two without being flaky under normal test-machine jitter.
    assert elapsed < 0.5


async def test_skill_path_cancels_the_privacy_task(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    cancelled = []

    async def fast_decide_tool(message, registry, cfg):
        # A real yield (not just an instant return) so the concurrently-
        # created privacy task actually gets scheduled and reaches its own
        # `await` before we cancel it - otherwise asyncio may cancel a task
        # that has never run a single step of its body yet, which throws
        # CancelledError in before the task's own try/except ever runs.
        await asyncio.sleep(0)
        return ("some_skill", {})

    async def slow_is_safe_for_cloud(text, cfg):
        try:
            await asyncio.sleep(5)
            return True
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "tool reply"

    monkeypatch.setattr(orchestrator, "decide_tool", fast_decide_tool)
    monkeypatch.setattr(brain, "is_safe_for_cloud", slow_is_safe_for_cloud)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    result = await orchestrator.chat("do a thing", config, session_id="wp03-test")

    assert result.reply == "tool reply"
    assert cancelled == [True]


async def test_privacy_exception_still_means_local(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def instant_decide_tool(message, registry, cfg):
        return None

    async def broken_is_safe_for_cloud(text, cfg):
        raise RuntimeError("boom")

    async def fake_cloud_chat(message, history, cfg):
        raise AssertionError("cloud must not be called when the privacy check failed")

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "local fallback reply"

    monkeypatch.setattr(orchestrator, "decide_tool", instant_decide_tool)
    monkeypatch.setattr(brain, "is_safe_for_cloud", broken_is_safe_for_cloud)
    monkeypatch.setattr(brain, "_cloud_chat", fake_cloud_chat)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    result = await orchestrator.chat("hello", config, session_id="wp03-test")

    assert result.reply == "local fallback reply"
