"""felt.tell (2026-11-02): any text from an HA automation, in her words, at the
desk and on the phone."""

import pytest

from felt_core.clients import local_llm, phone
from felt_core.config import FeltConfig
from felt_core.core import features, ha_tell


@pytest.fixture
def out(monkeypatch):
    sent = {"desk": [], "phone": []}

    def desk(config, kind, text, **kw):
        sent["desk"].append((kind, text))
        return 1

    async def notify(config, title, message, tag):
        sent["phone"].append((title, message, tag))

    async def her(messages, config, include_facts=True):
        return "Your dehumidifier's tank is full, empty it when you can."

    monkeypatch.setattr(ha_tell, "enqueue_proactive", desk)
    monkeypatch.setattr(phone, "notify", notify)
    monkeypatch.setattr("felt_core.brain.replies.local_reply", her)
    return sent


def _config(tmp_path) -> FeltConfig:
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


def test_facts_carry_the_text():
    assert "the tank is full" in ha_tell.facts("the tank is full")


async def test_she_words_it_and_it_goes_to_desk_and_phone(tmp_path, out):
    line = await ha_tell.tell(_config(tmp_path), "Dehumidifier tank full", "Dehumidifier", "dehum")
    assert line == "Your dehumidifier's tank is full, empty it when you can."
    assert out["desk"] == [("ha", line)]
    assert out["phone"] == [("Dehumidifier", line, "felt-ha-dehum")]


async def test_llm_down_sends_the_text_as_is(tmp_path, out, monkeypatch):
    async def down(messages, config, include_facts=True):
        raise local_llm.LocalLLMUnavailableError("down")

    monkeypatch.setattr("felt_core.brain.replies.local_reply", down)
    line = await ha_tell.tell(_config(tmp_path), "Dehumidifier tank full", "Felt", "ha")
    assert line == "Dehumidifier tank full"
    assert out["phone"][0][1] == "Dehumidifier tank full"


async def test_phone_down_still_says_it_at_the_desk(tmp_path, out, monkeypatch):
    async def unreachable(config, title, message, tag):
        raise phone.PhoneUnavailableError("down")

    monkeypatch.setattr(phone, "notify", unreachable)
    assert await ha_tell.tell(_config(tmp_path), "x", "Felt", "ha") is not None
    assert len(out["desk"]) == 1


async def test_switched_off_says_nothing(tmp_path, out):
    config = _config(tmp_path)
    await features.set_enabled(config.db_path, ha_tell.FEATURE, False)
    assert await ha_tell.tell(config, "x", "Felt", "ha") is None
    assert out == {"desk": [], "phone": []}
