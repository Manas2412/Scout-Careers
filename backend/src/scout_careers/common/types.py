"""Shared enumerations and literal aliases.

The Postgres enum types in ``DATA_MODEL.md`` §2 and these Python enums are one
declaration in two places; they are kept identical by the migration that
creates them and by the models that reference them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class AtsType(StrEnum):
    """``ats_type``. All twelve values ship in the first migration.

    The full roster is created up front on purpose: ``ALTER TYPE ... ADD VALUE``
    can add a member but Postgres cannot remove one, and adding each value later
    costs a standalone, non-transactional revision (DATA_MODEL.md §11). Phase 1
    implements four adapters; the other eight values exist and are simply
    unregistered until their adapter is written.
    """

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    SMARTRECRUITERS = "smartrecruiters"
    WORKABLE = "workable"
    RECRUITEE = "recruitee"
    GOOGLE = "google"
    AMAZON = "amazon"
    MICROSOFT = "microsoft"
    MAIL_ALERT = "mail_alert"
    MANUAL = "manual"


class CompanyTier(StrEnum):
    """``company_tier``."""

    DREAM = "dream"
    STRONG = "strong"
    VOLUME = "volume"


class CompanyStatus(StrEnum):
    """``company_status``."""

    TRACKING = "tracking"
    PAUSED = "paused"
    BLACKLISTED = "blacklisted"


class RunStatus(StrEnum):
    """``run_status``. Adapter failures never produce ``FAILED`` (§10.4)."""

    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class SourceStatus(StrEnum):
    """Per-source run outcome (SOURCE_ADAPTERS.md §10.2).

    Not a Postgres enum: it is written into the ``run_log.source_results`` JSONB
    and into ``source.last_status`` (TEXT), so the vocabulary can grow without a
    migration.
    """

    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    HTTP_ERROR = "http_error"
    SCHEMA_ERROR = "schema_error"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    ROBOTS_DENIED = "robots_denied"
    DENIED_BY_POLICY = "denied_by_policy"
    DISABLED = "disabled"


#: Statuses that do NOT increment ``source.consecutive_failures``: they are our
#: own back-pressure or an explicit operator decision, not the source's fault.
NON_FAULT_STATUSES: frozenset[SourceStatus] = frozenset(
    {
        SourceStatus.OK,
        SourceStatus.EMPTY,
        SourceStatus.RATE_LIMITED,
        SourceStatus.CIRCUIT_OPEN,
        SourceStatus.DISABLED,
    }
)

#: Statuses on which a posting's "not seen" counter may advance (§10.5).
COUNTS_AS_SEEN_STATUSES: frozenset[SourceStatus] = frozenset({SourceStatus.OK, SourceStatus.EMPTY})

EmploymentType = Literal["full_time", "part_time", "contract", "internship", "temporary", "unknown"]

Seniority = Literal[
    "intern",
    "entry",
    "mid",
    "senior",
    "staff",
    "principal",
    "manager",
    "director",
    "executive",
    "unknown",
]

__all__ = [
    "COUNTS_AS_SEEN_STATUSES",
    "NON_FAULT_STATUSES",
    "AtsType",
    "CompanyStatus",
    "CompanyTier",
    "EmploymentType",
    "RunStatus",
    "Seniority",
    "SourceStatus",
]
