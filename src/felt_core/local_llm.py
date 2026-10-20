"""Client for felt-llm, the self-hosted llama.cpp server (WP-36).

One OpenAI-compatible `/v1/chat/completions` call, used by every model
path: chat (with native tool calling), screen reactions (image +
json_schema), event lines, the reaction-tag classifier and the diary - plus
`chat_stream()`, the same call streamed, for chat replies she starts saying
while still writing them (WP-65). The server runs ONE slot, so callers keep
their prompts bounded and requests simply queue server-side.

Every transport-level problem - no URL configured, connection refused,
timeout, an HTTP error, or a body that isn't the expected shape - is one
exception, `LocalLLMUnavailable`. There is no cloud or Ollama fallback
(WP-36 D''): the API answers 503 "felt-llm offline" and the background
loops log and skip.
"""

import base64
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)


class LocalLLMUnavailable(Exception):
    """felt-llm couldn't produce a reply (down, timed out, 5xx, or a
    malformed response). The API maps it to a 503, background loops skip."""


@dataclass
class LocalReply:
    text: str
    # Raw OpenAI-shaped tool calls, e.g. [{"id": ..., "type": "function",
    # "function": {"name": ..., "arguments": "<json string>"}}]. Kept raw so
    # chat() can echo them back in the assistant message of the follow-up.
    tool_calls: list[dict] = field(default_factory=list)
    # True when the model hit max_tokens and the text was ended at its last
    # full sentence (FELT-71).
    truncated: bool = False
    prompt_n: int | None = None
    cache_n: int | None = None


def image_part(jpeg_bytes: bytes) -> dict:
    """An OpenAI `image_url` content part carrying the JPEG as a data URI."""
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


_SENTENCE_END = re.compile(r"[.!?…][\"')\]*~]*(?=\s|$)")


def end_at_last_sentence(text: str) -> str:
    """A reply cut off by max_tokens, ended at its last complete sentence so
    it never stops mid-word (FELT-71). No complete sentence at all: the
    fragment gets an ellipsis instead."""
    ends = list(_SENTENCE_END.finditer(text))
    if ends:
        return text[: ends[-1].end()].rstrip()
    return text.rstrip(" ,;:-—") + "…"


