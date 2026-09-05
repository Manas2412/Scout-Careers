"""Logging tests that actually emit through the configured pipeline.

The bug these exist to prevent shipped once: `structlog.stdlib.add_logger_name`
reads `logger.name`, the factory is `PrintLoggerFactory`, and `PrintLogger` has
no `.name`. Every unit test passed — none of them called `configure_logging`
and then logged, so the first real log call in the CLI raised `AttributeError`
in the middle of a discovery run.

The lesson generalised: asserting on `scrub` in isolation proves the processor
works, not that the pipeline it sits in does. These tests drive the real thing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog

from scout_careers.common.logging import REDACTED, configure_logging, get_logger
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _reset_structlog() -> Any:
    """Leave global structlog state as it was found."""
    yield
    structlog.reset_defaults()


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_a_configured_logger_can_actually_log(
    log_format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point: configure, then emit, in both renderers."""
    configure_logging(make_settings(log_format=log_format))
    get_logger("scout_careers.test").info("discovery_started", run_id="01JB", sources=3)

    captured = capsys.readouterr().err
    assert "discovery_started" in captured
    assert "01JB" in captured


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_the_logger_name_reaches_the_output(
    log_format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`get_logger` binds the name itself, so the field survives either factory."""
    configure_logging(make_settings(log_format=log_format))
    get_logger("scout_careers.sources.greenhouse").info("fetched", count=12)

    assert "scout_careers.sources.greenhouse" in capsys.readouterr().err


def test_json_output_is_parseable_and_carries_the_expected_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(make_settings(log_format="json"))
    get_logger("scout_careers.ingest.runner").warning("source_failed", source_id=77)

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["event"] == "source_failed"
    assert payload["level"] == "warning"
    assert payload["logger"] == "scout_careers.ingest.runner"
    assert payload["source_id"] == 77
    assert "timestamp" in payload


def test_credential_shaped_keys_are_redacted_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not the processor in isolation — the processor inside the real pipeline."""
    configure_logging(make_settings(log_format="json"))
    get_logger("scout_careers.mail").info(
        "token_refreshed",
        access_token="ya29.super-secret",
        nested={"authorization": "Bearer abc", "safe": "keep-me"},
        source_id=1,
    )

    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["access_token"] == REDACTED
    assert payload["nested"]["authorization"] == REDACTED
    assert payload["nested"]["safe"] == "keep-me"
    assert payload["source_id"] == 1
    # The strongest assertion: the secret is nowhere in the rendered bytes.
    assert "ya29.super-secret" not in line
    assert "Bearer abc" not in line


def test_the_level_filter_is_honoured(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_level="WARNING", log_format="json"))
    log = get_logger("scout_careers.test")
    log.info("should_not_appear")
    log.warning("should_appear")

    captured = capsys.readouterr().err
    assert "should_not_appear" not in captured
    assert "should_appear" in captured
