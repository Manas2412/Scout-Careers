"""Structured logging.

Two rules the processor enforces rather than trusting to discipline:

- keys that look like credentials are redacted by name, at any nesting depth;
- bodies are never logged. There is no ``LOG_INCLUDE_BODIES`` setting, and
  diagnosis works off identifiers instead (``run_id``, ``source_id``).
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from scout_careers.common.config import Settings

#: Key names that must never reach a log sink in clear text.
REDACT_KEY_RE = re.compile(r"(?i)(authorization|cookie|token|secret|password|api[_-]?key)")

REDACTED = "**********"

#: Depth guard, so a pathological nested payload cannot make logging expensive.
_MAX_SCRUB_DEPTH = 6


def _scrub_value(key: str, value: Any, depth: int) -> Any:
    if REDACT_KEY_RE.search(key):
        return REDACTED
    return _scrub_container(value, depth)


def _scrub_container(value: Any, depth: int) -> Any:
    if depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _scrub_value(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_container(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_container(item, depth + 1) for item in value)
    return value


def scrub(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor that redacts credential-shaped keys.

    Args:
        _logger: Unused; part of the processor signature.
        _method: Unused; part of the processor signature.
        event_dict: The event being rendered.

    Returns:
        The event with every matching key replaced by a fixed mask, recursively
        through dicts, lists and tuples.
    """
    return {key: _scrub_value(str(key), value, 0) for key, value in event_dict.items()}


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger.

    JSON in production, a human-readable console renderer locally. Called once,
    at process start, before anything logs.

    Args:
        settings: Supplies ``log_level`` and ``log_format``.
    """
    level = getattr(logging, settings.log_level)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        scrub,
    ]

    renderer: structlog.typing.Processor
    if settings.log_format == "json":
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for a module.

    Args:
        name: Usually ``__name__``.

    Returns:
        A structlog bound logger.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = ["REDACTED", "REDACT_KEY_RE", "configure_logging", "get_logger", "scrub"]
