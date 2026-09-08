"""Store the model's ``normalised_skill_hint`` on ``requirement``.

Extraction returns an advisory hint alongside each requirement, and code makes
the final assignment by resolving it through the controlled vocabulary. Until
now the hint was used and thrown away.

That made vocabulary iteration cost a full re-extraction. A ``skills.yaml``
change only affects ``normalised_skill`` — the requirement *text* is unchanged —
so recomputing the token needs no model call at all. But
:meth:`Vocabulary.resolve_with_hint` falls back to the hint whenever the phrase
itself does not resolve, and that fallback carries a large share of the hits:
"Proficient in SQL (ideally PostgreSQL)" is not an alias of anything and reaches
``sql`` only through the hint. Re-resolving without it would have quietly
*lowered* coverage on every row that depended on it.

With the hint stored, `scout-careers extract reresolve` recomputes the whole
corpus for nothing, and the unresolved-phrase queue becomes something the
operator can actually act on rather than a report that costs ₹2,900 to respond
to.

Nullable, and no backfill: rows written before this revision have no hint to
recover — the value existed only in a response that was never persisted. They
keep the ``normalised_skill`` they were given, and re-resolving them can only
use the phrase. `prompt_version` already distinguishes them.

Revision ID: 0004_requirement_skill_hint
Revises: 0003_requirement_condition
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_requirement_skill_hint"
down_revision: str | None = "0003_requirement_condition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column."""
    op.add_column(
        "requirement",
        sa.Column(
            "normalised_skill_hint",
            sa.Text(),
            nullable=True,
            comment=(
                "The model's advisory skill name, kept so `normalised_skill` can be "
                "recomputed against a new vocabulary without a model call."
            ),
        ),
    )


def downgrade() -> None:
    """Drop it.

    Safe, unlike the enum in 0003: dropping a nullable column that nothing
    references loses the hints but no requirement and no score. Re-resolution
    would fall back to the phrase alone until the next extraction pass.
    """
    op.drop_column("requirement", "normalised_skill_hint")
