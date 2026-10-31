"""
BinBot data model.

Layers, in order of trust:
  1. Council data (Snapshot -> CollectionEvent): the source of truth for dates.
  2. User overrides (UserBinSetting): enable/disable a bin, or a manual rule when the
     council is unsupported. Never written back into layer 1.
  3. Reminders: derived from 1 + 2 and the user's preferences; rebuilt whenever either changes.
"""
import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return uuid.uuid4().hex


class BinCategory(str, enum.Enum):
    recycling = "recycling"
    general = "general"
    food = "food"
    garden = "garden"
    glass = "glass"
    paper = "paper"
    other = "other"


class CouncilStatus(str, enum.Enum):
    ok = "ok"
    degraded = "degraded"  # some lookups fail
    broken = "broken"  # health check failing
    unsupported = "unsupported"  # no adapter; manual entry only


class Frequency(str, enum.Enum):
    weekly = "weekly"
    fortnightly = "fortnightly"
    four_weekly = "four_weekly"
    irregular = "irregular"


class ReminderMode(str, enum.Enum):
    night_before = "night_before"
    morning_of = "morning_of"


class ReminderStatus(str, enum.Enum):
    scheduled = "scheduled"
    sent = "sent"
    done = "done"
    snoozed = "snoozed"
    failed = "failed"
    cancelled = "cancelled"


class UserStatus(str, enum.Enum):
    pending = "pending"  # signed up, reminders not yet turned on
    active = "active"
    stopped = "stopped"  # replied STOP


# ---------------------------------------------------------------- councils


class Council(Base):
    __tablename__ = "councils"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    gss_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)  # LAD24CD
    name: Mapped[str] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2), default="E")  # E/S/W/N from GSS prefix
    adapter: Mapped[str | None] = mapped_column(String(80))  # UKBinCollectionData module name
    adapter_params: Mapped[dict] = mapped_column(JSON, default=dict)  # url, skip_get_url, needs...
    needs_uprn: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_postcode: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_house_number: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_browser: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[CouncilStatus] = mapped_column(Enum(CouncilStatus), default=CouncilStatus.ok)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)

    bin_types: Mapped[list["BinType"]] = relationship(back_populates="council")


