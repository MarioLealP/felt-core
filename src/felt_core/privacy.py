"""Decides whether a plain conversational message is safe to send to an
external cloud model (Gemini, via ninerouter) for a higher-quality reply,
or whether it needs to stay on the local model. Same pattern as router.py's
decide_tool - a small, dedicated, narrow classification call, not the chat
model reasoning about itself.

Fails closed, deliberately: this is the one thing standing between a
message and leaving the LAN, so any parse failure, timeout, or model error
means PRIVATE (stay local), never SAFE. A wrongly-blocked banter message
just costs a slightly worse local reply; a wrongly-allowed private one
actually reaches Google. Mario, 2026-09-21: wire poor local conversation
quality to Gemini Flash for plain banter, gated by exactly this check -
"run it through some local privacy checker... if acceptable sends to free
gemini flash".
"""

import httpx

from felt_core.config import FeltConfig

_PRIVACY_PROMPT = (
    "You are a privacy classifier, not a conversational assistant. Decide "
    "whether the following message is safe to send to an external cloud AI "
    "service (Google Gemini), or whether it contains anything private that "
    "must stay local.\n\n"
    "Answer PRIVATE if the message contains, references, or asks about any "
    "of: real names of specific people (family, friends, coworkers) with "
    "personal details about them; health, medical, or financial "
    "information; home address, exact location, or physical security "
    "details; passwords, keys, tokens, or other credentials; the home "
    "server/network/smart-home devices by name; the contents of emails, "
    "calendar events, documents, or messages; anything that reads like it's "
    "quoting or relying on private facts Felt already knows about Mario "
    "specifically.\n\n"
    "Answer SAFE only for plain small talk, jokes, general-knowledge "
    "questions, opinions, or casual conversation with none of the above in "
    "it. A general-knowledge or creative request being ABOUT a topic (space, "
    "cats, philosophy, whatever) is never PRIVATE by itself - only actual "
    "personal/sensitive content makes it PRIVATE.\n\n"
    "Worked examples:\n"
    'Message: "tell me a joke about cats"\n'
    "Correct: SAFE — a request for generic humor, no personal content at all.\n\n"
    'Message: "whats the meaning of life"\n'
    "Correct: SAFE — abstract/philosophical, nothing personal or private in it.\n\n"
    'Message: "haha okay thats funny, tell me another one"\n'
    "Correct: SAFE — casual banter continuing a joke, still nothing personal.\n\n"
    'Message: "whats a good recipe for pasta carbonara"\n'
    "Correct: SAFE — general knowledge, no personal details.\n\n"
    'Message: "whats my moms phone number again"\n'
    "Correct: PRIVATE — a specific family member's personal contact info.\n\n"
    'Message: "remind me what I told you about my doctor appointment"\n'
    "Correct: PRIVATE — references health information Felt was told in "
    "confidence.\n\n"
    'Message: "whats the wifi password"\n'
    "Correct: PRIVATE — a credential.\n\n"
    "When genuinely unsure ONLY (i.e. the message plausibly references real "
    "personal information about Mario or someone he knows, not just a topic "
    "in general), answer PRIVATE.\n\n"
    "Respond with exactly one word: SAFE or PRIVATE. No explanation, no "
    "punctuation, no other text."
)


async def is_safe_for_cloud(message: str, config: FeltConfig) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{config.ollama_url}/api/chat",
                json={
                    "model": config.router_model,
                    "messages": [
                        {"role": "system", "content": _PRIVACY_PROMPT},
                        {"role": "user", "content": message},
                    ],
                    "stream": False,
                    # num_gpu=0: keeps this tiny classifier off the Nitro5's
                    # GPU so it never contends with the main chat model for
                    # VRAM - see router.py's decide_tool for the full story
                    # (both call sites use router_model and must agree on
                    # this, or Ollama reloads the model every time the
                    # num_gpu setting flips between requests).
                    "options": {"temperature": 0, "num_gpu": 0},
                },
            )
            response.raise_for_status()
            raw = response.json()["message"]["content"]
    except Exception:
        return False

    return raw.strip().upper().startswith("SAFE")
