"""Tests for event filtering and recurrence expansion."""

from __future__ import annotations

from datetime import UTC, datetime

from when2leave.caldav_sync import event_qualifies, expand_ics_events

NOW = datetime(2026, 7, 9, 8, 0, tzinfo=UTC)


def test_qualifies_when_all_conditions_met() -> None:
    assert event_qualifies("Office, Main St", "OPAQUE", "CONFIRMED", NOW.replace(hour=10), NOW)


def test_disqualifies_without_location() -> None:
    assert not event_qualifies("", "OPAQUE", "CONFIRMED", NOW.replace(hour=10), NOW)
    assert not event_qualifies("   ", "OPAQUE", "CONFIRMED", NOW.replace(hour=10), NOW)


def test_disqualifies_when_not_busy() -> None:
    assert not event_qualifies("Office", "TRANSPARENT", "CONFIRMED", NOW.replace(hour=10), NOW)


def test_disqualifies_when_cancelled() -> None:
    assert not event_qualifies("Office", "OPAQUE", "CANCELLED", NOW.replace(hour=10), NOW)
    assert not event_qualifies("Office", "OPAQUE", "cancelled", NOW.replace(hour=10), NOW)


def test_disqualifies_when_in_the_past() -> None:
    assert not event_qualifies("Office", "OPAQUE", "CONFIRMED", NOW.replace(hour=6), NOW)


def test_disqualifies_when_starting_exactly_now() -> None:
    assert not event_qualifies("Office", "OPAQUE", "CONFIRMED", NOW, NOW)


_RECURRING_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//when2leave tests//EN
BEGIN:VEVENT
UID:standup-123
DTSTAMP:20260601T080000Z
DTSTART:20260701T090000Z
DTEND:20260701T093000Z
SUMMARY:Daily standup
LOCATION:HQ, Meeting Room 1
TRANSP:OPAQUE
STATUS:CONFIRMED
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
END:VCALENDAR
"""

_SINGLE_EVENT_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//when2leave tests//EN
BEGIN:VEVENT
UID:dentist-456
DTSTAMP:20260601T080000Z
DTSTART:20260709T160000Z
DTEND:20260709T170000Z
SUMMARY:Dentist appointment
LOCATION:123 Health St
TRANSP:OPAQUE
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""


def test_expand_recurring_event_produces_all_instances_in_window() -> None:
    window_start = datetime(2026, 7, 1, tzinfo=UTC)
    window_end = datetime(2026, 7, 10, tzinfo=UTC)
    instances = expand_ics_events("Work", _RECURRING_ICS, window_start, window_end)

    assert len(instances) == 5
    assert all(i.uid == "standup-123" for i in instances)
    assert all(i.location == "HQ, Meeting Room 1" for i in instances)
    starts = sorted(i.start for i in instances)
    assert starts[0] == datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    assert starts[-1] == datetime(2026, 7, 5, 9, 0, tzinfo=UTC)


def test_expand_recurring_event_respects_window_bounds() -> None:
    window_start = datetime(2026, 7, 3, tzinfo=UTC)
    window_end = datetime(2026, 7, 4, tzinfo=UTC)
    instances = expand_ics_events("Work", _RECURRING_ICS, window_start, window_end)
    assert len(instances) == 1
    assert instances[0].start == datetime(2026, 7, 3, 9, 0, tzinfo=UTC)


def test_expand_single_non_recurring_event() -> None:
    window_start = datetime(2026, 7, 1, tzinfo=UTC)
    window_end = datetime(2026, 7, 31, tzinfo=UTC)
    instances = expand_ics_events("Personal", _SINGLE_EVENT_ICS, window_start, window_end)

    assert len(instances) == 1
    event = instances[0]
    assert event.uid == "dentist-456"
    assert event.title == "Dentist appointment"
    assert event.location == "123 Health St"
    assert event.transp == "OPAQUE"
    assert event.ical_status == "CONFIRMED"
    assert event.start == datetime(2026, 7, 9, 16, 0, tzinfo=UTC)
