"""Phase 2 scoring: requirement, resume_variant, match_score, and two enums.

Only the tables the extraction-and-scoring slice actually reads or writes.
`claim`, `claim_usage`, `review_item`, `application`, `artifact` and
`email_message` are all specified in DATA_MODEL.md and all deliberately absent:
ROADMAP.md §3.2 defers the claims ledger, document generation and the review
queue out of Phase 2 entirely, and a table with no code that writes it is a
schema the operator can be misled by.

`coverage_level` ships even though no column has that type. `match_score.gaps`
is JSONB carrying `{requirement_id, kind, text, level, note}`, and `level` is a
coverage_level value — so the type is the vocabulary those documents are
validated against, and creating it here keeps DATA_MODEL.md §2 true in one
place. Postgres can add an enum value but never remove one, so the roster is
created once, correctly, exactly as `ats_type` was in 0001.

This revision also completes something 0001 left open: `company.default_variant_id`
shipped as a bare BIGINT with a comment saying the REFERENCES clause would
arrive with the table it points at. It arrives here.

Revision ID: 0002_phase2_scoring
Revises: 0001_phase1_core
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_phase2_scoring"
down_revision: str | None = "0001_phase1_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REQUIREMENT_KIND_VALUES = ("hard", "nice", "responsibility", "tool")
COVERAGE_LEVEL_VALUES = ("met", "partial", "missing")

requirement_kind = postgresql.ENUM(
    *REQUIREMENT_KIND_VALUES, name="requirement_kind", create_type=False
)
coverage_level = postgresql.ENUM(*COVERAGE_LEVEL_VALUES, name="coverage_level", create_type=False)


def _timestamptz(
    name: str, *, nullable: bool = True, default_now: bool = False
) -> sa.Column[object]:
    """One spelling of a timestamp column, since this revision writes eleven."""
    return sa.Column(
        name,
        postgresql.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default_now else None,
    )


def upgrade() -> None:
    bind = op.get_bind()

    for enum_type in (requirement_kind, coverage_level):
        enum_type.create(bind, checkfirst=True)

    # --- resume_variant ----------------------------------------------------
    # Created before `requirement` because `match_score` references both and
    # `company.default_variant_id` references this one.
    op.create_table(
        "resume_variant",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        # Mirrors the structure the .docx builder already consumes — summary,
        # skill lines, experience blocks with bullets, projects, achievements —
        # so a variant renders without a translation layer.
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column(
            "skill_set",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        _timestamptz("deleted_at"),
        _timestamptz("created_at", nullable=False, default_now=True),
        _timestamptz("updated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="resume_variant_pkey"),
        sa.UniqueConstraint("key", name="resume_variant_key_key"),
    )
    # `skill_set` is what `requirement.normalised_skill` is matched against, one
    # posting against six variants on every scoring pass. GIN makes containment
    # an index lookup rather than six array scans per posting.
    op.create_index(
        "resume_variant_skills_idx", "resume_variant", ["skill_set"], postgresql_using="gin"
    )

    # The forward reference 0001 could not express.
    op.create_foreign_key(
        "company_default_variant_id_fkey",
        "company",
        "resume_variant",
        ["default_variant_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- requirement -------------------------------------------------------
    op.create_table(
        "requirement",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("posting_id", sa.CHAR(26), nullable=False),
        sa.Column("kind", requirement_kind, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        # Free text mapped to the controlled vocabulary ("Strong C/C++ skills"
        # -> `cpp`) so coverage is computed without a second model call.
        sa.Column("normalised_skill", sa.Text(), nullable=True),
        sa.Column("weight", sa.Numeric(3, 2), nullable=False, server_default=sa.text("1.00")),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _timestamptz("extracted_at", nullable=False, default_now=True),
        # Provenance, not decoration: invariant 7 is that no artifact exists
        # whose model cannot be named, and these two columns are how a
        # requirement answers that question years later.
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="requirement_pkey"),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["job_posting.id"],
            name="requirement_posting_id_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index("requirement_posting_idx", "requirement", ["posting_id", "kind"])
    op.create_index("requirement_skill_idx", "requirement", ["normalised_skill"])

    # --- match_score -------------------------------------------------------
    op.create_table(
        "match_score",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("posting_id", sa.CHAR(26), nullable=False),
        sa.Column("variant_id", sa.BigInteger(), nullable=False),
        sa.Column("hard_met", sa.Integer(), nullable=False),
        sa.Column("hard_total", sa.Integer(), nullable=False),
        sa.Column("nice_met", sa.Integer(), nullable=False),
        sa.Column("nice_total", sa.Integer(), nullable=False),
        sa.Column("coverage_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("composite_score", sa.Numeric(5, 2), nullable=False),
        # [{requirement_id, kind, text, level, note}] — what the UI shows as
        # "missing: RTOS, device drivers" and what an honest-gap paragraph is
        # written from. `level` is a coverage_level value.
        sa.Column("gaps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        # Links each met requirement to the variant bullet that satisfies it, so
        # a coverage claim is inspectable rather than asserted.
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("is_recommended", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        _timestamptz("scored_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="match_score_pkey"),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["job_posting.id"],
            name="match_score_posting_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["resume_variant.id"],
            name="match_score_variant_id_fkey",
            ondelete="CASCADE",
        ),
        # prompt_version is in the key so a re-score under a new prompt inserts
        # beside the old row rather than destroying the comparison that shows
        # whether the new prompt is better.
        sa.UniqueConstraint(
            "posting_id", "variant_id", "prompt_version", name="match_score_posting_variant_key"
        ),
    )
    op.create_index(
        "match_posting_score_idx",
        "match_score",
        ["posting_id", sa.text("composite_score DESC")],
    )
    op.create_index(
        "match_recommended_idx",
        "match_score",
        [sa.text("composite_score DESC")],
        postgresql_where=sa.text("is_recommended"),
    )

    # Gate 2.8 in the schema rather than in application code: exactly one row
    # per posting may be the recommendation. A partial unique index is the
    # cheapest correct expression of "at most one true per posting" — and it
    # makes a scoring bug a failed INSERT rather than a digest that quietly
    # recommends two variants for the same job.
    op.execute(
        "CREATE UNIQUE INDEX match_one_recommendation_idx "
        "ON match_score (posting_id) WHERE is_recommended"
    )

    op.execute(
        "CREATE TRIGGER trg_resume_variant_touch_updated_at "
        "BEFORE UPDATE ON resume_variant "
        "FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_resume_variant_touch_updated_at ON resume_variant")
    op.drop_table("match_score")
    op.drop_table("requirement")
    # Dropped before the table it points at, or the drop is refused.
    op.drop_constraint("company_default_variant_id_fkey", "company", type_="foreignkey")
    op.drop_table("resume_variant")

    bind = op.get_bind()
    for enum_type in (coverage_level, requirement_kind):
        enum_type.drop(bind, checkfirst=True)
