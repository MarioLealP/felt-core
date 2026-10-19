"""Something she tells Mario unprompted, in her own words (Mario 2026-10-19:
"everything that comes through felt should be routed through her llm so it
gets her personality").

The caller hands over the facts; her model (local_reply, personality
prompt, no facts/taste) words them as one short sentence. The plain line
the caller also passes is what goes out when felt-llm is down, slow or
answers nothing - a printer that finished or an alarm going off still has
to reach him.
"""

import asyncio

from felt_core import local_llm
from felt_core.config import FeltConfig

ASK = "Tell him, in your own voice - one short sentence. Don't invent details."


def prompt(facts: str) -> str:
    """Pure: the facts plus the ask."""
    return f"{facts} {ASK}"


async def her_line(config: FeltConfig, facts: str, fallback: str, timeout_s: float = 30) -> str:
    from felt_core.brain.replies import local_reply

    try:
        line = await asyncio.wait_for(
            local_reply([{"role": "user", "content": prompt(facts)}], config, include_facts=False),
            timeout_s,
        )
    except (local_llm.LocalLLMUnavailable, TimeoutError):
        line = ""
    return line.strip(" \n\"'") or fallback
