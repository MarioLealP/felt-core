"""Felt's eyes: felt-vision (Qwen3-VL-2B, its own llama.cpp server) describes a
screenshot in text, and felt-llm reacts to the description instead of reading
the image itself (vision spike 2026-10-21, workspace runbooks/felt-vision-spike.md):
the same reaction quality in a blind review, ~14 s instead of ~35 s, and
Bonsai keeps its VRAM for text.

The describer gets the window's app and title - without them it named games
it couldn't see ("The Witcher 3" for Path of Exile 2). In Path of Exile 2 it
transcribes an item tooltip line by line instead, since the price lookup needs
the item name exactly as written. The call goes through llm_gate like every
model call: the GPUs are shared, and it keeps the caller's class (a screen
reaction stays ambient, "look at this" is his).
"""

import httpx

from felt_core import llm_gate
from felt_core.config import FeltConfig
from felt_core.local_llm import AmbientSkipped, LocalLLMUnavailable, image_part

SCREEN_PROMPT = (
    'The active window is {app} - "{title}". In at most 2 short sentences (under 50 '
    "words): what is happening, and the most important visible text quoted exactly. "
    "Never guess names or text you cannot read - don't name a game, site or app unless "
    "the window title or the screen shows its name."
)
POE_PROMPT = (
    'The active window is {app} - "{title}" (Path of Exile 2). If an item tooltip is '
    "visible, transcribe it line by line exactly as written, its name first; preserve "
    "spelling, numbers and signs, and write [unreadable] for text you cannot read. "
    "Otherwise describe what is happening in at most 2 short sentences, quoting visible "
    "text (a boss name, a message) exactly. Never guess."
)
_SCREEN_MAX_TOKENS = 90
_POE_MAX_TOKENS = 260


async def describe(
    jpeg_bytes: bytes, app: str, title: str, config: FeltConfig, *, poe: bool
) -> str:
    """felt-vision's description of the screenshot. Raises LocalLLMUnavailable
    when it is down or answers nothing usable (AmbientSkipped when the gate
    made an ambient call give way)."""
    if not config.vision_url:
        raise LocalLLMUnavailable("vision_url is not configured")
    prompt = (POE_PROMPT if poe else SCREEN_PROMPT).format(app=app, title=title)
    payload = {
        "messages": [
            {"role": "user", "content": [image_part(jpeg_bytes), {"type": "text", "text": prompt}]}
        ],
        "max_tokens": _POE_MAX_TOKENS if poe else _SCREEN_MAX_TOKENS,
        "temperature": 0,
    }

    url = f"{config.vision_url.rstrip('/')}/v1/chat/completions"

    async def call() -> dict:
        async with httpx.AsyncClient(timeout=config.vision_timeout_s) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()

    try:
        body = await llm_gate.run(call)
        text = (body["choices"][0]["message"].get("content") or "").strip()
    except llm_gate.Skipped as e:
        raise AmbientSkipped(str(e)) from e
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        raise LocalLLMUnavailable(f"felt-vision call failed: {e!r}") from e
    if not text:
        raise LocalLLMUnavailable("felt-vision described nothing")
    # a tooltip keeps its lines; a screen description is one line
    return text if poe else " ".join(text.split())
