"""Phase 1 core: extensions, enums, company, source, job_posting, run_log.

The full ``ats_type`` enum ships here, all twelve values, even though Phase 1
registers four adapters. Postgres can add an enum value but cannot remove one,
and each addition costs a standalone non-transactional revision — so the roster
is created once, correctly, and adapters are wired to it as they are written.

Revision ID: 0001_phase1_core
Revises:
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_phase1_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ATS_TYPE_VALUES = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "workable",
    "recruitee",
    "google",
    "amazon",
    "microsoft",
    "mail_alert",
    "manual",
)

ats_type = postgresql.ENUM(*ATS_TYPE_VALUES, name="ats_type", create_type=False)
company_tier = postgresql.ENUM("dream", "strong", "volume", name="company_tier", create_type=False)
company_status = postgresql.ENUM(
    "tracking", "paused", "blacklisted", name="company_status", create_type=False
)
run_status = postgresql.ENUM(
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    name="run_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # pg_trgm backs company_name_trgm_idx, which is how a mail-alert company
    # string is matched to a registry row.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for enum_type in (ats_type, company_tier, company_status, run_status):
        enum_type.create(bind, checkfirst=True)

    # --- updated_at trigger ------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_touch_updated_at()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END;
        $$;
        """
    )

    # --- company -----------------------------------------------------------
    op.create_table(
        "company",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tier", company_tier, nullable=False, server_default=sa.text("'volume'")),
        sa.Column("status", company_status, nullable=False, server_default=sa.text("'tracking'")),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("hq_location", sa.Text(), nullable=True),
        sa.Column("size_band", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("careers_url", sa.Text(), nullable=True),
        sa.Column(
            "cover_letter_worth", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        # FK to resume_variant(id) is added by the migration that creates that
        # table; a forward reference is not something Postgres will accept.
        sa.Column("default_variant_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "location_filter",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="company_pkey"),
        sa.UniqueConstraint("slug", name="company_slug_key"),
    )
    op.create_index(
        "company_status_tier_idx",
        "company",
        ["status", "tier"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("company_tags_idx", "company", ["tags"], postgresql_using="gin")
    op.execute("CREATE INDEX company_name_trgm_idx ON company USING GIN (name gin_trgm_ops)")

    # --- source ------------------------------------------------------------
    op.create_table(
        "source",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("adapter", ats_type, nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "poll_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1440"),
        ),
        sa.Column("last_run_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["company.id"], name="source_company_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="source_pkey"),
        sa.UniqueConstraint(
            "company_id", "adapter", "config", name="source_company_adapter_config_key"
        ),
    )
    op.create_index(
        "source_due_idx",
        "source",
        ["enabled", "last_run_at"],
        postgresql_where=sa.text("enabled"),
    )

    # --- job_posting -------------------------------------------------------
    op.create_table(
        "job_posting",
        sa.Column("id", sa.CHAR(length=26), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("location_raw", sa.Text(), nullable=True),
        sa.Column("location_city", sa.Text(), nullable=True),
        sa.Column("location_country", sa.Text(), nullable=True),
        sa.Column("is_remote", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("employment_type", sa.Text(), nullable=True),
        sa.Column("seniority_guess", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("posted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("filtered_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("filter_reason", sa.Text(), nullable=True),
        # Phase 1 addition, not in DATA_MODEL.md §4.1. The two-run close rule
        # needs a per-posting counter; adding a NOT NULL counter later would mean
        # backfilling it from run history nobody keeps.
        sa.Column("missed_runs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', "
                "coalesce(title,'') || ' ' || coalesce(description_text,''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["company.id"],
            name="job_posting_company_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name="job_posting_source_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="job_posting_pkey"),
        sa.UniqueConstraint("source_id", "external_id", name="job_posting_source_external_key"),
    )
    op.execute(
        "CREATE INDEX posting_company_seen_idx ON job_posting (company_id, first_seen_at DESC)"
    )
    op.execute(
        "CREATE INDEX posting_open_idx ON job_posting (first_seen_at DESC) "
        "WHERE closed_at IS NULL AND filtered_out = FALSE"
    )
    op.create_index("posting_hash_idx", "job_posting", ["content_hash"])
    op.create_index("posting_search_idx", "job_posting", ["search_tsv"], postgresql_using="gin")

    # --- run_log -----------------------------------------------------------
    op.create_table(
        "run_log",
        sa.Column("id", sa.CHAR(length=26), nullable=False),
        sa.Column("run_type", sa.Text(), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column(
            "started_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "source_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="run_log_pkey"),
    )
    op.execute("CREATE INDEX run_log_recent_idx ON run_log (run_type, started_at DESC)")

    for table in ("company", "source", "job_posting"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_touch_updated_at "
            f"BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()"
        )


def downgrade() -> None:
    for table in ("job_posting", "source", "company"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_touch_updated_at ON {table}")

    op.drop_table("run_log")
    op.drop_table("job_posting")
    op.drop_table("source")
    op.drop_table("company")

    op.execute("DROP FUNCTION IF EXISTS fn_touch_updated_at()")

    bind = op.get_bind()
    for enum_type in (run_status, company_status, company_tier, ats_type):
        enum_type.drop(bind, checkfirst=True)

    # pg_trgm is left in place: it is cheap, other schemas may use it, and
    # dropping an extension another object depends on fails noisily.
