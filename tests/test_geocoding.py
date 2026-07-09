"""Tests for haversine distance and proximity-based geocode candidate selection."""

from __future__ import annotations

import pytest

from when2leave.geocoding import GeocodeCandidate, haversine_distance_m, pick_nearest_candidate

MADRID = (40.4168, -3.7038)
BARCELONA = (41.3874, 2.1686)
NEW_YORK = (40.7128, -74.0060)


def test_haversine_zero_distance_for_identical_points() -> None:
    assert haversine_distance_m(*MADRID, *MADRID) == pytest.approx(0.0, abs=1e-6)


def test_haversine_known_distance_madrid_barcelona() -> None:
    # Great-circle distance between Madrid and Barcelona is ~505 km.
    distance_km = haversine_distance_m(*MADRID, *BARCELONA) / 1000
    assert distance_km == pytest.approx(505, abs=5)


def test_haversine_is_symmetric() -> None:
    a_to_b = haversine_distance_m(*MADRID, *BARCELONA)
    b_to_a = haversine_distance_m(*BARCELONA, *MADRID)
    assert a_to_b == pytest.approx(b_to_a)


def test_pick_nearest_candidate_prefers_close_point_over_far_one() -> None:
    """A same-named-street candidate on another continent shouldn't win over a local one."""
    candidates = [
        GeocodeCandidate(lat=NEW_YORK[0], lon=NEW_YORK[1], display_name="Calle Mayor, New York"),
        GeocodeCandidate(lat=BARCELONA[0], lon=BARCELONA[1], display_name="Calle Mayor, Barcelona"),
    ]
    nearest = pick_nearest_candidate(candidates, *MADRID)
    assert nearest.display_name == "Calle Mayor, Barcelona"


def test_pick_nearest_candidate_single_candidate() -> None:
    candidates = [GeocodeCandidate(lat=1.0, lon=1.0, display_name="Only option")]
    assert pick_nearest_candidate(candidates, 0.0, 0.0) is candidates[0]


def test_pick_nearest_candidate_empty_raises() -> None:
    with pytest.raises(ValueError):
        pick_nearest_candidate([], 0.0, 0.0)
