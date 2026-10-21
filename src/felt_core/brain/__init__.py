"""felt-core's brain: chat orchestration, reply generation (felt-llm),
screen reactions, and the background weather/music-taste/warm-up loops -
split (WP-40) out of a single 1080-line brain.py into a package by
responsibility:

- orchestrator.py: chat() itself - the local tool-calling turn and skill
  execution.
- replies.py: felt-llm reply generation and skill-result phrasing. Also
  this package's small public surface for other modules (events.py) that
  need a model-generated line without reaching into orchestrator internals.
- screen_reactions.py: process_screen_context (felt-vision, text) and
  react_to_screen (WP-07, JPEG) - the two screen-reaction entry points,
  sharing one cooldown-check helper.
- background.py: the weather-precompute, music-taste and felt-llm warm-up
  asyncio loops started from api.py's lifespan.
- personality.py: the system prompt every reply carries.
- result.py: ChatResult, the shape every entry point returns.

Re-exported here so `from felt_core.brain import ...` keeps working for
every existing caller (api.py, __init__.py's CLI) without changing their
imports - the split is purely internal.
"""

from felt_core.brain.background import (
    current_weather,
    llm_warmup_loop,
    music_taste_loop,
    weather_precompute_loop,
)
from felt_core.brain.orchestrator import chat, chat_streamed
from felt_core.brain.personality import PERSONALITY_PROMPT, personality_prompt
from felt_core.brain.replies import local_reply, music_taste_context
from felt_core.brain.result import ChatResult
from felt_core.brain.screen_reactions import process_screen_context, react_to_screen
from felt_core.brain.streaming import ReplyMeta

__all__ = [
    "ChatResult",
    "ReplyMeta",
    "PERSONALITY_PROMPT",
    "chat",
    "chat_streamed",
    "current_weather",
    "llm_warmup_loop",
    "local_reply",
    "music_taste_context",
    "music_taste_loop",
    "personality_prompt",
    "process_screen_context",
    "react_to_screen",
    "weather_precompute_loop",
]
