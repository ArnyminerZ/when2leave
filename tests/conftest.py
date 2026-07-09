"""Shared pytest fixtures: baseline required environment variables for Settings."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Set the minimal set of required environment variables for a valid ``Settings``."""
    env = {
        "CALDAV_URL": "https://cloud.example.com/remote.php/dav",
        "CALDAV_USERNAME": "alice",
        "CALDAV_PASSWORD": "app-password",
        "DAWARICH_URL": "https://dawarich.example.com",
        "DAWARICH_API_KEY": "dawarich-key",
        "NOMINATIM_USER_AGENT": "when2leave/0.1 (test@example.com)",
        "NTFY_TOPIC": "when2leave-alerts",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TZ", raising=False)
    yield env
