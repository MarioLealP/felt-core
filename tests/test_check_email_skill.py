from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.db import save_pending_email
from felt_core.plugin import ExecutionContext


def _load_skill():
    return load_plugin_module("check_email/skill.py")


async def _run(module, db_path):
    ctx = ExecutionContext(config=FeltConfig(db_path=db_path))
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
