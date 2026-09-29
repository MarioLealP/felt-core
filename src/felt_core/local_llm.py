"""Client for felt-llm, the self-hosted llama.cpp server (WP-36).

One OpenAI-compatible `/v1/chat/completions` call, used by every local
path when `config.local_first` is on: chat (with native tool calling),
screen reactions (image + json_schema), event lines, the privacy
classifier and the diary. The server runs ONE slot with an 8K context, so
callers keep their prompts bounded and requests simply queue server-side.

Every transport-level problem - no URL configured, connection refused,
timeout, an HTTP error, or a body that isn't the expected shape - is one
exception, `LocalLLMUnavailable`, so a caller's fallback is a single
`except`: chat() drops back to the router + cloud/Ollama path, screen
reactions to the cloud call, and the privacy classifier to "PRIVATE".
"""

import base64
import json
import logging
from dataclasses import dataclass, field

import httpx

from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)


class LocalLLMUnavailable(Exception):
    """felt-llm couldn't produce a reply (down, timed out, 5xx, or a
    malformed response). Callers fall back to their pre-WP-36 path."""


@dataclass
class LocalReply:
    text: str
    # Raw OpenAI-shaped tool calls, e.g. [{"id": ..., "type": "function",
    # "function": {"name": ..., "arguments": "<json string>"}}]. Kept raw so
    # chat() can echo them back in the assistant message of the follow-up.
    tool_calls: list[dict] = field(default_factory=list)
    prompt_n: int | None = None
    cache_n: int | None = None


def image_part(jpeg_bytes: bytes) -> dict:
    """An OpenAI `image_url` content part carrying the JPEG as a data URI."""
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


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


async def chat(
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.8,
    tools: list[dict] | None = None,
    json_schema: dict | None = None,
    config: FeltConfig,
) -> LocalReply:
    """`json_schema` is the bare JSON schema; it's sent as an OpenAI
    `response_format` so llama.cpp constrains the output to it."""
    if not config.local_llm_url:
        raise LocalLLMUnavailable("local_llm_url is not configured")

    payload: dict = {
        "model": config.local_llm_model,
        "messages": _merge_system_messages(messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "reply", "strict": True, "schema": json_schema},
        }

    try:
        async with httpx.AsyncClient(timeout=config.local_llm_timeout_s) as client:
            response = await client.post(
                f"{config.local_llm_url.rstrip('/')}/v1/chat/completions", json=payload
            )
            response.raise_for_status()
            body = response.json()
        message = body["choices"][0]["message"]
        text = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        timings = body.get("timings") or {}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        raise LocalLLMUnavailable(f"felt-llm call failed: {e!r}") from e

    if not isinstance(text, str) or not isinstance(tool_calls, list):
        raise LocalLLMUnavailable("felt-llm returned an unexpected message shape")
    return LocalReply(
        text=text,
        tool_calls=tool_calls,
        prompt_n=timings.get("prompt_n"),
        cache_n=timings.get("cache_n"),
    )


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
