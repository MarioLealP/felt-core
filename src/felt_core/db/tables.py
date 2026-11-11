"""Every felt.db table as a SQLModel model - the schema Alembic migrates (db/migrations).

The baseline is exactly the schema the hand-written CREATE TABLEs made (names, SQLite types, defaults, keys), so a
database that predates Alembic only gets stamped. Booleans are still INTEGER 0/1 and times still TEXT, as stored; the
Meru naming pass (singular tables, `_at` columns) comes later as its own migration.
"""

from sqlalchemy import REAL, Column, Index, Integer, MetaData, Table, Text, text
from sqlalchemy.types import TypeEngine
from sqlmodel import Field, SQLModel

NOW = text("(datetime('now'))")
AUTOINCREMENT = {"sqlite_autoincrement": True}


def _now() -> Column:
    return Column(Text, nullable=False, server_default=NOW)


def _text(default: str | None = None, *, nullable: bool = False) -> Column:
    return Column(Text, nullable=nullable, server_default=text(f"'{default}'") if default is not None else None)


def _int(default: int | None = None, *, nullable: bool = False) -> Column:
    return Column(Integer, nullable=nullable, server_default=text(str(default)) if default is not None else None)


def _key(type_: type[TypeEngine] = Integer) -> Column:
    # the hand-written tables never said NOT NULL on a primary key (an INTEGER one is the rowid anyway)
    return Column(type_, primary_key=True, nullable=True)


class Message(SQLModel, table=True):
    """A chat turn or any line she says (`kind`: db/kinds.py)."""

    __tablename__ = "messages"
    __table_args__ = (Index("messages_kind", "session_id", "kind", "id"), AUTOINCREMENT)

    id: int | None = Field(default=None, sa_column=_key())
    session_id: str = Field(sa_column=_text())
    role: str = Field(sa_column=_text())
    content: str = Field(sa_column=_text())
    created_at: str | None = Field(default=None, sa_column=_now())
    kind: str = Field(default="chat", sa_column=_text("chat"))
    source_ref: str | None = Field(default=None, sa_column=_text(nullable=True))
    read_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    dismissed_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    snoozed_until: str | None = Field(default=None, sa_column=_text(nullable=True))


class NtfyEvent(SQLModel, table=True):
    """One notification that came in over ntfy."""

    __tablename__ = "ntfy_events"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    title: str | None = Field(default=None, sa_column=_text(nullable=True))
    message: str = Field(sa_column=_text())
    priority: int | None = Field(default=None, sa_column=_int(nullable=True))
    received_at: str | None = Field(default=None, sa_column=_now())


class Fact(SQLModel, table=True):
    """One fact about Mario."""

    __tablename__ = "facts"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    content: str = Field(sa_column=_text())
    created_at: str | None = Field(default=None, sa_column=_now())


class Setting(SQLModel, table=True):
    """Small key/value state."""

    __tablename__ = "settings"

    key: str = Field(sa_column=_key(Text))
    value: str = Field(sa_column=_text())


class Alarm(SQLModel, table=True):
    """A time-of-day alarm."""

    __tablename__ = "alarms"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    fire_at: str = Field(sa_column=_text())
    label: str = Field(default="", sa_column=_text(""))
    fired: int = Field(default=0, sa_column=_int(0))
    created_at: str | None = Field(default=None, sa_column=_now())


class ScreenObservation(SQLModel, table=True):
    """What she saw on the screen, and what she said about it."""

    __tablename__ = "screen_observations"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    session_id: str = Field(sa_column=_text())
    description: str = Field(sa_column=_text())
    spoken_reply: str | None = Field(default=None, sa_column=_text(nullable=True))
    created_at: str | None = Field(default=None, sa_column=_now())


class MusicTaste(SQLModel, table=True):
    """One play: how much of it, and whether it was skipped."""

    __tablename__ = "music_taste"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    track_id: str = Field(sa_column=_text())
    track_name: str = Field(sa_column=_text())
    artists: str = Field(sa_column=_text())
    played_ms: int = Field(sa_column=_int())
    duration_ms: int = Field(sa_column=_int())
    skipped: int = Field(sa_column=_int())
    recorded_at: str | None = Field(default=None, sa_column=_now())


class EventReaction(SQLModel, table=True):
    """Her line about a desktop event (the cooldowns read these)."""

    __tablename__ = "event_reactions"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    session_id: str = Field(sa_column=_text())
    event: str = Field(sa_column=_text())
    dedupe_key: str | None = Field(default=None, sa_column=_text(nullable=True))
    line: str = Field(sa_column=_text())
    created_at: str | None = Field(default=None, sa_column=_now())


