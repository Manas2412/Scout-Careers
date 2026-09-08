"""The claims ledger: ``claim``, ``claim_usage`` and ``claim_confidentiality``.

Deferred out of 0002 on the grounds that a table with no code writing it is a
schema the operator can be misled by. Scoring is what changes that.

``MATCH_SCORING.md`` §4.2 makes the ledger load-bearing before a single document
is generated: a *quantified* bullet that cites no claim cannot prove a
requirement, and degrades to ``partial``. Almost every bullet in these resumes
carries a number — "300+ events/sec", "~60,000-LOC", "cut deploy time 80%" — so
without the ledger that one rule collapses every score, and ranking ends up
driven by the weakest, unquantified lines. The alternative was to switch the rule
off; building the table is the honest version of the same fix, and it is the
interlock the whole system is designed around:

    letting the ledger rot lowers the operator's own scores.

``claim_usage`` references ``artifact``, which does not exist yet — document
generation is Phase 3. Rather than create a dangling foreign key or invent the
table early, ``artifact_id`` ships as a bare ``CHAR(26)`` with the constraint
deferred, exactly as ``company.default_variant_id`` was in 0001 and completed in
0002. The comment on the column says so, and a later revision adds the clause.

Revision ID: 0005_claims_ledger
Revises: 0004_requirement_skill_hint
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_claims_ledger"
down_revision: str | None = "0004_requirement_skill_hint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Created once, correctly. Postgres can add an enum value but never remove one,
#: so the roster ships whole — the same reasoning as `ats_type` in 0001.
CONFIDENTIALITY_VALUES = ("public", "internal", "restricted")

claim_confidentiality = postgresql.ENUM(
    *CONFIDENTIALITY_VALUES, name="claim_confidentiality", create_type=False
)


def upgrade() -> None:
    """Create the enum and both tables."""
    bind = op.get_bind()
    claim_confidentiality.create(bind, checkfirst=True)

    op.create_table(
        "claim",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        # Stable, human-written, and cited from `resume_variant.content`:
        # 'khelo.cost_reduction'. A surrogate ID alone would make a bullet's
        # claim list unreadable in the seed file it is edited in.
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        # Text, not numeric. '~60,000' and '3x' are real claims, and coercing
        # them to a number either loses the qualifier or refuses the row.
        sa.Column("metric_value", sa.Text(), nullable=True),
        sa.Column("metric_unit", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=False),
        # Where it was verified. NOT NULL on purpose: a claim nobody can trace
        # is exactly the thing this table exists to refuse.
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column(
            "confidentiality",
            claim_confidentiality,
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("verified_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        # Facts that decay: a test count or a corpus size is true on a date.
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="claim_pkey"),
        sa.UniqueConstraint("key", name="claim_key_key"),
    )
    op.create_index("claim_tags_idx", "claim", ["tags"], postgresql_using="gin")
    op.create_index(
        "claim_project_idx",
        "claim",
        ["project"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "claim_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        # No REFERENCES yet: `artifact` arrives with document generation in
        # Phase 3. Same deliberate deferral as `company.default_variant_id` in
        # 0001, and a later revision adds the clause rather than this one
        # inventing a table nothing writes.
        sa.Column(
            "artifact_id",
            sa.CHAR(26),
            nullable=False,
            comment="FK to artifact(id); constraint deferred until that table exists.",
        ),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="claim_usage_pkey"),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name="claim_usage_claim_id_fkey", ondelete="CASCADE"
        ),
        # The provenance trail: one row per number per place it appears, so a
        # generated document can be walked back to the rows that authorised it.
        sa.UniqueConstraint(
            "claim_id", "artifact_id", "location", name="claim_usage_unique_placement"
        ),
    )


def downgrade() -> None:
    """Drop both tables and the enum, in dependency order."""
    op.drop_table("claim_usage")
    op.drop_index("claim_project_idx", table_name="claim")
    op.drop_index("claim_tags_idx", table_name="claim")
    op.drop_table("claim")
    claim_confidentiality.drop(op.get_bind(), checkfirst=True)