class BinType(Base):
    """A bin as the council names it, mapped to our canonical category."""

    __tablename__ = "bin_types"
    __table_args__ = (UniqueConstraint("council_id", "council_label"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    council_id: Mapped[str] = mapped_column(ForeignKey("councils.id"), index=True)
    council_label: Mapped[str] = mapped_column(String(160))  # "Blue lid recycling bin"
    category: Mapped[BinCategory] = mapped_column(Enum(BinCategory), default=BinCategory.other)
    colour: Mapped[str | None] = mapped_column(String(24))

    council: Mapped[Council] = relationship(back_populates="bin_types")


# ---------------------------------------------------------------- addresses


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    uprn: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    postcode: Mapped[str] = mapped_column(String(10), index=True)  # normalised "E8 3PB"
    house_number: Mapped[str | None] = mapped_column(String(40))  # PAON: "12", "Flat 3, 12"
    address_line: Mapped[str | None] = mapped_column(String(240))
    council_id: Mapped[str | None] = mapped_column(ForeignKey("councils.id"), index=True)
    lat: Mapped[float | None] = mapped_column(nullable=True)
    lng: Mapped[float | None] = mapped_column(nullable=True)
    # Cache key: properties on the same round share a calendar. Defaults to the property id;
    # an adapter can collapse it (e.g. to a council "round" id) to cut scraping.
    round_key: Mapped[str] = mapped_column(String(64), index=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    council: Mapped[Council | None] = relationship()


# ---------------------------------------------------------------- calendar (source of truth)


class Snapshot(Base):
    """One fetch from a council for a round. Raw payload kept for debugging and diffs."""

    __tablename__ = "snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    round_key: Mapped[str] = mapped_column(String(64), index=True)
    council_id: Mapped[str] = mapped_column(ForeignKey("councils.id"), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    source: Mapped[str] = mapped_column(String(20), default="council")  # council | manual | demo
    adapter_version: Mapped[str | None] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw: Mapped[dict] = mapped_column(JSON)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)


class CollectionEvent(Base):
    """A concrete collection date for one bin type on one round."""

    __tablename__ = "collection_events"
    __table_args__ = (
        UniqueConstraint("round_key", "bin_type_id", "date"),
        Index("ix_events_round_date", "round_key", "date"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    round_key: Mapped[str] = mapped_column(String(64))
    bin_type_id: Mapped[str] = mapped_column(ForeignKey("bin_types.id"))
    date: Mapped[date] = mapped_column(Date)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("snapshots.id"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Set when a later snapshot moved this date (bank holiday etc.).
    moved_from: Mapped[date | None] = mapped_column(Date, nullable=True)

    bin_type: Mapped[BinType] = relationship()


# ---------------------------------------------------------------- users


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    phone_e164: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    property_id: Mapped[str | None] = mapped_column(ForeignKey("properties.id"), index=True)
    reminder_mode: Mapped[ReminderMode] = mapped_column(
        Enum(ReminderMode), default=ReminderMode.night_before
    )
    reminder_time: Mapped[str] = mapped_column(String(5), default="19:00")  # HH:MM local
    timezone: Mapped[str] = mapped_column(String(40), default="Europe/London")
    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus), default=UserStatus.pending)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Separate, explicit opt-in to hear about other home services (GDPR purpose limitation:
    # the address was given for bin reminders, nothing else, unless this is set).
    marketing_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referral_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    referred_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    property: Mapped[Property | None] = relationship()
    bin_settings: Mapped[list["UserBinSetting"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserBinSetting(Base):
    """
    Per-user layer over a bin. Either references a council BinType (enabled/disabled),
    or is a manual bin the user defined (bin_type_id NULL) with its own rule.
    When `override_rule` is set it takes precedence over council dates for this bin.
    """

    __tablename__ = "user_bin_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    bin_type_id: Mapped[str | None] = mapped_column(ForeignKey("bin_types.id"))
    label: Mapped[str] = mapped_column(String(80))  # "Recycling"
    category: Mapped[BinCategory] = mapped_column(Enum(BinCategory))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Manual rule: {"days": ["TUE","FRI"], "frequency": "weekly", "anchor": "2026-11-03"}
    override_rule: Mapped[dict | None] = mapped_column(JSON)

    user: Mapped[User] = relationship(back_populates="bin_settings")
    bin_type: Mapped[BinType | None] = relationship()


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_due", "status", "send_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    collection_date: Mapped[date] = mapped_column(Date)
    bins: Mapped[list] = mapped_column(JSON)  # [{"label": "Recycling", "category": "recycling"}]
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus), default=ReminderStatus.scheduled
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(80))
    reply: Mapped[str | None] = mapped_column(String(40))  # done | snooze | stop


class Outbox(Base):
    """Every outbound message, whatever the provider. The stub provider only writes here."""

    __tablename__ = "outbox"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    to_phone: Mapped[str] = mapped_column(String(20), index=True)
    body: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))  # reminder | change | welcome
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    provider: Mapped[str] = mapped_column(String(20))
    provider_message_id: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- monitoring


class ChangeLog(Base):
    __tablename__ = "change_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    scope: Mapped[str] = mapped_column(String(10))  # council | round
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    kind: Mapped[str] = mapped_column(String(20))  # dates_changed | adapter_broken | adapter_recovered
    diff: Mapped[dict] = mapped_column(JSON)  # {"added": [...], "removed": [...], "moved": [...]}
    users_notified: Mapped[int] = mapped_column(Integer, default=0)


class Referral(Base):
    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("referrer_id", "referred_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    referrer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    referred_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


LEVELS = [
    (0, "Bin rookie"),
    (1, "Kerbside regular"),
    (3, "Street captain"),
    (5, "Binfluencer"),
]


def level_for(referrals: int) -> tuple[int, str, int | None]:
    """Returns (level_index, name, referrals_needed_for_next or None)."""
    idx = 0
    for i, (threshold, _) in enumerate(LEVELS):
        if referrals >= threshold:
            idx = i
    nxt = LEVELS[idx + 1][0] if idx + 1 < len(LEVELS) else None
    return idx + 1, LEVELS[idx][1], nxt
