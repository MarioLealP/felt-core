from conftest import load_plugin_module

from felt_core import emails
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext


def _load_skill(name: str = "check_email"):
    return load_plugin_module(f"{name}/skill.py")


async def _run(module, db_path, params=None):
    ctx = ExecutionContext(config=FeltConfig(db_path=db_path))
    return await module.handle(params or {}, ctx)


async def test_check_email_with_no_emails(tmp_path):
    result = await _run(_load_skill(), str(tmp_path / "felt.db"))
    assert result.text == "No recent emails."


async def test_check_email_lists_sender_subject_and_the_start(tmp_path):
    db_path = str(tmp_path / "felt.db")
    emails.add(db_path, "<1@x>", "Jane <jane@x.pt>", "Hello there", "How are you?", False)
    result = await _run(_load_skill(), db_path)
    assert result.text == "From Jane: Hello there\n  How are you?"


async def test_check_email_leaves_out_noise(tmp_path):
    db_path = str(tmp_path / "felt.db")
    emails.add(db_path, "<1@x>", "Shop <deals@shop.com>", "Sale", "50% off", True)
    emails.mute(db_path, "shop.com")
    await emails.sort_new(FeltConfig(db_path=db_path))
    result = await _run(_load_skill(), db_path)
    assert result.text == "No recent emails."


async def test_mute_email_sender(tmp_path):
    db_path = str(tmp_path / "felt.db")
    emails.add(db_path, "<1@x>", "Patreon <bingo@mail.patreon.com>", "Post", "", True)
    module = _load_skill("mute_email_sender")
    result = await _run(module, db_path, {"sender": "Patreon"})
    assert result.text == "Muted: emails from patreon.com won't be mentioned again."
    result = await _run(module, db_path, {"sender": "nobody"})
    assert result.text == "No recent email from anyone like 'nobody' - nothing muted."
