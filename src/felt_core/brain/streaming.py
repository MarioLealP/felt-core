"""WP-65: streamed chat replies - she starts talking while she's still
writing. chat_streamed() (orchestrator.py) runs the same turn as chat() with
a ReplySpeaker attached; the reply calls that can be her answer then go
through `ReplySpeaker.turn()` (felt-llm streamed) instead of
`local_llm.chat()`, and each sentence chunk is let out as soon as it's
complete.

The fake-claim guard (FELT-108, lookup_claims.py) can't wait for the whole
reply any more, so (Mario, 2026-10-20, "go with 1"):
- every chunk inside the guard's window (the reply's opening) is checked
  before it's let out - a hit holds it and everything after it, and the
  turn falls back to today's path (finish, guard, retry with a tool);
- the full text is still checked at the end (orchestrator, as before). A
  hit after something was already said is rare by design - she then
  corrects herself in her own words in the reply that follows the retry.
"""

import asyncio
import logging

from felt_core import local_llm
from felt_core.brain.lookup_claims import claims_a_tool_use
from felt_core.config import FeltConfig
from felt_core.speech import SpeechChunker

logger = logging.getLogger(__name__)


def _norm(text: str) -> str:
    return " ".join(text.split())


class ReplySpeaker:
    """Collects what she says during one streamed chat turn. `chunks` gets
    each released chunk (the reply's own text, not yet stripped for TTS);
    `said` keeps them in order."""

    def __init__(self, config: FeltConfig) -> None:
        self.first_chunk_chars = config.reply_stream_first_chunk_chars
        self.chunks: asyncio.Queue[str | None] = asyncio.Queue()
        self.said: list[str] = []
        # the latest streamed call: where its chunks start in `said`, its text
        self._turn_start = 0
        self._turn_text = ""

    def said_text(self) -> str:
        return " ".join(self.said)

    def _release(self, chunk: str) -> None:
        self.said.append(chunk)
        self.chunks.put_nowait(chunk)

    def _chunker(self) -> SpeechChunker:
        # the "first chunk" size only for her first words of the turn
        return SpeechChunker(self.first_chunk_chars if not self.said else 25)

    async def turn(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        tools: list[dict] | None = None,
        guard: bool = False,
        config: FeltConfig,
    ) -> local_llm.LocalReply:
        """One streamed felt-llm call; returns the whole reply like
        local_llm.chat(). Its chunks are let out as they complete, unless
        `guard` (the first, tool-carrying call) catches a claimed lookup or
        action in the opening - then nothing more of this call is said and
        the caller's full-text check takes over. A reply that turns into a
        tool call says nothing more either."""
        chunker = self._chunker()
        holding = False
        self._turn_start = len(self.said)

        def let_out(chunks: list[str]) -> None:
            nonlocal holding
            for chunk in chunks:
                if not holding and guard and claims_a_tool_use(chunker.text):
                    holding = True
                    logger.info(
                        "stream guard: the opening claims a lookup or action - holding the reply"
                    )
                if not holding:
                    self._release(chunk)

        reply = await local_llm.chat_stream(
            messages,
            on_text=lambda piece: let_out(chunker.feed(piece)),
            max_tokens=max_tokens,
            tools=tools,
            config=config,
        )
        if not reply.tool_calls:
            let_out(chunker.close(reply.text))
        self._turn_text = reply.text.strip()
        return reply

    def full_reply(self, reply: str) -> str:
        """What she said this turn, as one text: `reply` plus whatever was
        let out before the call it came from (a late fake-claim hit, a few
        words before a tool call), in front."""
        if reply.strip() == self._turn_text:  # the answer is the latest streamed call
            before = " ".join(self.said[: self._turn_start])
        else:  # a path that didn't stream
            before = self.said_text()
            if _norm(reply).startswith(_norm(before)):
                before = ""
        if not before:
            return reply
        return f"{before} {reply}" if reply else before

    def rest(self, reply: str) -> list[str]:
        """The chunks of `reply` not said yet - all of it when the answer
        came from a path that doesn't stream (a fast path, a held reply)."""
        said, full = _norm(self.said_text()), _norm(reply)
        remainder = full[len(said) :] if full.startswith(said) else full
        chunker = self._chunker()
        return chunker.feed(remainder) + chunker.close()


def correction_note(said: str) -> str:
    """Added to the tool result when the guard caught a claim only after she
    had already said part of the reply (a late hit): she corrects herself,
    in her own words."""
    return (
        f'You already said this out loud: "{said}" - but at that point you '
        "hadn't actually done or looked anything up. It has really happened now "
        "(the result above). Start by correcting yourself briefly and naturally, "
        "then answer. Don't repeat what you already said."
    )
