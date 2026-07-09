"""Travel-time computation via pluggable routing providers.

Supported providers (``ROUTING_PROVIDER``):

* ``osrm`` -- self-hosted or the public OSRM demo server.
* ``openrouteservice`` -- requires ``ROUTING_API_KEY``.
* ``valhalla`` -- self-hosted Valhalla instance.
* ``haversine`` -- no external service; estimates travel time from great-circle
  distance divided by a configurable average speed. Used as the always-available
  fallback and as an explicit opt-in for users who don't want to run/depend on a
  routing server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Literal

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from when2leave.geocoding import haversine_distance_m
from when2leave.logging_config import get_logger

logger = get_logger(__name__)

TravelMode = Literal["driving", "cycling", "walking"]

_DEFAULT_URLS = {
    "osrm": "https://router.project-osrm.org",
    "openrouteservice": "https://api.openrouteservice.org",
    "valhalla": "https://valhalla1.openstreetmap.de",
}

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


@dataclass(frozen=True, slots=True)
class TravelEstimate:
    """The result of a routing query."""

    duration_s: float
    distance_m: float
    provider: str


class RoutingProvider(ABC):
    """Base interface for travel-time providers."""

    name: str

    @abstractmethod
    async def estimate(
        self, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
    ) -> TravelEstimate:
        """Estimate travel time and distance from origin to destination."""


class HaversineFallbackProvider(RoutingProvider):
    """Estimate travel time as great-circle distance / average speed. No network calls."""

    name = "haversine"

    def __init__(self, avg_speed_kmh: float = 40.0) -> None:
        self._avg_speed_mps = avg_speed_kmh * 1000 / 3600

    async def estimate(
        self, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
    ) -> TravelEstimate:
        distance_m = haversine_distance_m(origin_lat, origin_lon, dest_lat, dest_lon)
        duration_s = distance_m / self._avg_speed_mps
        return TravelEstimate(duration_s=duration_s, distance_m=distance_m, provider=self.name)


class OSRMProvider(RoutingProvider):
    """OSRM ``/route`` HTTP API client."""

    name = "osrm"

    _PROFILE: ClassVar[dict[str, str]] = {
        "driving": "driving",
        "cycling": "cycling",
        "walking": "foot",
    }

    def __init__(self, base_url: str, mode: TravelMode, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._profile = self._PROFILE[mode]
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    async def estimate(
        self, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
    ) -> TravelEstimate:
        coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
        url = f"{self._base_url}/route/v1/{self._profile}/{coords}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, params={"overview": "false"})
            response.raise_for_status()
            payload = response.json()

        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise ValueError(f"OSRM returned no route: {payload.get('code')}")

        route = payload["routes"][0]
        return TravelEstimate(
            duration_s=float(route["duration"]),
            distance_m=float(route["distance"]),
            provider=self.name,
        )


class OpenRouteServiceProvider(RoutingProvider):
    """OpenRouteService ``/v2/directions`` HTTP API client."""

    name = "openrouteservice"

    _PROFILE: ClassVar[dict[str, str]] = {
        "driving": "driving-car",
        "cycling": "cycling-regular",
        "walking": "foot-walking",
    }

    def __init__(
        self, base_url: str, api_key: str, mode: TravelMode, timeout: float = 10.0
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._profile = self._PROFILE[mode]
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    async def estimate(
        self, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
    ) -> TravelEstimate:
        url = f"{self._base_url}/v2/directions/{self._profile}"
        headers = {"Authorization": self._api_key}
        body = {"coordinates": [[origin_lon, origin_lat], [dest_lon, dest_lat]]}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()

        summary = payload["routes"][0]["summary"]
        return TravelEstimate(
            duration_s=float(summary["duration"]),
            distance_m=float(summary["distance"]),
            provider=self.name,
        )


class ValhallaProvider(RoutingProvider):
    """Valhalla ``/route`` HTTP API client."""

    name = "valhalla"

    _COSTING: ClassVar[dict[str, str]] = {
        "driving": "auto",
        "cycling": "bicycle",
        "walking": "pedestrian",
    }

    def __init__(self, base_url: str, mode: TravelMode, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._costing = self._COSTING[mode]
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    async def estimate(
        self, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
    ) -> TravelEstimate:
        body = {
            "locations": [
                {"lat": origin_lat, "lon": origin_lon},
                {"lat": dest_lat, "lon": dest_lon},
            ],
            "costing": self._costing,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}/route", json=body)
            response.raise_for_status()
            payload = response.json()

        leg = payload["trip"]["legs"][0]
        return TravelEstimate(
            duration_s=float(leg["summary"]["time"]),
            distance_m=float(leg["summary"]["length"]) * 1000,  # km -> m
            provider=self.name,
        )


def build_routing_provider(
    provider: Literal["osrm", "openrouteservice", "valhalla", "haversine"],
    mode: TravelMode,
    base_url: str | None = None,
    api_key: str | None = None,
    fallback_avg_speed_kmh: float = 40.0,
) -> RoutingProvider:
    """Construct the configured ``RoutingProvider`` implementation."""
    if provider == "haversine":
        return HaversineFallbackProvider(avg_speed_kmh=fallback_avg_speed_kmh)
    if provider == "osrm":
        return OSRMProvider(base_url or _DEFAULT_URLS["osrm"], mode)
    if provider == "openrouteservice":
        if not api_key:
            raise ValueError("ROUTING_API_KEY is required for the openrouteservice provider")
        return OpenRouteServiceProvider(
            base_url or _DEFAULT_URLS["openrouteservice"], api_key, mode
        )
    if provider == "valhalla":
        return ValhallaProvider(base_url or _DEFAULT_URLS["valhalla"], mode)
    raise ValueError(f"unknown routing provider: {provider!r}")


class ResilientRoutingProvider(RoutingProvider):
    """Wraps a primary provider and falls back to haversine estimation on failure.

    This keeps the tracking loop alive (with a degraded estimate) when the configured
    routing server is temporarily unreachable, rather than skipping the recompute cycle
    entirely.
    """

    def __init__(self, primary: RoutingProvider, fallback_avg_speed_kmh: float = 40.0) -> None:
        self._primary = primary
        self._fallback = HaversineFallbackProvider(avg_speed_kmh=fallback_avg_speed_kmh)
        self.name = primary.name

    async def estimate(
        self, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
    ) -> TravelEstimate:
        try:
            return await self._primary.estimate(origin_lat, origin_lon, dest_lat, dest_lon)
        except Exception:
            logger.warning(
                "routing.fallback_to_haversine",
                extra={"provider": self._primary.name},
                exc_info=True,
            )
            return await self._fallback.estimate(origin_lat, origin_lon, dest_lat, dest_lon)
