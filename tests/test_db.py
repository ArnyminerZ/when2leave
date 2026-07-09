"""Tests for the SQLAlchemy models and session plumbing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from when2leave.db import (
    Event,
    EventStatus,
    GeocodeCache,
    NotifyState,
    create_engine,
    make_session_factory,
    session_scope,
)


def test_create_engine_creates_parent_directory_and_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "when2leave.db"
    create_engine(str(db_path))
    assert db_path.parent.exists()
    assert db_path.exists()


def test_event_roundtrip(tmp_path: Path) -> None:
    engine = create_engine(str(tmp_path / "test.db"))
    factory = make_session_factory(engine)
    now = datetime.now(tz=UTC)

    with session_scope(factory) as session:
        session.add(
            Event(
                uid="evt-1",
                recurrence_id="",
                calendar="Personal",
                title="Doctor",
                start=now,
                end=now,
                location_text="Clinic",
                transp="OPAQUE",
                ical_status="CONFIRMED",
                status=EventStatus.QUEUED,
                notify_state=NotifyState.NONE,
                created_at=now,
                updated_at=now,
            )
        )

    with session_scope(factory) as session:
        stored = session.query(Event).filter_by(uid="evt-1").one()
        assert stored.title == "Doctor"
        assert stored.status == EventStatus.QUEUED


def test_geocode_cache_roundtrip(tmp_path: Path) -> None:
    engine = create_engine(str(tmp_path / "test.db"))
    factory = make_session_factory(engine)
    now = datetime.now(tz=UTC)

    with session_scope(factory) as session:
        session.add(
            GeocodeCache(
                location_text="123 Main St",
                lat=40.0,
                lon=-3.0,
                display_name="123 Main St, City",
                created_at=now,
            )
        )

    with session_scope(factory) as session:
        cached = session.get(GeocodeCache, "123 Main St")
        assert cached is not None
        assert cached.lat == 40.0
