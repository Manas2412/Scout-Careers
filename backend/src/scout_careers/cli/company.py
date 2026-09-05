"""``scout-careers company …`` — the registry, from a terminal.

``company add`` is the add-company flow of COMPANY_REGISTRY.md §7.4 with the
confirmation step collapsed into one command: detect, probe, and write only what
the probe reached. It refuses to write an unprobed source unless the operator
says ``--force``, and it prints what it found either way, because "it said it
added Stripe" and "it added a board that answers 404" must not look the same.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import typer
from redis.asyncio import Redis
from redis.exceptions import RedisError

from scout_careers.cli.output import dash, echo, echo_json, echo_table, error
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.errors import DeniedByPolicy
from scout_careers.common.types import CompanyStatus, CompanyTier
from scout_careers.db.models import Company, Source
from scout_careers.db.session import session_scope
from scout_careers.registry.service import (
    DetectionResult,
    Undetectable,
    create_company,
    create_source,
    detect_for_url,
    list_companies,
    list_sources,
    set_company_status,
    slugify,
)

app = typer.Typer(no_args_is_help=True, help="Companies and their boards.")


async def _redis_if_reachable(settings: Settings) -> Redis | None:
    """Return a live Redis client, or ``None`` when there is not one.

    A probe is a single request. It honours robots.txt and the never-fetch list
    either way; without Redis it simply does not share a token bucket with a
    concurrent run. A discovery *run* makes the opposite trade and refuses to
    start (``ingest/runner.py``) — one request is not a crawl.
    """
    client: Redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        await client.ping()
    except (RedisError, OSError):
        await client.aclose()
        return None
    return client


def _detection_rows(detection: DetectionResult) -> list[list[str]]:
    """Render a detection result as table rows."""
    probe = detection.probe
    return [
        ["adapter", detection.adapter.value],
        ["config", ", ".join(f"{key}={value}" for key, value in detection.config.items())],
        ["reachable", "yes" if probe.reachable else "no"],
        ["postings seen", str(probe.sample_count)],
        ["latency", f"{probe.latency_ms} ms"],
        ["http status", dash(probe.http_status)],
        ["detail", dash(probe.detail)],
        ["name guess", dash(detection.company_name_guess)],
    ]


async def _add(
    url: str,
    *,
    name: str | None,
    tier: CompanyTier,
    tags: list[str],
    force: bool,
    as_json: bool,
) -> int:
    settings = get_settings()
    redis = await _redis_if_reachable(settings)
    try:
        detection = await detect_for_url(url, settings=settings, redis=redis)
    finally:
        if redis is not None:
            await redis.aclose()

    if not as_json:
        echo_table(["field", "value"], _detection_rows(detection))
        echo()

    if not detection.probe.reachable and not force:
        error(
            "The board did not answer. Nothing was written. "
            "Re-run with --force to add it anyway, or add the company with no source."
        )
        return 1

    resolved_name = name or detection.company_name_guess or slugify(url)
    async with session_scope() as session:
        company = await create_company(
            session,
            name=resolved_name,
            tier=tier,
            tags=tags,
            careers_url=url,
        )
        source = await create_source(
            session,
            company_id=company.id,
            adapter=detection.adapter,
            config=detection.config,
        )
        payload: dict[str, Any] = {
            "company_id": company.id,
            "company_slug": company.slug,
            "company_name": company.name,
            "tier": company.tier.value,
            "tags": list(company.tags),
            "source_id": source.id,
            "adapter": source.adapter.value,
            "config": dict(source.config),
            "poll_interval_minutes": source.poll_interval_minutes,
            "probe": detection.probe.model_dump(mode="json"),
        }

    if as_json:
        echo_json(payload)
    else:
        echo(
            f"Added {payload['company_name']} (company {payload['company_id']}) "
            f"with a {payload['adapter']} source (source {payload['source_id']}), "
            f"polling every {payload['poll_interval_minutes']} minutes."
        )
    return 0


@app.command("add")
def add(
    url: Annotated[str, typer.Argument(help="A careers or board URL to detect.")],
    name: Annotated[
        str | None, typer.Option(help="Display name; the probe's guess if omitted.")
    ] = None,
    tier: Annotated[
        CompanyTier, typer.Option(help="dream | strong | volume.")
    ] = CompanyTier.VOLUME,
    tags: Annotated[
        list[str] | None,
        typer.Option("--tag", help="An axis:value tag. Repeatable."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Write the source even if the probe failed.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Detect the board behind a URL, probe it, and add the company."""
    try:
        code = asyncio.run(
            _add(url, name=name, tier=tier, tags=tags or [], force=force, as_json=as_json)
        )
    except DeniedByPolicy as exc:
        error(
            f"{exc.host} is on the never-fetch list. Scout never requests it. "
            "Set up a job alert to your alerts mailbox instead, or import the role manually."
        )
        raise typer.Exit(code=2) from exc
    except Undetectable as exc:
        error(f"{exc.message}")
        raise typer.Exit(code=1) from exc
    if code:
        raise typer.Exit(code=code)


