"""Spoken replies: /v1/voice-chat, /v1/text-chat and the reply streams (/v1/reply/...), with their TTS."""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx2
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response

from felt_core.api.app_api import SESSION as APP_SESSION
from felt_core.api.app_api import schedule_mark
from felt_core.api.chat import ChatRequest
from felt_core.brain import (
    ChatResult,
    ReplyMeta,
    chat,
    chat_streamed,
)
from felt_core.clients import local_llm
from felt_core.clients.felt_tls import voice_client
from felt_core.config import load_config
from felt_core.core import metrics, reply_stream, tts_cache
from felt_core.core.speech import speech_chunks, strip_for_tts, tts_request_body

logger = logging.getLogger(__name__)
router = APIRouter()


class _TTSResponse(Protocol):
    """What `_synthesize_reply` needs back from a TTS POST - narrower than
    `httpx2.Response` so the unit tests' hand-written fake responses (no
    real network, see `tests/test_api_tts_cache.py`) satisfy the type
    without subclassing httpx2.
    """

    @property
    def content(self) -> bytes: ...

    def raise_for_status(self) -> object: ...


class _TTSClient(Protocol):
    """What `_synthesize_reply` needs from its HTTP client - just enough
    of `httpx2.AsyncClient`'s surface to also accept the tests' fake
    clients (`_ExplodingClient`/`FakeClient`), which duck-type `.post()`
    without subclassing `httpx2.AsyncClient`.
    """

    async def post(self, url: str, *, json: dict[str, Any]) -> _TTSResponse: ...


async def _tts_audio(
    tts_text: str,
    emotion: str | None,
    config: Any,
    client: _TTSClient,
    *,
    first_chunk: bool = False,
) -> bytes:
    """One felt-voice /v1/tts call behind the shared WP-06 cache (keyed on
    text + emotion). Each real call is timed as a "tts" stage; a streamed
    reply's chunk 0 is additionally "tts_first_chunk" (WP-54) - it's the
    part the user actually waits for.
    """
    # WP-06: a cache hit here (warmed by brain.py's weather precompute
    # loop) means the common "what's the weather right now?" turn makes
    # zero calls to felt-voice at all.
    audio_bytes = tts_cache.get(tts_text, emotion)
    if audio_bytes is not None:
        return audio_bytes
    async with contextlib.AsyncExitStack() as stack:
        if first_chunk:
            await stack.enter_async_context(metrics.timed("tts_first_chunk", chars=len(tts_text)))
        await stack.enter_async_context(metrics.timed("tts", chars=len(tts_text)))
        tts_resp = await client.post(f"{config.voice_url}/v1/tts", json=tts_request_body(tts_text, emotion))
        tts_resp.raise_for_status()
    audio_bytes = tts_resp.content
    tts_cache.put(tts_text, audio_bytes, emotion)
    return audio_bytes


async def _fill_reply_stream(stream: reply_stream.ReplyStream, config: Any) -> None:
    """Background half of a streamed reply: chunks 1..N-1, on a client of its
    own (the request's is closed as soon as chunk 0's response is sent).
    """
    async with voice_client(config, timeout=60) as client:
        await reply_stream.run(stream, lambda _n, text: _tts_audio(text, None, config, client))