def _merge_system_messages(messages: list[dict]) -> list[dict]:
    """One leading system message. Callers append context as extra
    "system" entries (facts, music taste, "currently on screen"), and
    llama.cpp's chat templates reject or mangle a system message that isn't
    first - so they're folded into the first one, in order."""
    system_parts = [str(m["content"]) for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if not system_parts:
        return rest
    return [{"role": "system", "content": "\n\n".join(system_parts)}, *rest]


def _payload(
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float,
    tools: list[dict] | None,
    tool_choice: str | None,
    json_schema: dict | None,
    config: FeltConfig,
) -> dict:
    payload: dict = {
        "model": config.local_llm_model,
        "messages": _merge_system_messages(messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        if tool_choice:  # "required": llama.cpp constrains the turn to a tool call
            payload["tool_choice"] = tool_choice
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "reply", "strict": True, "schema": json_schema},
        }
    return payload


def _reply(
    text: object,
    tool_calls: object,
    finish_reason: object,
    timings: dict,
    *,
    max_tokens: int,
    json_schema: dict | None,
) -> LocalReply:
    if not isinstance(text, str) or not isinstance(tool_calls, list):
        raise LocalLLMUnavailable("felt-llm returned an unexpected message shape")
    truncated = finish_reason == "length" and json_schema is None and not tool_calls and bool(text)
    if truncated:
        logger.warning(
            "felt-llm hit max_tokens=%d; ending the reply at its last sentence", max_tokens
        )
        text = end_at_last_sentence(text)
    return LocalReply(
        text=text,
        tool_calls=tool_calls,
        truncated=truncated,
        prompt_n=timings.get("prompt_n"),
        cache_n=timings.get("cache_n"),
    )


async def chat(
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.8,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    json_schema: dict | None = None,
    config: FeltConfig,
) -> LocalReply:
    """`json_schema` is the bare JSON schema; it's sent as an OpenAI
    `response_format` so llama.cpp constrains the output to it."""
    if not config.local_llm_url:
        raise LocalLLMUnavailable("local_llm_url is not configured")
    payload = _payload(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tools,
        tool_choice=tool_choice,
        json_schema=json_schema,
        config=config,
    )

    try:
        async with httpx.AsyncClient(timeout=config.local_llm_timeout_s) as client:
            response = await client.post(
                f"{config.local_llm_url.rstrip('/')}/v1/chat/completions", json=payload
            )
            response.raise_for_status()
            body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
        text = message.get("content") or ""
        finish_reason = choice.get("finish_reason")
        tool_calls = message.get("tool_calls") or []
        timings = body.get("timings") or {}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        raise LocalLLMUnavailable(f"felt-llm call failed: {e!r}") from e

    return _reply(
        text, tool_calls, finish_reason, timings, max_tokens=max_tokens, json_schema=json_schema
    )


def _add_tool_call_delta(calls: list[dict], delta: dict) -> None:
    """Tool calls stream in pieces: the first delta for an index carries the
    id and name, later ones append to the arguments string."""
    index = delta.get("index", len(calls))
    while len(calls) <= index:
        calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    call = calls[index]
    if delta.get("id"):
        call["id"] = delta["id"]
    function = delta.get("function") or {}
    if function.get("name"):
        call["function"]["name"] += function["name"]
    if function.get("arguments"):
        call["function"]["arguments"] += function["arguments"]


async def chat_stream(
    messages: list[dict],
    *,
    on_text: Callable[[str], None],
    max_tokens: int,
    temperature: float = 0.8,
    tools: list[dict] | None = None,
    config: FeltConfig,
) -> LocalReply:
    """`chat()` with `stream: true` (WP-65): `on_text` gets each piece of the
    reply text as felt-llm writes it, and the whole reply comes back at the
    end, same shape as chat(). Tool-call pieces are collected whole, never
    passed to `on_text` - and once a tool call has started, neither is any
    more text: a reply that turns into a tool call isn't spoken. Thinking is
    off on felt-llm (`--reasoning off`), so there are no reasoning pieces."""
    if not config.local_llm_url:
        raise LocalLLMUnavailable("local_llm_url is not configured")
    payload = _payload(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tools,
        tool_choice=None,
        json_schema=None,
        config=config,
    )
    payload["stream"] = True

    parts: list[str] = []
    tool_calls: list[dict] = []
    finish_reason = None
    timings: dict = {}
    try:
        async with (
            httpx.AsyncClient(timeout=config.local_llm_timeout_s) as client,
            client.stream(
                "POST", f"{config.local_llm_url.rstrip('/')}/v1/chat/completions", json=payload
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                timings = event.get("timings") or timings
                if not event.get("choices"):
                    continue
                choice = event["choices"][0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                for piece in delta.get("tool_calls") or []:
                    _add_tool_call_delta(tool_calls, piece)
                text = delta.get("content")
                if text:
                    parts.append(text)
                    if not tool_calls:
                        on_text(text)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        raise LocalLLMUnavailable(f"felt-llm call failed: {e!r}") from e

    return _reply(
        "".join(parts),
        tool_calls,
        finish_reason,
        timings,
        max_tokens=max_tokens,
        json_schema=None,
    )


async def is_healthy(config: FeltConfig) -> bool:
    """`GET {local_llm_url}/health` - True only on a 200 (llama.cpp answers
    503 while the model is still loading)."""
    if not config.local_llm_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{config.local_llm_url.rstrip('/')}/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def parse_tool_arguments(tool_call: dict) -> tuple[str, dict]:
    """(name, params) from one raw tool call. Arguments arrive as a JSON
    string; anything that doesn't decode to an object becomes {} - the
    skill then falls back to `ctx.original_message`, same as it always has
    when the router omitted params."""
    function = tool_call.get("function") or {}
    raw = function.get("arguments")
    params: object = raw
    if isinstance(raw, str):
        try:
            params = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            params = {}
    return str(function.get("name") or ""), params if isinstance(params, dict) else {}