class Proactive(SQLModel, table=True):
    """Something she'll bring up on her own, waiting to be handed out."""

    __tablename__ = "proactive"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    kind: str = Field(sa_column=_text())
    text: str = Field(sa_column=_text())
    payload_json: str = Field(default="{}", sa_column=_text("{}"))
    created_at: str | None = Field(default=None, sa_column=_now())
    expires_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    delivered_at: str | None = Field(default=None, sa_column=_text(nullable=True))


class Event(SQLModel, table=True):
    """Something that happened on the desktop."""

    __tablename__ = "events"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    kind: str = Field(sa_column=_text())
    data_json: str = Field(default="{}", sa_column=_text("{}"))
    at: str = Field(sa_column=_text())
    created_at: str | None = Field(default=None, sa_column=_now())


class Diary(SQLModel, table=True):
    """Her diary entry for one date."""

    __tablename__ = "diary"

    date: str = Field(sa_column=_key(Text))
    entry: str = Field(sa_column=_text())
    callbacks_json: str = Field(default="[]", sa_column=_text("[]"))
    mood_word: str = Field(default="", sa_column=_text(""))
    edited: int = Field(default=0, sa_column=_int(0))
    created_at: str | None = Field(default=None, sa_column=_now())


class DiaryWeek(SQLModel, table=True):
    """One week of the diary, condensed."""

    __tablename__ = "diary_weeks"

    week: str = Field(sa_column=_key(Text))
    summary: str = Field(sa_column=_text())
    callbacks_json: str = Field(default="[]", sa_column=_text("[]"))
    created_at: str | None = Field(default=None, sa_column=_now())


class SelfMemory(SQLModel, table=True):
    """Something she takes away from a day about herself."""

    __tablename__ = "self_memory"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    kind: str = Field(sa_column=_text())
    text: str = Field(sa_column=_text())
    keywords_json: str = Field(default="[]", sa_column=_text("[]"))
    source_date: str | None = Field(default=None, sa_column=_text(nullable=True))
    uses: int = Field(default=0, sa_column=_int(0))
    last_used: str | None = Field(default=None, sa_column=_text(nullable=True))
    status: str = Field(default="active", sa_column=_text("active"))
    pinned: int = Field(default=0, sa_column=_int(0))
    edited: int = Field(default=0, sa_column=_int(0))
    created_at: str | None = Field(default=None, sa_column=_now())


class Latency(SQLModel, table=True):
    """One request stage's timing."""

    __tablename__ = "latency"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    request_id: str = Field(sa_column=_text())
    endpoint: str | None = Field(default=None, sa_column=_text(nullable=True))
    stage: str = Field(sa_column=_text())
    ms: float = Field(sa_column=Column(REAL, nullable=False))
    route: str | None = Field(default=None, sa_column=_text(nullable=True))
    skill: str | None = Field(default=None, sa_column=_text(nullable=True))
    model: str | None = Field(default=None, sa_column=_text(nullable=True))
    chars: int | None = Field(default=None, sa_column=_int(nullable=True))
    session_id: str | None = Field(default=None, sa_column=_text(nullable=True))
    created_at: str | None = Field(default=None, sa_column=_now())


class MessageMark(SQLModel, table=True):
    """Her circle, underline or strike on a line in their notebook."""

    __tablename__ = "message_marks"
    __table_args__ = (Index("message_marks_msg", "message_id"),)

    id: str = Field(sa_column=_key(Text))
    message_id: int = Field(sa_column=_int())
    type: str = Field(sa_column=_text())
    quote: str = Field(sa_column=_text())
    note: str = Field(default="", sa_column=_text(""))
    created_at: str | None = Field(default=None, sa_column=_now())
    seen_at: str | None = Field(default=None, sa_column=_text(nullable=True))


class DjPick(SQLModel, table=True):
    """A song she queued in DJ mode, and how it went."""

    __tablename__ = "dj_picks"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    uri: str = Field(sa_column=_text())
    title: str = Field(sa_column=_text())
    artist: str = Field(sa_column=_text())
    discovery: int = Field(sa_column=_int())
    picked_at: str | None = Field(default=None, sa_column=_now())
    outcome: str | None = Field(default=None, sa_column=_text(nullable=True))