async def _synthesize_reply(
    reply: str,
    ui_action: dict[str, Any] | None,
    animation_tag: str | None,
    config: Any,
    client: _TTSClient,
    extra_headers: dict[str, str] | None = None,
    emotion: str | None = None,
    stream: bool = False,
) -> Response:
    """Shared by voice-chat and text-chat: TTS the reply and build the same
    audio/wav + X-* header response shape, so a client doesn't need to know
    or care which input mode produced it.

    `stream` (WP-54, only the two chat endpoints pass it): a reply that
    splits into 2+ sentence chunks answers with chunk 0's audio plus
    X-Reply-Stream/X-Reply-Chunks, and the rest is synthesised in the
    background for GET /v1/reply/{id}/chunk/{n}. A one-chunk reply is
    exactly the non-streamed response.
    """
    tts_text = strip_for_tts(reply)
    logger.debug("TTS input after strip_for_tts: %r", tts_text)

    chunks = speech_chunks(tts_text, config.reply_stream_first_chunk_chars) if stream else [tts_text]
    streaming = len(chunks) >= 2
    voice_down = False
    try:
        audio_bytes = await _tts_audio(
            chunks[0] if streaming else tts_text, emotion, config, client, first_chunk=streaming
        )
    except httpx2.HTTPError:
        # felt-voice (the laptop) is off or failing: her reply still exists, so
        # it goes out as text only instead of a 500 that loses it (2026-10-15).
        logger.warning("felt-voice unavailable - replying without audio", exc_info=True)
        audio_bytes, streaming, voice_down = b"", False, True

    # Percent-encoded — headers must be ASCII-safe, a non-English reply
    # (accents, etc.) would otherwise break this.
    headers = {"X-Reply-Text": quote(reply), **(extra_headers or {})}
    if voice_down:
        headers["X-Voice"] = "unavailable"
    if streaming:
        live = reply_stream.create(chunks, audio_bytes)
        live.task = asyncio.create_task(_fill_reply_stream(live, config))
        headers["X-Reply-Stream"] = live.id
        headers["X-Reply-Chunks"] = str(len(chunks))
    if ui_action is not None:
        headers["X-UI-Action"] = quote(json.dumps(ui_action))
    if animation_tag is not None:
        headers["X-Animation-Tag"] = quote(animation_tag)

    return Response(content=audio_bytes, media_type="audio/wav", headers=headers)


def _wants_stream_v2(request: Request, config: Any) -> bool:
    """WP-65: a client that plays protocol v2 says so per request; every
    other client gets WP-54's whole-reply-then-chunks behaviour.
    """
    return config.reply_streaming and request.headers.get("x-accept-stream") == "2"


def _result_body(result: ChatResult) -> dict:
    return {
        "done": True,
        "text": result.reply,
        "ui_action": result.ui_action.model_dump() if result.ui_action else None,
        "animation_tag": result.animation_tag,
        "emotion": result.emotion,
    }


async def _fill_open_stream(
    stream: reply_stream.ReplyStream,
    items: AsyncIterator[str | ReplyMeta | ChatResult],
    config: Any,
    on_result: Callable[[ChatResult], None] | None,
) -> None:
    """Background half of a v2 reply: each further chunk as chat_streamed()
    writes it, synthesised in order, then the finished result. felt-llm
    keeps writing while a chunk is in TTS (chat_streamed queues them).
    """
    result: ChatResult | None = None
    try:
        async with voice_client(config, timeout=60) as client:
            async for item in items:
                if isinstance(item, ChatResult):
                    result = item
                    continue
                if isinstance(item, ReplyMeta):
                    continue  # went out with chunk 0 (or the result has it)
                tts_text = strip_for_tts(item)
                if not tts_text:
                    continue  # nothing to say out loud (an emoji line); the text has it
                n = stream.append(item)
                if stream.failed_from is not None:
                    continue
                try:
                    stream.set_chunk(n, await _tts_audio(tts_text, None, config, client))
                except Exception:
                    logger.exception("reply stream %s: TTS failed for chunk %d", stream.id, n)
                    stream.fail_from(n)
    except local_llm.LocalLLMUnavailable as e:
        logger.warning("reply stream %s: felt-llm went away mid-reply (%s)", stream.id, e)
    finally:
        if result is None:
            stream.end({"done": True, "text": " ".join(stream.texts), "error": "felt-llm offline"})
        else:
            stream.end(_result_body(result))
            if on_result is not None:
                on_result(result)


