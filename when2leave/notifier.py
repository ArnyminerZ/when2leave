"""Notification timing/state machine and the ntfy publishing client.

Given an event's start time ``ST``, an estimated travel time ``TT``, a prep buffer and a
notify lead, we compute:

    leave_at = ST - TT - PREP_BUFFER

and drive three possible notifications per event: a heads-up before ``leave_at``, a
"leave now" at ``leave_at``, and a one-shot "running late" notice if the event only
became trackable after ``leave_at`` already passed. The state machine is careful not to
spam: once a notification tier has fired, it only fires again if ``leave_at`` gets
meaningfully worse (moves earlier by more than ``NOTIFY_RESHIFT_THRESHOLD``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from when2leave.db import NotifyState
from when2leave.logging_config import get_logger

logger = get_logger(__name__)


def compute_leave_at(event_start: datetime, travel_time_s: float, prep_buffer_s: int) -> datetime:
    """Compute the time by which the user must leave to arrive at ``event_start``."""
    return event_start - timedelta(seconds=travel_time_s) - timedelta(seconds=prep_buffer_s)


@dataclass(frozen=True, slots=True)
class Notification:
    """A notification decision produced by ``evaluate_notification``."""

    kind: str  # "heads_up" | "leave_now" | "running_late"
    title: str
    message: str
    tags: str
    priority: str


def evaluate_notification(
    *,
    now: datetime,
    event_title: str,
    leave_at: datetime,
    travel_time_s: float,
    notify_lead_s: int,
    notify_state: NotifyState,
    last_notified_leave_at: datetime | None,
    reshift_threshold_s: int,
    default_priority: str = "default",
) -> tuple[NotifyState, Notification | None]:
    """Decide whether a notification should fire for the current recompute cycle.

    Returns the (possibly unchanged) new ``NotifyState`` and a ``Notification`` to send,
    or ``None`` if nothing should be sent this cycle.
    """
    heads_up_at = leave_at - timedelta(seconds=notify_lead_s)
    travel_minutes = round(travel_time_s / 60)
    leave_at_local = leave_at.strftime("%H:%M")

    if notify_state == NotifyState.NONE:
        if now >= leave_at:
            return NotifyState.RUNNING_LATE_SENT, Notification(
                kind="running_late",
                title=f"Running late for {event_title}",
                message=(
                    f"You should already be on your way ({travel_minutes} min travel). "
                    f"Leave now to minimize lateness."
                ),
                tags="rotating_light,running",
                priority="urgent",
            )
        if now >= heads_up_at:
            return NotifyState.HEADS_UP_SENT, Notification(
                kind="heads_up",
                title=f"Leave soon for {event_title}",
                message=(f"~{travel_minutes} min travel; leave by {leave_at_local}."),
                tags="clock3",
                priority=default_priority,
            )
        return NotifyState.NONE, None

    if notify_state == NotifyState.HEADS_UP_SENT:
        if now >= leave_at:
            return NotifyState.LEAVE_NOW_SENT, Notification(
                kind="leave_now",
                title=f"Leave now for {event_title}",
                message=f"~{travel_minutes} min travel. Time to go!",
                tags="walking,dash",
                priority="high",
            )
        if _worsened(leave_at, last_notified_leave_at, reshift_threshold_s):
            return NotifyState.HEADS_UP_SENT, Notification(
                kind="heads_up",
                title=f"Leave soon for {event_title} (traffic got worse)",
                message=(
                    f"~{travel_minutes} min travel; leave by {leave_at_local} (updated estimate)."
                ),
                tags="clock3,warning",
                priority=default_priority,
            )
        return notify_state, None

    # LEAVE_NOW_SENT / RUNNING_LATE_SENT: one-shot tiers already fired, stay quiet.
    return notify_state, None


def _worsened(
    leave_at: datetime, last_notified_leave_at: datetime | None, threshold_s: int
) -> bool:
    """Return True if ``leave_at`` moved earlier by more than ``threshold_s`` seconds."""
    if last_notified_leave_at is None:
        return False
    shift = (last_notified_leave_at - leave_at).total_seconds()
    return shift > threshold_s


def maps_url(lat: float, lon: float) -> str:
    """Return an OpenStreetMap link usable as a ntfy click action / maps link."""
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"


class NtfyClient:
    """Async client for publishing messages to an ntfy server/topic."""

    def __init__(
        self, base_url: str, topic: str, token: str | None = None, timeout: float = 10.0
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._topic = topic
        self._token = token
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    )
    async def publish(
        self,
        notification: Notification,
        click_lat: float | None = None,
        click_lon: float | None = None,
    ) -> None:
        """Publish a notification to the configured ntfy topic."""
        headers = {
            "Title": notification.title,
            "Tags": notification.tags,
            "Priority": notification.priority,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if click_lat is not None and click_lon is not None:
            headers["Click"] = maps_url(click_lat, click_lon)

        # ntfy requires header values to be valid latin-1/ASCII; percent-encode titles
        # that might contain non-ASCII characters (event names, addresses).
        headers = {k: quote(v, safe=" ,:/?#&=.-_") for k, v in headers.items()}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/{self._topic}",
                content=notification.message.encode("utf-8"),
                headers=headers,
            )
            response.raise_for_status()

        logger.info(
            "ntfy.published",
            extra={"kind": notification.kind, "title": notification.title},
        )
