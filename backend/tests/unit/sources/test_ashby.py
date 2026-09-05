"""AshbyAdapter — the six required tests (SOURCE_ADAPTERS.md §12).

The caveat this file exists to pin is ``isListed: false``. Yielding an unlisted
posting puts a role the employer has not published into the operator's queue,
and the count under ``skipped["unlisted"]`` is what makes the omission visible
rather than silent.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime

import httpx
import pytest
import respx

from scout_careers.common.errors import AdapterConfigError, SchemaDriftError
from scout_careers.common.types import AtsType
from scout_careers.sources.ashby import AshbyAdapter
from scout_careers.sources.base import AshbyConfig, RawPosting
from scout_careers.sources.http import build_client
from scout_careers.sources.policy import assert_fetch_allowed
from tests.unit.sources.conftest import ASHBY_HOST, load_json, make_http, mock_robots

BOARD = "openai"
JOBS_URL = f"{ASHBY_HOST}/posting-api/job-board/{BOARD}"

UNLISTED_ID = "d5f1b2e3-4066-4c8d-ae9f-1a2b3c4d5e6f"


def build_adapter(
    settings, client: httpx.AsyncClient, *, include_compensation: bool = True
) -> AshbyAdapter:
    return AshbyAdapter(
        source_id=31,
        config=AshbyConfig(board_name=BOARD, include_compensation=include_compensation),
        http=make_http(
            settings, client, adapter=AtsType.ASHBY, bucket_key="api.ashbyhq.com", source_id=31
        ),
    )


async def collect(adapter: AshbyAdapter) -> list[RawPosting]:
    return [posting async for posting in adapter.fetch()]


# --------------------------------------------------------------------------
# 1. parse_config
# --------------------------------------------------------------------------


def test_parse_config_accepts_a_valid_board_name() -> None:
    config = AshbyAdapter.parse_config({"board_name": "openai"})
    assert config == AshbyConfig(board_name="openai", include_compensation=True)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"board_name": ""},
        {"board_name": "openai", "api_key": "sk-live-secret"},  # extra=forbid
        {"board_name": "x" * 65},
        {"board_name": "openai", "include_compensation": "maybe"},
    ],
)
def test_parse_config_rejects_bad_input(raw: dict[str, object]) -> None:
    with pytest.raises(AdapterConfigError):
        AshbyAdapter.parse_config(raw)


def test_parse_config_round_trips_canonically() -> None:
    config = AshbyAdapter.parse_config({"board_name": "openai", "include_compensation": False})
    again = AshbyAdapter.parse_config(config.model_dump())
    assert again == config
    assert (
        config.model_dump_json()
        == again.model_dump_json()
        == '{"board_name":"openai","include_compensation":false}'
    )


# --------------------------------------------------------------------------
# 2. probe
# --------------------------------------------------------------------------


@respx.mock
async def test_probe_counts_only_listed_postings(settings) -> None:
    mock_robots(ASHBY_HOST)
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=load_json("ashby_openai.json")))

    async with build_client(settings) as client:
        result = await build_adapter(settings, client).probe()

    assert result.reachable is True
    # Four jobs in the payload, one unlisted. A draft is not evidence that the
    # board is usable.
    assert result.sample_count == 3
    assert result.http_status == 200
    assert result.company_name_guess == "Openai"


@respx.mock
async def test_probe_reports_a_404_without_echoing_the_body(settings) -> None:
    mock_robots(ASHBY_HOST)
    respx.get(JOBS_URL).mock(
        return_value=httpx.Response(404, json={"error": "job board not found", "code": "NOT_FOUND"})
    )

    async with build_client(settings) as client:
        result = await build_adapter(settings, client).probe()

    assert result.reachable is False
    assert result.http_status == 404
    assert result.detail == "Board not found — check the identifier in this source's config"
    assert "NOT_FOUND" not in (result.detail or "")


# --------------------------------------------------------------------------
# 3. field mapping
# --------------------------------------------------------------------------


@respx.mock
async def test_fetch_maps_fields(settings) -> None:
    mock_robots(ASHBY_HOST)
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=load_json("ashby_openai.json")))

    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        postings = await collect(adapter)

    # THE caveat: the unlisted posting is skipped, and counted.
    assert [p.external_id for p in postings] == [
        "b3d9f0c1-2e44-4a6b-8c7d-9e0f1a2b3c4d",
        "c4e0a1d2-3f55-4b7c-9d8e-0f1a2b3c4d5e",
        "e6a2c3f4-5177-4d9e-bfa0-2b3c4d5e6f70",
    ]
    assert UNLISTED_ID not in [p.external_id for p in postings]
    assert adapter.skipped == {"unlisted": 1}

    first = postings[0]
    assert (
        str(first.url) == f"https://jobs.ashbyhq.com/{BOARD}/b3d9f0c1-2e44-4a6b-8c7d-9e0f1a2b3c4d"
    )
    assert first.title == "Member of Technical Staff, Applied AI"
    assert first.department == "Applied Engineering"  # team wins over department
    assert first.location_raw == "Bengaluru, India"
    assert first.location_city == "Bengaluru"
    assert first.location_country == "IN"
    # isRemote is false, but a secondary location says "Remote - India".
    assert first.is_remote is True
    assert first.employment_type == "full_time"
    # "Member of Technical Staff" matches the staff pattern. Title inference is
    # a cheap deterministic hint for the stage-④ filter, not a scoring input.
    assert first.seniority_guess == "staff"
    assert first.posted_at == datetime(2026, 8, 21, 9, 12, 44, tzinfo=UTC)

    # descriptionPlain is already clean and is still normalised: NFKC,
    # whitespace collapse and the blank-run rule all ran.
    assert first.description_text.startswith("About the team")
    assert "5+ years shipping production software." in first.description_text
    assert "\n\n\n" not in first.description_text
    assert first.description_html is not None
    assert first.description_html.startswith("<div><p>About the team</p>")

    # The compensation block is provenance and display only — never scoring.
    assert first.raw["compensation"]["compensationTierSummary"] == "₹45L – ₹70L"
    assert set(first.raw) == {
        "id",
        "department",
        "team",
        "employmentType",
        "isRemote",
        "secondaryLocations",
        "compensation",
    }
    assert first.raw["secondaryLocations"][0]["location"] == "Remote - India"

    explicitly_remote = postings[1]
    assert explicitly_remote.is_remote is True  # isRemote: true
    assert explicitly_remote.department == "Research"  # no team; department wins
    assert explicitly_remote.location_city is None
    assert explicitly_remote.raw["compensation"] is None

    intern = postings[2]
    assert intern.employment_type == "internship"
    assert intern.seniority_guess == "intern"
    assert intern.location_city == "Hyderabad"
    assert intern.is_remote is False


@respx.mock
async def test_include_compensation_is_passed_through(settings) -> None:
    mock_robots(ASHBY_HOST)
    route = respx.get(JOBS_URL).mock(
        return_value=httpx.Response(200, json=load_json("ashby_openai.json"))
    )

    async with build_client(settings) as client:
        await collect(build_adapter(settings, client, include_compensation=False))

    assert route.calls[0].request.url.params["includeCompensation"] == "false"


# --------------------------------------------------------------------------
# 4. pagination
# --------------------------------------------------------------------------


@respx.mock
async def test_pagination_is_a_single_request(settings) -> None:
    mock_robots(ASHBY_HOST)
    route = respx.get(JOBS_URL).mock(
        return_value=httpx.Response(200, json=load_json("ashby_openai.json"))
    )

    async with build_client(settings) as client:
        await collect(build_adapter(settings, client))

    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["includeCompensation"] == "true"
    assert "cursor" not in params
    assert "offset" not in params


# --------------------------------------------------------------------------
# 5. partial failure
# --------------------------------------------------------------------------


@respx.mock
async def test_a_source_cancelled_at_the_ceiling_yields_nothing(settings) -> None:
    mock_robots(ASHBY_HOST)
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


# --------------------------------------------------------------------------
# 6. schema drift
# --------------------------------------------------------------------------


@respx.mock
async def test_schema_drift_raises_and_yields_nothing(settings) -> None:
    mock_robots(ASHBY_HOST)
    drifted = copy.deepcopy(load_json("ashby_openai.json"))
    del drifted["jobs"][0]["jobUrl"]
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=drifted))

    emitted: list[RawPosting] = []
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        with pytest.raises(SchemaDriftError) as excinfo:
            async for posting in adapter.fetch():
                emitted.append(posting)

    assert excinfo.value.error_code == "adapter.schema_drift"
    assert emitted == []
    assert "api.ashbyhq.com/posting-api/job-board/{board_name}" in str(excinfo.value)
    assert BOARD not in str(excinfo.value)


@respx.mock
async def test_a_new_optional_field_is_not_drift(settings) -> None:
    # Ashby adds fields without warning. A new optional field must not fail the
    # board; only a missing required one is drift.
    mock_robots(ASHBY_HOST)
    payload = copy.deepcopy(load_json("ashby_openai.json"))
    payload["jobs"][0]["shouldDisplayCompensationOnJobPostings"] = True
    payload["newTopLevelKey"] = {"anything": 1}
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=payload))

    async with build_client(settings) as client:
        postings = await collect(build_adapter(settings, client))

    assert len(postings) == 3


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


async def test_every_url_the_adapter_can_construct_is_allowed(settings) -> None:
    async with build_client(settings) as client:
        adapter = build_adapter(settings, client)
        assert_fetch_allowed(adapter._url())
        assert adapter.describe() == "Ashby · openai"
        assert AshbyAdapter.fidelity_rank == 90
        assert AshbyAdapter.requires_detail_fetch is False
