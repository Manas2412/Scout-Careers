"""SQLAlchemy models — the Phase 1 subset of DATA_MODEL.md.

Four tables: ``company`` (§3.1), ``source`` (§3.2), ``job_posting`` (§4.1) and
``run_log`` (§9.1). The remaining tables land with the phases that need them;
the enum types they will reference are already created by migration 0001 where
DATA_MODEL.md §2 puts them in Phase 1 scope.
"""

from __future__ import annotations

from datetime import datetime
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scout_careers.common.types import AtsType, CompanyStatus, CompanyTier, RunStatus
from scout_careers.db.base import Base, TimestampMixin


def _pg_enum(enum_cls: type[AtsType | CompanyTier | CompanyStatus | RunStatus], name: str) -> Enum:
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


__all__ = ["Company", "JobPosting", "RunLog", "Source"]
