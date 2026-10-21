"""WP-65: streamed replies - felt-llm's streamed call, the sentence splitter
on streamed text, and chat_streamed() (chunks as they complete, the
first-sentence fake-claim guard, the late-hit correction, history once)."""

import json
import random

import httpx
import pytest

from felt_core import local_llm
from felt_core.brain import orchestrator
from felt_core.brain.lookup_claims import claims_a_tool_use
from felt_core.brain.result import ChatResult
from felt_core.brain.streaming import ReplyMeta
from felt_core.config import FeltConfig
from felt_core.db import get_history
from felt_core.plugin import SkillResult, UIAction
from felt_core.plugin_loader import LoadedSkill
from felt_core.speech import SpeechChunker, speech_chunks


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


# -- the splitter on streamed text --------------------------------------------

PROSE = [
    "Fried rice! You've got everything for it. Day-old rice is actually better, it fries "
    "without going mushy. Scramble the eggs first, set them aside, then the chicken. "
    "Soy sauce at the end, and some spring onion if you have it.",
    "Dr. Smith said the U.S. version costs 3.5 euros. That's cheap, honestly! "
    "Want me to look for it?",
    "Hm. Okay. So the thing is... I don't know. Maybe tomorrow? We'll see how it goes, I guess.",
    "Short one.",
    'She said "fine." Then she left!? Wow. That is a whole story right there, isn\'t it.',
]


def _streamed(text, rng, first=40):
    chunker = SpeechChunker(first)
    out, i = [], 0
    while i < len(text):
        n = rng.randint(1, 7)
        out += chunker.feed(text[i : i + n])
        i += n
    return out + chunker.close()


@pytest.mark.parametrize("text", PROSE)
def test_streamed_split_matches_speech_chunks(text):
    rng = random.Random(7)
    for _ in range(20):
        assert _streamed(text, rng) == speech_chunks(text)


def test_streamed_split_does_not_depend_on_how_the_text_arrives():
    text = PROSE[0]
    whole = SpeechChunker()
    at_once = whole.feed(text) + whole.close()
    assert _streamed(text, random.Random(1)) == at_once


def test_first_chunk_goes_out_before_the_reply_ends():
    chunker = SpeechChunker()
    assert chunker.feed("Fried rice! You've got everything for it.") == []  # end not confirmed
    assert chunker.feed(" Day") == ["Fried rice! You've got everything for it."]


def test_decimal_and_abbreviation_never_split_mid_stream():
    chunker = SpeechChunker(10)
    assert chunker.feed("It costs 3.") == []
    assert chunker.feed("5 euros at Dr. ") == []
    assert chunker.feed("Smith's. Ok") == ["It costs 3.5 euros at Dr. Smith's."]


def test_a_short_tail_after_the_first_chunk_is_its_own_chunk():
    # the one place it differs from speech_chunks: chunk 0 is already out
    text = "Fried rice! You've got everything for it. Enjoy."
    chunker = SpeechChunker()
    assert chunker.feed(text) == ["Fried rice! You've got everything for it."]
    assert chunker.close() == ["Enjoy."]
    assert speech_chunks(text) == [text]


def test_close_takes_the_trimmed_final_text():
    chunker = SpeechChunker(10)
    chunker.feed("Option three is nice. Option two feels like an office. But honestly, it")
    assert chunker.close("Option three is nice. Option two feels like an office.") == [
        "Option two feels like an office."
    ]


# -- local_llm.chat_stream ----------------------------------------------------


def _sse(*events):
    lines = [f"data: {json.dumps(e)}" for e in events] + ["data: [DONE]"]
    return "\n\n".join(lines) + "\n\n"


def _delta(**delta):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _end(reason="stop", **timings):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}], "timings": timings}


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(local_llm.httpx, "AsyncClient", factory)


