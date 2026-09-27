from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext


def _load_skill():
    return load_plugin_module("search_documents/skill.py")


async def test_reports_unconfigured_when_no_url_set():
    skill = _load_skill()
    config = FeltConfig(drive_rag_url=None)
    ctx = ExecutionContext(config=config, original_message="find that pdf about taxes")

    result = await skill.handle({"question": "taxes"}, ctx)

    assert "isn't configured" in result.text
