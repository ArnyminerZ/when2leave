"""FastAPI application: the dashboard, its JSON API, and the DAV Push webhook receiver.

Everything runs in a single process/port, as specified: the dashboard UI (Jinja2 +
htmx polling, no JS build step), a small JSON API behind it, ``/health``, and the DAV
Push callback endpoint that Nextcloud POSTs to when a subscribed calendar changes.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from when2leave.config import Settings
from when2leave.db import Event, EventStatus, LocationUpdate
from when2leave.logging_config import get_logger
from when2leave.tracker import Tracker

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_security = HTTPBasic(auto_error=False)

_ACTIVE_LIKE = (EventStatus.QUEUED, EventStatus.ACTIVE)
_RECENT_LIKE = (EventStatus.DONE, EventStatus.CANCELLED, EventStatus.DROPPED)


def create_app(
    settings: Settings, session_factory: sessionmaker[Session], tracker: Tracker
) -> FastAPI:
    """Build the FastAPI application, wiring in the given settings/DB/tracker."""
    app = FastAPI(title="when2leave", version="0.1.0")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.tracker = tracker
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def require_auth(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(_security)],
    ) -> None:
        pair = settings.dashboard_auth_pair
        if pair is None:
            return
        user, password = pair
        ok = (
            credentials is not None
            and secrets.compare_digest(credentials.username, user)
            and secrets.compare_digest(credentials.password, password)
        )
        if not ok:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness/readiness probe. Not behind dashboard auth."""
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _auth: None = Depends(require_auth)) -> HTMLResponse:
        context = _dashboard_context(session_factory, tracker, settings)
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/partials/sections", response_class=HTMLResponse)
    async def dashboard_sections(
        request: Request, _auth: None = Depends(require_auth)
    ) -> HTMLResponse:
        """htmx polling target: re-renders just the status header + event sections."""
        context = _dashboard_context(session_factory, tracker, settings)
        return templates.TemplateResponse(request, "_sections.html", context)

    @app.get("/partials/events/{event_id}/history", response_class=HTMLResponse)
    async def event_history_partial(
        request: Request, event_id: int, _auth: None = Depends(require_auth)
    ) -> HTMLResponse:
        """htmx target for expanding a tracked event's location-update history."""
        with session_factory() as session:
            updates = session.scalars(
                select(LocationUpdate)
                .where(LocationUpdate.event_id == event_id)
                .order_by(LocationUpdate.ts.desc())
            ).all()
            return templates.TemplateResponse(
                request, "_history.html", {"updates": [_update_to_dict(u) for u in updates]}
            )

    @app.get("/api/events")
    async def api_events(_auth: None = Depends(require_auth)) -> list[dict[str, Any]]:
        """List all tracked events, most soonest-first."""
        with session_factory() as session:
            events = session.scalars(select(Event).order_by(Event.start)).all()
            return [_event_to_dict(e) for e in events]

    @app.get("/api/events/{event_id}/updates")
    async def api_event_updates(
        event_id: int, _auth: None = Depends(require_auth)
    ) -> list[dict[str, Any]]:
        """Return the full location-update history for one tracked event."""
        with session_factory() as session:
            event = session.get(Event, event_id)
            if event is None:
                raise HTTPException(status_code=404, detail="event not found")
            updates = session.scalars(
                select(LocationUpdate)
                .where(LocationUpdate.event_id == event_id)
                .order_by(LocationUpdate.ts)
            ).all()
            return [_update_to_dict(u) for u in updates]

    @app.post("/davpush/{token}", status_code=204)
    async def davpush_webhook(token: str, background_tasks: BackgroundTasks) -> None:
        """DAV Push receiver.

        We deliberately do not attempt to decrypt the (Web Push encrypted) request
        body -- see ``when2leave.davpush`` for why. Any POST to a known token is treated
        as "this calendar changed" and triggers a full re-sync of it in the background,
        so we can respond immediately as the spec expects.
        """
        subscription = tracker.find_subscription_by_token(token)
        if subscription is None:
            raise HTTPException(status_code=404, detail="unknown subscription")
        calendar_url, calendar_name = subscription
        background_tasks.add_task(tracker.resync_calendar, calendar_url, calendar_name)
        return None

    return app


def _dashboard_context(
    session_factory: sessionmaker[Session], tracker: Tracker, settings: Settings
) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    with session_factory() as session:
        active = session.scalars(
            select(Event).where(Event.status == EventStatus.ACTIVE).order_by(Event.start)
        ).all()
        queued = session.scalars(
            select(Event).where(Event.status == EventStatus.QUEUED).order_by(Event.start)
        ).all()
        recent = session.scalars(
            select(Event)
            .where(Event.status.in_(_RECENT_LIKE))
            .order_by(Event.updated_at.desc())
            .limit(20)
        ).all()
        counts = {
            "active": len(active),
            "queued": len(queued),
            "recent": len(recent),
        }

        active_rows = []
        for e in active:
            row = _event_to_dict(e, now=now, recompute_interval_s=settings.recompute_interval)
            latest_update = session.scalars(
                select(LocationUpdate)
                .where(LocationUpdate.event_id == e.id)
                .order_by(LocationUpdate.ts.desc())
                .limit(1)
            ).one_or_none()
            if latest_update is not None:
                row["last_distance_km"] = latest_update.distance_m / 1000
                row["last_travel_min"] = round(latest_update.travel_time_s / 60)
            active_rows.append(row)
        queued_rows = [
            _event_to_dict(e, now=now, geocode_window_s=settings.geocode_window) for e in queued
        ]
        recent_rows = [_event_to_dict(e) for e in recent]

    status = tracker.status
    return {
        "status": {
            "last_sync_at": status.last_sync_at,
            "last_sync_error": status.last_sync_error,
            "dawarich_reachable": status.dawarich_reachable,
            "davpush_subscription_count": status.davpush_subscription_count,
            "davpush_enabled": settings.davpush_enabled,
            "started_at": status.started_at,
        },
        "counts": counts,
        "active": active_rows,
        "queued": queued_rows,
        "recent": recent_rows,
        "now": now,
    }


def _event_to_dict(
    event: Event,
    now: datetime | None = None,
    recompute_interval_s: int | None = None,
    geocode_window_s: int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": event.id,
        "uid": event.uid,
        "title": event.title,
        "calendar": event.calendar,
        "start": event.start,
        "end": event.end,
        "location_text": event.location_text,
        "status": event.status.value,
        "resolved_lat": event.resolved_lat,
        "resolved_lon": event.resolved_lon,
        "resolved_address": event.resolved_address,
        "leave_at": event.leave_at,
        "notify_state": event.notify_state.value,
        "updated_at": event.updated_at,
    }
    if now is not None and recompute_interval_s is not None:
        row["next_recompute_at"] = event.updated_at + timedelta(seconds=recompute_interval_s)
        row["next_recompute_in_s"] = max(0, int((row["next_recompute_at"] - now).total_seconds()))
    if now is not None and geocode_window_s is not None:
        row["activates_at"] = event.start - timedelta(seconds=geocode_window_s)
    return row


def _update_to_dict(update: LocationUpdate) -> dict[str, Any]:
    return {
        "id": update.id,
        "ts": update.ts,
        "current_lat": update.current_lat,
        "current_lon": update.current_lon,
        "distance_m": update.distance_m,
        "travel_time_s": update.travel_time_s,
        "leave_at": update.leave_at,
        "routing_provider": update.routing_provider,
    }
