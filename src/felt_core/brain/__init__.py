"""felt-core's brain: chat orchestration, reply generation (local/cloud),
screen reactions, and the background weather/music-taste loops - split
(WP-40) out of a single 1080-line brain.py into a package by
responsibility:

- orchestrator.py: chat() itself - router/privacy dispatch and skill
  execution.
- replies.py: local (Ollama) and cloud (Gemini/Gemma) reply generation,
  and skill-result phrasing. Also this package's small public surface for
  other modules (events.py) that need a model-generated line without
  reaching into orchestrator internals.
- screen_reactions.py: process_screen_context (felt-vision, text) and
  react_to_screen (WP-07, JPEG) - the two screen-reaction entry points,
  sharing one cooldown-check helper.
- background.py: the weather-precompute and music-taste asyncio loops
  started from api.py's lifespan.
- personality.py: the system prompt every reply carries, local or cloud.
- result.py: ChatResult, the shape every entry point returns.

Re-exported here so `from felt_core.brain import ...` keeps working for
every existing caller (api.py, __init__.py's CLI) without changing their
imports - the split is purely internal.
"""

from felt_core.brain.background import current_weather, music_taste_loop, weather_precompute_loop
from felt_core.brain.orchestrator import chat
from felt_core.brain.personality import PERSONALITY_PROMPT, personality_prompt
from felt_core.brain.replies import local_reply, music_taste_context
from felt_core.brain.result import ChatResult
from felt_core.brain.screen_reactions import process_screen_context, react_to_screen

__all__ = [
    "ChatResult",
    "PERSONALITY_PROMPT",
    "chat",
    "current_weather",
    "local_reply",
    "music_taste_context",
    "music_taste_loop",
    "personality_prompt",
    "process_screen_context",
    "react_to_screen",
    "weather_precompute_loop",
]
