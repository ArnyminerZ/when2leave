"""Geocoding via OSM Nominatim, with proximity-based candidate selection.

Resolving a free-text ``LOCATION`` string to coordinates is ambiguous: Nominatim may
return a match on the other side of the world. We ask for several candidates and pick
the one closest to the user's current location (great-circle / haversine distance),
optionally biasing the query itself with a viewbox around that location.

Results are cached by the raw location string, since a fixed address never moves.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from when2leave.logging_config import get_logger

logger = get_logger(__name__)

EARTH_RADIUS_M = 6_371_000.0


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in meters between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True, slots=True)
class GeocodeCandidate:
    """A single Nominatim search result."""

    lat: float
    lon: float
    display_name: str


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """The chosen geocoding result for a location string."""

    lat: float
    lon: float
    display_name: str


def pick_nearest_candidate(
    candidates: list[GeocodeCandidate], reference_lat: float, reference_lon: float
) -> GeocodeCandidate:
    """Return the candidate with the smallest haversine distance to the reference point.

    Raises ``ValueError`` if ``candidates`` is empty.
    """
    if not candidates:
        raise ValueError("no geocode candidates to choose from")
    return min(
        candidates,
        key=lambda c: haversine_distance_m(reference_lat, reference_lon, c.lat, c.lon),
    )


class RateLimiter:
    """A simple blocking-free async rate limiter enforcing a minimum interval between calls."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._last_call: float | None = None

    async def wait(self) -> None:
        """Sleep, if necessary, so at least ``min_interval_seconds`` has elapsed since last call."""
        import asyncio

        if self._last_call is not None:
            elapsed = time.monotonic() - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_call = time.monotonic()


class GeocodeCacheProtocol:
    """Minimal cache interface, implemented by ``web.AppState``-backed cache adapters."""

    def get(self, location_text: str) -> GeocodeResult | None:  # pragma: no cover - protocol
        raise NotImplementedError

    def put(self, location_text: str, result: GeocodeResult) -> None:  # pragma: no cover
        raise NotImplementedError


class NominatimClient:
    """Async client for OSM Nominatim's ``/search`` endpoint, with rate limiting and retries."""

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        email: str | None = None,
        rate_limit_seconds: float = 1.0,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._email = email
        self._limiter = RateLimiter(rate_limit_seconds)
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    )
    async def search(
        self,
        query: str,
        limit: int = 5,
        bias_lat: float | None = None,
        bias_lon: float | None = None,
    ) -> list[GeocodeCandidate]:
        """Query Nominatim for ``query``, returning up to ``limit`` candidates.

        If ``bias_lat``/``bias_lon`` are given, a viewbox around them is passed to nudge
        (not restrict) results toward the user's current area, per Nominatim's API.
        """
        await self._limiter.wait()

        params: dict[str, str | int | float] = {
            "q": query,
            "format": "jsonv2",
            "limit": limit,
        }
        if bias_lat is not None and bias_lon is not None:
            delta = 5.0  # degrees, a loose bias box
            params["viewbox"] = (
                f"{bias_lon - delta},{bias_lat + delta},{bias_lon + delta},{bias_lat - delta}"
            )
            params["bounded"] = 0

        headers = {"User-Agent": self._user_agent}
        if self._email:
            params["email"] = self._email

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._base_url}/search", params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()

        candidates = [
            GeocodeCandidate(
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                display_name=item.get("display_name", query),
            )
            for item in payload
        ]
        logger.info(
            "nominatim.search",
            extra={"query": query, "candidate_count": len(candidates)},
        )
        return candidates


async def geocode_location(
    client: NominatimClient,
    cache: GeocodeCacheProtocol,
    location_text: str,
    reference_lat: float,
    reference_lon: float,
    candidates: int = 5,
) -> GeocodeResult:
    """Resolve ``location_text`` to coordinates, using the cache if available.

    On a cache miss, queries Nominatim for several candidates and picks the one
    nearest to ``(reference_lat, reference_lon)``. Raises ``ValueError`` if Nominatim
    returns no results.
    """
    cached = cache.get(location_text)
    if cached is not None:
        return cached

    results = await client.search(
        location_text, limit=candidates, bias_lat=reference_lat, bias_lon=reference_lon
    )
    if not results:
        raise ValueError(f"Nominatim returned no results for {location_text!r}")

    best = pick_nearest_candidate(results, reference_lat, reference_lon)
    result = GeocodeResult(lat=best.lat, lon=best.lon, display_name=best.display_name)
    cache.put(location_text, result)
    logger.info(
        "geocode.resolved",
        extra={
            "location_text": location_text,
            "lat": result.lat,
            "lon": result.lon,
            "cached_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    return result
