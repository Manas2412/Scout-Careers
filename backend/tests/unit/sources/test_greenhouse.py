"""GreenhouseAdapter — the six required tests (SOURCE_ADAPTERS.md §12).

The caveat this file exists to pin is the entity escaping: if ``html.unescape``
ever stops running before the parse, `test_fetch_maps_fields` fails on the very
first assertion about description text rather than shipping a board full of
``&lt;p&gt;``.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime

import httpx
import pytest
import respx

from scout_careers.common.errors import AdapterConfigError, SchemaDriftError, UpstreamHttpError
from scout_careers.common.types import AtsType
from scout_careers.sources.base import GreenhouseConfig, RawPosting
from scout_careers.sources.greenhouse import GreenhouseAdapter
from scout_careers.sources.http import build_client
from scout_careers.sources.policy import assert_fetch_allowed
from tests.unit.sources.conftest import (
    GREENHOUSE_HOST,
    load_json,
    make_http,
    mock_robots,
)

BOARD = "stripe"
JOBS_URL = f"{GREENHOUSE_HOST}/v1/boards/{BOARD}/jobs"


def build_adapter(settings, client: httpx.AsyncClient) -> GreenhouseAdapter:
    return GreenhouseAdapter(
        source_id=11,
        config=GreenhouseConfig(board_token=BOARD),
        http=make_http(
            settings,
            client,
            adapter=AtsType.GREENHOUSE,
            bucket_key="boards-api.greenhouse.io",
            source_id=11,
        ),
    )


async def collect(adapter: GreenhouseAdapter) -> list[RawPosting]:
    return [posting async for posting in adapter.fetch()]


# --------------------------------------------------------------------------
# 1. parse_config
# --------------------------------------------------------------------------


def test_parse_config_accepts_a_valid_board_token() -> None:
    config = GreenhouseAdapter.parse_config({"board_token": "stripe"})
    assert config == GreenhouseConfig(board_token="stripe")


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"board_token": ""},
        {"board_token": "Stripe"},  # upper case is not the board token shape
        {"board_token": "stripe/../evil"},  # path traversal into another host's URL
        {"board_token": "stripe", "base_url": "https://evil.example"},  # extra=forbid
        {"board_token": "-leading-hyphen"},
    ],
)
def test_parse_config_rejects_anything_that_could_move_the_host(raw: dict[str, object]) -> None:
    with pytest.raises(AdapterConfigError):
        GreenhouseAdapter.parse_config(raw)


def test_parse_config_round_trips_canonically() -> None:
    # UNIQUE (company_id, adapter, config) depends on this: the same config must
    # serialise to the same JSON every time it is stored.
    config = GreenhouseAdapter.parse_config({"board_token": "stripe"})
    again = GreenhouseAdapter.parse_config(config.model_dump())
    assert again == config
    assert config.model_dump_json() == again.model_dump_json() == '{"board_token":"stripe"}'


# --------------------------------------------------------------------------
# 2. probe
# --------------------------------------------------------------------------


@respx.mock
async def test_probe_reports_a_reachable_board(settings) -> None:
    mock_robots(GREENHOUSE_HOST)
    respx.get(JOBS_URL).mock(
        return_value=httpx.Response(200, json=load_json("greenhouse_stripe.json"))
    )

    async with build_client(settings) as client:
        result = await build_adapter(settings, client).probe()

    assert result.reachable is True
    assert result.sample_count == 4
    assert result.http_status == 200
    assert result.company_name_guess == "Stripe"
    assert result.latency_ms >= 0


@respx.mock
async def test_probe_reports_a_404_without_echoing_the_body(settings) -> None:
    mock_robots(GREENHOUSE_HOST)
    body = load_json("greenhouse_404.json")
    route = respx.get(JOBS_URL).mock(return_value=httpx.Response(404, json=body))

    async with build_client(settings) as client:
        result = await build_adapter(settings, client).probe()

    assert result.reachable is False
    assert result.http_status == 404
    assert result.detail == "Board not found — check the identifier in this source's config"
    # detail is rendered in the UI, so the upstream body must not reach it.
    assert "Not found" not in (result.detail or "")
    assert route.call_count == 1  # a 404 is a detection failure, never retried


# --------------------------------------------------------------------------
# 3. field mapping
# --------------------------------------------------------------------------


@respx.mock
async def test_fetch_maps_fields(settings) -> None:
    mock_robots(GREENHOUSE_HOST)
    respx.get(JOBS_URL).mock(
        return_value=httpx.Response(200, json=load_json("greenhouse_stripe.json"))
    )

    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        postings = await collect(adapter)

    # The fourth fixture job has an empty description and is dropped: an empty
    # JD cannot be extracted or scored and would burn a filter slot.
    assert [p.external_id for p in postings] == ["6789012", "6789013", "6789014"]
    assert adapter.skipped == {"no_description": 1}

    first = postings[0]
    assert str(first.url) == "https://boards.greenhouse.io/stripe/jobs/6789012"
    assert first.title == "Software Engineer, Payments Infrastructure"

    # THE caveat: content is entity-escaped upstream. One unescape, before the
    # parse — not zero, and not two.
    assert first.description_html is not None
    assert first.description_html.startswith("<p>Stripe builds the economic infrastructure")
    assert "&lt;p&gt;" not in first.description_html
    assert "<p>" not in first.description_text
    assert "&lt;" not in first.description_text
    assert "Stripe builds the economic infrastructure" in first.description_text
    # The requirements survive as bullets, which is what the extractor reads.
    assert "• 5+ years of backend engineering experience." in first.description_text
    # A single unescape, so an entity the recruiter typed is decoded exactly once.
    assert "what you'll do" in first.description_text.lower()
    assert "Postgres & MySQL" in first.description_text
    # <script> is dropped before text extraction: its content is
    # attacker-controllable and must never reach a prompt.
    assert "window.__gh" not in first.description_text

    # The deepest department wins; the root is usually just "Engineering".
    assert first.department == "Payments Infrastructure"
    assert first.location_raw == "Bengaluru, India"
    assert first.location_city == "Bengaluru"
    assert first.location_country == "IN"
    assert first.is_remote is False
    assert first.employment_type == "full_time"
    assert first.seniority_guess == "mid"
    # posted_at comes from updated_at — the documented drift caveat — converted
    # to UTC from the board's -04:00 offset.
    assert first.posted_at == datetime(2026, 8, 29, 15, 4, 33, tzinfo=UTC)
    assert first.raw == {
        "id": 6789012,
        # Identity is the board post id, never internal_job_id: that is the
        # requisition, shared across posts for one multi-location role.
        "internal_job_id": 5544332,
        "requisition_id": "JR-2026-4471",
        "updated_at": "2026-08-29T11:04:33-04:00",
        "departments": [
            {"id": 41, "name": "Engineering", "parent_id": None},
            {"id": 98, "name": "Payments Infrastructure", "parent_id": 41},
        ],
        "offices": [{"id": 9, "name": "Bengaluru", "parent_id": 1, "location": "Bengaluru, India"}],
        "metadata": [{"name": "Employment Type", "value": "Full time"}],
    }

    remote = postings[1]
    assert remote.external_id == "6789013"
    assert remote.is_remote is True
    assert remote.location_city is None
    assert remote.location_country == "IN"
    assert remote.seniority_guess == "senior"
    assert remote.posted_at == datetime(2026, 8, 31, 9, 15, tzinfo=UTC)

    # Two fixture jobs share internal_job_id 5544332 and have different ids.
    # That is exactly why identity is `id`.
    assert first.raw["internal_job_id"] == remote.raw["internal_job_id"]
    assert first.external_id != remote.external_id

    third = postings[2]
    # No updated_at at all: posted_at is None rather than fabricated as now().
    assert third.posted_at is None
    assert third.employment_type == "contract"
    assert third.location_city == "Dublin"
    assert third.location_country == "IE"
    # A department whose only child has no name falls back to the named root.
    assert third.department == "Operations"


# --------------------------------------------------------------------------
# 4. pagination
# --------------------------------------------------------------------------


@respx.mock
async def test_pagination_is_a_single_request(settings) -> None:
    mock_robots(GREENHOUSE_HOST)
    route = respx.get(JOBS_URL).mock(
        return_value=httpx.Response(200, json=load_json("greenhouse_stripe.json"))
    )

    async with build_client(settings) as client:
        await collect(build_adapter(settings, client))

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.params["content"] == "true"
    # The whole board arrives in one response; there is no cursor to follow and
    # therefore no loop that could fail to terminate.
    assert "page" not in request.url.params
    assert "limit" not in request.url.params


# --------------------------------------------------------------------------
# 5. partial failure
# --------------------------------------------------------------------------


@respx.mock
async def test_a_source_cancelled_at_the_ceiling_yields_nothing(settings) -> None:
    # §4.4: a source cancelled by the 180 s ceiling has whatever it had already
    # yielded discarded. Partial ingestion would look like "everything else
    # closed" to the two-run closed_at rule and would close live postings.
    mock_robots(GREENHOUSE_HOST)
    never_returns = asyncio.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        await never_returns.wait()
        return httpx.Response(200, json={"jobs": []})

    respx.get(JOBS_URL).mock(side_effect=hang)

    emitted: list[RawPosting] = []

    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)

        async def drain() -> None:
            async for posting in adapter.fetch():
                emitted.append(posting)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert emitted == []


@respx.mock
async def test_a_500_after_the_retry_budget_yields_nothing(settings) -> None:
    mock_robots(GREENHOUSE_HOST)
    respx.get(JOBS_URL).mock(return_value=httpx.Response(500))

    emitted: list[RawPosting] = []
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        with pytest.raises(UpstreamHttpError):
            async for posting in adapter.fetch():
                emitted.append(posting)

    assert emitted == []


# --------------------------------------------------------------------------
# 6. schema drift
# --------------------------------------------------------------------------


@respx.mock
async def test_schema_drift_raises_and_yields_nothing(settings) -> None:
    mock_robots(GREENHOUSE_HOST)
    drifted = copy.deepcopy(load_json("greenhouse_stripe.json"))
    del drifted["jobs"][1]["title"]  # the vendor dropped a field we depend on
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=drifted))

    emitted: list[RawPosting] = []
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        with pytest.raises(SchemaDriftError) as excinfo:
            async for posting in adapter.fetch():
                emitted.append(posting)

    assert excinfo.value.error_code == "adapter.schema_drift"
    # The page is validated whole, so the *first* job — which was fine — is not
    # yielded either. A half-populated board is worse than a failed one.
    assert emitted == []
    assert "boards-api.greenhouse.io/v1/boards/{board_token}/jobs" in str(excinfo.value)
    # The message names the field, never the upstream value.
    assert "title" in str(excinfo.value)
    assert BOARD not in str(excinfo.value)


@respx.mock
async def test_a_non_jobs_envelope_is_drift_not_an_empty_board(settings) -> None:
    mock_robots(GREENHOUSE_HOST)
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    async with build_client(settings) as client:
        with pytest.raises(SchemaDriftError):
            await collect(build_adapter(settings, client))


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


async def test_every_url_the_adapter_can_construct_is_allowed(settings) -> None:
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        # boards-api.greenhouse.io is not on NEVER_FETCH_HOSTS, and the config
        # pattern is what stops a board token from moving the host.
        assert_fetch_allowed(adapter._url())
        assert adapter.describe() == "Greenhouse · stripe"
    assert GreenhouseAdapter.fidelity_rank == 90
    assert GreenhouseAdapter.default_poll_interval_minutes == 1440
    assert GreenhouseAdapter.requires_detail_fetch is False
