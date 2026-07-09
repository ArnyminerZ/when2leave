"""Application entrypoint: wires together config, persistence, clients, the scheduler
and the web app, then serves everything through a single Uvicorn process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from when2leave.caldav_sync import CalDAVSyncClient
from when2leave.config import Settings, load_settings
from when2leave.davpush import DavPushClient
from when2leave.db import create_engine, make_session_factory
from when2leave.geocoding import NominatimClient
from when2leave.location import DawarichClient
from when2leave.logging_config import configure_logging, get_logger
from when2leave.notifier import NtfyClient
from when2leave.routing import ResilientRoutingProvider, build_routing_provider
from when2leave.tracker import Tracker
from when2leave.web import create_app

logger = get_logger(__name__)


def _build_components(settings: Settings) -> tuple[Tracker, sessionmaker[Session]]:
    engine = create_engine(settings.database_path)
    session_factory = make_session_factory(engine)

    caldav_client = CalDAVSyncClient(
        settings.caldav_url, settings.caldav_username, settings.caldav_password
    )
    nominatim_client = NominatimClient(
        settings.nominatim_url,
        settings.nominatim_user_agent,
        settings.nominatim_email,
        settings.nominatim_rate_limit_seconds,
    )
    dawarich_client = DawarichClient(settings.dawarich_url, settings.dawarich_api_key)
    primary_routing = build_routing_provider(
        settings.routing_provider,
        settings.travel_mode,
        settings.routing_url,
        settings.routing_api_key,
        settings.fallback_avg_speed_kmh,
    )
    routing_provider = ResilientRoutingProvider(primary_routing, settings.fallback_avg_speed_kmh)
    ntfy_client = NtfyClient(settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token)

    davpush_client: DavPushClient | None = None
    if settings.davpush_enabled:
        davpush_client = DavPushClient(settings.caldav_username, settings.caldav_password)

    tracker = Tracker(
        settings=settings,
        session_factory=session_factory,
        caldav_client=caldav_client,
        nominatim_client=nominatim_client,
        dawarich_client=dawarich_client,
        routing_provider=routing_provider,
        ntfy_client=ntfy_client,
        davpush_client=davpush_client,
    )
    return tracker, session_factory


def create_application(settings: Settings | None = None) -> FastAPI:
    """Build the fully-wired FastAPI application (used by both ``run()`` and tests)."""
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    tracker, session_factory = _build_components(settings)
    scheduler = AsyncIOScheduler()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("when2leave.starting", extra={"routing_provider": settings.routing_provider})

        # Initial sync so the dashboard isn't empty on first load.
        try:
            await tracker.full_sync()
        except Exception:
            logger.error("when2leave.initial_sync_failed", exc_info=True)

        if settings.davpush_enabled:
            try:
                await tracker.register_davpush_for_calendars()
            except Exception:
                logger.error("when2leave.davpush_initial_registration_failed", exc_info=True)

        scheduler.add_job(
            tracker.full_sync,
            "interval",
            seconds=settings.poll_interval_seconds,
            id="poll_sync",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            tracker.recompute_tick,
            "interval",
            seconds=settings.recompute_interval,
            id="recompute",
            max_instances=1,
            coalesce=True,
        )
        if settings.davpush_enabled:
            scheduler.add_job(
                tracker.renew_expiring_davpush_subscriptions,
                "interval",
                minutes=30,
                id="davpush_renew",
                max_instances=1,
                coalesce=True,
            )
        scheduler.start()
        logger.info("when2leave.started")

        yield

        scheduler.shutdown(wait=False)
        logger.info("when2leave.stopped")

    app = create_app(settings, session_factory, tracker)
    app.router.lifespan_context = lifespan
    return app


def run() -> None:
    """CLI/Docker entrypoint: load settings, build the app, and serve it with Uvicorn."""
    settings = load_settings()
    configure_logging(settings.log_level)
    app = create_application(settings)
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, log_config=None)


if __name__ == "__main__":
    run()