class DjFeedback(SQLModel, table=True):
    """Mario's "more like this" / "not this" on a song."""

    __tablename__ = "dj_feedback"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    title: str = Field(sa_column=_text())
    artist: str = Field(sa_column=_text())
    liked: int = Field(sa_column=_int())
    at: str | None = Field(default=None, sa_column=_now())


class Email(SQLModel, table=True):
    """A mail she may mention."""

    __tablename__ = "emails"
    __table_args__ = (AUTOINCREMENT,)

    id: int | None = Field(default=None, sa_column=_key())
    key: str = Field(sa_column=Column(Text, nullable=False, unique=True))
    sender: str = Field(sa_column=_text())
    address: str = Field(sa_column=_text())
    subject: str = Field(sa_column=_text())
    body: str = Field(sa_column=_text())
    bulk: int = Field(default=0, sa_column=_int(0))
    state: str = Field(default="new", sa_column=_text("new"))
    received_at: str | None = Field(default=None, sa_column=_now())
    mentioned_at: str | None = Field(default=None, sa_column=_text(nullable=True))


class EmailNoise(SQLModel, table=True):
    """A sender pattern Mario doesn't want to hear about."""

    __tablename__ = "email_noise"

    pattern: str = Field(sa_column=_key(Text))
    added_at: str | None = Field(default=None, sa_column=_now())


class Incident(SQLModel, table=True):
    """One problem, from its first alert to handled."""

    __tablename__ = "incidents"
    __table_args__ = (Index("incidents_live", "state", "key"), AUTOINCREMENT)

    id: int | None = Field(default=None, sa_column=_key())
    type: str = Field(sa_column=_text())
    key: str = Field(sa_column=_text())
    title: str = Field(sa_column=_text())
    message: str = Field(default="", sa_column=_text(""))
    tier: str = Field(sa_column=_text())
    handler: str = Field(sa_column=_text())
    state: str = Field(sa_column=_text())
    count: int = Field(default=1, sa_column=_int(1))
    first_at: str = Field(sa_column=_text())
    last_at: str = Field(sa_column=_text())
    due_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    resolved_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    handled_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    line: str | None = Field(default=None, sa_column=_text(nullable=True))
    line_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    back_line: str | None = Field(default=None, sa_column=_text(nullable=True))
    handed_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    report: str | None = Field(default=None, sa_column=_text(nullable=True))
    session_id: str | None = Field(default=None, sa_column=_text(nullable=True))
    missed_at: str | None = Field(default=None, sa_column=_text(nullable=True))
    followup: str | None = Field(default=None, sa_column=_text(nullable=True))
    handled_by: str | None = Field(default=None, sa_column=_text(nullable=True))
    missed_why: str | None = Field(default=None, sa_column=_text(nullable=True))
    investigator: str | None = Field(default=None, sa_column=_text(nullable=True))


class RouterShadow(SQLModel, table=True):
    """What the semantic router would have picked, next to what she did (cm-6b0)."""

    __tablename__ = "router_shadow"

    id: int | None = Field(default=None, sa_column=_key())
    at: str = Field(sa_column=_text())
    message: str = Field(sa_column=_text())
    pick: str | None = Field(default=None, sa_column=_text(nullable=True))
    score: float = Field(sa_column=Column(REAL, nullable=False))
    sure: int = Field(sa_column=_int())
    via: str = Field(sa_column=_text())
    actual: str | None = Field(default=None, sa_column=_text(nullable=True))
    fast: int = Field(sa_column=_int())


class AsidePick(SQLModel, table=True):
    """A line of hers picked to wait in Talk as an aside."""

    __tablename__ = "aside_picks"

    message_id: int = Field(sa_column=_key())
    after_user_id: int = Field(sa_column=_int())
    picked_at: str | None = Field(default=None, sa_column=_now())


class AnsweredRemark(SQLModel, table=True):
    """Which remark of hers a message of Mario's answered."""

    __tablename__ = "answered_remarks"

    user_message_id: int = Field(sa_column=_key())
    remark_id: int = Field(sa_column=_int())


def _incident_alerts(metadata: MetaData) -> Table:
    # no primary key in the hand-written table (rowid only), which the ORM needs - it gets one when incidents moves
    return Table(
        "incident_alerts",
        metadata,
        Column("incident_id", Integer, nullable=False),
        Column("ntfy_event_id", Integer, nullable=True),
        Column("title", Text, nullable=False),
        Column("message", Text, nullable=False, server_default=text("''")),
        Column("at", Text, nullable=False),
    )


incident_alerts = _incident_alerts(SQLModel.metadata)
