"""Helpers shared by the concrete adapters.

Deliberately small. An adapter's mapping, its caveats and its response models
stay in its own module — that is where a reader looks when a board breaks. What
lives here is only the machinery that must behave *identically* everywhere:
config validation, probe timing, the curated probe message, and the §9.1
description bound. Three copies of those would drift, and drift in the probe
message is drift in what the operator is told.

Nothing here fetches, and nothing here logs a response body.
"""

from __future__ import annotations

import time

from pydantic import BeforeValidator, ValidationError

from scout_careers.common.errors import AdapterConfigError, SchemaDriftError
from scout_careers.common.text import truncate_at_paragraph

HTTP_NOT_FOUND = 404
HTTP_FORBIDDEN = 403
HTTP_GONE = 410


def first_error(exc: ValidationError) -> str:
    """Return one short, non-sensitive line describing a validation failure.

    Only the field location and pydantic's own message survive. The offending
    *value* never does: it comes from an upstream body, and upstream bodies are
    untrusted input that must not reach a log line or a UI string.

    Args:
        exc: The validation error.

    Returns:
        A single ``"field.path: message"`` line.
    """
    errors = exc.errors()
    if not errors:
        return "validation failed"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    return f"{location}: {first.get('msg', 'invalid')}"


def config_error(adapter: str, exc: ValidationError) -> AdapterConfigError:
    """Build the error for a ``source.config`` that does not validate.

    Returned rather than raised so the caller keeps the ``raise ... from exc``
    chain visible at the point the validation actually failed.

    Args:
        adapter: The adapter's name, for the message.
        exc: The validation error.

    Returns:
        An ``AdapterConfigError``. The API maps it to HTTP 422; the runner
        raises it again at the top of every run, so a config that rotted since
        it was saved fails loudly rather than silently.
    """
    return AdapterConfigError(f"invalid {adapter} config: {first_error(exc)}")


def drift_error(url_template: str, exc: ValidationError) -> SchemaDriftError:
    """Build the error for an upstream page that no longer matches its model.

    This is what turns silent vendor drift into ``schema_error`` rather than an
    empty board. Adapters validate the whole page before yielding anything, so a
    drifted item cannot leave a half-populated posting behind it.

    Args:
        url_template: The log-safe URL pattern — never the interpolated URL.
        exc: The validation error.

    Returns:
        A ``SchemaDriftError``, whose ``error_code`` is ``adapter.schema_drift``.
    """
    return SchemaDriftError(f"{url_template} did not match the expected shape: {first_error(exc)}")


def elapsed_ms(started: float) -> int:
    """Return milliseconds since a ``time.monotonic()`` reading.

    Args:
        started: The monotonic timestamp the operation began at.

    Returns:
        Elapsed milliseconds, rounded down.
    """
    return int((time.monotonic() - started) * 1000)


def probe_detail(status: int) -> str:
    """Return the curated, displayable reason for an upstream HTTP status.

    Curated on purpose: ``ProbeResult.detail`` is rendered in the UI, and an
    upstream body is untrusted input that must never be surfaced verbatim.

    Args:
        status: The HTTP status code.

    Returns:
        A short human string.
    """
    if status == HTTP_NOT_FOUND:
        return "Board not found — check the identifier in this source's config"
    if status == HTTP_GONE:
        return "Board has been removed"
    if status == HTTP_FORBIDDEN:
        return "Board is not publicly readable"
    return f"Board returned HTTP {status}"


def company_guess(identifier: str) -> str:
    """Guess a display name from a board identifier.

    A guess, and the field it feeds is named ``company_name_guess`` for exactly
    that reason: it seeds the "Add company" form and is never stored unreviewed.

    Args:
        identifier: The board token, site or board name from the config.

    Returns:
        A title-cased best effort.
    """
    return identifier.replace("-", " ").replace("_", " ").strip().title()


def bound_description(text: str, limit: int) -> tuple[str, bool]:
    """Apply the §9.1 description bound.

    An unbounded JD is a token-cost hazard and, more importantly, a
    prompt-injection surface. Cutting on a paragraph boundary keeps the
    remaining text readable to the extractor.

    Args:
        text: The normalised description text.
        limit: ``settings.max_description_chars``.

    Returns:
        The bounded text, and whether truncation happened — the caller records
        the flag in ``raw`` so the inspector can say so.
    """
    bounded = truncate_at_paragraph(text, limit)
    return bounded, len(bounded) < len(text)


# ---------------------------------------------------------------------------
# Null tolerance on defaulted upstream fields
# ---------------------------------------------------------------------------
#
# A pydantic default — `= False`, `Field(default_factory=list)` — applies only
# when the key is ABSENT. An explicit JSON `null` still hits the validator and
# fails. Job-board APIs use the two interchangeably: Greenhouse sends
# `"metadata": null` on boards with no custom fields, Ashby sends
# `"isRemote": null` on postings where the recruiter left it unset.
#
# This cost a live run: eight of forty-three sources — Stripe, Figma, Postman,
# OpenAI, Cohere, ElevenLabs, Notion, Supabase — failed `schema_error` on
# exactly this, while every fixture-backed test passed, because hand-written
# fixtures contain the fields their author remembered to include.
#
# The rule these encode: **every defaulted field on an upstream model tolerates
# an explicit null.** A null from an upstream we do not control is missing data,
# not a contract breach, and refusing a whole board over one unset boolean is
# the wrong trade.


def _none_to_empty_list(value: object) -> object:
    return [] if value is None else value


def _none_to_empty_dict(value: object) -> object:
    return {} if value is None else value


def _none_to_false(value: object) -> object:
    return False if value is None else value


def _none_to_true(value: object) -> object:
    return True if value is None else value


#: Annotate a defaulted list field: ``Annotated[list[X], NullIsEmptyList]``.
NullIsEmptyList = BeforeValidator(_none_to_empty_list)
#: Annotate a defaulted nested model: ``Annotated[Model, NullIsEmptyModel]``.
#: An empty mapping validates into the model's own field defaults.
NullIsEmptyModel = BeforeValidator(_none_to_empty_dict)
#: Annotate a defaulted boolean whose absent meaning is False.
NullIsFalse = BeforeValidator(_none_to_false)
#: Annotate a defaulted boolean whose absent meaning is True.
NullIsTrue = BeforeValidator(_none_to_true)


__all__ = [
    "HTTP_FORBIDDEN",
    "HTTP_GONE",
    "HTTP_NOT_FOUND",
    "NullIsEmptyList",
    "NullIsEmptyModel",
    "NullIsFalse",
    "NullIsTrue",
    "bound_description",
    "company_guess",
    "config_error",
    "drift_error",
    "elapsed_ms",
    "first_error",
    "probe_detail",
]
