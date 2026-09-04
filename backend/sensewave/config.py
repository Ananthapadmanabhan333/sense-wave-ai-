"""Environment-bound settings. Every value in .env.example lands here."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENSEWAVE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sensewave:sensewave@localhost:5432/sensewave"

    jwt_secret: str = "change-me-in-production"
    jwt_ttl_minutes: int = 720
    admin_email: str = "admin@sensewave.local"
    admin_password: str = "change-me"

    upstream_ws_url: str = "ws://sensing-server:8765/ws/sensing"
    upstream_http_url: str = "http://sensing-server:8080"
    upstream_token: str = ""

    ingest_hz: float = Field(default=1.0, gt=0)
    stale_after_seconds: float = Field(default=10.0, gt=0)
    reconnect_backoff_initial: float = Field(default=0.5, gt=0)
    reconnect_backoff_max: float = Field(default=30.0, gt=0)
    min_vitals_confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # Newly discovered nodes are left unassigned by default: SenseWave does
    # not guess which room a sensor is in. Set this to a room id to have new
    # nodes land there automatically -- it exists so the zero-hardware
    # simulate demo produces visible data, and is off in real deployments.
    auto_map_room_id: int | None = None

    log_level: str = "INFO"

    @property
    def ingest_min_interval(self) -> float:
        """Minimum seconds between persisted readings for one room."""
        return 1.0 / self.ingest_hz

    def resolved_upstream_token(self) -> str:
        """Upstream bearer token.

        RuView's own Python clients read ``RUVIEW_API_TOKEN``. We prefer our
        namespaced ``SENSEWAVE_UPSTREAM_TOKEN`` but fall back to RuView's name
        so one .env can drive both this backend and a stock ruview client.
        """
        if self.upstream_token:
            return self.upstream_token
        import os

        return os.environ.get("RUVIEW_API_TOKEN", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
