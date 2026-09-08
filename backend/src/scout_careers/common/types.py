"""Shared enumerations and literal aliases.

The Postgres enum types in ``DATA_MODEL.md`` §2 and these Python enums are one
declaration in two places; they are kept identical by the migration that
creates them and by the models that reference them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal


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


class RequirementKind(StrEnum):
    """``requirement_kind``. What a line in a job description is.

    The distinction that matters is HARD vs the rest: a missing hard
    requirement is a reason not to apply, a missing nice-to-have is a sentence
    in a cover letter. RESPONSIBILITY and CONDITION are extracted but carry no
    coverage weight — they describe the job rather than gate the candidate, and
    scoring them would dilute the number the operator reads.

    TOOL is scored, pooled into the nice bucket, per MATCH_SCORING.md §4.4. It
    was briefly unscored here on the same reasoning as RESPONSIBILITY, and that
    was wrong: "Jira", "Figma", "Databricks" are named because someone screens
    on them, and a tool the operator does not have is exactly the kind of small,
    nameable gap the gap list exists to surface. A responsibility ("own the
    roadmap for X") describes the job; a tool is still a demand on the
    candidate, just a cheap one to close.

    CONDITION was added after the first real extractions. Lines like "required
    location in European time zones", "4 10-hour shifts covering weekends" and
    "full-time position" were arriving as HARD, at weight 1.00 — in one posting,
    four of nine. Every one would have scored as an unmet gap, so a posting's
    rank would have partly measured how many scheduling sentences its employer
    chose to write. They are facts about the job's shape, not demands on a
    candidate, and the enum had nowhere to say so.

    Kept rather than dropped: "weekend shifts, Europe only" is exactly what the
    operator wants to see before applying. It just must not be scored.
    """

    HARD = "hard"
    NICE = "nice"
    RESPONSIBILITY = "responsibility"
    TOOL = "tool"
    CONDITION = "condition"


#: The ``hard`` bucket — ``H`` in the composite. One kind, but named, because
#: the whole formula turns on this bucket being separable from the other.
HARD_REQUIREMENT_KINDS: Final[frozenset[RequirementKind]] = frozenset({RequirementKind.HARD})

#: The ``nice`` bucket — ``N``. TOOL is pooled in here (MATCH_SCORING.md §4.4)
#: rather than given a bucket of its own: a third term would need a third weight
#: in the blend, and there is no evidence about what that weight should be.
NICE_REQUIREMENT_KINDS: Final[frozenset[RequirementKind]] = frozenset(
    {RequirementKind.NICE, RequirementKind.TOOL}
)

#: The kinds coverage scoring weighs. RESPONSIBILITY and CONDITION are excluded:
#: they describe the job rather than gate the candidate, and a scorer that
#: counted them would rank a verbose posting below a terse one for saying more
#: about itself.
SCORED_REQUIREMENT_KINDS: Final[frozenset[RequirementKind]] = (
    HARD_REQUIREMENT_KINDS | NICE_REQUIREMENT_KINDS
)


class ClaimConfidentiality(StrEnum):
    """``claim_confidentiality``. Who a claim may be shown to.

    RESTRICTED gates emission: such a claim never reaches a document sent
    outside a named allow-list of employers. That is how an internal cost figure
    stays controllable per application rather than per resume file — the
    alternative is maintaining two versions of the same bullet and eventually
    sending the wrong one.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class CoverageLevel(StrEnum):
    """``coverage_level``. How well one variant answers one requirement.

    PARTIAL exists so the scorer is not forced to lie in either direction:
    "Kubernetes in production" against someone who has deployed to ECS is
    neither met nor missing, and collapsing it either way produces a number
    the operator learns to distrust.
    """

    MET = "met"
    PARTIAL = "partial"
    MISSING = "missing"


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
    "HARD_REQUIREMENT_KINDS",
    "NICE_REQUIREMENT_KINDS",
    "NON_FAULT_STATUSES",
    "SCORED_REQUIREMENT_KINDS",
    "AtsType",
    "ClaimConfidentiality",
    "CompanyStatus",
    "CompanyTier",
    "CoverageLevel",
    "EmploymentType",
    "RequirementKind",
    "RunStatus",
    "Seniority",
    "SourceStatus",
]
