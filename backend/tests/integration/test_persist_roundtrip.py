"""Insert, conflict, update — against a real Postgres.

The unit tests prove the classification rules. This proves the statements: that
``ON CONFLICT (source_id, external_id)`` actually finds the constraint, that
``first_seen_at`` survives an update, that the close rule's timestamp comparison
selects what it means to, and that a superseded row keeps its ``closed_at``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from scout_careers.common.clock import utcnow
from scout_careers.common.hashing import content_hash
from scout_careers.common.types import AtsType, CompanyStatus, CompanyTier, SourceStatus
from scout_careers.db.models import Company, JobPosting, Source
from scout_careers.ingest.close import close_missing
from scout_careers.ingest.dedup import SUPERSEDED_PREFIX, collapse_duplicates
from scout_careers.ingest.persist import persist_postings
from scout_careers.sources.base import RawPosting
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

SLUG = "scout-integration-fixture"
MAX_CHARS = 60_000


def posting(external_id: str, *, title: str, description: str) -> RawPosting:
    return RawPosting(
        external_id=external_id,
        url=f"https://boards.example.com/jobs/{external_id}",
        title=title,
        description_text=description,
        location_city="Bengaluru",
        location_country="IN",
        raw={"id": external_id},
    )


@pytest.fixture
async def fixture_company(session):
    """A throwaway company with two sources, removed afterwards."""
    await session.execute(delete(Company).where(Company.slug == SLUG))
    await session.commit()

    company = Company(
        slug=SLUG,
        name="Scout Integration Fixture",
        tier=CompanyTier.VOLUME,
        status=CompanyStatus.TRACKING,
        tags=["origin:seed"],
    )
    session.add(company)
    await session.flush()

    board = Source(
        company_id=company.id,
        adapter=AtsType.GREENHOUSE,
        config={"board_token": "scout-integration"},
    )
    alert = Source(
        company_id=company.id,
        adapter=AtsType.MAIL_ALERT,
        config={"label": "job-alerts", "lookback_hours": 26, "senders": []},
    )
    session.add_all([board, alert])
    await session.commit()

    yield company, board, alert

    await session.execute(delete(Company).where(Company.slug == SLUG))
    await session.commit()


async def test_insert_conflict_and_update_roundtrip(session, fixture_company) -> None:
    company, board, _alert = fixture_company
    first_run = utcnow()

    counts = await persist_postings(
        session,
        [
            posting("1", title="Senior Backend Engineer", description="Build services."),
            posting("2", title="Data Engineer", description="Build pipelines."),
        ],
        source_id=board.id,
        company_id=company.id,
        now=first_run,
        max_description_chars=MAX_CHARS,
    )
    await session.commit()

    assert (counts.new, counts.updated, counts.unchanged) == (2, 0, 0)

    rows = (
        (await session.execute(select(JobPosting).where(JobPosting.source_id == board.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    stored = {row.external_id: row for row in rows}
    assert stored["1"].content_hash == content_hash("Build services.")
    assert stored["1"].missed_runs == 0
    assert stored["1"].closed_at is None
    original_id = stored["1"].id
    original_first_seen = stored["1"].first_seen_at

    # -- second run: identical content ------------------------------------
    second_run = first_run + timedelta(hours=24)
    counts = await persist_postings(
        session,
        [
            posting("1", title="Senior Backend Engineer", description="Build services."),
            posting("2", title="Data Engineer", description="Build pipelines."),
        ],
        source_id=board.id,
        company_id=company.id,
        now=second_run,
        max_description_chars=MAX_CHARS,
    )
    await session.commit()

    assert (counts.new, counts.updated, counts.unchanged) == (0, 0, 2)
    refreshed = await session.get(JobPosting, original_id)
    await session.refresh(refreshed)
    assert refreshed.last_seen_at == second_run
    assert refreshed.first_seen_at == original_first_seen

    # -- third run: one description changed, one posting gone -------------
    third_run = second_run + timedelta(hours=24)
    counts = await persist_postings(
        session,
        [posting("1", title="Senior Backend Engineer", description="Build better services.")],
        source_id=board.id,
        company_id=company.id,
        now=third_run,
        max_description_chars=MAX_CHARS,
    )
    await session.commit()

    assert (counts.new, counts.updated, counts.unchanged) == (0, 1, 0)
    changed = await session.get(JobPosting, original_id)
    await session.refresh(changed)
    assert changed.content_hash == content_hash("Build better services.")
    # Identity is (source_id, external_id): an update is not a new row.
    assert changed.id == original_id
    assert changed.first_seen_at == original_first_seen


async def test_the_close_rule_needs_two_clean_runs(session, fixture_company) -> None:
    company, board, _alert = fixture_company
    first_run = utcnow()

    await persist_postings(
        session,
        [posting("1", title="Senior Backend Engineer", description="Build services.")],
        source_id=board.id,
        company_id=company.id,
        now=first_run,
        max_description_chars=MAX_CHARS,
    )
    await session.commit()

    # A run in which the source failed: the counter must not move.
    closed = await close_missing(
        session,
        source_id=board.id,
        status=SourceStatus.HTTP_ERROR,
        seen_at=first_run + timedelta(hours=24),
        threshold=2,
    )
    await session.commit()
    assert closed == 0

    row = (
        await session.execute(select(JobPosting).where(JobPosting.source_id == board.id))
    ).scalar_one()
    await session.refresh(row)
    assert row.missed_runs == 0

    # Two clean runs in which it is not listed.
    closed = await close_missing(
        session,
        source_id=board.id,
        status=SourceStatus.OK,
        seen_at=first_run + timedelta(hours=48),
        threshold=2,
    )
    await session.commit()
    assert closed == 0
    await session.refresh(row)
    assert row.missed_runs == 1
    assert row.closed_at is None

    closed = await close_missing(
        session,
        source_id=board.id,
        status=SourceStatus.OK,
        seen_at=first_run + timedelta(hours=72),
        threshold=2,
    )
    await session.commit()
    assert closed == 1
    await session.refresh(row)
    assert row.closed_at is not None


async def test_a_mail_stub_is_superseded_by_the_board_row(session, fixture_company) -> None:
    company, board, alert = fixture_company
    now = utcnow()

    await persist_postings(
        session,
        [posting("stub-1", title="Senior Backend Engineer", description="Seen in an alert.")],
        source_id=alert.id,
        company_id=company.id,
        now=now,
        max_description_chars=MAX_CHARS,
    )
    await persist_postings(
        session,
        [posting("1", title="Senior Backend Engineer", description="Build services.")],
        source_id=board.id,
        company_id=company.id,
        now=now + timedelta(minutes=1),
        max_description_chars=MAX_CHARS,
    )
    await session.commit()

    superseded = await collapse_duplicates(session, company_ids=[company.id])
    await session.commit()
    assert superseded == 1

    rows = {
        row.source_id: row
        for row in (
            await session.execute(select(JobPosting).where(JobPosting.company_id == company.id))
        )
        .scalars()
        .all()
    }
    stub = rows[alert.id]
    winner = rows[board.id]

    assert stub.filtered_out is True
    assert stub.filter_reason == f"{SUPERSEDED_PREFIX}{winner.id}"
    # Supersession is not closure. The stub is still an open posting.
    assert stub.closed_at is None
    assert winner.filtered_out is False
