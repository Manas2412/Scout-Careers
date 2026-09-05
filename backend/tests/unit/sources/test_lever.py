"""LeverAdapter — the six required tests (SOURCE_ADAPTERS.md §12).

Two caveats carry this file. First, the response is a bare array with no
envelope. Second, and more expensively, the requirements live in ``lists`` —
an adapter that maps only ``descriptionPlain`` scores every Lever posting near
zero, and the assertions below are what stop that from being reintroduced
quietly.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime

import httpx
import pytest
import respx

from scout_careers.common.errors import (
    AdapterConfigError,
    SchemaDriftError,
    UpstreamHttpError,
)
from scout_careers.common.types import AtsType
from scout_careers.sources.base import LeverConfig, RawPosting
from scout_careers.sources.http import build_client
from scout_careers.sources.lever import LeverAdapter
from scout_careers.sources.policy import assert_fetch_allowed
from tests.unit.sources.conftest import LEVER_HOST, load_json, make_http, mock_robots

SITE = "netflix"
POSTINGS_URL = f"{LEVER_HOST}/v0/postings/{SITE}"


def build_adapter(settings, client: httpx.AsyncClient, *, limit: int | None = None) -> LeverAdapter:
    return LeverAdapter(
        source_id=21,
        config=LeverConfig(site=SITE),
        http=make_http(
            settings, client, adapter=AtsType.LEVER, bucket_key="api.lever.co", source_id=21
        ),
        limit=limit,
    )


async def collect(adapter: LeverAdapter) -> list[RawPosting]:
    return [posting async for posting in adapter.fetch()]


# --------------------------------------------------------------------------
# 1. parse_config
# --------------------------------------------------------------------------


def test_parse_config_accepts_a_valid_site() -> None:
    assert LeverAdapter.parse_config({"site": "netflix"}) == LeverConfig(site="netflix")


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"site": ""},
        {"site": "Netflix"},
        {"site": "netflix?mode=json"},
        {"site": "netflix", "host": "https://evil.example"},
        {"site": "../netflix"},
    ],
)
def test_parse_config_rejects_anything_that_could_move_the_host(raw: dict[str, object]) -> None:
    with pytest.raises(AdapterConfigError):
        LeverAdapter.parse_config(raw)


def test_parse_config_round_trips_canonically() -> None:
    config = LeverAdapter.parse_config({"site": "netflix"})
    again = LeverAdapter.parse_config(config.model_dump())
    assert again == config
    assert config.model_dump_json() == again.model_dump_json() == '{"site":"netflix"}'


# --------------------------------------------------------------------------
# 2. probe
# --------------------------------------------------------------------------


@respx.mock
async def test_probe_reports_a_reachable_site(settings) -> None:
    mock_robots(LEVER_HOST)
    route = respx.get(POSTINGS_URL).mock(
        return_value=httpx.Response(200, json=load_json("lever_netflix.json"))
    )

    async with build_client(settings) as client:
        result = await build_adapter(settings, client).probe()

    assert result.reachable is True
    assert result.sample_count == 4
    assert result.http_status == 200
    assert result.company_name_guess == "Netflix"
    # The probe must never page.
    assert route.call_count == 1
    assert "limit" not in route.calls[0].request.url.params


@respx.mock
async def test_probe_reports_a_404_without_echoing_the_body(settings) -> None:
    mock_robots(LEVER_HOST)
    respx.get(POSTINGS_URL).mock(
        return_value=httpx.Response(404, text="<html><body>Not Found</body></html>")
    )

    async with build_client(settings) as client:
        result = await build_adapter(settings, client).probe()

    assert result.reachable is False
    assert result.http_status == 404
    assert result.detail == "Board not found — check the identifier in this source's config"
    assert "html" not in (result.detail or "")


# --------------------------------------------------------------------------
# 3. field mapping
# --------------------------------------------------------------------------


@respx.mock
async def test_fetch_maps_fields(settings) -> None:
    mock_robots(LEVER_HOST)
    respx.get(POSTINGS_URL).mock(
        return_value=httpx.Response(200, json=load_json("lever_netflix.json"))
    )

    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        postings = await collect(adapter)

    # The bare array parsed at all — there is no `jobs` envelope here.
    assert [p.external_id for p in postings] == [
        "6f1a2c8e-4b7d-4a11-9d2e-0f3a5b6c7d8e",
        "a1b2c3d4-5e6f-4708-9a0b-1c2d3e4f5061",
        "9c8b7a65-4321-4fed-8cba-098765432100",
    ]
    assert adapter.skipped == {"no_description": 1}

    first = postings[0]
    assert str(first.url) == "https://jobs.lever.co/netflix/6f1a2c8e-4b7d-4a11-9d2e-0f3a5b6c7d8e"
    assert first.title == "Senior Software Engineer, Streaming Platform"

    # THE caveat: the requirements are in `lists`, not in `description`. An
    # adapter that maps only descriptionPlain drops exactly this text.
    assert "What we are looking for" in first.description_text
    assert "• 7+ years building distributed systems in production." in first.description_text
    assert "• Work in Java and Kotlin on the playback control plane." in first.description_text
    # The intro and the closing survive too, in that order.
    assert first.description_text.startswith("Netflix is one of the world's leading")
    assert first.description_text.endswith("we celebrate the diversity of our teams.")
    assert first.description_html is not None
    assert "<h3>What you will do</h3>" in first.description_html
    assert "<li>Design and operate services" in first.description_html

    assert first.department == "Streaming Platform"  # team wins over department
    assert first.location_raw == "Bengaluru, India"
    assert first.location_city == "Bengaluru"
    assert first.location_country == "IN"
    assert first.is_remote is False  # workplaceType "hybrid"
    assert first.employment_type == "full_time"
    assert first.seniority_guess == "senior"
    # THE other caveat: createdAt is epoch MILLISECONDS. Read as seconds this
    # would land in 1970 and recency ranking would silently invert.
    assert first.posted_at == datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    assert first.posted_at.year == 2026
    # One posting, one application: allLocations is provenance, not a fan-out.
    assert first.raw["all_locations"] == [
        "Bengaluru, India",
        "Remote - India",
        "Mumbai, India",
    ]
    assert first.raw["createdAt"] == 1787824800000
    assert first.raw["workplaceType"] == "hybrid"
    assert first.raw["categories"]["team"] == "Streaming Platform"
    assert set(first.raw) == {
        "id",
        "categories",
        "workplaceType",
        "createdAt",
        "all_locations",
    }

    remote = postings[1]
    assert remote.is_remote is True  # workplaceType "remote"
    assert remote.department == "Data Science and Engineering"  # no team; department wins
    assert remote.location_country == "IN"
    assert remote.seniority_guess == "staff"
    assert "• 8+ years in data engineering" in remote.description_text
    # No `additional` on this posting: the assembly simply omits it.
    assert "equal opportunity" not in remote.description_text

    manager = postings[2]
    assert manager.seniority_guess == "manager"
    assert manager.location_city == "Amsterdam"
    assert manager.location_country == "NL"
    assert manager.posted_at == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# 4. pagination
# --------------------------------------------------------------------------


@respx.mock
async def test_pagination_is_dormant_by_default(settings) -> None:
    mock_robots(LEVER_HOST)
    route = respx.get(POSTINGS_URL).mock(
        return_value=httpx.Response(200, json=load_json("lever_netflix.json"))
    )

    async with build_client(settings) as client:
        await collect(build_adapter(settings, client))

    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["mode"] == "json"
    assert "limit" not in params
    assert "skip" not in params


@respx.mock
async def test_pagination_engages_and_terminates_on_a_short_page(settings) -> None:
    mock_robots(LEVER_HOST)
    fixture = load_json("lever_netflix.json")
    page_one = fixture[:2]
    page_two = fixture[2:3]  # short: one posting, which is what ends the loop
    route = respx.get(POSTINGS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page_one),
            httpx.Response(200, json=page_two),
        ]
    )

    async with build_client(settings) as client:
        postings = await collect(build_adapter(settings, client, limit=2))

    assert len(postings) == 3
    # Two requests: the second page came back short, which ends the loop. No
    # third request is made to "confirm" the end.
    assert route.call_count == 2
    assert "skip" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["skip"] == "2"
    assert route.calls[1].request.url.params["limit"] == "2"


@respx.mock
async def test_pagination_terminates_on_an_empty_page(settings) -> None:
    mock_robots(LEVER_HOST)
    fixture = load_json("lever_netflix.json")
    route = respx.get(POSTINGS_URL).mock(
        side_effect=[
            httpx.Response(200, json=fixture[:2]),
            httpx.Response(200, json=[]),
        ]
    )

    async with build_client(settings) as client:
        postings = await collect(build_adapter(settings, client, limit=2))

    assert len(postings) == 2
    assert route.call_count == 2


@respx.mock
async def test_pagination_cannot_loop_forever(settings) -> None:
    # An upstream that keeps returning a full page — a bug, or a cursor we
    # misuse — must not spin the adapter for the whole per-source ceiling.
    mock_robots(LEVER_HOST)
    fixture = load_json("lever_netflix.json")
    route = respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=fixture[:2]))

    async with build_client(settings) as client:
        adapter = build_adapter(settings, client, limit=2)
        postings = await collect(adapter)

    assert route.call_count == LeverAdapter.MAX_PAGES
    # Every page after the first repeats the same two ids, so exactly two
    # postings are emitted and the rest are counted as duplicates.
    assert len(postings) == 2
    assert adapter.skipped["duplicate"] == 2 * (LeverAdapter.MAX_PAGES - 1)


# --------------------------------------------------------------------------
# 5. partial failure
# --------------------------------------------------------------------------


@respx.mock
async def test_a_source_cancelled_at_the_ceiling_yields_nothing(settings) -> None:
    mock_robots(LEVER_HOST)
    never_returns = asyncio.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        await never_returns.wait()
        return httpx.Response(200, json=[])

    respx.get(POSTINGS_URL).mock(side_effect=hang)
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
async def test_a_failure_on_page_two_discards_page_one(settings) -> None:
    # §4.4: partial ingestion is not allowed. The runner collects the whole
    # stream or none of it, so a failure mid-stream must leave nothing behind.
    mock_robots(LEVER_HOST)
    fixture = load_json("lever_netflix.json")
    respx.get(POSTINGS_URL).mock(
        side_effect=[
            httpx.Response(200, json=fixture[:2]),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),
        ]
    )

    # The runner's own idiom: collect the whole stream, or none of it. The
    # adapter does not swallow the failure (that is the runner's job), so the
    # assignment never happens and page one is discarded with it.
    ingested: list[RawPosting] = []
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client, limit=2)
        with pytest.raises(UpstreamHttpError) as excinfo:
            ingested = await collect(adapter)

    assert excinfo.value.status_code == 500
    assert ingested == []


# --------------------------------------------------------------------------
# 6. schema drift
# --------------------------------------------------------------------------


@respx.mock
async def test_schema_drift_raises_and_yields_nothing(settings) -> None:
    mock_robots(LEVER_HOST)
    drifted = copy.deepcopy(load_json("lever_netflix.json"))
    del drifted[1]["hostedUrl"]
    respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=drifted))

    emitted: list[RawPosting] = []
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        with pytest.raises(SchemaDriftError) as excinfo:
            async for posting in adapter.fetch():
                emitted.append(posting)

    assert excinfo.value.error_code == "adapter.schema_drift"
    assert emitted == []
    assert "api.lever.co/v0/postings/{site}" in str(excinfo.value)
    assert SITE not in str(excinfo.value)


@respx.mock
async def test_an_envelope_where_an_array_belongs_is_drift(settings) -> None:
    # The mirror image of the bare-array caveat: if Lever ever wrapped the
    # array, that is drift and must be reported, not read as an empty board.
    mock_robots(LEVER_HOST)
    respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json={"postings": []}))

    async with build_client(settings) as client:
        with pytest.raises(SchemaDriftError):
            await collect(build_adapter(settings, client))


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


async def test_every_url_the_adapter_can_construct_is_allowed(settings) -> None:
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        assert_fetch_allowed(adapter._url())
        assert adapter.describe() == "Lever · netflix"
        assert LeverAdapter.fidelity_rank == 88
        assert LeverAdapter.requires_detail_fetch is False
