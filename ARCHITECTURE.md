# felt-core — Architecture Audit (WP-38)

Read-only audit, 2026-09-26. Measured via `git ls-files`, `wc -l`, grep of
imports/config, and one `uv run pytest -q` run. No source changed.

## 1. Purpose

felt-core is Felt's "brain": a FastAPI service that takes a message (typed,
spoken, or a screen/event observation), decides whether a local model, a
cloud model, or a plugin skill should answer, and returns a spoken reply
plus optional animation/UI hints. It owns every skill (plugins/), all
conversation/state persistence, and the local-vs-cloud routing policy.

## 2. Runtime

- Runs on the **clockmost server** (`192.168.1.110:8420`), one Docker
  service (`docker-compose.yml`: image `felt-core:local`, `restart:
  unless-stopped`, `mem_limit: 1g`, `edge`+`authentik` networks). Port 8420
  is LAN-reachable, not public.
- **Not auto-deployed from this repo.** The server runs a *vendored copy*
  under `clockmost-infra`'s own git history
  (`/home/clockmost-infra/projects/felt-core/app`). Flow: commit here →
  `git archive HEAD | ssh … tar -x` into that path → commit+push inside
  `clockmost-infra` → trigger `clockmost-deploy.service` (health-checks,
  auto-reverts on failure). Pushing this repo's own `origin` does **not**
  touch the running container.
- Persistence is one SQLite file (`FELT_DB_PATH`) on the `felt-data`
  volume; plugins mount read-only (`./plugins:/app/plugins:ro`).
- Secrets live in `clockmost-infra`'s **sops** `secrets.env` (not read for
  this audit). By key name: `FELT_GEMINI_API_KEY`,
  `FELT_GOOGLE_CALENDAR_CLIENT_SECRET`, `FELT_GOOGLE_CALENDAR_REFRESH_TOKEN`,
  `FELT_SPOTIFY_CLIENT_SECRET`, `FELT_SPOTIFY_REFRESH_TOKEN`,
  `FELT_ZABBIX_API_TOKEN`, `FELT_OCTOPRINT_API_KEY`, `FELT_IMAP_PASSWORD`,
  `FELT_NTFY_PASSWORD`, `FELT_NTFY_PUBLISH_PASSWORD`,
  `FELT_NINEROUTER_API_KEY`, `FELT_HA_TOKEN`. Non-secret config is `.env`
  (see §6).

## 3. Entry points

- `src/felt_core/__init__.py::main()` — CLI entry (`felt-core` script);
  thin wrapper, not used in the deployed container.
