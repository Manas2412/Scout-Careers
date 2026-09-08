"""Stage ④ against the stored flags: what gets written, and what must not be.

``filters.py`` decides; this decides *whether that verdict needs persisting*.
The interesting rules are all about not writing: an unchanged verdict costs a
round trip for nothing, and a superseded row rewritten costs dedup its ability
to heal.
"""

from __future__ import annotations

from scout_careers.common.types import CompanyStatus
from scout_careers.ingest.dedup import SUPERSEDED_PREFIX
from scout_careers.ingest.filters import CompanyView, PostingView
from scout_careers.ingest.screen import (
    ScreenChange,
    apply_screen,
    judge,
    reason_families,
)
from tests.conftest import make_settings

JD = "We are hiring a backend engineer. " * 40


def posting(**overrides: object) -> PostingView:
    values: dict[str, object] = {
        "id": "01POSTING",
        "title": "Backend Engineer",
        "description_text": JD,
        "location_city": "Bengaluru",
        "location_country": "IN",
        "is_remote": False,
        "seniority_guess": "mid",
        "closed_at": None,
        "filtered_out": False,
        "filter_reason": None,
        "raw": {},
    }
    values.update(overrides)
    return PostingView(**values)  # type: ignore[arg-type]


def company(**overrides: object) -> CompanyView:
    values: dict[str, object] = {
        "id": 1,
        "slug": "acme",
        "status": CompanyStatus.TRACKING,
        "location_filter": (),
    }
    values.update(overrides)
    return CompanyView(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The rule that protects another stage
# --------------------------------------------------------------------------


def test_a_superseded_row_is_never_rewritten() -> None:
    """`dedup.py` stores `superseded_by:<winner id>`, and that ID is load-bearing.

    `plan_collapse` finds those rows by prefix to restore them when the winner
    disappears. Rewriting the reason to a bare `superseded` would erase the
    pointer, and the damage would be invisible — postings that simply stay
    hidden after their duplicate is gone.
    """
    stored = f"{SUPERSEDED_PREFIX}01WINNER"
    judgement = judge(
        posting(filtered_out=True, filter_reason=stored),
        company(),
        make_settings(),
    )
    assert judgement.change is None, "the row must be left exactly as dedup wrote it"
    assert judgement.skipped is True
    assert not judgement.passed


# --------------------------------------------------------------------------
# Writing only what changed
# --------------------------------------------------------------------------


def test_a_passing_posting_that_was_never_hidden_is_not_written() -> None:
    assert judge(posting(), company(), make_settings()).change is None


def test_an_unchanged_rejection_is_not_rewritten() -> None:
    """Idempotence. A second `filter apply` over an unchanged corpus must be a
    read, not seven thousand identical UPDATEs."""
    judgement = judge(
        posting(filtered_out=True, filter_reason="company_blacklisted"),
        company(status=CompanyStatus.BLACKLISTED),
        make_settings(),
    )
    assert judgement.reason == "company_blacklisted"
    assert judgement.change is None


def test_a_changed_reason_is_rewritten() -> None:
    """The reason is the only explanation the operator gets for a role they
    never saw, so a stale one is worse than none."""
    judgement = judge(
        posting(filtered_out=True, filter_reason="location:remote_only"),
        company(status=CompanyStatus.BLACKLISTED),
        make_settings(),
    )
    assert judgement.change == ScreenChange("01POSTING", True, "company_blacklisted")


def test_a_newly_rejected_posting_is_hidden() -> None:
    judgement = judge(posting(), company(status=CompanyStatus.BLACKLISTED), make_settings())
    assert judgement.change is not None
    assert judgement.change.filtered_out is True


def test_a_posting_that_now_passes_is_released() -> None:
    """The property that makes the deny lists safe to edit.

    Widening a list releases what it was hiding, rather than requiring someone
    to remember which rows a previous setting wrote.
    """
    judgement = judge(
        posting(filtered_out=True, filter_reason="seniority:director"),
        company(),
        make_settings(),
    )
    assert judgement.passed
    assert judgement.change == ScreenChange("01POSTING", False, None)


def test_releasing_clears_the_reason_too() -> None:
    """A visible posting carrying the reason it used to be hidden for would
    make `filter status` report a rejection that is not in effect."""
    judgement = judge(
        posting(filtered_out=True, filter_reason="location:excluded"),
        company(),
        make_settings(),
    )
    assert judgement.change is not None
    assert judgement.change.filter_reason is None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_reason_families_collapse_onto_the_predicate() -> None:
    """The exact reason explains one posting; the family shows the filter's shape.

    A chain removing 78% where 74 points come from one predicate is one rule
    with seven decorations, and only the grouped view says so.
    """
    families = reason_families(
        {"seniority:director": 10, "seniority:intern": 5, "location:excluded": 3}
    )
    assert families == {"seniority": 15, "location": 3}


# --------------------------------------------------------------------------
# The write
# --------------------------------------------------------------------------


class FakeSession:
    """Records the statements `apply_screen` issues, without a database."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object) -> None:
        self.statements.append(statement)


async def test_no_changes_issues_no_statements() -> None:
    session = FakeSession()
    assert await apply_screen(session, []) == 0  # type: ignore[arg-type]
    assert session.statements == []


async def test_writes_are_grouped_by_verdict_not_issued_per_row() -> None:
    """A recompute over the corpus is thousands of rows taking a handful of
    distinct values. One statement per value is the shape that fits."""
    session = FakeSession()
    changes = [ScreenChange(f"posting-{n}", True, "seniority:director") for n in range(50)]
    changes += [ScreenChange(f"other-{n}", False, None) for n in range(30)]

    assert await apply_screen(session, changes) == 80  # type: ignore[arg-type]
    assert len(session.statements) == 2, "one UPDATE per distinct verdict"


async def test_a_large_group_is_split_into_batches() -> None:
    """7,000 bound parameters in one statement is a different failure mode
    from a slow one."""
    from scout_careers.ingest.screen import BATCH_SIZE

    session = FakeSession()
    changes = [ScreenChange(f"p-{n}", True, "location:excluded") for n in range(BATCH_SIZE * 2 + 1)]

    assert await apply_screen(session, changes) == len(changes)  # type: ignore[arg-type]
    assert len(session.statements) == 3
