"""Retiring, deleting, listing and re-probing a source.

The distinction these tests exist to hold: ``retire`` takes a board out of
service and destroys nothing, ``rm`` cascades to every posting ever discovered
through it (DATA_MODEL.md §4.1). If those two ever behave alike, the wrong one is
the one that silently deletes history.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx

from scout_careers.common.errors import DeniedByPolicy
from scout_careers.common.logging import REDACTED
from scout_careers.common.types import AtsType
from scout_careers.registry.service import (
    RETIRED_STATUS,
    SourceNotFound,
    SourceNotProbeable,
    config_identity,
    count_source_postings,
    delete_source,
    open_posting_counts,
    probe_source,
    redact_config,
    retire_source,
    source_probe_url,
    sources_query,
)
from scout_careers.sources import registry as adapter_registry
from tests.conftest import make_settings
from tests.unit.registry.conftest import (
    DeniedHostAdapter,
    FakeSession,
    compiled,
    make_source,
)
from tests.unit.sources.conftest import ASHBY_HOST, load_json, mock_robots

# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------


async def test_retire_disables_without_deleting_anything() -> None:
    source = make_source(7, consecutive_failures=4)
    session = FakeSession(rows={7: source})

    retired = await retire_source(session, 7)

    assert retired.enabled is False
    assert retired.last_status == RETIRED_STATUS
    # The postings survive: nothing was deleted, and no DELETE was issued.
    assert session.deleted == []
    assert session.statements == []


async def test_retire_clears_the_failure_count_but_keeps_the_error() -> None:
    # The counter exists only to drive the auto-disable threshold, and a retired
    # source will not run again — leaving it set keeps a decided board in the
    # "needs attention" list forever. last_error is why it was retired.
    source = make_source(7, consecutive_failures=5, last_error="Board token not found")
    session = FakeSession(rows={7: source})

    retired = await retire_source(session, 7)

    assert retired.consecutive_failures == 0
    assert retired.last_error == "Board token not found"


async def test_retire_is_distinct_from_disable() -> None:
    # `disabled` means paused and expected back; `auto_disabled` means the
    # threshold fired; `retired` means a human decided this board is gone.
    session = FakeSession(rows={7: make_source(7)})
    assert (await retire_source(session, 7)).last_status == "retired"


async def test_retiring_a_source_that_does_not_exist_is_refused() -> None:
    with pytest.raises(SourceNotFound):
        await retire_source(FakeSession(), 404)


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


async def test_delete_reports_the_postings_it_destroyed() -> None:
    source = make_source(7)
    session = FakeSession(rows={7: source}, scalars=[214])

    deletion = await delete_source(session, 7)

    assert deletion.source_id == 7
    assert deletion.postings_deleted == 214
    assert session.deleted == [source]


async def test_delete_counts_closed_and_filtered_rows_too() -> None:
    # The cascade takes every job_posting row, not only the open ones, so the
    # number shown before the confirmation has to be the real one.
    session = FakeSession(rows={7: make_source(7)}, scalars=[9])
    await delete_source(session, 7)
    sql = compiled(session.statements[0])
    assert "closed_at" not in sql
    assert "filtered_out" not in sql
    assert "job_posting.source_id = 7" in sql


async def test_deleting_a_source_that_does_not_exist_is_refused() -> None:
    session = FakeSession()
    with pytest.raises(SourceNotFound):
        await delete_source(session, 404)
    assert session.deleted == []


async def test_count_source_postings_counts_that_source_only() -> None:
    session = FakeSession(scalars=[3])
    assert await count_source_postings(session, 42) == 3
    assert "job_posting.source_id = 42" in compiled(session.statements[0])


async def test_open_posting_counts_excludes_closed_and_filtered() -> None:
    session = FakeSession(select_rows=[[(1, 12), (2, 4)]])
    assert await open_posting_counts(session, [1, 2]) == {1: 12, 2: 4}
    sql = compiled(session.statements[0])
    assert "closed_at IS NULL" in sql
    assert "filtered_out IS false" in sql


async def test_open_posting_counts_for_no_sources_asks_nothing() -> None:
    session = FakeSession()
    assert await open_posting_counts(session, []) == {}
    assert session.statements == []


# ---------------------------------------------------------------------------
# list filters
# ---------------------------------------------------------------------------


def test_failing_selects_only_sources_with_consecutive_failures() -> None:
    assert "source.consecutive_failures > 0" in compiled(sources_query(failing=True))


def test_no_filter_selects_every_source() -> None:
    sql = compiled(sources_query())
    assert "WHERE" not in sql
    assert "ORDER BY source.id" in sql


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"company_id": 42}, "source.company_id = 42"),
        ({"adapter": AtsType.ASHBY}, "source.adapter = 'ashby'"),
        ({"last_status": "retired"}, "source.last_status = 'retired'"),
        ({"enabled": False}, "source.enabled IS false"),
    ],
)
def test_each_filter_lands_in_the_where_clause(kwargs: dict[str, Any], expected: str) -> None:
    assert expected in compiled(sources_query(**kwargs))


def test_filters_combine() -> None:
    sql = compiled(sources_query(company_id=42, failing=True, enabled=False))
    assert "source.company_id = 42" in sql
    assert "source.consecutive_failures > 0" in sql
    assert "source.enabled IS false" in sql


# ---------------------------------------------------------------------------
# what a config summary is allowed to show
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"board_token": "stripe"}, "board_token=stripe"),
        ({"site": "netflix"}, "site=netflix"),
        ({"board_name": "cursor", "include_compensation": True}, "board_name=cursor"),
        ({"include_compensation": True}, None),
        ({}, None),
    ],
)
def test_config_identity_is_the_value_that_moves(
    config: dict[str, Any], expected: str | None
) -> None:
    assert config_identity(config) == expected


def test_a_public_board_token_is_shown_in_full() -> None:
    # `board_token` matches the log scrubber's pattern and is nevertheless a
    # public identifier — it is in the URL the employer publishes. Masking it
    # would hide the one field the operator needs to compare.
    assert redact_config({"board_token": "stripe"}) == {"board_token": "stripe"}


def test_a_credential_shaped_key_is_masked() -> None:
    # No Phase 1 adapter stores one. This is what happens the day one does.
    redacted = redact_config({"board_name": "cursor", "api_key": "sk-live-1234"})
    assert redacted == {"api_key": REDACTED, "board_name": "cursor"}


def test_redaction_covers_the_scrubber_vocabulary() -> None:
    redacted = redact_config(
        {"authorization": "a", "cookie": "b", "secret": "c", "password": "d", "site": "netflix"}
    )
    assert redacted == {
        "authorization": REDACTED,
        "cookie": REDACTED,
        "password": REDACTED,
        "secret": REDACTED,
        "site": "netflix",
    }


# ---------------------------------------------------------------------------
# the probe target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "config", "expected"),
    [
        (
            AtsType.GREENHOUSE,
            {"board_token": "stripe"},
            "https://boards-api.greenhouse.io/v1/boards/stripe/jobs",
        ),
        (AtsType.LEVER, {"site": "netflix"}, "https://api.lever.co/v0/postings/netflix"),
        (
            AtsType.ASHBY,
            {"board_name": "cursor", "include_compensation": True},
            "https://api.ashbyhq.com/posting-api/job-board/cursor",
        ),
    ],
)
def test_the_probe_url_comes_from_the_adapter(
    adapter: AtsType, config: dict[str, Any], expected: str
) -> None:
    assert source_probe_url(adapter, config) == expected


@pytest.mark.parametrize("adapter", [AtsType.MAIL_ALERT, AtsType.MANUAL])
def test_an_adapter_that_fetches_no_board_has_no_probe_url(adapter: AtsType) -> None:
    assert source_probe_url(adapter, {"label": "job-alerts"}) is None


def test_a_config_missing_the_templated_field_has_no_probe_url() -> None:
    assert source_probe_url(AtsType.GREENHOUSE, {"site": "netflix"}) is None


async def test_probing_a_mail_alert_source_is_refused_rather_than_attempted() -> None:
    session = FakeSession(rows={3: make_source(3, adapter=AtsType.MAIL_ALERT, config={})})
    with pytest.raises(SourceNotProbeable):
        await probe_source(session, 3, settings=make_settings())


async def test_probing_a_source_that_does_not_exist_is_refused() -> None:
    with pytest.raises(SourceNotFound):
        await probe_source(FakeSession(), 404, settings=make_settings())


# ---------------------------------------------------------------------------
# the never-scrape gate
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_denied_host_is_refused_before_a_single_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(adapter_registry.ADAPTERS, AtsType.WORKDAY, DeniedHostAdapter)
    monkeypatch.setattr(DeniedHostAdapter, "probed", False)
    source = make_source(11, adapter=AtsType.WORKDAY, config={"host": "www.linkedin.com"})
    session = FakeSession(rows={11: source})

    with pytest.raises(DeniedByPolicy) as excinfo:
        await probe_source(session, 11, settings=make_settings())

    assert excinfo.value.host == "www.linkedin.com"
    # Nothing was fetched, and the adapter was never even constructed.
    assert respx.calls.call_count == 0
    assert DeniedHostAdapter.probed is False


@respx.mock
async def test_a_subdomain_of_a_denied_host_is_refused_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(adapter_registry.ADAPTERS, AtsType.WORKDAY, DeniedHostAdapter)
    monkeypatch.setattr(DeniedHostAdapter, "probed", False)
    session = FakeSession(
        rows={11: make_source(11, adapter=AtsType.WORKDAY, config={"host": "jobs.indeed.com"})}
    )

    with pytest.raises(DeniedByPolicy):
        await probe_source(session, 11, settings=make_settings())
    assert respx.calls.call_count == 0
    assert DeniedHostAdapter.probed is False


# ---------------------------------------------------------------------------
# a real probe, against a mocked board
# ---------------------------------------------------------------------------


@respx.mock
async def test_probe_reads_the_board_and_changes_nothing() -> None:
    import httpx

    mock_robots(ASHBY_HOST)
    respx.get(f"{ASHBY_HOST}/posting-api/job-board/cursor").mock(
        return_value=httpx.Response(200, json=load_json("ashby_openai.json"))
    )
    source = make_source(
        5, config={"board_name": "cursor", "include_compensation": True}, enabled=False
    )
    session = FakeSession(rows={5: source})

    probed = await probe_source(session, 5, settings=make_settings())

    assert probed.probe.reachable is True
    assert probed.url == "https://api.ashbyhq.com/posting-api/job-board/cursor"
    # Read-only: a reachable board does not re-enable a source the operator
    # retired, and does not clear its failure count. `source enable` does that.
    assert source.enabled is False
    assert source.consecutive_failures == 3
    assert session.flushes == 0
