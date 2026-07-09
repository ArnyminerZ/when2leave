"""Tests for Dawarich timestamp parsing and location age calculation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from when2leave.location import CurrentLocation, _parse_timestamp


def test_parse_timestamp_from_unix_epoch_int() -> None:
    ts = _parse_timestamp(1_700_000_000)
    assert ts.tzinfo is not None
    assert ts == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_parse_timestamp_from_epoch_string() -> None:
    ts = _parse_timestamp("1700000000")
    assert ts == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_parse_timestamp_from_iso_string() -> None:
    ts = _parse_timestamp("2026-07-09T12:00:00Z")
    assert ts == datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


def test_current_location_age_seconds() -> None:
    recorded_at = datetime.now(tz=UTC) - timedelta(minutes=5)
    location = CurrentLocation(lat=40.0, lon=-3.0, timestamp=recorded_at)
    assert location.age_seconds == pytest.approx(300, abs=2)
