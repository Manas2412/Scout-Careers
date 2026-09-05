"""Cross-source collapse: who wins, what happens to the loser, and what does not.

The "what does not" is the important half. A superseded record is marked, never
deleted, and its ``closed_at`` is never touched — supersession says which of two
records to read, closure says whether the role is still listed, and conflating
them makes a live role look dead the moment a better source finds it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Update

from scout_careers.common.types import AtsType
from scout_careers.ingest.dedup import (
    SUPERSEDED_PREFIX,
    DedupCandidate,
    apply_collapse,
    fidelity_rank,
    plan_collapse,
)
from tests.unit.ingest.conftest import FakeSession

EARLY = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2026, 9, 4, tzinfo=UTC)


def candidate(
    posting_id: str,
    *,
    adapter: AtsType,
    title: str = "Senior Backend Engineer",
    city: str | None = "Bengaluru",
    first_seen_at: datetime = EARLY,
    company_id: int = 1,
    filtered_out: bool = False,
    filter_reason: str | None = None,
) -> DedupCandidate:
    return DedupCandidate(
        id=posting_id,
        company_id=company_id,
        title=title,
        location_city=city,
        adapter=adapter,
        first_seen_at=first_seen_at,
        filtered_out=filtered_out,
        filter_reason=filter_reason,
    )


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def test_the_ranks_come_from_the_adapters() -> None:
    assert fidelity_rank(AtsType.ASHBY) == 90
    assert fidelity_rank(AtsType.GREENHOUSE) == 90
    assert fidelity_rank(AtsType.LEVER) == 88
    assert fidelity_rank(AtsType.MAIL_ALERT) == 20


def test_manual_outranks_every_adapter() -> None:
    # A human read it and pasted it; there is no class to ask, and there is not
    # meant to be.
    assert fidelity_rank(AtsType.MANUAL) == 95


def test_an_adapter_whose_module_has_not_shipped_still_ranks() -> None:
    assert fidelity_rank(AtsType.WORKDAY) == 85


def test_higher_fidelity_wins() -> None:
    plan = plan_collapse(
        [
            candidate("01STUB", adapter=AtsType.MAIL_ALERT),
            candidate("01BOARD", adapter=AtsType.GREENHOUSE, first_seen_at=LATER),
        ]
    )
    assert plan.supersede == {"01STUB": "01BOARD"}
    assert plan.restore == ()


def test_equal_ranks_break_on_the_earlier_first_seen_at() -> None:
    # ashby and greenhouse both sit at 90. The record we saw first wins, so
    # collapsing does not thrash when both boards list the same role.
    plan = plan_collapse(
        [
            candidate("01ASHBY", adapter=AtsType.ASHBY, first_seen_at=LATER),
            candidate("01GH", adapter=AtsType.GREENHOUSE, first_seen_at=EARLY),
        ]
    )
    assert plan.supersede == {"01ASHBY": "01GH"}

    reversed_dates = plan_collapse(
        [
            candidate("01ASHBY", adapter=AtsType.ASHBY, first_seen_at=EARLY),
            candidate("01GH", adapter=AtsType.GREENHOUSE, first_seen_at=LATER),
        ]
    )
    assert reversed_dates.supersede == {"01GH": "01ASHBY"}


def test_the_key_is_company_title_and_city() -> None:
    different_city = plan_collapse(
        [
            candidate("01A", adapter=AtsType.MAIL_ALERT, city="Bengaluru"),
            candidate("01B", adapter=AtsType.GREENHOUSE, city="Pune"),
        ]
    )
    assert different_city.supersede == {}

    different_company = plan_collapse(
        [
            candidate("01A", adapter=AtsType.MAIL_ALERT, company_id=1),
            candidate("01B", adapter=AtsType.GREENHOUSE, company_id=2),
        ]
    )
    assert different_company.supersede == {}


def test_titles_collapse_through_normalisation() -> None:
    plan = plan_collapse(
        [
            candidate("01STUB", adapter=AtsType.MAIL_ALERT, title="Sr. Software Engineer II"),
            candidate("01GH", adapter=AtsType.GREENHOUSE, title="Senior SDE 2 (Bengaluru)"),
        ]
    )
    assert plan.supersede == {"01STUB": "01GH"}


# --------------------------------------------------------------------------
# The mail-alert stub, which is the whole point of the 50-point gap
# --------------------------------------------------------------------------


def test_a_mail_alert_stub_is_superseded_the_moment_the_ats_row_appears() -> None:
    stub = candidate("01STUB", adapter=AtsType.MAIL_ALERT, first_seen_at=EARLY)

    alone = plan_collapse([stub])
    assert alone.supersede == {}

    with_board = plan_collapse(
        [stub, candidate("01GH", adapter=AtsType.GREENHOUSE, first_seen_at=LATER)]
    )
    assert with_board.supersede == {"01STUB": "01GH"}


def test_the_reason_names_the_winner_with_one_prefix() -> None:
    plan = plan_collapse(
        [
            candidate("01STUB", adapter=AtsType.MAIL_ALERT),
            candidate("01GH", adapter=AtsType.GREENHOUSE),
        ]
    )
    winner = plan.supersede["01STUB"]
    assert f"{SUPERSEDED_PREFIX}{winner}" == "superseded_by:01GH"


# --------------------------------------------------------------------------
# Reversibility
# --------------------------------------------------------------------------


def test_a_row_already_pointing_at_this_winner_is_left_alone() -> None:
    plan = plan_collapse(
        [
            candidate(
                "01STUB",
                adapter=AtsType.MAIL_ALERT,
                filtered_out=True,
                filter_reason="superseded_by:01GH",
            ),
            candidate("01GH", adapter=AtsType.GREENHOUSE),
        ]
    )
    assert plan.is_empty is True


def test_a_superseded_row_whose_winner_disappeared_is_restored() -> None:
    plan = plan_collapse(
        [
            candidate(
                "01STUB",
                adapter=AtsType.MAIL_ALERT,
                filtered_out=True,
                filter_reason="superseded_by:01GONE",
            )
        ]
    )
    assert plan.restore == ("01STUB",)
    assert plan.supersede == {}


def test_a_row_filtered_out_for_another_reason_is_not_restored() -> None:
    # Phase 2's filters own their own reasons; un-filtering them here would be
    # this module quietly overruling a stage it knows nothing about.
    plan = plan_collapse(
        [
            candidate(
                "01A",
                adapter=AtsType.GREENHOUSE,
                filtered_out=True,
                filter_reason="location_mismatch",
            )
        ]
    )
    assert plan.is_empty is True


def test_a_loser_repointed_at_a_new_winner_is_rewritten() -> None:
    plan = plan_collapse(
        [
            candidate(
                "01STUB",
                adapter=AtsType.MAIL_ALERT,
                filtered_out=True,
                filter_reason="superseded_by:01OLD",
            ),
            candidate("01GH", adapter=AtsType.GREENHOUSE),
        ]
    )
    assert plan.supersede == {"01STUB": "01GH"}


# --------------------------------------------------------------------------
# What the writes touch
# --------------------------------------------------------------------------


async def test_supersession_never_touches_closed_at() -> None:
    session = FakeSession()
    plan = plan_collapse(
        [
            candidate("01STUB", adapter=AtsType.MAIL_ALERT),
            candidate("01GH", adapter=AtsType.GREENHOUSE),
        ]
    )
    await apply_collapse(session, plan)

    updates = [str(stmt) for stmt in session.statements if isinstance(stmt, Update)]
    assert updates
    for text in updates:
        assert "filtered_out" in text
        # closed_at belongs to the two-run close rule alone.
        assert "closed_at" not in text


async def test_losers_are_marked_not_deleted() -> None:
    session = FakeSession()
    plan = plan_collapse(
        [
            candidate("01STUB", adapter=AtsType.MAIL_ALERT),
            candidate("01GH", adapter=AtsType.GREENHOUSE),
        ]
    )
    await apply_collapse(session, plan)

    assert all(isinstance(stmt, Update) for stmt in session.statements)
    assert not any("DELETE" in str(stmt).upper() for stmt in session.statements)
