import importlib.util
from pathlib import Path

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "search_documents" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("search_documents_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reports_unconfigured_when_no_url_set():
    skill = _load_skill()
    config = FeltConfig(drive_rag_url=None)
    ctx = ExecutionContext(config=config, original_message="find that pdf about taxes")

    result = await skill.handle({"question": "taxes"}, ctx)

    assert "isn't configured" in result.text
