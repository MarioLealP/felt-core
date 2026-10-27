"""WP-43: her "btw, you've got mail" - what she mentions, what she never
does, once each, at most once an hour, and "tell me more" right after."""

import importlib.util
from pathlib import Path

import pytest

from felt_core import emails, features, local_llm
from felt_core.brain import orchestrator
from felt_core.config import FeltConfig
from felt_core.db import EMAIL, _connect

_spec = importlib.util.spec_from_file_location(
    "felt_ha_mail", Path(__file__).parent.parent / "ha/custom_components/felt/mail.py"
)
assert _spec and _spec.loader
ha_mail = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ha_mail)


def _config(tmp_path, gap_s: int = 3600) -> FeltConfig:
    return FeltConfig(db_path=str(tmp_path / "felt.db"), email_mention_gap_s=gap_s)


def _states(db: str) -> dict[str, str]:
    conn = _connect(db)
    rows = conn.execute("SELECT subject, state FROM emails ORDER BY id").fetchall()
    conn.close()
    return dict(rows)


def _email_lines(db: str) -> list[tuple]:
    conn = _connect(db)
    rows = conn.execute("SELECT content, kind FROM messages WHERE kind = ?", (EMAIL,)).fetchall()
    conn.close()
    return rows


def _proactive(db: str) -> list[tuple]:
    conn = _connect(db)
    rows = conn.execute("SELECT kind, text FROM proactive").fetchall()
    conn.close()
    return rows


def _ana(db: str, key: str = "<1@x>", subject: str = "Dinner") -> int | None:
    return emails.add(db, key, "Ana Silva <ana@gmail.com>", subject, "Dinner on Friday?", False)


def _patreon(db: str, key: str = "<2@x>", subject: str = "New post") -> int | None:
    return emails.add(
        db, key, "Patreon <bingo@mail.patreon.com>", subject, "<p>A new post!</p>", True
    )


# pure parts


def test_sender_split():
    assert emails.split_sender("Patreon <Bingo@Patreon.com>") == ("Patreon", "bingo@patreon.com")
    assert emails.split_sender("ana@gmail.com") == ("ana@gmail.com", "ana@gmail.com")


def test_html_only_mail_becomes_text():
    body = "<style>p{color:red}</style><p>Hi &amp; bye</p><script>x()</script>"
    assert emails.plain_text(body) == "Hi & bye"


def test_domains():
    assert emails.domain_of("bingo@mail.patreon.com") == "patreon.com"
    assert emails.domain_of("news@loja.com.pt") == "loja.com.pt"
    assert emails.domain_of("a@x.news.loja.com.pt") == "loja.com.pt"


def test_noise_patterns():
    assert emails.is_noise("bingo@mail.patreon.com", ["patreon.com"])
    assert not emails.is_noise("bingo@notpatreon.com", ["patreon.com"])
    assert emails.is_noise("ana@gmail.com", ["ana@gmail.com"])
    assert not emails.is_noise("rui@gmail.com", ["ana@gmail.com"])


def test_muting_a_list_takes_its_domain_a_person_only_their_address():
    senders = [("Patreon", "bingo@mail.patreon.com", True), ("Ana Silva", "ana@gmail.com", False)]
    assert emails.noise_pattern("patreon", senders) == "patreon.com"
    assert emails.noise_pattern("Ana", senders) == "ana@gmail.com"
    assert emails.noise_pattern("linkedin.com", senders) == "linkedin.com"
    assert emails.noise_pattern("x@y.com", senders) == "x@y.com"
    assert emails.noise_pattern("nobody", senders) is None


def test_one_line_names_three_and_counts_the_rest():
    facts, fallback = emails.mention_facts([(f"S{i}", f"T{i}") for i in range(5)])
    assert 'from S0: "T0"; from S1: "T1"; from S2: "T2"; and 2 more' in facts
    assert fallback == 'Btw, you\'ve got mail from S0: "T0" and 4 more.'


def test_the_ha_side_forwards_message_id_and_list_mail():
    body = ha_mail.payload(
        {
            "sender": "Patreon <bingo@patreon.com>",
            "subject": "New post",
            "text": "hi",
            "uid": "4",
            "headers": {"Message-ID": ("<abc@x>",), "List-Unsubscribe": ("<mailto:u@x>",)},
        }
    )
    assert body == {
        "key": "<abc@x>",
        "sender": "Patreon <bingo@patreon.com>",
        "subject": "New post",
        "text": "hi",
        "bulk": True,
    }
    plain = ha_mail.payload(
        {"sender": "a@b.c", "uid": "4", "username": "u", "folder": "INBOX", "headers": {}}
    )
    assert plain is not None and plain["key"] == "u:INBOX:4" and plain["bulk"] is False
    assert ha_mail.payload({"subject": "no sender"}) is None


# the flow


def test_the_same_mail_is_stored_once(tmp_path):
    db = str(tmp_path / "felt.db")
    assert _ana(db) is not None
    assert _ana(db) is None


