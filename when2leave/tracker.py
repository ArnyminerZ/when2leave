"""The tracking state machine: ties calendar sync, geocoding, location, routing and
notifications together and drives the per-event lifecycle.

Each tracked event instance moves through:

``QUEUED`` (qualifies, but further out than ``GEOCODE_WINDOW``) --> ``ACTIVE`` (within
the window: geocoded, travel time recomputed on ``RECOMPUTE_INTERVAL``) --> ``DONE``
(start time has passed). Events that stop qualifying (deleted, cancelled, location
removed, marked free) move to ``CANCELLED``/``DROPPED`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from when2leave.caldav_sync import (
    DEFAULT_LOOKAHEAD_DAYS,
    CalDAVSyncClient,
    ParsedEvent,
    event_qualifies,
)
from when2leave.config import Settings
from when2leave.davpush import DavPushClient, SubscriptionKeys, new_callback_token
from when2leave.db import (
    DavPushSubscription,
    Event,
    EventStatus,
    GeocodeCache,
    LocationUpdate,
    NotifyState,
)
from when2leave.geocoding import (
    GeocodeCacheProtocol,
    GeocodeResult,
    NominatimClient,
    geocode_location,
    haversine_distance_m,
)
from when2leave.location import CurrentLocation, DawarichClient
from when2leave.logging_config import get_logger
from when2leave.notifier import NtfyClient, compute_leave_at, evaluate_notification
from when2leave.routing import RoutingProvider

logger = get_logger(__name__)


class DbGeocodeCache(GeocodeCacheProtocol):
    """``GeocodeCacheProtocol`` implementation backed by the ``geocode_cache`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, location_text: str) -> GeocodeResult | None:
        with self._session_factory() as session:
            row = session.get(GeocodeCache, location_text)
            if row is None:
                return None
            return GeocodeResult(lat=row.lat, lon=row.lon, display_name=row.display_name)

    def put(self, location_text: str, result: GeocodeResult) -> None:
        with self._session_factory() as session:
            row = GeocodeCache(
                location_text=location_text,
                lat=result.lat,
                lon=result.lon,
                display_name=result.display_name,
                created_at=datetime.now(tz=UTC),
            )
            session.merge(row)
            session.commit()


@dataclass
class ServiceStatus:
    """In-memory snapshot of service health, surfaced on the dashboard header."""

    last_sync_at: datetime | None = None
    last_sync_error: str | None = None
    dawarich_reachable: bool | None = None
    davpush_subscription_count: int = 0
    davpush_last_error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class Tracker:
    """Owns the sync/recompute/notify pipeline and its scheduled jobs."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        caldav_client: CalDAVSyncClient,
        nominatim_client: NominatimClient,
        dawarich_client: DawarichClient,
        routing_provider: RoutingProvider,
        ntfy_client: NtfyClient,
        davpush_client: DavPushClient | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._caldav = caldav_client
        self._nominatim = nominatim_client
        self._dawarich = dawarich_client
        self._routing = routing_provider
        self._ntfy = ntfy_client
        self._davpush = davpush_client
        self._geocode_cache = DbGeocodeCache(session_factory)
        self.status = ServiceStatus()

    # -- Calendar sync ------------------------------------------------------------

    async def full_sync(self) -> None:
        """Sync every configured calendar. Runs on the poll interval as a safety net."""
        now = datetime.now(tz=UTC)
        try:
            calendars = self._caldav.discover_calendars(self._settings.calendar_filter)
            for cal in calendars:
                calendar_name = cal.name or str(cal.url)
                events = self._caldav.fetch_calendar_events(cal, now, now + _lookahead())
                self._apply_calendar_sync(calendar_name, events, now)
            self._reconcile_states(now)
            self.status.last_sync_at = now
            self.status.last_sync_error = None
            logger.info("tracker.full_sync_complete", extra={"calendar_count": len(calendars)})
        except Exception as exc:
            self.status.last_sync_error = str(exc)
            logger.error("tracker.full_sync_failed", exc_info=True)
            raise

    async def resync_calendar(self, calendar_url: str, calendar_name: str) -> None:
        """Re-sync a single calendar, triggered by a DAV Push notification."""
        now = datetime.now(tz=UTC)
        events = self._caldav.sync_calendar_by_url(
            calendar_url, now, lookahead_days=(_lookahead().days or 1)
        )
        self._apply_calendar_sync(calendar_name, events, now)
        self._reconcile_states(now)
        self.status.last_sync_at = now
        logger.info("tracker.push_resync_complete", extra={"calendar": calendar_name})

    def _apply_calendar_sync(
        self, calendar_name: str, events: list[ParsedEvent], now: datetime
    ) -> None:
        """Upsert freshly-fetched events for one calendar and drop ones no longer present."""
        seen_keys: set[tuple[str, str]] = set()
        with self._session_factory() as session:
            for parsed in events:
                seen_keys.add((parsed.uid, parsed.recurrence_id))
                self._upsert_event(session, parsed, now)

            stmt = select(Event).where(
                Event.calendar == calendar_name,
                Event.status.in_([EventStatus.QUEUED, EventStatus.ACTIVE]),
            )
            for existing in session.scalars(stmt):
                if (existing.uid, existing.recurrence_id) not in seen_keys:
                    existing.status = EventStatus.DROPPED
                    existing.updated_at = now
            session.commit()

    @staticmethod
    def _upsert_event(session: Session, parsed: ParsedEvent, now: datetime) -> None:
        qualifies = event_qualifies(
            parsed.location, parsed.transp, parsed.ical_status, parsed.start, now
        )
        stmt = select(Event).where(
            Event.uid == parsed.uid, Event.recurrence_id == parsed.recurrence_id
        )
        existing = session.scalars(stmt).one_or_none()

        if existing is None:
            if not qualifies:
                return
            session.add(
                Event(
                    uid=parsed.uid,
                    recurrence_id=parsed.recurrence_id,
                    calendar=parsed.calendar,
                    title=parsed.title,
                    start=parsed.start,
                    end=parsed.end,
                    location_text=parsed.location,
                    transp=parsed.transp,
                    ical_status=parsed.ical_status,
                    status=EventStatus.QUEUED,
                    notify_state=NotifyState.NONE,
                    created_at=now,
                    updated_at=now,
                )
            )
            return

        existing.title = parsed.title
        existing.start = parsed.start
        existing.end = parsed.end
        existing.location_text = parsed.location
        existing.transp = parsed.transp
        existing.ical_status = parsed.ical_status
        existing.updated_at = now

        if not qualifies:
            if existing.status not in (EventStatus.DONE,):
                existing.status = (
                    EventStatus.CANCELLED
                    if parsed.ical_status.upper() == "CANCELLED"
                    else EventStatus.DROPPED
                )
        elif existing.status in (EventStatus.CANCELLED, EventStatus.DROPPED):
            existing.status = EventStatus.QUEUED
            existing.notify_state = NotifyState.NONE
            existing.resolved_lat = None
            existing.resolved_lon = None
            existing.resolved_address = None

    def _reconcile_states(self, now: datetime) -> None:
        """Promote QUEUED->ACTIVE within the geocode window and expire past events to DONE."""
        window_s = self._settings.geocode_window
        with self._session_factory() as session:
            stmt = select(Event).where(Event.status.in_([EventStatus.QUEUED, EventStatus.ACTIVE]))
            for event in session.scalars(stmt):
                if event.start <= now:
                    event.status = EventStatus.DONE
                    event.updated_at = now
                elif (
                    event.status == EventStatus.QUEUED
                    and (event.start - now).total_seconds() <= window_s
                ):
                    event.status = EventStatus.ACTIVE
                    event.updated_at = now
            session.commit()

    # -- Recompute loop -------------------------------------------------------------

    async def recompute_tick(self) -> None:
        """Refresh location/travel-time/notifications for every ACTIVE event."""
        now = datetime.now(tz=UTC)
        try:
            current_location = await self._dawarich.get_current_location()
            self.status.dawarich_reachable = True
        except Exception:
            self.status.dawarich_reachable = False
            logger.warning("tracker.dawarich_unreachable", exc_info=True)
            return

        with self._session_factory() as session:
            stmt = select(Event).where(Event.status == EventStatus.ACTIVE)
            for event in session.scalars(stmt):
                try:
                    await self._recompute_event(session, event, current_location, now)
                except Exception:
                    logger.error(
                        "tracker.recompute_event_failed",
                        extra={"event_id": event.id, "title": event.title},
                        exc_info=True,
                    )
            session.commit()

    async def _recompute_event(
        self, session: Session, event: Event, current_location: CurrentLocation, now: datetime
    ) -> None:
        if event.resolved_lat is None or event.resolved_lon is None:
            result = await geocode_location(
                self._nominatim,
                self._geocode_cache,
                event.location_text,
                current_location.lat,
                current_location.lon,
                candidates=self._settings.geocode_candidates,
            )
            event.resolved_lat = result.lat
            event.resolved_lon = result.lon
            event.resolved_address = result.display_name

        assert event.resolved_lat is not None
        assert event.resolved_lon is not None

        travel = await self._routing.estimate(
            current_location.lat, current_location.lon, event.resolved_lat, event.resolved_lon
        )
        distance_m = haversine_distance_m(
            current_location.lat, current_location.lon, event.resolved_lat, event.resolved_lon
        )
        leave_at = compute_leave_at(event.start, travel.duration_s, self._settings.prep_buffer)

        session.add(
            LocationUpdate(
                event_id=event.id,
                ts=now,
                current_lat=current_location.lat,
                current_lon=current_location.lon,
                distance_m=distance_m,
                travel_time_s=travel.duration_s,
                leave_at=leave_at,
                routing_provider=travel.provider,
            )
        )
        event.leave_at = leave_at

        new_state, notification = evaluate_notification(
            now=now,
            event_title=event.title,
            leave_at=leave_at,
            travel_time_s=travel.duration_s,
            notify_lead_s=self._settings.notify_lead,
            notify_state=event.notify_state,
            last_notified_leave_at=event.last_notified_leave_at,
            reshift_threshold_s=self._settings.notify_reshift_threshold,
            default_priority=self._settings.ntfy_priority,
        )
        event.notify_state = new_state
        if notification is not None:
            await self._ntfy.publish(
                notification, click_lat=event.resolved_lat, click_lon=event.resolved_lon
            )
            event.last_notified_leave_at = leave_at
        event.updated_at = now

    # -- DAV Push -----------------------------------------------------------------

    async def register_davpush_for_calendars(self) -> None:
        """Discover and (re)register DAV Push subscriptions for every configured calendar."""
        if self._davpush is None or not self._settings.davpush_callback_url:
            return
        calendars = self._caldav.discover_calendars(self._settings.calendar_filter)
        count = 0
        for cal in calendars:
            calendar_url = str(cal.url)
            calendar_name = cal.name or calendar_url
            try:
                caps = await self._davpush.discover(calendar_url)
                if not caps.supported:
                    logger.info("davpush.not_supported", extra={"calendar_url": calendar_url})
                    continue
                await self._register_one(calendar_url, calendar_name)
                count += 1
            except Exception:
                self.status.davpush_last_error = f"{calendar_url}: registration failed"
                logger.warning(
                    "davpush.registration_failed",
                    extra={"calendar_url": calendar_url},
                    exc_info=True,
                )
        self.status.davpush_subscription_count = count

    async def _register_one(self, calendar_url: str, calendar_name: str | None = None) -> None:
        assert self._davpush is not None
        base_callback = self._settings.davpush_callback_url
        assert base_callback is not None

        with self._session_factory() as session:
            existing = session.get(DavPushSubscription, calendar_url)
            token = existing.callback_token if existing else new_callback_token()
            if calendar_name is None:
                calendar_name = existing.calendar_name if existing else calendar_url

        keys = SubscriptionKeys.generate()
        push_resource_url = f"{base_callback.rstrip('/')}/{token}"
        result = await self._davpush.register(calendar_url, push_resource_url, keys)

        now = datetime.now(tz=UTC)
        with self._session_factory() as session:
            row = DavPushSubscription(
                calendar_url=calendar_url,
                calendar_name=calendar_name,
                callback_token=token,
                registration_url=result.registration_url,
                public_key=keys.public_key_b64,
                auth_secret=keys.auth_secret_b64,
                expires_at=result.expires_at,
                created_at=now,
                updated_at=now,
            )
            session.merge(row)
            session.commit()

    async def renew_expiring_davpush_subscriptions(self, margin_seconds: int = 3600) -> None:
        """Renew any DAV Push subscription expiring within ``margin_seconds``."""
        if self._davpush is None:
            return
        now = datetime.now(tz=UTC)
        with self._session_factory() as session:
            stmt = select(DavPushSubscription)
            expiring = [
                row
                for row in session.scalars(stmt)
                if (row.expires_at - now).total_seconds() <= margin_seconds
            ]
        for row in expiring:
            try:
                await self._register_one(row.calendar_url)
            except Exception:
                logger.warning(
                    "davpush.renewal_failed",
                    extra={"calendar_url": row.calendar_url},
                    exc_info=True,
                )

    def find_subscription_by_token(self, token: str) -> tuple[str, str] | None:
        """Look up ``(calendar_url, calendar_name)`` for a DAV Push callback token."""
        with self._session_factory() as session:
            stmt = select(DavPushSubscription).where(DavPushSubscription.callback_token == token)
            row = session.scalars(stmt).one_or_none()
            return (row.calendar_url, row.calendar_name) if row else None


def _lookahead() -> timedelta:
    """Sync lookahead window: how far into the future we ask CalDAV for events."""
    return timedelta(days=DEFAULT_LOOKAHEAD_DAYS)
