"""Felt's config system.

Every environment-specific value (model endpoints, API keys, device
addresses) flows through here — env vars / .env, never hardcoded in code.
See the project README for why this matters.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class FeltConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FELT_", env_file=".env", extra="ignore")

    # felt-llm: the self-hosted llama.cpp server (OpenAI-compatible, one
    # model, native tool calling). The only model backend: nothing here
    # talks to a cloud API, and when felt-llm is unreachable the model
    # endpoints answer 503 "felt-llm offline" (WP-36 D'').
    local_llm_url: str | None = "http://felt-llm:8080"
    local_llm_model: str = "felt"
    local_llm_timeout_s: float = 60

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
    # WP-55: shared bearer token for every API route (FELT_API_TOKEN, sops on
    # the server; the desktop clients send it). Unset = no auth (the old LAN
    # posture) - set it only once every client sends it.
    api_token: str | None = None
    # WP-58: the felt-responder's own token, accepted only on /v1/incidents/*
    # (FELT_RESPONDER_TOKEN). Unset = the responder can't get in.
    responder_token: str | None = None
    # WP-62 phase 4: the browser app's own origin, for the CSRF guard on the
    # SSO-gated (cookie/forward-auth) state-changing routes. The forward-auth
    # cookie is SameSite=Lax and every *.clockmost.com app is same-site, so a
    # sibling page could otherwise drive a credentialed mutation; felt-core has
    # no CSRF token. An unsafe SSO request whose Origin is set and isn't this is
    # refused. Native clients use the API token (not these routes) and send no
    # Origin, so they're unaffected.
    app_origin: str = "https://felt.clockmost.com"

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

    # Plane — the WP board (WP-37). Felt only ever reads it (board_status
    # refuses anything but GET); moving cards is Mario's or Claude's job.
    plane_url: str = "https://plane.clockmost.com"
    plane_api_token: str | None = None
    plane_workspace: str = "clockmost"
    plane_project: str = "FELT"

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
    # direct-not-through-SSO pattern as local_llm_url (comfy.clockmost.com is
    # Authentik-gated for Mario's own browser use; a backend skill talks
    # to the box directly, same as felt-core already does for felt-llm).
    # Unset = disabled, same convention as everything else here. Shares
    # the Nitro5's one GPU with other GPU work, so it only works while
    # ComfyUI is what's running there.
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