async def test_a_person_s_mail_is_mentioned_once(tmp_path, fake_llm):
    config = _config(tmp_path)
    _ana(config.db_path)
    fake_llm.script = ["Btw, Ana wrote about dinner."]
    await emails.tick(config)
    await emails.tick(config)
    assert _states(config.db_path) == {"Dinner": "mentioned"}
    assert _email_lines(config.db_path) == [("Btw, Ana wrote about dinner.", EMAIL)]
    assert _proactive(config.db_path) == [("email", "Btw, Ana wrote about dinner.")]
    assert len(fake_llm.calls) == 1  # no model call for a person's mail, only her line


async def test_at_most_one_mention_an_hour_then_everything_waiting(tmp_path, fake_llm):
    config = _config(tmp_path)
    _ana(config.db_path)
    await emails.tick(config)
    _ana(config.db_path, "<3@x>", "Lunch")
    _ana(config.db_path, "<4@x>", "Film")
    await emails.tick(config)
    assert _states(config.db_path) == {"Dinner": "mentioned", "Lunch": "waiting", "Film": "waiting"}
    await emails.tick(_config(tmp_path, gap_s=0))
    assert _states(config.db_path)["Film"] == "mentioned"
    assert len(_email_lines(config.db_path)) == 2
    assert 'from Ana Silva: "Lunch"; from Ana Silva: "Film"' in str(fake_llm.calls[-1]["messages"])


async def test_noise_senders_are_never_mentioned(tmp_path, fake_llm):
    config = _config(tmp_path)
    _patreon(config.db_path)
    assert emails.mute(config.db_path, "patreon") == "patreon.com"
    _patreon(config.db_path, "<5@x>", "Another post")
    await emails.tick(config)
    assert set(_states(config.db_path).values()) == {"noise"}
    assert _email_lines(config.db_path) == []
    assert fake_llm.calls == []


async def test_list_mail_goes_past_her_first(tmp_path, fake_llm):
    config = _config(tmp_path, gap_s=0)
    _patreon(config.db_path, "<6@x>", "Sale!")
    fake_llm.script = ['{"reason": "a sale", "mention": false}']
    await emails.tick(config)
    assert _states(config.db_path) == {"Sale!": "skipped"}
    assert fake_llm.calls[0]["json_schema"] == emails.JUDGE_SCHEMA
    assert "A new post!" in fake_llm.calls[0]["messages"][0]["content"]  # tags stripped

    _patreon(config.db_path, "<7@x>", "New episode")
    fake_llm.script = [
        '{"reason": "a creator\'s post", "mention": true}',
        "Btw, a new Patreon post.",
    ]
    await emails.tick(config)
    assert _states(config.db_path)["New episode"] == "mentioned"


async def test_list_mail_waits_while_her_model_is_busy(tmp_path, fake_llm):
    config = _config(tmp_path)
    _patreon(config.db_path)
    fake_llm.script = [local_llm.LocalLLMUnavailable("busy")]
    await emails.tick(config)
    assert _states(config.db_path) == {"New post": "new"}


async def test_switched_off_says_nothing(tmp_path, fake_llm):
    config = _config(tmp_path)
    features.set_enabled(config.db_path, "email.new_mail", False)
    _ana(config.db_path)
    await emails.tick(config)
    assert _email_lines(config.db_path) == []


async def test_muting_also_drops_what_was_waiting(tmp_path, fake_llm):
    config = _config(tmp_path)
    _ana(config.db_path)
    await emails.tick(config)  # mentioned
    _ana(config.db_path, "<8@x>", "Again")
    await emails.tick(config)  # waiting (gap)
    assert emails.mute(config.db_path, "ana") == "ana@gmail.com"
    assert _states(config.db_path) == {"Dinner": "mentioned", "Again": "noise"}


@pytest.mark.parametrize("answer", ["yes", "tell me more", "What does it say?"])
async def test_tell_me_more_right_after_summarises(tmp_path, fake_llm, answer):
    config = _config(tmp_path)
    _ana(config.db_path)
    fake_llm.script = ["Btw, Ana wrote.", "Ana wants dinner on Friday."]
    await emails.tick(config)
    result = await orchestrator.chat(answer, config, "dashboard")
    assert result.reply == "Ana wants dinner on Friday."
    assert "Dinner on Friday?" in fake_llm.calls[-1]["messages"][-1]["content"]
    assert _states(config.db_path) == {"Dinner": "summarised"}
    assert emails.just_mentioned(config.db_path) == []


async def test_a_list_mail_she_cant_judge_yet_doesnt_hold_back_a_person(tmp_path, fake_llm):
    config = _config(tmp_path)
    _patreon(config.db_path)
    _ana(config.db_path)
    fake_llm.script = [local_llm.LocalLLMUnavailable("busy")]
    await emails.sort_new(config)
    assert _states(config.db_path) == {"New post": "new", "Dinner": "waiting"}
