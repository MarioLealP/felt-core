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

## Status

Scaffold only (2026-09-11) — config system, plugin interface, and project
layout exist; the actual brain loop (chat → tool-calling → plugin
execution → response) isn't built yet.
