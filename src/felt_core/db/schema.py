"""The tables and the in-place migrations, run once per process by session._connect."""

import sqlite3

from felt_core.db.kinds import CHAT, EVENT, REMARK

_MESSAGE_COLUMNS = (
    ("kind", f"TEXT NOT NULL DEFAULT '{CHAT}'"),
    # where the line came from, e.g. "screen:812" or "event:song" (free-form)
    ("source_ref", "TEXT"),
    # Margins state (read / dismissed / snoozed); unused for chat
    ("read_at", "TEXT"),
    ("dismissed_at", "TEXT"),
    ("snoozed_until", "TEXT"),
)


def _migrate_message_kinds(conn: sqlite3.Connection) -> None:
    """Adds the WP-42.3 columns to a `messages` table made before them.

    Labels the remarks already in it: the last use of the old test (her line's
    text matching a screen reaction or event line), which `kind` replaces.
    Only runs while the column is missing.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "kind" in have:
        return
    try:
        with conn:
            for name, decl in _MESSAGE_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")
            conn.execute(
                "UPDATE messages SET kind = ? WHERE role = 'assistant' AND content IN"
                " (SELECT spoken_reply FROM screen_observations"
                "  WHERE session_id = messages.session_id AND spoken_reply IS NOT NULL)",
                (REMARK,),
            )
            conn.execute(
                "UPDATE messages SET kind = ? WHERE role = 'assistant' AND kind = ? AND content IN"
                " (SELECT line FROM event_reactions WHERE session_id = messages.session_id)",
                (EVENT, CHAT),
            )
    except sqlite3.OperationalError as exc:
        # another connection migrated first
        if "duplicate column" not in str(exc):
            raise
    conn.execute("CREATE INDEX IF NOT EXISTS messages_kind ON messages (session_id, kind, id)")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Incoming ntfy messages — a log Felt can be asked about
    # conversationally ("anything wrong recently?"), separate from both
    # the conversation log and the facts table.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ntfy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            message TEXT NOT NULL,
            priority INTEGER,
            received_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Durable facts — distinct from conversation history above. These
    # persist across every session, not just one conversation thread, and
    # are only written via an explicit "remember this" mechanism (the
    # remember_fact skill), never inferred silently — keeps this
    # auditable and correctable rather than a black box.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Small key-value store for user preferences (e.g. temperature unit)
    # that need to persist and be changeable from any client (the
    # dashboard, the tray app) — distinct from `facts`, which is
    # LLM-context memory, not app configuration.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    # Time-of-day alarms (distinct from set_timer, which is duration-only
    # and deliberately not persisted — assistant-skills.md, Mario
    # 2026-09-12: "another one skill i remembered was set an alarm /
    # timer"). Alarms ARE persisted: one set the night before has to
    # survive a felt-core restart between then and when it fires, unlike
    # a short timer.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fire_at TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            fired INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Passive screen observations from felt-vision, deliberately NOT the
    # `messages` table (2026-09-22 fix, WP-02): a description was
    # previously saved there as a fake `user` message, which both wiped
    # real conversation out of get_history's 20-row window after ~20
    # window switches, and made every observation show up as a fake
    # "Mario said" bubble in the dashboard chat. `spoken_reply` is NULL
    # until (if) a reaction is actually spoken - see
    # seconds_since_last_spoken_reaction, the deterministic cooldown that
    # replaces letting the model decide to stay silent.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screen_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            description TEXT NOT NULL,
            spoken_reply TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # WP-16 Part 2: one row per finished listen (a track that stopped
    # being "currently playing" - changed or playback stopped), classified
    # skip/full-play by music_taste.is_skip. music_taste_loop in api.py
    # writes these; get_music_taste_stats aggregates them per track for
    # music_taste.summarize_taste to turn into a prompt note.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS music_taste (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id TEXT NOT NULL,
            track_name TEXT NOT NULL,
            artists TEXT NOT NULL,
            played_ms INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            skipped INTEGER NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # /v1/react/event's spoken lines (events.py) - deliberately NOT
    # screen_observations: an event isn't something seen on screen, and
    # must never feed the screen-reaction cooldown/observations block. The
    # rows are also the deterministic per-event cooldown/dedupe state.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event TEXT NOT NULL,
            dedupe_key TEXT,
            line TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # WP-25: the proactive channel (felt-core -> desktop). One row per
    # server-initiated remark (the diary morning greeting, the calendar
    # heads-up, ...) waiting to be picked up by felt-tray's long-poll.
    # `payload_json` carries the optional animation_tag/emotion - not
    # separate columns, since the WP-25 spec's table is exactly these six
    # columns and both are purely additive extras on top of `text`.
    # `expires_at` NULL means "never expires" (not currently used by any
    # caller, but a real state, not a footgun); `delivered_at` NULL means
    # still waiting.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proactive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT,
            delivered_at TEXT
        )
        """
    )
    # WP-26: felt-animation's silent desktop-event log (POST /v1/events) -
    # distinct from event_reactions above, which is SPOKEN reactions to
    # /v1/react/event. This is just a record (game_start/stop, afk_start/
    # end, day_start, music_track, mood, window_fall, prop_used, hush,
    # ledge_sit, ...) that the diary's material gatherer reads back out by
    # date range. `at` is stored normalized to a UTC 'YYYY-MM-DD HH:MM:SS'
    # string (api.py's normalize_at) so it sorts/range-queries the same way
    # every other `created_at` column here does.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # WP-26: one row per day's diary entry, written once by the nightly
    # writer (felt_core.core.diary). `date` is the Europe/Lisbon calendar date
    # (YYYY-MM-DD) the entry is ABOUT, not when it was written (it's always
    # written the next morning). `callbacks_json` is a list of
    # {"text", "uses"} objects - each callback is usable in the prompt at
    # most twice (see felt_core.core.self_memory.memory_block) before it's
    # skipped, tracked here rather than a separate table since it's small
    # and always read/written alongside the entry it belongs to.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diary (
            date TEXT PRIMARY KEY,
            entry TEXT NOT NULL,
            callbacks_json TEXT NOT NULL DEFAULT '[]',
            mood_word TEXT NOT NULL DEFAULT '',
            edited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # WP-26: Sunday compression of the past week's diary rows into one
    # summary. `week` is an ISO week string, e.g. "2026-W39".
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diary_weeks (
            week TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            callbacks_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # WP-26 combined with personality plan P4: what SHE takes away from a
    # day, distinct from the diary (their shared record). Added
    # automatically by the nightly writer, never behind an approval queue
    # (Mario, 2026-09-27 decision 3) - he views/pins/edits/vetoes/deletes
    # after the fact instead, via the /v1/self-memory API.
    # `status`: active (usable in the prompt) / retired (aged out past the
    # 30-cap, still kept for history) / vetoed (Mario rejected it - never
    # re-added, its text goes on the writer's "never" list).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS self_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            keywords_json TEXT NOT NULL DEFAULT '[]',
            source_date TEXT,
            uses INTEGER NOT NULL DEFAULT 0,
            last_used TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            pinned INTEGER NOT NULL DEFAULT 0,
            edited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS latency (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            endpoint TEXT,
            stage TEXT NOT NULL,
            ms REAL NOT NULL,
            route TEXT,
            skill TEXT,
            model TEXT,
            chars INTEGER,
            session_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # last: it reads the remark tables created above
    _migrate_message_kinds(conn)
