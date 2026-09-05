"""Retire and delete, against a real Postgres.

The unit tests prove the service's decisions. This proves the two facts only the
database can: that ``job_posting.source_id ON DELETE CASCADE`` really does take
the postings with the source (DATA_MODEL.md §4.1), and that retiring the same
source really does leave every one of them in place. Those two statements are
the whole reason ``retire`` exists, and neither is checkable against a fake.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from scout_careers.common.hashing import content_hash
from scout_careers.common.types import AtsType, CompanyStatus, CompanyTier
from scout_careers.db.models import Company, JobPosting, Source
from scout_careers.registry.service import (
    RETIRED_STATUS,
    delete_source,
    list_sources,
    retire_source,
)
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

SLUG = "scout-source-admin-fixture"


async def _posting_count(session, source_id: int) -> int:
    total = await session.scalar(
        select(func.count()).select_from(JobPosting).where(JobPosting.source_id == source_id)
    )
    return int(total or 0)


@pytest.fixture
async def fixture_company(session):
    """A throwaway company with two sources and a posting on each."""
    await session.execute(delete(Company).where(Company.slug == SLUG))
    await session.commit()

    company = Company(
        slug=SLUG,
        name="Scout Source Admin Fixture",
        tier=CompanyTier.VOLUME,
        status=CompanyStatus.TRACKING,
        tags=["origin:seed"],
    )
    session.add(company)
    await session.flush()

    moved = Source(
        company_id=company.id,
        adapter=AtsType.ASHBY,
        config={"board_name": "anysphere", "include_compensation": True},
        consecutive_failures=4,
        last_status="http_error",
        last_error="Board token not found",
    )
    current = Source(
        company_id=company.id,
        adapter=AtsType.ASHBY,
        config={"board_name": "cursor", "include_compensation": True},
    )
    session.add_all([moved, current])
    await session.flush()

    for source, external in ((moved, "old-1"), (current, "new-1")):
        session.add(
            JobPosting(
                id=f"01SOURCEADMIN{external.replace('-', '')}".ljust(26, "0")[:26],
                company_id=company.id,
                source_id=source.id,
                external_id=external,
                title="Senior Backend Engineer",
                description_text="Build and run distributed services.",
                url=f"https://jobs.ashbyhq.com/x/{external}",
                content_hash=content_hash("Build and run distributed services."),
            )
        )
    await session.commit()

    yield company, moved, current

    await session.execute(delete(Company).where(Company.slug == SLUG))
    await session.commit()


async def test_retire_keeps_every_posting(session, fixture_company) -> None:
    _company, moved, _current = fixture_company

    retired = await retire_source(session, moved.id)
    await session.commit()

    assert retired.enabled is False
    assert retired.last_status == RETIRED_STATUS
    assert retired.consecutive_failures == 0
    assert await _posting_count(session, moved.id) == 1


async def test_delete_cascades_to_the_postings(session, fixture_company) -> None:
    _company, moved, current = fixture_company

    deletion = await delete_source(session, moved.id)
    await session.commit()

    assert deletion.postings_deleted == 1
    assert await _posting_count(session, moved.id) == 0
    # The sibling board is untouched: the cascade is scoped to one source.
    assert await _posting_count(session, current.id) == 1


async def test_failing_filter_selects_the_source_that_is_failing(session, fixture_company) -> None:
    company, moved, _current = fixture_company

    failing = await list_sources(session, company_id=company.id, failing=True)

    assert [source.id for source in failing] == [moved.id]
