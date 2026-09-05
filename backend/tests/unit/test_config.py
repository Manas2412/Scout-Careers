"""Boot validation: the configuration that must refuse to start."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scout_careers.common.config import DEFAULT_USER_AGENT, Settings, get_settings
from scout_careers.common.errors import ConfigError
from tests.conftest import make_settings


def test_defaults_match_the_documented_ones() -> None:
    settings = make_settings()
    assert settings.scout_env == "local"
    assert settings.database_pool_size == 10
    assert settings.database_max_overflow == 5
    assert settings.database_statement_timeout_ms == 30_000
    assert settings.database_lock_timeout_ms == 5_000
    assert settings.run_lock_ttl_s == 3_600
    assert settings.run_wall_clock_budget_s == 900
    assert settings.robots_cache_ttl_s == 86_400
    assert settings.source_concurrency == 8
    assert settings.source_timeout_s == 180
    assert settings.source_retry_budget_s == 90
    assert settings.rate_limit_wait_s == 20
    assert settings.auto_disable_threshold == 5
    assert settings.circuit_breaker_failures == 5
    assert settings.max_description_chars == 60_000
    assert settings.max_response_bytes == 20_971_520
    assert settings.ingest_close_after_missed_runs == 2


def test_sync_dsn_is_refused_at_boot() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(database_url="postgresql://scout:scout@localhost:5432/scout")
    assert "asyncpg" in str(excinfo.value)


def test_psycopg_dsn_is_also_refused() -> None:
    with pytest.raises(ConfigError):
        make_settings(database_url="postgresql+psycopg://scout:scout@localhost:5432/scout")


def test_asyncpg_dsn_is_accepted() -> None:
    settings = make_settings(database_url="postgresql+asyncpg://u:p@db:5432/scout")
    assert "+asyncpg" in str(settings.database_url)


def test_sql_echo_in_production_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(
            scout_env="production",
            database_echo=True,
            source_user_agent="ScoutCareers/1.0 (+mailto:ops@example.com)",
        )
    assert "echo" in str(excinfo.value).lower()


def test_sql_echo_locally_is_fine() -> None:
    assert make_settings(database_echo=True).database_echo is True


def test_debug_logging_in_production_is_refused() -> None:
    with pytest.raises(ConfigError):
        make_settings(
            scout_env="production",
            log_level="DEBUG",
            source_user_agent="ScoutCareers/1.0 (+mailto:ops@example.com)",
        )


def test_run_lock_ttl_not_exceeding_the_run_budget_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(run_lock_ttl_s=900, run_wall_clock_budget_s=900)
    assert "run lock TTL must exceed the run budget" in str(excinfo.value)

    with pytest.raises(ConfigError):
        make_settings(run_lock_ttl_s=600, run_wall_clock_budget_s=900)

    # One second of headroom is enough for the invariant to hold.
    assert make_settings(run_lock_ttl_s=901, run_wall_clock_budget_s=900).run_lock_ttl_s == 901


def test_unreplaced_user_agent_is_refused_in_production() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(scout_env="production", source_user_agent=DEFAULT_USER_AGENT)
    assert "SOURCE_USER_AGENT" in str(excinfo.value)

    with pytest.raises(ConfigError):
        make_settings(scout_env="production", source_user_agent="   ")


def test_attributable_user_agent_passes_in_production() -> None:
    settings = make_settings(
        scout_env="production",
        source_user_agent="ScoutCareers/1.0 (personal job-search agent; +mailto:ops@example.com)",
    )
    assert settings.is_production is True
    assert settings.user_agent_is_attributable is True


def test_unknown_key_is_a_boot_failure() -> None:
    # extra="forbid": SCOREING_BLEND_HARD in a .env would otherwise be silently
    # ignored and cost an operator an afternoon.
    with pytest.raises(ValidationError) as excinfo:
        make_settings(scoreing_blend_hard=0.9)
    assert "scoreing_blend_hard" in str(excinfo.value).lower()


def test_settings_are_frozen() -> None:
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.source_concurrency = 32  # type: ignore[misc]


def test_out_of_bounds_numeric_is_refused() -> None:
    with pytest.raises(ValidationError):
        make_settings(source_concurrency=64)
    with pytest.raises(ValidationError):
        make_settings(database_pool_size=0)


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings.cache_info().currsize == 0
    finally:
        get_settings.cache_clear()


def test_no_phase_two_keys_leaked_into_settings() -> None:
    # extra="forbid" means .env may only carry keys declared here, so a stray
    # LLM_/SCORING_/FF_ field would force those keys into .env.example early.
    prefixes = ("llm_", "scoring_", "generation_", "ledger_", "ff_", "export_", "mail_")
    for field_name in Settings.model_fields:
        assert not field_name.startswith(prefixes), f"{field_name} is not Phase 1"


ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


def test_env_example_constructs_settings() -> None:
    # extra="forbid" makes this a real contract: if .env.example carries a key
    # that is not a Phase 1 field, an operator who copies it cannot boot.
    settings = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    assert settings.scout_env == "local"
    assert "+asyncpg" in str(settings.database_url)


def test_env_example_declares_every_phase_1_key_and_no_others() -> None:
    declared = {
        line.split("=", 1)[0].strip().lower()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert declared == set(Settings.model_fields)