- `src/felt_core/api.py::run()` — the deployed entry (`felt-core-serve`
  script, and the `Dockerfile`'s `CMD`). Calls `uvicorn.run(app, …)`.
- `api.py::app` (FastAPI instance) with `lifespan()` starting four
  background tasks on boot: `ntfy_listen`, `email_watch`,
  `poe_trade_cache_watch`, `weather_precompute_loop`, `music_taste_loop`,
  plus a one-shot `reschedule_pending_alarms` and a latency-row prune.
- `src/felt_core/brain.py::chat()` — the actual per-message orchestration
  every chat/voice endpoint funnels through.

## 4. Module map

### `src/felt_core/` — core (5,436 LOC)

| file | LOC | responsibility |
|---|---:|---|
| `brain.py` | 1080 | **God file.** Orchestration: router/privacy dispatch, local vs. cloud reply generation, skill-result phrasing, screen-reaction (two implementations), weather-precompute background loop, music-taste background loop. |
| `db.py` | 687 | SQLite schema (12 tables, all in one `_connect()`) + every persistence function (messages, alarms, facts, settings, shopping list, observations, cloud usage, music taste, event reactions, latency). |
| `api.py` | 600 | FastAPI app: CORS, latency middleware, all 20 HTTP/WS routes, request→brain wiring, TTS synthesis glue. |
| `cloud.py` | 470 | Sole Gemini/Gemma API client: purpose-routed model choice, RPM/TPM/RPD quota tracking, thinking-budget handling, `CloudUnavailable`. |
| `events.py` | 400 | Desktop-event (`/v1/react/event`) policy table, cooldown/dedupe decision, prompt building, phrasing. |
| `proactive.py` | ~90 | WP-25: the `proactive` queue's `enqueue_proactive`/`wait_for_next` (long-poll logic, timeout clamping) - other WPs' server→desktop pushes call `enqueue_proactive`, not `db.py` directly. |
| `router.py` | 257 | Tool-vs-chat routing: builds a ~160-line few-shot prompt for the router model, parses its JSON reply. |
| `config.py` | 227 | `FeltConfig` (pydantic-settings), every env-driven setting. |
| `lyrics_client.py` | 204 | LRCLIB lookup + disk cache (one JSON file per track). |
| `google_calendar.py` | 173 | Calendar OAuth token cache + list/create-event REST calls. |
| `metrics.py` | 164 | Per-request `Server-Timing` instrumentation, backed by the `latency` table. |
| `email_watcher.py` | 138 | IMAP poll loop, pending-email confirmation flow. |
| `poe_trade_cache.py` | 134 | Weekly-refreshed PoE2 trade reference-data cache. |
| `spotify_client.py` | 133 | Spotify OAuth token cache + now-playing/search/playback calls. |
| `privacy.py` | 108 | `is_safe_for_cloud()` — fail-closed classifier gating any cloud call. |
| `plugin_loader.py` | 81 | Discovers `plugins/*/skill.py`, merges `tags.json`/`skills.json`. |
| `zabbix_client.py` | 68 | Pure parsing helpers for Zabbix container-health items. |
| `alarms.py` | 66 | Alarm persistence + reschedule-on-restart. |
| `date_resolution.py` | 66 | Free-text day/date phrase → concrete date. |
| `speech.py` | 57 | Strips markdown/emoji/URLs before TTS. |
| `plugin.py` | 53 | `Skill` Protocol, `SkillResult`/`UIAction`/`ExecutionContext` dataclasses. |
| `music_taste.py` | 53 | Skip/full-play stats → a prompt-ready summary sentence. |
| `ntfy_listener.py` | 52 | ntfy subscribe loop → `ntfy_events` table. |
| `animation.py` | 45 | Resolves a skill result → animation tag (output-tag > default-tag > None). |
| `tts_cache.py` | 34 | In-memory LRU cache of rendered TTS audio, keyed by text. |
| `params.py` | 31 | Recovers a skill param the router put under the wrong key. |
| `notify.py` | 30 | ntfy publish (write-only credential, separate from the listener's). |
| `tools.py` | 15 | `run_skill()` — looks up and calls a plugin's `handle()`. |
| `__init__.py` | 10 | CLI `main()`. |

### `plugins/` — skills (2,146 LOC across 28 skill dirs + 1 broken one)

| plugin | LOC | responsibility |
|---|---:|---|
| `poe_price_check` | 280 | PoE2 currency/unique price lookup via poe.ninja + trade API. |
| `get_weather` | 244 | Open-Meteo forecast, day/date resolution, condition→animation escalation. |
| `generate_image` | 144 | ComfyUI job submit/poll (SD1.5/SDXL/AnimeXL workflows). |
| `felt_presence` | 133 | Mario-triggered hush/unhush/hide/come/look/repeat → felt-tray UIAction. |
| `smart_home_control` | 122 | Home Assistant entity status/service call (explicit allowlist). |
| `printer_status` | 112 | OctoPrint (Ender 3) + raw IPP (HP printer) status. |
| `create_calendar_event` | 89 | Google Calendar write. |
| `set_alarm` | 88 | Time-of-day alarm, persisted. |
| `get_container_status` | 85 | Docker container health via Zabbix. |
| `media_control` | 75 | Play/pause/skip/volume via felt-desktop-agent (MPRIS). |
| `now_playing` | 62 | Read-only "what's this song" via Spotify. |
| `news_digest` | 60 | RSS/Atom feed digest. |
| `list_calendar_events` | 59 | Google Calendar read, all calendars. |
| `play_music` | 58 | Start named track/artist via Spotify. |
| `shopping_list` | 57 | Add/list items (shared with the `/shopping-list` mini web app). |
| `delegate_research` | 55 | Explicit cloud-delegation tool (via ninerouter). |
| `search_documents` | 52 | RAG query to the sibling `drive-rag` service. |
| `cancel_alarm` | 51 | Cancels a pending alarm. |
| `web_search` | 46 | Self-hosted SearXNG query, local-only. |
| `set_timer` | 42 | In-process (unpersisted) countdown → ntfy on elapse. |
| `get_gpu_status` | 42 | Nitro5 GPU/VRAM via felt-voice's own status endpoint. |
| `get_phone_battery` | 29 | KDE Connect battery, via felt-desktop-agent. |
| `remember_fact` | 28 | Writes to the durable `facts` table. |
| `find_phone` | 27 | KDE Connect ring, via felt-desktop-agent. |
| `list_alarms` | 25 | Lists pending alarms. |
| `check_email` | 24 | Reads the email_watcher's own log. |
| `get_recent_alerts` | 22 | Reads the ntfy_listener's own log. |
| `get_time` | 18 | Reference/minimal skill. |
| `change_shirt` | 17 | **Dead** — see §8. |

`plugins/skills.json` — privacy-class manifest. `plugins/tags.json` —
skill→default-animation-tag manifest. `plugins/README.md` — the
plugin-authoring contract. `static/shopping-list.html` (136 LOC) —
standalone page served at `GET /shopping-list`, same REST routes a voice
command uses.

## 5. Interfaces

### HTTP/WS endpoints (all in `src/felt_core/api.py`)

| method | path | handler |
|---|---|---|
| GET | `/v1/metrics/latency` | `latency_metrics_endpoint` |
| GET | `/v1/metrics/cloud` | `cloud_metrics_endpoint` |
| POST | `/v1/chat` | `chat_endpoint` |
| GET | `/v1/history` | `history_endpoint` |
| POST | `/v1/voice-chat` | `voice_chat_endpoint` |
| POST | `/v1/text-chat` | `text_chat_endpoint` |
| POST | `/v1/context/screen` | `screen_context_endpoint` |
| POST | `/v1/react/screen` | `react_screen_endpoint` |
| POST | `/v1/react/event` | `react_event_endpoint` |
| POST/GET | `/v1/settings/temperature-unit` | `set_temperature_unit` / `get_temperature_unit` |
| GET | `/v1/weather/now` | `weather_now_endpoint` |
| GET | `/v1/music/now` | `music_now_endpoint` |
| GET | `/v1/music/lyrics` | `music_lyrics_endpoint` |
| GET/POST | `/v1/shopping-list` | `get_shopping_list_endpoint` / `add_shopping_list_item_endpoint` |
| DELETE | `/v1/shopping-list/checked` | `clear_checked_shopping_list_endpoint` |
| PATCH/DELETE | `/v1/shopping-list/{item_id}` | `set_shopping_item_checked_endpoint` / `delete_shopping_item_endpoint` |
| GET | `/shopping-list` | `shopping_list_page` (static HTML) |
| WS | `/v1/chat/ws` | `chat_ws` |
| GET | `/v1/proactive/next?timeout=` | `proactive_next_endpoint` (WP-25 long-poll, 204 on timeout) |
| GET | `/v1/proactive/{item_id}/audio` | `proactive_audio_endpoint` (WP-25, reuses the WP-06 TTS cache) |
| POST | `/v1/proactive/{item_id}/requeue` | `proactive_requeue_endpoint` (WP-25) |
| POST | `/v1/proactive/debug` | `proactive_debug_endpoint` (WP-25, 404 unless `FELT_DEBUG=1`) |

No auth on any route (documented, deliberate — LAN-only posture, same as
Ollama/ComfyUI elsewhere on clockmost). All routes are one file, `api.py`.

### Outbound calls

| target | file(s) |
|---|---|
| Ollama (chat + router models) | `brain.py::_ollama_chat`, `router.py::decide_tool` |
| Gemini / Gemma (cloud) | `cloud.py::cloud_generate` (sole choke point) |
| felt-voice (STT/TTS, GPU status) | `api.py` (`_synthesize_reply`, `voice_chat_endpoint`), `plugins/get_gpu_status/skill.py` |
| Spotify | `spotify_client.py`; `plugins/play_music`, `plugins/now_playing` |
| LRCLIB | `lyrics_client.py` |
| Gmail (IMAP) | `email_watcher.py` |
| Google Calendar | `google_calendar.py`; `plugins/list_calendar_events`, `plugins/create_calendar_event` |
| Zabbix | `zabbix_client.py` (parsing) + `plugins/get_container_status/skill.py` (the actual HTTP call) |
| ntfy | `ntfy_listener.py` (subscribe), `notify.py` (publish); `plugins/get_recent_alerts`, `plugins/set_timer` |
| ComfyUI | `plugins/generate_image/skill.py` |
| Home Assistant | `plugins/smart_home_control/skill.py` |
| OctoPrint / IPP printer | `plugins/printer_status/skill.py` |
| drive-rag | `plugins/search_documents/skill.py` |
| felt-desktop-agent (KDE Connect/MPRIS) | `plugins/find_phone`, `plugins/get_phone_battery`, `plugins/media_control`, `plugins/set_alarm` |
| SearXNG | `plugins/web_search/skill.py` |
| ninerouter (cloud delegation) | `plugins/delegate_research/skill.py` |
| poe.ninja / official PoE trade API | `poe_trade_cache.py`, `plugins/poe_price_check/skill.py` |
| felt-animation (port 8421) | `plugins/felt_presence/skill.py`; `plugins/change_shirt/__init__.py` (dead, §8) |

### Data stores (SQLite, `db.py`, one file at `FELT_DB_PATH`)

`messages`, `ntfy_events`, `facts`, `settings`, `alarms`, `pending_emails`,
`shopping_list`, `screen_observations`, `cloud_usage`, `music_taste`,
`event_reactions`, `latency` — 12 tables, all created in one `_connect()`.
Plus two flat-file caches outside SQLite: `lyrics_client.py`'s per-track
JSON cache dir, and `poe_trade_cache.py`'s single JSON file. TTS cache
(`tts_cache.py`) is in-memory only, not persisted.

## 6. Config

`config.py`'s `FeltConfig` (pydantic-settings, `FELT_` prefix) is the only
place env vars are read; every plugin/module takes settings from a
`FeltConfig` instance, never `os.environ` directly (confirmed by grep).

**Missing from `.env.example`** (present in `config.py`, undocumented):
`FELT_GEMINI_API_KEY` (the actual cloud enable switch — a deployer
following only the example file would never discover cloud routing
exists), `FELT_GEMINI_CHAT_MODEL` (legacy override, low priority), and
`FELT_HP_PRINTER_HOST`/`FELT_HA_URL`/`FELT_HA_TOKEN`/`FELT_HA_ENTITIES` —
the whole Home Assistant + HP-printer integrations are invisible in the
example file.

**Hardcoded addresses** (violate the project's own "no hardcoding" rule,
README.md §Philosophy):
- `plugins/change_shirt/__init__.py:7` — `http://127.0.0.1:8421`
  (felt-animation's port), not read from `config.desktop_agent_url` or any
  setting. Moot in practice since the plugin never loads (§8), but if it
  were fixed to load, this would also need fixing.
- `plugins/web_search/skill.py:32` — `_SEARXNG_URL =
  "http://searxng:8080/search"`, not configurable at all (every other
  sibling-service URL — `drive_rag_url`, `comfyui_url`, `ha_url` — is an
  env setting with a documented default).

## 7. Tests

**359 passed** (`uv run pytest -q`, 2026-09-26; zero failures/skips). Test
suite is 4,788 LOC across ~70 files, ~4.4x the size of the code it tests.

- **Core modules with no test file:** `ntfy_listener.py` (no coverage at
  all), `tools.py` (untested directly but trivial — 15 lines — and
  exercised transitively by every skill test).
- **Plugins with no test at all** (18 of 28): `cancel_alarm`,
  `change_shirt` (dead, §8), `delegate_research`, `find_phone`,
  `get_container_status`, `get_gpu_status`, `get_phone_battery`,
  `get_recent_alerts`, `get_time`, `list_alarms`, `media_control`,
  `news_digest`, `printer_status`, `remember_fact`, `set_alarm`,
  `set_timer`, `shopping_list`, `smart_home_control`, `web_search`.
- **Environment-dependent tests:** none that actually require live
  credentials — the five `*_integration.py` suites (calendar, Spotify,
  lyrics, PoE price/trade-cache) replay recorded VCR cassettes
  (`tests/cassettes/`) by default; `conftest.py` only hits real APIs when
  manually re-recorded with `--record-mode=once` plus real `FELT_*` env
  vars. A deliberately good pattern, not a smell.

## 8. Smells

- **God file — `brain.py:1-1080`.** Five unrelated responsibilities in one
  module: request orchestration (`chat()` 708-848), two independent
  screen-reaction implementations (`process_screen_context` 867-959,
  `react_to_screen` 997-1080, which duplicate most of their
  cooldown/observation/cloud-fallback logic), a background
  weather-precompute loop (538-632), and a background music-taste loop
  (635-705).
- **Dead plugin — `plugins/change_shirt/__init__.py` +
  `plugins/change_shirt/manifest.json`.** `plugin_loader.py:62` only
  discovers a directory containing `skill.py`; this one uses an older
  `__init__.py`+`manifest.json` convention instead, so it is **never
  loaded** — absent from the router's tool list, uncallable — yet still
  listed as `"public"` in `plugins/skills.json` and `"ambient/show_off"` in
  `plugins/tags.json` as if live. Its `handle()` also returns a bare `str`,
  not the `SkillResult` the `Skill` Protocol requires (`plugin.py:53`), so
  it wouldn't work even if loaded. No test would have caught this — it has
  none.
- **Cross-module private-name reach-through** — `events.py:314`
  (`_generate_line`) imports `_local_reply`, `_music_taste_context`,
  `_personality_prompt` straight from `brain.py`'s underscore-prefixed
  (private-by-convention) internals. `brain.py` never imports `events.py`
  back, so it's not circular, but `events.py` now depends on `brain.py`'s
  private internals staying stable.
- **Duplicated OAuth-refresh logic** — `spotify_client.py:33` and
  `google_calendar.py:46`, both `_get_access_token()`: identical shape
  (module-level `_token_cache` dict, 60s expiry margin,
  `grant_type=refresh_token` POST), never factored into a shared helper.
- **Duplicated cloud→local fallback pattern** — five near-identical
  `try: cloud_generate(...) / except Exception: fall back to local`
  blocks: `brain.py:242,377,469,559` and `events.py:329`. Each has slightly
  different privacy gating, but no shared `try_cloud_then_local()` exists.
- **Long functions (>80 lines):** `brain.py:708` `chat()` (140),
  `brain.py:867` `process_screen_context()` (92), `brain.py:997`
  `react_to_screen()` (83), `cloud.py:361` `cloud_generate()` (86),
  `router.py:25` `_build_router_prompt()` (161, almost entirely a
  hand-written few-shot prompt string), `db.py:13` `_connect()` (197, all
  `CREATE TABLE` statements — mechanical/low-risk, but one function owns
  all 12 schemas with no migration structure).
- **Broad `except Exception`:** 22 occurrences project-wide, 10 in
  `brain.py` alone. The riskiest is `api.py:589`, `chat_ws`'s handler,
  which swallows *any* error and just closes the socket with zero logging.
- **Stale docstring** — `privacy.py:2` says cloud routing goes "via
  ninerouter", but `config.py:90-97`/`brain.py:12-15` are explicit that
  plain-conversation cloud calls go **directly** to the Gemini API
  specifically to avoid ninerouter's Antigravity contamination.
- **No TODO/FIXME/XXX markers anywhere** — a positive: dated rationale in
  comments instead of deferred-work markers.

## 9. Ranked refactor suggestions

1. **Delete or fix `plugins/change_shirt/`.** Either delete the directory
   (+ its manifest entries) or give it a real `skill.py` returning
   `SkillResult`, routed through `config.desktop_agent_url`. Why: dead,
   misleading, hardcodes an address if ever revived. Effort **S**. Risk:
   none, unreachable. **agent-ok** — Mario just picks delete-vs-fix.
2. **Split `brain.py`** into routing/replies/screen-reaction/background-
   loops modules (e.g. a `brain/` package, re-exported so `api.py`'s
   imports don't change). Why: 1080 lines, 5 unrelated responsibilities,
   confirmed by measurement, matches Track B's own plan. Effort **M**.
   Risk: medium — many call sites and `lifespan()` task wiring must keep
   working; prove it with before/after parity on the 359 passing tests.
   **Needs Claude/design care** — its own WP, not a quick pass.
3. **Add the missing `.env.example` keys** (`FELT_GEMINI_API_KEY`,
   `FELT_HA_URL`/`_TOKEN`/`_ENTITIES`, `FELT_HP_PRINTER_HOST`). Why:
   README's rule #1 promises every setting is documented; right now a
   fresh deployer can't discover cloud routing or two integrations exist.
   Effort **S**. Risk: none. **agent-ok**.
4. **Factor the OAuth-token-cache pattern** out of `spotify_client.py` and
   `google_calendar.py` into one shared helper. Why: identical logic
   duplicated verbatim; a third OAuth integration would copy it again.
   Effort **S**. Risk: low, both sites already unit-tested. **agent-ok**.
5. **Move `router.py`'s 161-line few-shot prompt out of Python**, or split
   it into one example per skill co-located with that skill. Why: every
   new plugin also needs a hand-written example maintained in a separate
   file, contradicting the "drop a folder in, it's a skill" pitch. Effort
   **M** (S if just extracted to a template with no behavior change).
   Risk: low-medium — routing behavior can shift; verify with
   `test_router.py` plus manual spot-checks. Design call — **needs
   Claude**.
6. **Extract a shared `try_cloud_then_local()` helper** for the five
   repeated cloud-fallback blocks. Why: shrinks `brain.py`, removes
   copy-paste drift risk. Effort **S**. Risk: low, well tested. **agent-ok**
   given a precise spec; worth a quick Claude pass to confirm signature.
7. **Fix `api.py:589` `chat_ws`'s bare `except Exception: await
   websocket.close()`** with a logged exception. Why: the one
   exception-swallow with zero logging in the codebase. Effort **S**.
   Risk: none. **agent-ok**.
