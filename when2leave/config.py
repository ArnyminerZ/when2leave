"""Application configuration.

All configuration is supplied via environment variables (optionally loaded from a
``.env`` file for local development) and validated eagerly at startup through
``pydantic-settings``. Duration-like settings accept human-friendly strings such as
``"12h"``, ``"5m"`` or ``"30s"`` in addition to plain integer seconds.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "": 1,  # bare number defaults to seconds
}


def parse_duration(value: object) -> int:
    """Parse a duration string (``"12h"``, ``"5m"``, ``"30s"``, ``"90"``) into seconds.

    Accepts ints/floats directly (already seconds). Raises ``ValueError`` on anything
    that doesn't match the expected grammar.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, int | float):
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"invalid duration: {value!r}")

    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(
            f"invalid duration {value!r}: expected a number optionally followed by "
            "s/m/h/d, e.g. '30s', '5m', '12h', '1d'"
        )
    amount, unit = match.groups()
    return int(amount) * _UNIT_SECONDS[unit.lower()]


Duration = Annotated[int, BeforeValidator(parse_duration)]


class Settings(BaseSettings):
    """Top-level, validated application settings sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- CalDAV / Nextcloud ---------------------------------------------------
    caldav_url: str = Field(..., description="Nextcloud CalDAV base URL")
    caldav_username: str = Field(..., description="Nextcloud username")
    caldav_password: str = Field(..., description="Nextcloud app password")
    caldav_calendars: str = Field(
        default="", description="Comma-separated calendar names/URLs; empty = all"
    )

    # --- DAV Push ---------------------------------------------------------------
    davpush_enabled: bool = Field(default=True)
    davpush_callback_url: str | None = Field(
        default=None, description="Public URL of the push receiver on this service"
    )
    poll_interval_seconds: Duration = Field(default=900)

    # --- Dawarich (current location) --------------------------------------------
    dawarich_url: str = Field(..., description="Dawarich base URL")
    dawarich_api_key: str = Field(..., description="Dawarich API key")

    # --- Nominatim geocoding -----------------------------------------------------
    nominatim_url: str = Field(default="https://nominatim.openstreetmap.org")
    nominatim_user_agent: str = Field(..., description="Required by OSM usage policy")
    nominatim_email: str | None = Field(default=None)
    nominatim_rate_limit_seconds: float = Field(default=1.0)
    geocode_candidates: int = Field(default=5, ge=1, le=50)

    # --- Routing -------------------------------------------------------------------
    routing_provider: Literal["osrm", "openrouteservice", "valhalla", "haversine"] = Field(
        default="osrm"
    )
    routing_url: str | None = Field(default=None)
    routing_api_key: str | None = Field(default=None)
    travel_mode: Literal["driving", "cycling", "walking"] = Field(default="driving")
    fallback_avg_speed_kmh: float = Field(
        default=40.0, description="Average speed used by the haversine routing fallback"
    )

    # --- Timing windows --------------------------------------------------------------
    geocode_window: Duration = Field(default=parse_duration("12h"))
    recompute_interval: Duration = Field(default=parse_duration("5m"))
    notify_lead: Duration = Field(default=parse_duration("15m"))
    prep_buffer: Duration = Field(default=parse_duration("10m"))
    notify_reshift_threshold: Duration = Field(
        default=parse_duration("5m"),
        description="Minimum worsening of leave_at that triggers a re-notification",
    )

    # --- ntfy ------------------------------------------------------------------------
    ntfy_url: str = Field(default="https://ntfy.sh")
    ntfy_topic: str = Field(...)
    ntfy_token: str | None = Field(default=None)
    ntfy_priority: Literal["min", "low", "default", "high", "urgent"] = Field(default="default")

    # --- HTTP server -------------------------------------------------------------------
    http_host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8080, ge=1, le=65535)
    dashboard_auth: str | None = Field(
        default=None, description="Optional 'user:pass' for HTTP basic auth on the dashboard"
    )

    # --- Persistence -------------------------------------------------------------------
    database_path: str = Field(default="/data/when2leave.db")

    # --- Misc ------------------------------------------------------------------------
    tz: str | None = Field(default=None, alias="TZ")
    log_level: str = Field(default="INFO")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        level = v.upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(valid)}, got {v!r}")
        return level

    @field_validator("caldav_url", "dawarich_url", "nominatim_url", "ntfy_url", "routing_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.rstrip("/")

    @model_validator(mode="after")
    def _validate_davpush(self) -> Settings:
        if self.davpush_enabled and not self.davpush_callback_url:
            # Not fatal: we fall back to polling, but make it loud in the logs.
            object.__setattr__(self, "davpush_enabled", False)
        return self

    @property
    def calendar_filter(self) -> list[str]:
        """Return the configured calendar names/URLs, or an empty list for "all"."""
        if not self.caldav_calendars.strip():
            return []
        return [c.strip() for c in self.caldav_calendars.split(",") if c.strip()]

    @property
    def dashboard_auth_pair(self) -> tuple[str, str] | None:
        """Return (user, pass) parsed from ``dashboard_auth``, if configured."""
        if not self.dashboard_auth:
            return None
        if ":" not in self.dashboard_auth:
            raise ValueError("DASHBOARD_AUTH must be in 'user:pass' format")
        user, _, password = self.dashboard_auth.partition(":")
        return user, password


def load_settings() -> Settings:
    """Load and validate settings from the environment. Fails fast on bad config."""
    return Settings()  # type: ignore[call-arg]
