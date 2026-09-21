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

A consequence worth naming explicitly: **the local model is the default
brain; cloud is opt-in per call.** Cloud delegation isn't a different
"backend" swapped in by a router — it's just another tool the local model
can choose to call. Nothing here should default to a cloud API for
something the local model can already handle.

Tool-calling itself is a manual parsing loop, not dependent on any
particular model's native function-calling support — this deliberately
decouples model choice from tool-calling capability, so the local model
gets picked for chat quality (including non-English quality) first.

## Quickstart

```
uv sync
cp .env.example .env   # edit for your environment
uv run felt-core
```

## Status (2026-09-21)

No longer a scaffold — the full brain loop is built and running:

- **Routing**: a small dedicated model (`router_model`, `qwen2.5:1.5b`,
  temperature 0) decides tool-vs-chat per message; the main chat model
  never sees tool-call instructions, it just converses (`router.py`,
  `brain.py`).
- **Conversation memory**: session history is persisted to SQLite and
  loaded back in as real context on every reply, not just the current
  message in isolation (`db.py`).
- **Personality**: every reply, local or cloud, carries a real system
  prompt (`brain.py`'s `_PERSONALITY_PROMPT`) - Felt has an actual voice
  now (warm, playful, opinionated) instead of no framing at all, which
  read as flat/robotic in practice, especially over text chat.
- **Cloud delegation for plain conversation**: local chat-model quality was
  genuinely weak for banter, so a plain (no-skill) message can be routed to
  Google's Gemini API directly (`gemini_api_key`/`gemini_chat_model` in
  config) for a better reply - gated by `privacy.py`'s `is_safe_for_cloud()`,
  a small dedicated fail-closed classifier that judges the *whole* recent
  context window (not just the new message) before anything leaves the LAN.
  Deliberately **not** routed through ninerouter/Antigravity - every `ag/`
  model there carries Antigravity's own baked-in coding-agent identity that
  no system prompt could override; Google's own free tier has no such
  contamination. A tool-result reply always stays local regardless, since
  tool output can carry real personal data.
- **API surface**: `/v1/chat`, `/v1/voice-chat` (audio in, audio out via
  felt-voice), `/v1/text-chat` (typed input gets the same TTS + reaction-
  animation treatment voice does), `/v1/history`, a WS chat endpoint, plus
  the shopping-list and settings routes. CORS-enabled, LAN-reachable.
- 24 skills via the plugin system (weather, calendar, email, smart home,
  PoE2 price checks, image generation, and more) - see `plugins/`.

Deployed on the clockmost server via `clockmost-infra`'s vendored-copy
pattern (this repo isn't auto-synced there - see that repo's runbook for
the sync/deploy flow). Not yet built: felt-overlay's fuller tag→clip
resolution (fallback broadening, repeat-avoidance) is still felt-overlay's
own responsibility per the architecture split - felt-core just resolves an
`animation_tag`, it doesn't reach into what clips exist.
