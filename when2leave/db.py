"""SQLAlchemy 2.x ORM models and session/engine setup.

The whole application persists to a single SQLite file (``DATABASE_PATH``). Tables:

* ``events`` -- one row per tracked calendar event *instance* (recurring events are
  expanded, so each occurrence gets its own row keyed by ``(uid, recurrence_id)``).
* ``location_updates`` -- history of recompute cycles for an event, used to drive the
  "expand for history" view on the dashboard.
* ``geocode_cache`` -- LOCATION string -> resolved coordinates, so we never geocode the
  same address twice.
* ``kv`` -- small generic key/value store (DAV Push subscription state, sync tokens...).
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class EventStatus(enum.StrEnum):
    """Lifecycle state of a tracked event instance."""

    QUEUED = "queued"
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"
    DROPPED = "dropped"


class NotifyState(enum.StrEnum):
    """How far along the notification cycle an event is."""

    NONE = "none"
    HEADS_UP_SENT = "heads_up_sent"
    LEAVE_NOW_SENT = "leave_now_sent"
    RUNNING_LATE_SENT = "running_late_sent"


class Event(Base):
    """A single tracked calendar event instance."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("uid", "recurrence_id", name="uq_event_uid_recurrence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String, index=True)
    recurrence_id: Mapped[str] = mapped_column(String, default="")
    calendar: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    location_text: Mapped[str] = mapped_column(String)
    transp: Mapped[str] = mapped_column(String, default="OPAQUE")
    ical_status: Mapped[str] = mapped_column(String, default="CONFIRMED")

    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, native_enum=False), default=EventStatus.QUEUED, index=True
    )

    resolved_lat: Mapped[float | None] = mapped_column(Float, default=None)
    resolved_lon: Mapped[float | None] = mapped_column(Float, default=None)
    resolved_address: Mapped[str | None] = mapped_column(String, default=None)

    leave_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    notify_state: Mapped[NotifyState] = mapped_column(
        Enum(NotifyState, native_enum=False), default=NotifyState.NONE
    )
    last_notified_leave_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    location_updates: Mapped[list[LocationUpdate]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="LocationUpdate.ts"
    )


class LocationUpdate(Base):
    """A single recompute snapshot for a tracked event (history row)."""

    __tablename__ = "location_updates"
    __table_args__ = (Index("ix_location_updates_event_ts", "event_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_lat: Mapped[float] = mapped_column(Float)
    current_lon: Mapped[float] = mapped_column(Float)
    distance_m: Mapped[float] = mapped_column(Float)
    travel_time_s: Mapped[float] = mapped_column(Float)
    leave_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    routing_provider: Mapped[str] = mapped_column(String)

    event: Mapped[Event] = relationship(back_populates="location_updates")


class GeocodeCache(Base):
    """Cached geocoding result for a LOCATION string (addresses don't move)."""

    __tablename__ = "geocode_cache"

    location_text: Mapped[str] = mapped_column(String, primary_key=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    display_name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KV(Base):
    """Generic key/value store for small bits of service state."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class DavPushSubscription(Base):
    """A registered WebDAV Push subscription for one calendar collection.

    ``callback_token`` is the path segment of our webhook URL for this calendar
    (``{DAVPUSH_CALLBACK_URL}/{callback_token}``), letting the receiver identify which
    collection changed without needing to decrypt the (Web Push encrypted) push body --
    see ``when2leave.davpush`` for why we treat the payload as signal-only.
    """

    __tablename__ = "davpush_subscriptions"

    calendar_url: Mapped[str] = mapped_column(String, primary_key=True)
    calendar_name: Mapped[str] = mapped_column(String)
    callback_token: Mapped[str] = mapped_column(String, unique=True)
    registration_url: Mapped[str] = mapped_column(String)
    public_key: Mapped[str] = mapped_column(String)
    auth_secret: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def create_engine(database_path: str) -> Engine:
    """Create the SQLAlchemy engine for the given SQLite file path, ensuring its parent exists."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = _create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to the given engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional session scope."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@asynccontextmanager
async def async_session_scope(factory: sessionmaker[Session]) -> AsyncIterator[Session]:
    """Async-friendly wrapper around ``session_scope`` (SQLite ops are fast/sync)."""
    with session_scope(factory) as session:
        yield session
