"""The exception hierarchy.

Every error carries a stable ``error_code`` classvar. Those codes are the
machine-readable vocabulary in ``run_log.source_results`` and in API error
bodies (SOURCE_ADAPTERS.md §10.3), so they are part of the contract and are
never reworded to make a log line read better.
"""

from __future__ import annotations

from typing import ClassVar


class ScoutError(Exception):
    """Base for every error this system raises deliberately.

    ``error_code`` defaults to the catch-all so that an exception introduced
    without a code still classifies, rather than crashing the classifier.
    """

    error_code: ClassVar[str] = "adapter.unknown"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__


class ConfigError(ScoutError):
    """Boot-time configuration is invalid. Raised before the first request."""

    error_code: ClassVar[str] = "config.invalid"


class AdapterConfigError(ScoutError):
    """``source.config`` did not validate against the adapter's config model.

    Also raised when the configured board is not there at all: from the
    operator's seat the two are the same fault, and the remedy is the same —
    fix the config (SOURCE_ADAPTERS.md §2.1). Mapped to HTTP 422 by the API.
    """

    error_code: ClassVar[str] = "adapter.board_not_found"


class DeniedByPolicy(ScoutError):
    """The URL resolved to a host on ``NEVER_FETCH_HOSTS`` (invariant 4).

    There is no configuration, environment variable or admin toggle that
    disables the check that raises this.
    """

    error_code: ClassVar[str] = "source.denied_by_policy"

    def __init__(self, host: str) -> None:
        super().__init__(f"fetching {host} is denied by policy")
        self.host = host


class RobotsDenied(ScoutError):
    """robots.txt disallows the endpoint, or could not be read fail-closed."""

    error_code: ClassVar[str] = "adapter.robots_denied"


class RateLimitTimeout(ScoutError):
    """No token-bucket lease within ``RATE_LIMIT_WAIT_S``."""

    error_code: ClassVar[str] = "adapter.rate_limited"


class CircuitOpen(ScoutError):
    """The in-run breaker is open for this ``bucket_key``; no request was made."""

    error_code: ClassVar[str] = "adapter.circuit_open"


class SchemaDriftError(ScoutError):
    """The response parsed but did not match the model the adapter expects."""

    error_code: ClassVar[str] = "adapter.schema_drift"


class SourceTimeout(ScoutError):
    """The per-source wall-clock ceiling was hit."""

    error_code: ClassVar[str] = "adapter.timeout"


class TransportError(ScoutError):
    """A transport-layer failure, or an upstream status we will not retry."""

    error_code: ClassVar[str] = "adapter.transport"


class UpstreamHttpError(TransportError):
    """A non-retryable 4xx, or a retryable status that outlived its budget."""

    error_code: ClassVar[str] = "adapter.board_not_found"

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ResponseTooLarge(TransportError):
    """The response exceeded ``MAX_RESPONSE_BYTES``.

    The response is failed, never truncated: half a JSON body is not a smaller
    board, it is a parse error wearing a board's clothes.
    """

    error_code: ClassVar[str] = "adapter.transport"


__all__ = [
    "AdapterConfigError",
    "CircuitOpen",
    "ConfigError",
    "DeniedByPolicy",
    "RateLimitTimeout",
    "ResponseTooLarge",
    "RobotsDenied",
    "SchemaDriftError",
    "ScoutError",
    "SourceTimeout",
    "TransportError",
    "UpstreamHttpError",
]