async def _list(status: CompanyStatus | None, tier: CompanyTier | None, as_json: bool) -> None:
    async with session_scope() as session:
        companies = await list_companies(session, status=status, tier=tier)
        sources = await list_sources(session)

    by_company: dict[int, list[Source]] = {}
    for source in sources:
        by_company.setdefault(source.company_id, []).append(source)

    if as_json:
        echo_json(
            [_company_payload(company, by_company.get(company.id, [])) for company in companies]
        )
        return

    echo_table(
        ["id", "slug", "name", "tier", "status", "sources", "tags"],
        [
            [
                str(company.id),
                company.slug,
                company.name,
                company.tier.value,
                company.status.value,
                _sources_cell(by_company.get(company.id, [])),
                ", ".join(company.tags) or "—",
            ]
            for company in companies
        ],
    )


def _sources_cell(sources: list[Source]) -> str:
    """Summarise a company's sources as "2 (1 off)"."""
    if not sources:
        return "0"
    disabled = sum(1 for source in sources if not source.enabled)
    return f"{len(sources)}" + (f" ({disabled} off)" if disabled else "")


def _company_payload(company: Company, sources: list[Source]) -> dict[str, Any]:
    """Render one company and its sources as JSON."""
    return {
        "id": company.id,
        "slug": company.slug,
        "name": company.name,
        "tier": company.tier.value,
        "status": company.status.value,
        "tags": list(company.tags),
        "careers_url": company.careers_url,
        "sources": [
            {
                "id": source.id,
                "adapter": source.adapter.value,
                "config": dict(source.config),
                "enabled": source.enabled,
                "poll_interval_minutes": source.poll_interval_minutes,
                "last_run_at": source.last_run_at,
                "last_status": source.last_status,
                "last_error": source.last_error,
                "consecutive_failures": source.consecutive_failures,
            }
            for source in sources
        ],
    }


@app.command("list")
def list_command(
    status: Annotated[CompanyStatus | None, typer.Option(help="Filter by status.")] = None,
    tier: Annotated[CompanyTier | None, typer.Option(help="Filter by tier.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List companies, their sources and each source's health."""
    asyncio.run(_list(status, tier, as_json))


async def _set_status(company_id: int, status: CompanyStatus) -> str:
    async with session_scope() as session:
        company = await set_company_status(session, company_id, status)
        return company.name


@app.command("disable")
def disable(
    company_id: Annotated[int, typer.Argument(help="The company id.")],
) -> None:
    """Pause a company and stop every one of its sources polling."""
    name = asyncio.run(_set_status(company_id, CompanyStatus.PAUSED))
    echo(f"Paused {name}; its sources will not be polled.")


@app.command("enable")
def enable(
    company_id: Annotated[int, typer.Argument(help="The company id.")],
) -> None:
    """Resume a company and re-enable its sources, clearing their failure counts."""
    name = asyncio.run(_set_status(company_id, CompanyStatus.TRACKING))
    echo(f"Tracking {name}; its sources are enabled and their failure counts are reset.")


__all__ = ["app"]