async def test_chat_stream_hands_over_text_as_it_comes(tmp_path, monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        body = _sse(
            _delta(role="assistant", content=None),
            _delta(content="Hello"),
            _delta(content=" there!"),
            _end(prompt_n=4, cache_n=261),
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    _mock_client(monkeypatch, handler)
    pieces = []
    reply = await local_llm.chat_stream(
        [{"role": "system", "content": "a"}, {"role": "user", "content": "hi"}],
        on_text=pieces.append,
        max_tokens=50,
        config=_config(tmp_path, local_llm_url="http://llm:8080"),
    )
    assert pieces == ["Hello", " there!"]
    assert reply.text == "Hello there!"
    assert not reply.tool_calls and not reply.truncated
    assert (reply.prompt_n, reply.cache_n) == (4, 261)
    assert seen["body"]["stream"] is True
    assert "tool_choice" not in seen["body"]


async def test_chat_stream_collects_a_tool_call_whole_and_says_nothing(tmp_path, monkeypatch):
    def handler(request):
        start = {"index": 0, "id": "c1", "type": "function", "function": {"name": "get_weather"}}
        body = _sse(
            _delta(tool_calls=[{**start, "function": {**start["function"], "arguments": "{"}}]),
            _delta(tool_calls=[{"index": 0, "function": {"arguments": '"city":"Lis'}}]),
            _delta(tool_calls=[{"index": 0, "function": {"arguments": 'bon"}'}}]),
            _delta(content="ignored"),
            _end("tool_calls"),
        )
        return httpx.Response(200, text=body)

    _mock_client(monkeypatch, handler)
    pieces = []
    reply = await local_llm.chat_stream(
        [{"role": "user", "content": "weather"}],
        on_text=pieces.append,
        max_tokens=50,
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        config=_config(tmp_path),
    )
    assert pieces == []
    assert local_llm.parse_tool_arguments(reply.tool_calls[0]) == (
        "get_weather",
        {"city": "Lisbon"},
    )
    assert reply.tool_calls[0]["id"] == "c1"


async def test_chat_stream_hitting_max_tokens_ends_at_last_sentence(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, text=_sse(_delta(content="One. Two is cut"), _end("length")))

    _mock_client(monkeypatch, handler)
    reply = await local_llm.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_text=lambda _: None,
        max_tokens=5,
        config=_config(tmp_path),
    )
    assert reply.truncated and reply.text == "One."


async def test_chat_stream_errors_are_unavailable(tmp_path, monkeypatch):
    _mock_client(monkeypatch, lambda request: httpx.Response(503, text="loading"))
    with pytest.raises(local_llm.LocalLLMUnavailable):
        await local_llm.chat_stream(
            [{"role": "user", "content": "hi"}],
            on_text=lambda _: None,
            max_tokens=5,
            config=_config(tmp_path),
        )
    with pytest.raises(local_llm.LocalLLMUnavailable):
        await local_llm.chat_stream(
            [], on_text=lambda _: None, max_tokens=5, config=_config(tmp_path, local_llm_url="")
        )


# -- chat_streamed --------------------------------------------------------------


async def _run(gen, metas=None):
    chunks, result = [], None
    async for item in gen:
        if isinstance(item, ChatResult):
            result = item
        elif isinstance(item, ReplyMeta):
            assert not chunks, "a ReplyMeta only ever comes before her first words"
            if metas is not None:
                metas.append(item)
        else:
            chunks.append(item)
    return chunks, result


def _alarm_registry(handled):
    async def handle(params, ctx):
        handled.append(params)
        return SkillResult(text="Alarm set for 07:30.")

    return {
        "set_alarm": LoadedSkill(
            name="set_alarm",
            description="set an alarm",
            parameters_schema={"type": "object", "properties": {"time": {"type": "string"}}},
            handle=handle,
        )
    }


def _alarm_call():
    return {
        "id": "a1",
        "type": "function",
        "function": {"name": "set_alarm", "arguments": json.dumps({"time": "07:30"})},
    }


def _assistant_lines(config, session):
    return [m["content"] for m in get_history(config.db_path, session) if m["role"] == "assistant"]


async def test_chat_streamed_yields_chunks_while_writing(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    fake_llm.script = [PROSE[0]]

    first_while_streaming = None
    chunks, result = [], None
    async for item in orchestrator.chat_streamed("what's for dinner?", config, session_id="s"):
        if isinstance(item, ChatResult):
            result = item
            continue
        if first_while_streaming is None:
            first_while_streaming = fake_llm.streaming
        chunks.append(item)

    assert first_while_streaming is True  # her first words went out mid-reply
    assert chunks == speech_chunks(PROSE[0])
    assert result is not None and result.reply == PROSE[0]
    assert fake_llm.calls[0]["stream"] and not fake_llm.calls[0]["tools"]  # no skills here
    assert _assistant_lines(config, "s") == [PROSE[0]]  # written once, at the end


async def test_chat_streamed_same_result_as_chat(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    fake_llm.script = ["  Morning! Coffee first, then we talk about it, deal?  "]
    plain = await orchestrator.chat("hi", FeltConfig(db_path=str(tmp_path / "a.db")), "s")
    fake_llm.script = ["  Morning! Coffee first, then we talk about it, deal?  "]
    chunks, streamed = await _run(orchestrator.chat_streamed("hi", _config(tmp_path), "s"))
    assert streamed == plain
    assert " ".join(chunks) == plain.reply


@pytest.mark.parametrize(
    "fake_claim",
    [
        "Set for 7:30. You'll be up bright and early tomorrow, sleep well!",
        "Done. Milk's in there, along with the eggs from before.",
        "Okay, ringing it now - it should be going off somewhere near the couch.",
    ],
)
def test_guard_catches_the_e2e_fake_claims(fake_claim):
    first = SpeechChunker().feed(fake_claim + " And")[0]
    assert claims_a_tool_use(first)


async def test_first_sentence_fake_claim_is_never_said(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _alarm_registry(handled))
    fake_llm.script = [
        "Set for 7:30. You'll be up bright and early tomorrow, sleep well!",
        {"text": "", "tool_calls": [_alarm_call()]},
        "Alright, your alarm's on for seven thirty. Sleep well, okay?",
    ]

    chunks, result = await _run(orchestrator.chat_streamed("alarm at 7:30", config, "s"))

    assert handled == [{"time": "07:30"}]
    assert [c.get("stream", False) for c in fake_llm.calls] == [True, False, True]
    assert fake_llm.calls[1]["tool_choice"] == "required"
    assert "Set for 7:30" not in " ".join(chunks)
    assert " ".join(chunks) == "Alright, your alarm's on for seven thirty. Sleep well, okay?"
    assert result.reply == "Alright, your alarm's on for seven thirty. Sleep well, okay?"
    assert "correcting yourself" not in fake_llm.calls[2]["messages"][-1]["content"]
    assert _assistant_lines(config, "s") == [result.reply]


async def test_late_fake_claim_gets_corrected_out_loud(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _alarm_registry(handled))
    opening = "Oh, an early one tomorrow, huh? Big day then."
    fake_llm.script = [
        f"{opening} I've set your alarm for seven thirty, sleep well!",
        {"text": "", "tool_calls": [_alarm_call()]},
        "Wait, I hadn't actually set it - it's on now, seven thirty.",
    ]

    chunks, result = await _run(orchestrator.chat_streamed("alarm at 7:30", config, "s"))

    assert handled == [{"time": "07:30"}]
    assert chunks == [opening, "Wait, I hadn't actually set it - it's on now, seven thirty."]
    tool_message = fake_llm.calls[2]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert f'You already said this out loud: "{opening}"' in tool_message["content"]
    assert result.reply == " ".join(chunks)
    assert _assistant_lines(config, "s") == [result.reply]


async def test_forced_retry_without_a_tool_says_the_held_reply(tmp_path, monkeypatch, fake_llm):
    # today's behaviour, streamed: no tool on the retry -> the plain reply stands
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _alarm_registry([]))
    reply = "Done. Milk's in there, along with the eggs from before."
    fake_llm.script = [reply, "Still no tool."]

    chunks, result = await _run(orchestrator.chat_streamed("add milk", _config(tmp_path), "s"))

    assert result.reply == reply
    assert " ".join(chunks) == reply


async def test_non_streamed_paths_still_come_out_in_chunks(tmp_path, monkeypatch, fake_llm):
    # the presence fast path phrases with the plain call - said at the end
    async def handle(params, ctx):
        return SkillResult(text="ok, quiet")

    monkeypatch.setattr(
        orchestrator,
        "load_plugins",
        lambda d: {
            "felt_presence": LoadedSkill(
                name="felt_presence",
                description="presence",
                parameters_schema={"type": "object", "properties": {}},
                handle=handle,
            )
        },
    )
    fake_llm.script = ["Fine, fine. I'll be quiet now, you grump. Call me when you need me."]

    chunks, result = await _run(orchestrator.chat_streamed("shut up", _config(tmp_path), "s"))

    assert "stream" not in fake_llm.calls[0]
    assert chunks == speech_chunks(result.reply)


async def test_chat_streamed_raises_when_felt_llm_is_down(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]
    with pytest.raises(local_llm.LocalLLMUnavailable):
        await _run(orchestrator.chat_streamed("hi", _config(tmp_path), "s"))


async def test_a_skills_tag_and_action_come_before_her_first_words(tmp_path, monkeypatch, fake_llm):
    async def handle(params, ctx):
        return SkillResult(
            text="Alarm set for 07:30.",
            ui_action=UIAction(kind="toast", payload={"title": "Alarm"}),
            animation_tag="reaction/alarm",
        )

    registry = _alarm_registry([])
    registry["set_alarm"].handle = handle
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: registry)
    fake_llm.script = [
        {"text": "", "tool_calls": [_alarm_call()]},
        "Alright, your alarm's on for seven thirty. Sleep well, okay?",
    ]
    metas = []

    chunks, result = await _run(
        orchestrator.chat_streamed("alarm at 7:30", _config(tmp_path), "s"), metas
    )

    assert len(metas) == 1
    assert metas[0].ui_action == result.ui_action
    assert metas[0].animation_tag == result.animation_tag
    assert " ".join(chunks) == "Alright, your alarm's on for seven thirty. Sleep well, okay?"


async def test_a_plain_reply_has_no_meta(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    fake_llm.script = [PROSE[0]]
    metas = []
    await _run(orchestrator.chat_streamed("dinner?", _config(tmp_path), "s"), metas)
    assert metas == []


def test_first_chunk_can_end_at_a_clause_break():
    long_opening = (
        "Rice, eggs, and leftover chicken is basically the starter pack, so honestly "
        "you should just make fried rice tonight. Day-old rice works best."
    )
    chunker = SpeechChunker(first_at_clause=True)
    out = []
    for i in range(0, len(long_opening), 4):
        out += chunker.feed(long_opening[i : i + 4])
    out += chunker.close()
    # the list commas come before 40 chars, so the cut is the first comma after it
    assert out[0] == "Rice, eggs, and leftover chicken is basically the starter pack,"
    assert " ".join(out) == long_opening
    assert all(len(c) >= 25 for c in out)


def test_clause_cut_only_for_the_first_chunk():
    text = (
        "Okay. Fine, fine. "
        + "That one sentence goes on, and on, and on, and on for a good while longer."
    )
    chunker = SpeechChunker(first_at_clause=True)
    out = chunker.feed(text + " End") + chunker.close()
    assert out[0] == "Okay. Fine, fine. That one sentence goes on,"
    assert out[1] == "and on, and on, and on for a good while longer. End"
