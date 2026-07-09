"""Tests for duration parsing and Settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from when2leave.config import Settings, parse_duration


@pytest.mark.parametrize(
    ("raw", "expected_seconds"),
    [
        ("30s", 30),
        ("5m", 300),
        ("12h", 43200),
        ("1d", 86400),
        ("90", 90),
        (90, 90),
        ("  15  m ", 900),
        ("15M", 900),
    ],
)
def test_parse_duration_valid(raw: object, expected_seconds: int) -> None:
    assert parse_duration(raw) == expected_seconds


@pytest.mark.parametrize("raw", ["12x", "abc", "", "-5m", None, True])
def test_parse_duration_invalid(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(raw)


def test_settings_loads_with_required_env(required_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert settings.caldav_url == required_env["CALDAV_URL"]
    assert settings.geocode_window == 12 * 3600
    assert settings.recompute_interval == 5 * 60
    assert settings.notify_lead == 15 * 60
    assert settings.prep_buffer == 10 * 60


def test_settings_missing_required_field_fails_fast(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_duration_env_override(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    monkeypatch.setenv("GEOCODE_WINDOW", "6h")
    monkeypatch.setenv("RECOMPUTE_INTERVAL", "2m")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.geocode_window == 6 * 3600
    assert settings.recompute_interval == 120


def test_davpush_disabled_without_callback_url(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    monkeypatch.setenv("DAVPUSH_ENABLED", "true")
    monkeypatch.delenv("DAVPUSH_CALLBACK_URL", raising=False)
    settings = Settings()  # type: ignore[call-arg]
    assert settings.davpush_enabled is False


def test_davpush_enabled_with_callback_url(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    monkeypatch.setenv("DAVPUSH_CALLBACK_URL", "https://example.com/davpush")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.davpush_enabled is True


def test_calendar_filter_parses_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    monkeypatch.setenv("CALDAV_CALENDARS", "Work, Personal ,Family")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.calendar_filter == ["Work", "Personal", "Family"]


def test_calendar_filter_empty_means_all(required_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert settings.calendar_filter == []


def test_dashboard_auth_pair(monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]) -> None:
    monkeypatch.setenv("DASHBOARD_AUTH", "admin:s3cret")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.dashboard_auth_pair == ("admin", "s3cret")


def test_dashboard_auth_none_by_default(required_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert settings.dashboard_auth_pair is None


def test_invalid_log_level_rejected(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "NOISY")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_invalid_routing_provider_rejected(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    monkeypatch.setenv("ROUTING_PROVIDER", "carrier-pigeon")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
