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


class MailError(ScoutError):
    """Base for every ``mail/`` failure.

    Every subclass carries a message built from identifiers and configuration
    *names* only. Never a token, never a token prefix, never a token length,
    never a mail body and never a subject line: an exception message is a log
    line and a UI string in waiting (SECURITY_ARCHITECTURE.md §7.2 rule 7).
    """

    error_code: ClassVar[str] = "mail.error"


class MailNotConfigured(MailError):
    """Gmail is not configured, so no mailbox reader can be built.

    Not a failure of anything: it is the state of an install that has not been
    authorised yet. The runner reports the ``mail_alert`` source as ``disabled``
    on the strength of it, and counts nothing against the source.
    """

    error_code: ClassVar[str] = "mail.not_configured"


class MailTokenKeyMissing(MailError):
    """``MAIL_TOKEN_KEY`` is absent, so the token cannot be encrypted at rest.

    Raised instead of writing the token in clear. There is no plaintext
    fallback, and adding one would make the encryption optional in the only
    situation where it matters (EMAIL_INGESTION.md §2.5).
    """

    error_code: ClassVar[str] = "mail.token_key_missing"


class MailTokenUnreadable(MailError):
    """The token file is missing, truncated, or not decryptable with this key.

    The three cases are one message on purpose: distinguishing them for the
    caller would mean saying something about the ciphertext, and the remedy is
    the same either way — ``scout-careers auth gmail``.
    """

    error_code: ClassVar[str] = "mail.token_unreadable"


class GmailClientSecretsMissing(MailError):
    """No desktop OAuth client is configured, so the flow cannot start."""

    error_code: ClassVar[str] = "mail.client_secrets_missing"


class GmailAuthError(MailError):
    """The refresh token is no longer valid. Permanent; never retried.

    Google answers ``400 invalid_grant`` after a revocation, a password change,
    six months of disuse, or a publishing-status change. Retrying a permanent
    failure only burns quota (EMAIL_INGESTION.md §2.5).
    """

    error_code: ClassVar[str] = "mail.auth_invalid_grant"


class GmailApiNotEnabled(MailError):
    """The Gmail API is not enabled on the Google Cloud project.

    Google answers ``403 accessNotConfigured`` (or ``SERVICE_DISABLED``). This
    is not an authorisation failure and the stored token is fine: the OAuth
    consent screen, the client and the token all live in the Cloud console
    *project*, and enabling the API is a fourth, separate step that is easy to
    miss because every earlier step succeeds without it.

    It has its own class because the remedy is the opposite of the one for a bad
    grant. Reported as ``mail.auth_invalid_grant``, it sends the operator to
    ``scout-careers auth gmail``, which completes successfully every time and
    changes nothing — a loop that looks like a broken credential and is actually
    a disabled API.
    """

    error_code: ClassVar[str] = "mail.api_not_enabled"


class GmailScopeInsufficient(MailError):
    """The token is valid but was not granted the scope this call needs.

    Distinct from a disabled API, and unlike it, re-authorising *is* the remedy:
    the consent screen has to be completed again for the wider scope.
    """

    error_code: ClassVar[str] = "mail.scope_insufficient"


class GmailApiError(MailError):
    """Any other Gmail API status. Carries the status and nothing else.

    Separated from :class:`GmailAuthError` so that "the credential is bad" stays
    a claim this system only makes when it is true. A rate limit or a Google
    5xx is neither permanent nor an auth problem, and labelling it one buries a
    transient failure under a permanent-sounding code.
    """

    error_code: ClassVar[str] = "mail.api_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
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
    "GmailApiError",
    "GmailApiNotEnabled",
    "GmailAuthError",
    "GmailClientSecretsMissing",
    "GmailScopeInsufficient",
    "MailError",
    "MailNotConfigured",
    "MailTokenKeyMissing",
    "MailTokenUnreadable",
    "RateLimitTimeout",
    "ResponseTooLarge",
    "RobotsDenied",
    "SchemaDriftError",
    "ScoutError",
    "SourceTimeout",
    "TransportError",
    "UpstreamHttpError",
]