async def _reply_v2(
    message: str,
    session_id: str,
    tone: str | None,
    config: Any,
    client: _TTSClient,
    extra_headers: dict[str, str] | None = None,
    on_result: Callable[[ChatResult], None] | None = None,
) -> Response:
    """WP-65 protocol v2: chat_streamed(), and chunk 0's audio goes back the
    moment it exists - X-Reply-Text is chunk 0's text, X-Reply-Chunks
    "open", and the rest of the reply (its text, ui_action, tag) is at
    GET /v1/reply/{id} once done. A reply that never came out in chunks, or
    felt-voice being down, falls back to the whole-reply response.
    """
    items = chat_streamed(message, config, session_id, tone)
    first: str | ChatResult | None = None
    meta = ReplyMeta()
    async for item in items:
        if isinstance(item, ReplyMeta):
            meta = item  # the skill's hush / reaction: with chunk 0, not at the end
            continue
        if isinstance(item, ChatResult) or strip_for_tts(item):
            first = item
            break
    wav: bytes | None = None
    if isinstance(first, str):
        try:
            wav = await _tts_audio(strip_for_tts(first), None, config, client, first_chunk=True)
        except httpx2.HTTPError:
            logger.warning("felt-voice unavailable - replying without audio", exc_info=True)
    if not isinstance(first, str) or wav is None:
        # the whole reply, the WP-54 way
        result = first if isinstance(first, ChatResult) else None
        async for item in items:
            if isinstance(item, ChatResult):
                result = item
        assert result is not None
        if on_result is not None:
            on_result(result)
        return await _synthesize_reply(
            result.reply,
            result.ui_action.model_dump() if result.ui_action else None,
            result.animation_tag,
            config,
            client,
            extra_headers,
        )
    live = reply_stream.open_stream(first, wav)
    live.task = asyncio.create_task(_fill_open_stream(live, items, config, on_result))
    headers = {
        "X-Reply-Text": quote(first),
        "X-Reply-Stream": live.id,
        "X-Reply-Chunks": "open",
        "X-Reply-Stream-Version": "2",
        **(extra_headers or {}),
    }
    if meta.ui_action is not None:
        headers["X-UI-Action"] = quote(json.dumps(meta.ui_action.model_dump()))
    if meta.animation_tag is not None:
        headers["X-Animation-Tag"] = quote(meta.animation_tag)
    return Response(content=wav, media_type="audio/wav", headers=headers)


@router.post("/v1/voice-chat")
async def voice_chat_endpoint(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = "default",
    tone: str | None = Form(None),
) -> Response:
    """One call: audio in, audio out. Orchestrates felt-voice (STT) ->
    the brain -> felt-voice (TTS), so a client doesn't have to.
    """
    config = load_config()
    if not config.voice_url:
        raise HTTPException(status_code=503, detail="Voice isn't configured (no FELT_VOICE_URL set).")

    async with voice_client(config, timeout=60) as client:
        async with metrics.timed("stt"):
            stt_resp = await client.post(
                f"{config.voice_url}/v1/stt",
                files={"file": (file.filename, await file.read(), file.content_type)},
            )
            stt_resp.raise_for_status()
            transcribed = stt_resp.json()["text"]

        ui_action: dict[str, Any] | None = None
        animation_tag: str | None = None
        if transcribed.strip() and _wants_stream_v2(request, config):
            return await _reply_v2(
                transcribed,
                session_id,
                tone,
                config,
                client,
                {"X-Transcribed-Text": quote(transcribed)},
            )
        if transcribed.strip():
            result = await chat(transcribed, config, session_id, tone)
            reply = result.reply
            ui_action = result.ui_action.model_dump() if result.ui_action else None
            animation_tag = result.animation_tag
        else:
            # Real bug found live, 2026-09-12: an empty/silent recording
            # (e.g. a wheel-tilt that immediately auto-stopped on silence,
            # see felt-desktop-agent's audio.rs) was still handed to the
            # router as a real message, which — faced with nothing to go
            # on — guessed a no-required-params tool (get_weather) rather
            # than the router having any "actually, nothing was said"
            # option. Skipping the router entirely when there's no
            # transcript at all is the honest answer, not a guess.
            reply = "I didn't catch that."

        return await _synthesize_reply(
            reply,
            ui_action,
            animation_tag,
            config,
            client,
            {"X-Transcribed-Text": quote(transcribed)},
            stream=config.reply_streaming,
        )


