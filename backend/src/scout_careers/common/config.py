"""The entire Phase 1 configuration surface.

One ``Settings`` object, built by ``pydantic-settings``, injected everywhere.
There is no second place a tunable may live (DEVELOPMENT.md §4.1). The narrow
exception is policy rather than configuration — ``NEVER_FETCH_HOSTS`` is a code
constant in ``sources/policy.py`` precisely so that invariant 4 has nowhere to
be switched off, and nothing here references it.

Field names are lower-case; ``pydantic-settings`` matches them
case-insensitively against ``SCREAMING_SNAKE_CASE`` environment keys, so
``source_user_agent`` is populated from ``SOURCE_USER_AGENT``.

``extra="forbid"`` is load-bearing: an unknown key in ``.env`` is a typo, and a
typo that is silently ignored costs an afternoon. It also means ``.env.example``
must list Phase 1 keys and nothing else.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scout_careers.common.errors import ConfigError

#: The unreplaced contact placeholder shipped in ``.env.example``. Outbound
#: traffic carrying it is not attributable to a contactable operator, which is
#: the whole point of the user-agent policy (SOURCE_ADAPTERS.md §4.5).
USER_AGENT_PLACEHOLDER = "<operator>"

DEFAULT_USER_AGENT = (
    f"ScoutCareers/1.0 (personal job-search agent; +mailto:{USER_AGENT_PLACEHOLDER})"
)


class Settings(BaseSettings):
    """Phase 1 configuration. Frozen after construction; never mutated at runtime."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    # ---- core ---------------------------------------------------------
    scout_env: Literal["local", "production"] = "local"
    scout_base_url: str = "http://localhost:8000"
    tz: str = "Asia/Kolkata"
    app_version: str = "dev"

    # ---- database -----------------------------------------------------
    database_url: PostgresDsn
    database_pool_size: Annotated[int, Field(ge=1, le=50)] = 10
    database_max_overflow: Annotated[int, Field(ge=0, le=50)] = 5
    database_pool_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    database_pool_recycle_s: Annotated[int, Field(ge=0)] = 1800
    database_statement_timeout_ms: Annotated[int, Field(ge=0)] = 30_000
    database_lock_timeout_ms: Annotated[int, Field(ge=0)] = 5_000
    database_echo: bool = False
    database_migrate_on_boot: bool = False

    # ---- redis --------------------------------------------------------
    redis_url: RedisDsn = RedisDsn("redis://redis:6379/0")
    redis_max_connections: Annotated[int, Field(ge=1, le=200)] = 20
    redis_socket_timeout_s: Annotated[float, Field(gt=0)] = 5.0
    run_lock_ttl_s: Annotated[int, Field(ge=1)] = 3_600
    robots_cache_ttl_s: Annotated[int, Field(ge=1)] = 86_400

    # ---- ingestion and sources ----------------------------------------
    source_user_agent: str = DEFAULT_USER_AGENT
    source_concurrency: Annotated[int, Field(ge=1, le=32)] = 8
    source_probe_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    source_timeout_s: Annotated[int, Field(ge=1)] = 180
    source_retry_budget_s: Annotated[int, Field(ge=1)] = 90
    rate_limit_wait_s: Annotated[int, Field(ge=1)] = 20
    auto_disable_threshold: Annotated[int, Field(ge=1)] = 5
    circuit_breaker_failures: Annotated[int, Field(ge=1)] = 5
    max_description_chars: Annotated[int, Field(ge=1_000)] = 60_000
    max_response_bytes: Annotated[int, Field(ge=1_024)] = 20_971_520
    ingest_close_after_missed_runs: Annotated[int, Field(ge=1)] = 2

    # ---- scheduler ----------------------------------------------------
    run_wall_clock_budget_s: Annotated[int, Field(ge=1)] = 900

    # ---- logging ------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ---- derived ------------------------------------------------------
    @property
    def is_production(self) -> bool:
        """True when running under the production boot-validation profile."""
        return self.scout_env == "production"

    @property
    def user_agent_is_attributable(self) -> bool:
        """True when the user agent carries a replaced contact address."""
        return bool(self.source_user_agent.strip()) and (
            USER_AGENT_PLACEHOLDER not in self.source_user_agent
        )

    @model_validator(mode="after")
    def _boot_checks(self) -> Settings:
        _run_boot_checks(self)
        return self


def _run_boot_checks(settings: Settings) -> None:
    """Refuse to start on anything touching data integrity or the compliance boundary.

    Args:
        settings: The freshly constructed settings object.

    Raises:
        ConfigError: On any refusing condition from CONFIGURATION.md §14.2 that
            is in Phase 1 scope.
    """
    dsn = str(settings.database_url)
    if "+asyncpg" not in dsn.split("://", 1)[0]:
        raise ConfigError("DATABASE_URL must use the asyncpg driver")

    if settings.run_lock_ttl_s <= settings.run_wall_clock_budget_s:
        raise ConfigError(
            "run lock TTL must exceed the run budget: "
            f"RUN_LOCK_TTL_S={settings.run_lock_ttl_s} "
            f"RUN_WALL_CLOCK_BUDGET_S={settings.run_wall_clock_budget_s}"
        )

    if not settings.is_production:
        return

    if settings.database_echo:
        raise ConfigError("SQL echo is not permitted in production")

    if settings.log_level == "DEBUG":
        raise ConfigError("DEBUG logging is not permitted in production")

    if not settings.user_agent_is_attributable:
        raise ConfigError(
            "SOURCE_USER_AGENT is required in production and must carry a real "
            "contact address, not the .env.example placeholder"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object.

    Cached: configuration is read once, at first use, and never reloaded. A
    value that changes system behaviour requires an ``.env`` edit and a
    restart, which is the correct amount of friction.

    Returns:
        The validated ``Settings`` instance.

    Raises:
        ConfigError: When boot validation refuses the configuration.
        pydantic.ValidationError: When a field is missing or out of bounds.
    """
    return Settings()


__all__ = ["DEFAULT_USER_AGENT", "USER_AGENT_PLACEHOLDER", "Settings", "get_settings"]
