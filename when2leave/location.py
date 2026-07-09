"""Current-location lookup via Dawarich.

Dawarich (https://dawarich.app) is a self-hosted location-tracking server. We poll its
API for the most recent tracked point on every recompute cycle so travel-time estimates
stay accurate as the user moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from when2leave.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CurrentLocation:
    """A single point returned by Dawarich."""

    lat: float
    lon: float
    timestamp: datetime

    @property
    def age_seconds(self) -> float:
        """Seconds elapsed since this point was recorded."""
        return (datetime.now(tz=UTC) - self.timestamp).total_seconds()


class StaleLocationError(RuntimeError):
    """Raised when the most recent Dawarich point is older than an acceptable threshold."""


class DawarichClient:
    """Async client for fetching the latest tracked point from Dawarich."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    )
    async def get_current_location(self) -> CurrentLocation:
        """Fetch the most recent point from Dawarich's points API.

        Raises ``httpx.HTTPStatusError``/``httpx.TransportError`` on failure (after
        retries) and ``ValueError`` if the response contains no points.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/api/v1/points",
                params={"api_key": self._api_key, "per_page": 1, "order": "desc"},
            )
            response.raise_for_status()
            payload = response.json()

        points = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not points:
            raise ValueError("Dawarich returned no location points")

        point = points[0]
        # Dawarich's point payloads nest coordinates under "geometry" (GeoJSON-like) or
        # expose flat lat/lon/timestamp fields depending on version; support both.
        attrs = point.get("attributes", point)
        if "geometry" in attrs:
            lon, lat = attrs["geometry"]["coordinates"][:2]
        else:
            lat, lon = attrs["latitude"], attrs["longitude"]
        raw_ts = attrs.get("timestamp") or attrs.get("recorded_at")
        timestamp = _parse_timestamp(raw_ts)

        location = CurrentLocation(lat=float(lat), lon=float(lon), timestamp=timestamp)
        logger.info(
            "dawarich.location_fetched",
            extra={"lat": location.lat, "lon": location.lon, "age_seconds": location.age_seconds},
        )
        return location


def _parse_timestamp(raw: object) -> datetime:
    """Parse a Dawarich timestamp, which may be an ISO string or a unix epoch (int)."""
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except ValueError:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    raise ValueError(f"unrecognized timestamp format: {raw!r}")