@router.post("/v1/text-chat")
async def text_chat_endpoint(req: ChatRequest, request: Request) -> Response:
    """Same shape as /v1/voice-chat's response (audio/wav + X-Reply-Text/
    X-Animation-Tag headers) but for already-typed text - so a typed
    message gets the same TTS + reaction-animation treatment a spoken one
    does, minus the STT step (Mario, 2026-09-21: "hook up the chat like it
    was voice input, need her to reply with TTS + animation").
    """
    config = load_config()
    if not config.voice_url:
        raise HTTPException(status_code=503, detail="Voice isn't configured (no FELT_VOICE_URL set).")

    if _wants_stream_v2(request, config):

        def marks(_result: ChatResult) -> None:
            if req.session_id == APP_SESSION:
                schedule_mark(config)  # WP-42.2: the tray window's sends get her marks too

        async with voice_client(config, timeout=60) as client:
            return await _reply_v2(req.message, req.session_id, req.tone, config, client, on_result=marks)

    result = await chat(req.message, config, req.session_id, req.tone)
    if req.session_id == APP_SESSION:
        schedule_mark(config)  # WP-42.2: the tray window's sends get her marks too
    async with voice_client(config, timeout=60) as client:
        return await _synthesize_reply(
            result.reply,
            result.ui_action.model_dump() if result.ui_action else None,
            result.animation_tag,
            config,
            client,
            stream=config.reply_streaming,
        )


@router.get("/v1/reply/{stream_id}/chunk/{n}")
async def reply_chunk_endpoint(stream_id: str, n: int, timeout: float = 30.0) -> Response:
    """WP-54: long-polls for chunk n of a streamed reply (see
    _synthesize_reply). 200 + WAV + X-Chunk-Text once ready, 408 if it still
    isn't after `timeout` (clamped to 1-60 s), 204 for n past the last chunk
    (the stream is done), 404 for an unknown/expired id, 502 if that chunk's
    TTS failed.
    """
    stream = reply_stream.get(stream_id)
    if stream is None or n < 0:
        raise HTTPException(status_code=404, detail="unknown or expired stream")
    timeout = max(1.0, min(60.0, timeout))
    start = time.monotonic()
    # WP-65: an open stream may not have chunk n yet - 204 only once it ended
    if not await stream.wait_for_chunk_slot(n, timeout):
        if stream.done:
            return Response(status_code=204)
        raise HTTPException(status_code=408, detail="chunk not ready")
    try:
        wav = await stream.wait_chunk(n, max(0.0, timeout - (time.monotonic() - start)))
    except reply_stream.ChunkFailed:
        raise HTTPException(status_code=502, detail="chunk TTS failed") from None
    if wav is None:
        raise HTTPException(status_code=408, detail="chunk not ready")
    return Response(content=wav, media_type="audio/wav", headers={"X-Chunk-Text": quote(stream.texts[n])})


@router.get("/v1/reply/{stream_id}")
async def reply_result_endpoint(stream_id: str) -> dict:
    """WP-65 (protocol v2): the finished reply behind a stream - `{done,
    text, ui_action, animation_tag, emotion}` (+ `error` if felt-llm went
    away mid-reply). Before it's done: `{done: false, text}` with the chunks
    so far. 404 for an unknown/expired id.
    """
    stream = reply_stream.get(stream_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="unknown or expired stream")
    if not stream.done or stream.result is None:
        return {"done": False, "text": " ".join(stream.texts)}
    return stream.result
