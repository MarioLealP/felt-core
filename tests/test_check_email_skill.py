import importlib.util
from pathlib import Path

from felt_core.db import save_pending_email
from felt_core.plugin import ExecutionContext

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "check_email" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("check_email_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(module, db_path):
    class Config:
        pass

    config = Config()
    config.db_path = db_path
    ctx = ExecutionContext(config=config)
    return await module.handle({}, ctx)


async def test_check_email_with_no_emails(tmp_path):
    module = _load_skill()
    db_path = str(tmp_path / "felt.db")
    result = await _run(module, db_path)
    assert result.text == "No recent emails logged."


async def test_check_email_lists_sender_and_subject(tmp_path):
    module = _load_skill()
    db_path = str(tmp_path / "felt.db")
    save_pending_email(db_path, "1", "Jane", "Hello there")
    result = await _run(module, db_path)
    assert result.text == "From Jane: Hello there"
