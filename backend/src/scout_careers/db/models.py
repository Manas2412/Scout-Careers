"""SQLAlchemy models — the tables that have code reading and writing them.

Phase 1: ``company`` (§3.1), ``source`` (§3.2), ``job_posting`` (§4.1),
``run_log`` (§9.1). Phase 2's extraction-and-scoring slice adds
``resume_variant`` (§5.1), ``requirement`` (§4.2) and ``match_score`` (§6.1).

``claim`` and ``claim_usage`` (§5.2, §5.3) arrive with scoring rather than with
document generation, because MATCH_SCORING.md §4.2 makes the ledger load-bearing
before a document exists: a *quantified* bullet citing no claim cannot prove a
requirement. Almost every bullet in these resumes carries a number, so without
the ledger that rule collapses every score.

Absent on purpose, though fully specified in DATA_MODEL.md: ``review_item``,
``application``, ``application_event``, ``artifact`` and ``email_message``.
ROADMAP.md §3.2 defers document generation and the review queue out of Phase 2,
and a model with no code behind it is an invitation to write against a table
that does not exist yet.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Computed,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scout_careers.common.types import (
    AtsType,
    ClaimConfidentiality,
    CompanyStatus,
    CompanyTier,
    CoverageLevel,
    RequirementKind,
    RunStatus,
)
from scout_careers.db.base import Base, TimestampMixin


def _pg_enum(
    enum_cls: type[
        AtsType
        | CompanyTier
        | CompanyStatus
        | RunStatus
        | RequirementKind
        | CoverageLevel
        | ClaimConfidentiality
    ],
    name: str,
) -> Enum:
    """Bind a Python StrEnum to a native Postgres enum type.

    ``values_callable`` makes SQLAlchemy persist the *value* (``"mail_alert"``)
    rather than the member name (``"MAIL_ALERT"``), which is what the migration
    creates and what every document quotes.

    Args:
        enum_cls: The Python enum.
        name: The Postgres type name.

    Returns:
        A configured SQLAlchemy ``Enum`` type.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        create_type=False,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Company(TimestampMixin, Base):
    """An employer we track. DATA_MODEL.md §3.1."""

    __tablename__ = "company"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[CompanyTier] = mapped_column(
        _pg_enum(CompanyTier, "company_tier"),
        nullable=False,
        server_default=text("'volume'"),
    )
    status: Mapped[CompanyStatus] = mapped_column(
        _pg_enum(CompanyStatus, "company_status"),
        nullable=False,
        server_default=text("'tracking'"),
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    hq_location: Mapped[str | None] = mapped_column(Text)
    size_band: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    cover_letter_worth: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # DATA_MODEL.md §3.1 declares this as a FK to resume_variant(id). That table
    # arrives with the generation phase, so the column ships now (it is on the
    # documented shape) and the REFERENCES clause is added by the migration that
    # creates resume_variant. A forward FK to a non-existent table is not a
    # thing Postgres will accept.
    default_variant_id: Mapped[int | None] = mapped_column(BigInteger)
    location_filter: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    sources: Mapped[list[Source]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    postings: Mapped[list[JobPosting]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index(
            "company_status_tier_idx",
            "status",
            "tier",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("company_tags_idx", "tags", postgresql_using="gin"),
        Index(
            "company_name_trgm_idx",
            text("name gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )


class Source(TimestampMixin, Base):
    """One board belonging to one company. DATA_MODEL.md §3.2."""

    __tablename__ = "source"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    adapter: Mapped[AtsType] = mapped_column(_pg_enum(AtsType, "ats_type"), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    poll_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1440")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    company: Mapped[Company] = relationship(back_populates="sources")
    postings: Mapped[list[JobPosting]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "adapter", "config", name="source_company_adapter_config_key"
        ),
        Index("source_due_idx", "enabled", "last_run_at", postgresql_where=text("enabled")),
    )


class JobPosting(TimestampMixin, Base):
    """A posting as seen on one source. DATA_MODEL.md §4.1."""

    __tablename__ = "job_posting"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(Text)
    location_raw: Mapped[str | None] = mapped_column(Text)
    location_city: Mapped[str | None] = mapped_column(Text)
    location_country: Mapped[str | None] = mapped_column(Text)
    is_remote: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    employment_type: Mapped[str | None] = mapped_column(Text)
    seniority_guess: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description_html: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    filtered_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    filter_reason: Mapped[str | None] = mapped_column(Text)
    # Not in DATA_MODEL.md §4.1. Added in Phase 1 because the two-run close rule
    # (INGEST_CLOSE_AFTER_MISSED_RUNS) needs a per-posting counter and adding a
    # NOT NULL counter later means backfilling it from run history we do not
    # keep. The counter advances only on runs where this posting's own source
    # returned ok or empty (SOURCE_ADAPTERS.md §10.5).
    missed_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description_text,''))",
            persisted=True,
        ),
    )

    company: Mapped[Company] = relationship(back_populates="postings")
    source: Mapped[Source] = relationship(back_populates="postings")
    requirements: Mapped[list[Requirement]] = relationship(
        back_populates="posting", cascade="all, delete-orphan", passive_deletes=True
    )
    scores: Mapped[list[MatchScore]] = relationship(
        back_populates="posting", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="job_posting_source_external_key"),
        Index("posting_company_seen_idx", "company_id", text("first_seen_at DESC")),
        Index(
            "posting_open_idx",
            text("first_seen_at DESC"),
            postgresql_where=text("closed_at IS NULL AND filtered_out = FALSE"),
        ),
        Index("posting_hash_idx", "content_hash"),
        Index("posting_search_idx", "search_tsv", postgresql_using="gin"),
    )


class RunLog(Base):
    """One pipeline run. DATA_MODEL.md §9.1.

    No ``created_at`` / ``updated_at``: ``started_at`` and ``finished_at`` are
    the row's timestamps, and §9.1's DDL is explicit about the column set.
    """

    __tablename__ = "run_log"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RunStatus] = mapped_column(_pg_enum(RunStatus, "run_status"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("run_log_recent_idx", "run_type", text("started_at DESC")),)


class ResumeVariant(TimestampMixin, Base):
    """One resume, structured. DATA_MODEL.md §5.1.

    ``content`` mirrors what the ``.docx`` builder already consumes, so a
    variant renders without a translation layer. ``skill_set`` is the flattened,
    normalised vocabulary that ``requirement.normalised_skill`` is matched
    against — the thing that lets coverage be computed with no second model
    call.
    """

    __tablename__ = "resume_variant"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    skill_set: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    scores: Mapped[list[MatchScore]] = relationship(
        back_populates="variant", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("resume_variant_skills_idx", "skill_set", postgresql_using="gin"),)


class Requirement(Base):
    """One extracted line of a job description. DATA_MODEL.md §4.2.

    No ``updated_at``: a requirement is not edited. A re-extraction under a new
    prompt replaces the set for that posting, and ``extracted_at`` with
    ``model`` and ``prompt_version`` says which pass produced it.
    """

    __tablename__ = "requirement"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    posting_id: Mapped[str] = mapped_column(
        CHAR(26), ForeignKey("job_posting.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[RequirementKind] = mapped_column(
        _pg_enum(RequirementKind, "requirement_kind"), nullable=False
    )
    # The column is `text`; the attribute cannot be. `text` is SQLAlchemy's
    # own function, imported at module scope and used a few lines below for
    # `server_default`. A class-body assignment named `text` would shadow it
    # for the rest of this class body and the next use would fail at import
    # time — so the attribute takes the underscore and the column keeps the
    # documented name.
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    normalised_skill: Mapped[str | None] = mapped_column(Text)
    # The model's advisory answer, kept so `normalised_skill` can be recomputed
    # against a new vocabulary without a model call. Not authoritative: it is
    # resolved through the same index as everything else, so it can never mint a
    # token (`extract/vocabulary.py`).
    normalised_skill_hint: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text("1.00")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    # Invariant 7: nothing this system produces may be unable to name the model
    # and prompt that produced it.
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)

    posting: Mapped[JobPosting] = relationship(back_populates="requirements")

    __table_args__ = (
        Index("requirement_posting_idx", "posting_id", "kind"),
        Index("requirement_skill_idx", "normalised_skill"),
    )


class Claim(Base):
    """One verified assertion. DATA_MODEL.md §5.2.

    The single most important table in the system: nothing may be asserted in a
    generated document unless it resolves here. Scoring depends on it before any
    document exists — MATCH_SCORING.md §4.2 refuses to let a *quantified* bullet
    prove a requirement unless it cites claims, which is what makes letting the
    ledger rot lower the operator's own scores.
    """

    __tablename__ = "claim"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # Human-written and stable, because a bullet's `claim_ids` are edited by
    # hand in the seed file and a list of surrogate integers is unreadable there.
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    # Text, not numeric: "~60,000" and "3x" are real claims, and coercing them
    # either loses the qualifier or refuses the row.
    metric_value: Mapped[str | None] = mapped_column(Text)
    metric_unit: Mapped[str | None] = mapped_column(Text)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL on purpose. A claim nobody can trace back is exactly what this
    # table exists to refuse.
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    confidentiality: Mapped[ClaimConfidentiality] = mapped_column(
        _pg_enum(ClaimConfidentiality, "claim_confidentiality"),
        nullable=False,
        server_default=text("'internal'"),
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    verified_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Facts decay. A test count or a corpus size is true on a date and drifts.
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    usages: Mapped[list[ClaimUsage]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("claim_tags_idx", "tags", postgresql_using="gin"),
        Index("claim_project_idx", "project", postgresql_where=text("deleted_at IS NULL")),
    )


class ClaimUsage(Base):
    """Where a claim was used. DATA_MODEL.md §5.3.

    The provenance trail: given any generated document, every number in it can
    be walked back to the ledger row that authorised it.
    """

    __tablename__ = "claim_usage"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    claim_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("claim.id", ondelete="CASCADE"), nullable=False
    )
    # No ForeignKey yet: `artifact` arrives with document generation in Phase 3.
    # The same deliberate deferral `company.default_variant_id` used in 0001.
    artifact_id: Mapped[str] = mapped_column(CHAR(26), nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    claim: Mapped[Claim] = relationship(back_populates="usages")

    __table_args__ = (
        UniqueConstraint(
            "claim_id", "artifact_id", "location", name="claim_usage_unique_placement"
        ),
    )


class MatchScore(Base):
    """One posting scored against one variant. DATA_MODEL.md §6.1.

    There is deliberately no ``selection_probability``. MATCH_SCORING.md §7 is
    the argument; the short form is that the number cannot be computed from
    anything this system observes, and a fabricated one would be the most
    trusted number on the page.
    """

    __tablename__ = "match_score"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    posting_id: Mapped[str] = mapped_column(
        CHAR(26), ForeignKey("job_posting.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("resume_variant.id", ondelete="CASCADE"), nullable=False
    )
    hard_met: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_total: Mapped[int] = mapped_column(Integer, nullable=False)
    nice_met: Mapped[int] = mapped_column(Integer, nullable=False)
    nice_total: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    composite_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # [{requirement_id, kind, text, level, note}] where `level` is a
    # CoverageLevel. Named requirements, never categories — gate 2.8.
    gaps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # At most one true per posting, enforced by the partial unique index
    # `match_one_recommendation_idx` rather than by application code.
    is_recommended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    posting: Mapped[JobPosting] = relationship(back_populates="scores")
    variant: Mapped[ResumeVariant] = relationship(back_populates="scores")

    __table_args__ = (
        # prompt_version is in the key so a re-score under a new prompt lands
        # beside the old row rather than destroying the comparison that shows
        # whether the new prompt is an improvement.
        UniqueConstraint(
            "posting_id", "variant_id", "prompt_version", name="match_score_posting_variant_key"
        ),
        Index("match_posting_score_idx", "posting_id", text("composite_score DESC")),
        Index(
            "match_recommended_idx",
            text("composite_score DESC"),
            postgresql_where=text("is_recommended"),
        ),
        Index(
            "match_one_recommendation_idx",
            "posting_id",
            unique=True,
            postgresql_where=text("is_recommended"),
        ),
    )


__all__ = [
    "Company",
    "JobPosting",
    "MatchScore",
    "Requirement",
    "ResumeVariant",
    "RunLog",
    "Source",
]
