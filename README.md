# felt-core

Felt's brain — the orchestrator service, the plugin interface every skill
implements, the config system, and the local-model routing that makes Felt
local-first.

This is one service in the [Felt](https://git.clockmost.com/Benylin/Felt)
project — see that repo for the overall architecture and how the services
fit together.

## Philosophy

Felt is built assuming it may eventually be public, and to be genuinely
usable by someone who isn't its original author. Two rules follow from
that, enforced project-wide, not just here:

1. **No hardcoding.** No IPs, hostnames, file paths, API keys, or other
   environment-specific values baked into code. Everything goes through
   config (`src/felt_core/config.py`, env vars / `.env`), with sensible
   generic defaults. `.env.example` documents every setting.
2. **Modular / plugin architecture.** Skills (weather, price checks, smart
   home, whatever else) are self-contained plugins implementing one small
   interface (`src/felt_core/plugin.py`) — addable/removable without
   touching core code. New skill = new folder in `plugins/`.

A consequence worth naming explicitly: **felt-llm is the only brain, and
nothing leaves the LAN** (WP-36 D''). There is no cloud model and no
Ollama; when felt-llm is unreachable the model endpoints answer 503
`{"detail": "felt-llm offline"}` and background loops skip. Tool calling is
llama.cpp's native tool calling (`--jinja`).

## Quickstart

```
uv sync
cp .env.example .env   # edit for your environment
uv run felt-core
```

## Status (2026-09-21)

No longer a scaffold — the full brain loop is built and running:

- **Tool calling**: one felt-llm call carries the persona, the history and
  every skill as a native tool; a tool call runs the skill and a second call
  phrases the result (`brain/orchestrator.py`, `local_llm.py`).
- **Conversation memory**: session history is persisted to SQLite and
  loaded back in as real context on every reply, not just the current
  message in isolation (`db.py`).
- **Personality**: every reply carries a real system
  prompt (`brain.py`'s `_PERSONALITY_PROMPT`) - Felt has an actual voice
  now (warm, playful, opinionated) instead of no framing at all, which
  read as flat/robotic in practice, especially over text chat.
- **API surface**: `/v1/chat`, `/v1/voice-chat` (audio in, audio out via
  felt-voice), `/v1/text-chat` (typed input gets the same TTS + reaction-
  animation treatment voice does), `/v1/history`, a WS chat endpoint, `/v1/reaction-tag` (picks the animation tag for a line), plus
  the shopping-list and settings routes. CORS-enabled, LAN-reachable.
- 24 skills via the plugin system (weather, calendar, email, smart home,
  PoE2 price checks, image generation, and more) - see `plugins/`.

Deployed on the clockmost server via `clockmost-infra`'s vendored-copy
pattern (this repo isn't auto-synced there - see that repo's runbook for
the sync/deploy flow). Not yet built: felt-overlay's fuller tag→clip
resolution (fallback broadening, repeat-avoidance) is still felt-overlay's
own responsibility per the architecture split - felt-core just resolves an
`animation_tag`, it doesn't reach into what clips exist.
