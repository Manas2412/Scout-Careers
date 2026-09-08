"""Add ``condition`` to ``requirement_kind``.

The first real extractions stored lines like "required location in European time
zones", "full-time position with 4 10-hour shifts covering weekends" and "this
position covers Friday to Monday" as ``hard`` requirements at weight 1.00 — four
of nine in one posting.

Coverage scoring weighs ``hard`` and ``nice``, so every one of those would have
counted as an unmet gap against a resume variant. A posting's rank would then
have partly measured how many scheduling sentences its employer chose to write,
which is noise wearing the costume of signal, and it gets worse the more verbose
the advert.

They are facts about the shape of the job, not demands on a candidate. They are
kept rather than dropped — "weekend shifts, Europe only" is exactly what the
operator wants to know before applying — but they must not be scored, and until
now the enum had no way to say so.

**No backfill.** Rows already written under
``requirement_extraction@2026-09-06.1`` keep their ``hard`` kind. Re-classifying
them here would mean guessing from free text what the next extraction pass will
be told to decide properly, and ``prompt_version`` exists precisely so the two
generations can be told apart. The prompt bump that teaches the model this kind
supersedes them.

Revision ID: 0003_requirement_condition
Revises: 0002_phase2_scoring
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_requirement_condition"
down_revision: str | None = "0002_phase2_scoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_VALUE = "condition"
ENUM_NAME = "requirement_kind"


def upgrade() -> None:
    """Add the value.

    ``ADD VALUE`` needs its own connection outside the migration's transaction
    on Postgres before 12, and even on 12+ the new label cannot be *used* in the
    transaction that adds it. ``autocommit_block`` sidesteps both, at the cost
    of this revision not being atomic — acceptable, because it is a single
    idempotent statement with nothing to roll back alongside it.
    """
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    """Deliberately a no-op.

    Postgres cannot remove an enum value. Doing it properly means creating a
    replacement type, rewriting every column that uses it, and first deciding
    what happens to the rows already holding ``condition`` — which is a data
    decision, not a schema one, and not something a downgrade should make
    silently at 02:00.

    Leaving the value in place is harmless: nothing is required to emit it, and
    an unused enum member costs nothing. The alternative — a downgrade that
    quietly rewrites a table and reassigns rows — is how a rollback becomes the
    outage.
    """
