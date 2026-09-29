"""Felt's config system.

Every environment-specific value (model endpoints, API keys, device
addresses) flows through here — env vars / .env, never hardcoded in code.
See the project README for why this matters.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeltConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FELT_", env_file=".env", extra="ignore")

    # Local model backend (default brain — see decoupling principle in the README)
    ollama_url: str = "http://localhost:11434"
    # qwen2.5:3b since 2026-09-25: it shares the Nitro5's 6 GB card with
    # felt-voice's Chatterbox-Turbo + GPU RVC (llama3.1:8b doesn't fit
    # beside them). num_ctx is sent explicitly on every local chat call so
    # the context (and so the KV-cache VRAM) never depends on Ollama's
    # server-side default.
    ollama_model: str = "qwen2.5:3b"
    ollama_num_ctx: int = 4096

    # Small, dedicated router model for tool-vs-chat decisions (2026-09-11
    # finding: the chat model itself is unreliable at this under a mixed
    # chat+tool-call prompt — a separate narrow classifier is more robust).
    router_model: str = "qwen2.5:1.5b"

    # WP-36: felt-llm, the self-hosted llama.cpp server (OpenAI-compatible,
    # one model, native tool calling). `local_first` is the single switch:
    # True = every local path (chat + tools, screen reactions, events,
    # privacy check, diary) goes to felt-llm first and the old
    # router/Ollama/cloud path is only the fallback when it's unreachable;
    # False = exactly the pre-WP-36 behaviour, i.e. the rollback.
    local_llm_url: str | None = "http://felt-llm:8080"
    local_llm_model: str = "felt"
    local_llm_timeout_s: float = 60
    local_first: bool = True

    # Deterministic frequency limit for spoken screen-observation reactions
    # (WP-02, Mario: screen reactions are always spoken - the model never
    # decides to stay silent, so this cooldown is what actually limits how
    # often she reacts). See brain.py's process_screen_context.
    screen_reaction_cooldown_s: int = 90

    # /v1/react/event (events.py): minimum seconds between any two of her
    # unprompted remarks (event lines and spoken screen reactions), plus an
    # optional JSON override of the per-event policy table, e.g.
    # '{"music_new_track": {"cooldown_s": 600}, "afk_end": {"min_duration_s": 1200}}'.
    event_reaction_min_gap_s: int = 30
    event_policies: str = ""

    # felt-voice — STT/TTS service. Unset means voice-chat simply isn't
    # available, not a hardcoded assumption it's always running.
    voice_url: str | None = None

    # WP-54: /v1/voice-chat and /v1/text-chat answer with the first
    # sentence chunk's audio and synthesise the rest in the background
    # (GET /v1/reply/{id}/chunk/{n}). The first chunk aims for at least this
    # many characters so it isn't a lone "Hm.".
    reply_streaming: bool = True
    reply_stream_first_chunk_chars: int = 40

    # felt-desktop-agent — KDE Connect etc. lives on the desktop, not here.
    desktop_agent_url: str | None = None

    # OctoPrint (the Ender 3) — vendor-agnostic on purpose: if the Ender 3
    # is ever replaced, this integration keeps working unchanged.
    octoprint_url: str | None = None
    octoprint_api_key: str | None = None

    # HP ENVY 6000 — plain IPP (Get-Printer-Attributes), no API key needed
    # at all; confirmed reachable from the server and unauthenticated
    # 2026-09-12. Host only (an IP or hostname), not a full URL — IPP has
    # its own port/path convention (see printer_status/skill.py).
    hp_printer_host: str | None = None

    # News/RSS digest — comma-separated feed URLs. Empty by default;
    # Mario hasn't specified which feeds he actually wants yet.
    news_feeds: str = ""

    def news_feeds_list(self) -> list[str]:
        return [f.strip() for f in self.news_feeds.split(",") if f.strip()]

    # ntfy — consuming Mario's existing alert topic (assistant-automation.md),
    # not a new notification system. Unset = the listener simply doesn't run.
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_user: str | None = None
    ntfy_password: str | None = None

    # Publish side — a separate, write-only ntfy user/topic, distinct from
    # the read-only subscription above (least-privilege: the credential
    # that can push to Mario's phone is not the same one that can only
    # read the existing clockmost topic).
    ntfy_publish_user: str | None = None
    ntfy_publish_password: str | None = None
    ntfy_publish_topic: str | None = None

    # Cloud delegation is a tool the model can call, not a router swap — see README
    ninerouter_url: str | None = None
    ninerouter_api_key: str | None = None

    # Plain-chat cloud routing (2026-09-21, Mario: local conversation
    # quality is weak, wire safe/banter-only replies to Gemini) — see
    # privacy.py for the gate. Talks to Google's Gemini API directly, NOT
    # via ninerouter/Antigravity — found live, 2026-09-21: every "ag/"
    # model on ninerouter carries Antigravity's own baked-in coding-agent
    # system identity ("Ready for work. What need fix?" for "how are you
    # feeling"), which no client-supplied system prompt could override,
    # even in total isolation with no other context. Google's own free
    # tier (aistudio.google.com/apikey) has no such contamination. Unset
    # means this is simply unavailable, same convention as everything else
    # here.
    gemini_api_key: str | None = None
    # Legacy field, superseded by cloud_model_conversation below (WP-05
    # split "the one cloud model" into a purpose-routed set) - kept only
    # as a back-compat override in case an existing deployment already
    # has FELT_GEMINI_CHAT_MODEL set to something specific. Unset (the
    # normal case, confirmed 2026-09-22: nothing in this deployment's
    # env sets it) means cloud_model_conversation's own default wins.
    gemini_chat_model: str | None = None

    # WP-05: felt-core's cloud (Gemini API) calls are purpose-routed
    # across three free-tier models, not one - see felt_core/cloud.py.
    # Real model ids confirmed 2026-09-22 via GET /v1beta/models against
    # the actual configured API key.
    cloud_model_conversation: str = "gemini-3.5-flash-lite"
    cloud_model_overflow: str = "gemini-3.1-flash-lite"
    cloud_model_volume: str = "gemma-4-26b-a4b-it"
    # Optional JSON override of cloud.py's built-in RPM/RPD/TPM table,
    # e.g. '{"gemma-4-26b-a4b-it": {"rpd": 1}}' - mainly for forcing a
    # fallback path in testing without waiting for the real daily reset.
    cloud_limits: str = ""

    @model_validator(mode="after")
    def _apply_legacy_gemini_chat_model(self) -> "FeltConfig":
        if self.gemini_chat_model:
            self.cloud_model_conversation = self.gemini_chat_model
        return self

    # Zabbix — clockmost's existing monitoring, reused rather than building
    # a separate health-check path. Unset means the skill reports itself
    # unavailable, same pattern as everything else here.
    zabbix_url: str | None = None
    zabbix_api_token: str | None = None
    zabbix_host: str = "clockmost"

    # Home location, for location-dependent skills (weather, etc.) — never
    # hardcoded, unset by default, must be set for this deployment.
    home_latitude: float | None = None
    home_longitude: float | None = None

    # Home Assistant — reuses the same instance/token clockmost-console
    # already talks to (CM_HA_URL/CM_HA_TOKEN there), not a new HA
    # integration. Deliberately its own explicit allowlist rather than
    # exposing every HA entity to voice control sight-unseen — empty by
    # default, same "unset/empty = disabled" convention as news_feeds.
    ha_url: str | None = None
    ha_token: str | None = None
    ha_entities: str = ""

    def ha_entities_list(self) -> list[str]:
        return [e.strip() for e in self.ha_entities.split(",") if e.strip()]

    # ComfyUI (image generation) — the Nitro5's raw LAN address, same
    # direct-not-through-SSO pattern as ollama_url (comfy.clockmost.com is
    # Authentik-gated for Mario's own browser use; a backend skill talks
    # to the box directly, same as felt-core already does for Ollama).
    # Unset = disabled, same convention as everything else here. Shares
    # the Nitro5's one GPU with Ollama — Mario swaps them by hand, so
    # this only actually works while ComfyUI is the one he's got running.
    comfyui_url: str | None = None
    # The Authentik-fronted hostname, for the link shown back to Mario
    # (his browser already has an SSO session there; felt-core's own
    # calls never go through it). Falls back to comfyui_url if unset.
    comfyui_display_url: str | None = None

    # Proactive email awareness (assistant-skills.md, Mario 2026-09-11:
    # "on email arrives -> You have mail from X, want me summarize?").
    # Unset host = the watcher simply doesn't start, same "unset/empty =
    # disabled" convention as everything else here. Poll-based (not IMAP
    # IDLE) — Mario's own spec explicitly allows "IDLE for push, or a
    # short poll interval," and polling is far simpler to get right.
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_mailbox: str = "INBOX"
    imap_poll_seconds: int = 60

    # Personal document search (drive-rag, a sibling internal service on the
    # server) — RAG over Mario's locally-synced Google Drive (docs, PDFs,
    # his Obsidian vault). Unset = the skill reports itself unconfigured,
    # same convention as everything else here.
    drive_rag_url: str | None = None

    # Google Calendar — Felt's own dedicated OAuth client (2026-09-14),
    # not a shared/borrowed credential. Mario's "Desktop app" OAuth
    # client from Google Cloud Console; refresh_token was minted via a
    # one-time local consent flow requesting the full read/write
    # "calendar" scope. Unset = the skill reports itself unconfigured,
    # same convention as everything else here.
    google_calendar_client_id: str | None = None
    google_calendar_client_secret: str | None = None
    google_calendar_refresh_token: str | None = None

    # Spotify — Felt's own dedicated OAuth client (2026-09-14), for
    # play_music (search + start new playback by name). Distinct from
    # media_control, which only controls whatever's already loaded via
    # local MPRIS on the desktop. Unset = the skill reports itself
    # unconfigured, same convention as everything else here.
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    spotify_refresh_token: str | None = None

    # WP-16 Part 3: LRCLIB lyrics, one JSON file per track id
    # (lyrics_client.py) - lyrics never change for a given track, so this
    # is a permanent cache (including negative results), unlike
    # currently-playing state's short in-process TTL.
    lyrics_cache_dir: str = "./data/lyrics"
    # LRCLIB asks clients to identify themselves with a descriptive
    # User-Agent. Neutral by default; set a contact address here per
    # deployment if wanted - never hardcoded in code.
    lyrics_user_agent: str = "felt-core/0.1 (personal assistant)"

    # Path of Exile 2 trade-API reference-data cache (poe_trade_cache.py)
    # — the item name -> base-type index + league list, refreshed weekly.
    # Lives next to the sqlite db by default, on the same persistent volume.
    poe_trade_cache_path: str = "./data/poe_trade_cache.json"

    # Persistence
    db_path: str = "./data/felt.db"

    # Plugin discovery
    plugins_dir: str = "./plugins"

    # HTTP/WebSocket frontend API
    host: str = "0.0.0.0"
    port: int = 8420

    # WP-25: enables POST /v1/proactive/debug (insert a proactive item by
    # hand, for the accept test / live poking). Off by default - this is a
    # debug-only door into the proactive queue, not a normal client path.
    debug: bool = False


def load_config() -> FeltConfig:
    return FeltConfig()
