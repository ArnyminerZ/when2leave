"""Nextcloud CalDAV sync: calendar discovery, event fetching and recurrence expansion.

Each CalDAV calendar object resource contains a single event's full recurrence set (the
master ``VEVENT`` plus any ``RECURRENCE-ID`` overrides), per RFC 4791. We hand that raw
ICS text to ``recurring_ical_events`` to expand it into concrete upcoming instances
within a lookahead window, then filter down to the ones worth tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import caldav
import recurring_ical_events
from icalendar import Calendar
from tenacity import retry, stop_after_attempt, wait_exponential

from when2leave.logging_config import get_logger

logger = get_logger(__name__)

#: ``caldav``'s type stubs are incomplete/inconsistent (e.g. ``Principal``/``Calendar``
#: aren't usable as static types), so we deliberately treat its objects as ``Any`` here.
CalDAVPrincipal = Any
CalDAVCalendar = Any

#: How far into the future we ask recurring_ical_events to expand occurrences. This is
#: independent of GEOCODE_WINDOW: events are recorded (as QUEUED) as soon as they're
#: known so the dashboard can show what's coming, but external lookups are deferred.
DEFAULT_LOOKAHEAD_DAYS = 30


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """A single, concrete (already-recurrence-expanded) calendar event instance."""

    uid: str
    recurrence_id: str
    calendar: str
    title: str
    start: datetime
    end: datetime
    location: str
    transp: str
    ical_status: str


def event_qualifies(
    location: str, transp: str, ical_status: str, start: datetime, now: datetime
) -> bool:
    """Return True if an event instance should be tracked.

    Requires a non-empty LOCATION, TRANSP:OPAQUE (busy), STATUS != CANCELLED, and a
    start time in the future relative to ``now``.
    """
    if not location or not location.strip():
        return False
    if transp.upper() != "OPAQUE":
        return False
    if ical_status.upper() == "CANCELLED":
        return False
    return start > now


def _as_utc(value: datetime | date) -> datetime:
    """Normalize an icalendar DTSTART/DTEND value (date or datetime) to aware UTC."""
    from datetime import UTC

    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Floating time: icalendar/recurring_ical_events already resolves most
            # cases against the calendar's VTIMEZONE; a bare naive datetime here means
            # "floating", which we treat as UTC as a reasonable, documented default.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    # All-day (date-only) event: treat as starting/ending at midnight UTC.
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def expand_ics_events(
    calendar_name: str, ics_text: str, window_start: datetime, window_end: datetime
) -> list[ParsedEvent]:
    """Expand a single CalDAV object resource's ICS text into concrete event instances.

    ``ics_text`` may contain a recurring master VEVENT plus its RECURRENCE-ID overrides;
    ``recurring_ical_events`` handles RRULE/RDATE/EXDATE expansion and override merging.
    """
    parsed_calendar = Calendar.from_ical(ics_text)
    occurrences = recurring_ical_events.of(parsed_calendar).between(window_start, window_end)

    parsed: list[ParsedEvent] = []
    for occ in occurrences:
        uid = str(occ.get("UID", ""))
        recurrence_id_prop = occ.get("RECURRENCE-ID")
        recurrence_id = recurrence_id_prop.dt.isoformat() if recurrence_id_prop else ""
        title = str(occ.get("SUMMARY", "(no title)"))
        location = str(occ.get("LOCATION", "") or "")
        transp = str(occ.get("TRANSP", "OPAQUE") or "OPAQUE")
        status = str(occ.get("STATUS", "CONFIRMED") or "CONFIRMED")
        start = _as_utc(occ["DTSTART"].dt)
        end = _as_utc(occ["DTEND"].dt) if occ.get("DTEND") else start

        parsed.append(
            ParsedEvent(
                uid=uid,
                recurrence_id=recurrence_id,
                calendar=calendar_name,
                title=title,
                start=start,
                end=end,
                location=location,
                transp=transp,
                ical_status=status,
            )
        )
    return parsed


class CalDAVSyncClient:
    """Wraps the ``caldav`` library to discover calendars and fetch/expand their events."""

    def __init__(self, url: str, username: str, password: str) -> None:
        # caldav's py.typed stubs mistype DAVClient/Calendar as non-callable; see the
        # CalDAVPrincipal/CalDAVCalendar comment above.
        self._client = caldav.DAVClient(url=url, username=username, password=password)  # type: ignore[operator]
        self._principal: CalDAVPrincipal | None = None

    def _get_principal(self) -> CalDAVPrincipal:
        if self._principal is None:
            self._principal = self._client.principal()
        return self._principal

    @retry(
        reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10)
    )
    def discover_calendars(self, name_filter: list[str]) -> list[CalDAVCalendar]:
        """Return the calendars to sync: all of them, or only those matching ``name_filter``.

        ``name_filter`` entries are matched case-insensitively against the calendar's
        display name or its URL.
        """
        principal = self._get_principal()
        calendars = principal.calendars()
        if not name_filter:
            return list(calendars)

        wanted = {n.lower() for n in name_filter}
        selected = [
            cal
            for cal in calendars
            if (cal.name or "").lower() in wanted or str(cal.url).lower() in wanted
        ]
        logger.info(
            "caldav.calendars_selected",
            extra={"requested": name_filter, "matched": [c.name for c in selected]},
        )
        return selected

    @retry(
        reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10)
    )
    def fetch_calendar_events(
        self, cal: CalDAVCalendar, window_start: datetime, window_end: datetime
    ) -> list[ParsedEvent]:
        """Fetch and expand all event instances in a calendar within the given window."""
        calendar_name = cal.name or str(cal.url)
        results: list[ParsedEvent] = []
        for obj in cal.date_search(start=window_start, end=window_end, expand=False):
            ics_text = obj.data
            try:
                results.extend(expand_ics_events(calendar_name, ics_text, window_start, window_end))
            except Exception:
                logger.warning(
                    "caldav.parse_failed",
                    extra={"calendar": calendar_name, "url": str(getattr(obj, "url", ""))},
                    exc_info=True,
                )
        return results

    def sync_all(
        self,
        name_filter: list[str],
        now: datetime,
        lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    ) -> list[ParsedEvent]:
        """Discover calendars and fetch all upcoming event instances across them."""
        window_start = now
        window_end = now + timedelta(days=lookahead_days)
        events: list[ParsedEvent] = []
        for cal in self.discover_calendars(name_filter):
            events.extend(self.fetch_calendar_events(cal, window_start, window_end))
        logger.info("caldav.sync_complete", extra={"event_count": len(events)})
        return events

    def sync_calendar_by_url(
        self, calendar_url: str, now: datetime, lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS
    ) -> list[ParsedEvent]:
        """Re-sync a single calendar identified by its URL (used by the DAV Push handler)."""
        cal = caldav.Calendar(client=self._client, url=calendar_url)  # type: ignore[operator]
        return self.fetch_calendar_events(cal, now, now + timedelta(days=lookahead_days))


def parse_single_vevent_ics(ics_text: str) -> Calendar:
    """Parse raw ICS text into an ``icalendar.Calendar`` (helper for tests/debugging)."""
    return Calendar.from_ical(ics_text)
