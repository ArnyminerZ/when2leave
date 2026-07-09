"""Tests for the haversine fallback routing provider and provider construction."""

from __future__ import annotations

import pytest

from when2leave.routing import HaversineFallbackProvider, build_routing_provider

MADRID = (40.4168, -3.7038)
BARCELONA = (41.3874, 2.1686)


async def test_haversine_provider_estimates_duration_from_speed() -> None:
    provider = HaversineFallbackProvider(avg_speed_kmh=60.0)
    estimate = await provider.estimate(*MADRID, *BARCELONA)
    expected_hours = estimate.distance_m / 1000 / 60
    assert estimate.duration_s == pytest.approx(expected_hours * 3600, rel=1e-6)
    assert estimate.provider == "haversine"


async def test_haversine_provider_zero_distance_zero_duration() -> None:
    provider = HaversineFallbackProvider(avg_speed_kmh=40.0)
    estimate = await provider.estimate(*MADRID, *MADRID)
    assert estimate.duration_s == pytest.approx(0.0, abs=1e-6)
    assert estimate.distance_m == pytest.approx(0.0, abs=1e-6)


def test_build_routing_provider_haversine() -> None:
    provider = build_routing_provider("haversine", "driving")
    assert isinstance(provider, HaversineFallbackProvider)


def test_build_routing_provider_openrouteservice_requires_api_key() -> None:
    with pytest.raises(ValueError, match="ROUTING_API_KEY"):
        build_routing_provider("openrouteservice", "driving", api_key=None)


def test_build_routing_provider_unknown_raises() -> None:
    with pytest.raises(ValueError):
        build_routing_provider("carrier-pigeon", "driving")  # type: ignore[arg-type]
