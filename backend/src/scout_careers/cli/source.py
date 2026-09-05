"""``scout-careers source …`` — the boards behind the companies.

Board identifiers move. When a company's Ashby board turns out to be named
``cursor`` rather than ``anysphere``, editing ``seeds/companies.yaml`` and
re-seeding does **not** correct the row: ``seed companies`` is idempotent on
``(company_id, adapter, config)``, so a changed config inserts a *second*
source, and the dead one keeps failing every night until ``consecutive_failures``
auto-disables it five days later. These commands are how that row is dealt with
without a psql prompt.

``retire`` is the answer almost every time, and it is the one the help text
leads with. It stops the source polling and destroys nothing: the postings stay
searchable, they close naturally under the two-run rule, and the roles that
genuinely moved collapse against the new board's rows. ``rm`` is the only
destructive command in this CLI — ``job_posting.source_id`` is
``ON DELETE CASCADE`` (DATA_MODEL.md §4.1), so deleting a source deletes every
posting ever discovered through it — and it therefore states the exact number of
postings it is about to destroy and waits for a yes.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from redis.asyncio import Redis
from redis.exceptions import RedisError

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import dash, echo, echo_json, echo_table, error
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.errors import AdapterConfigError, DeniedByPolicy
from scout_careers.common.logging import configure_logging
from scout_careers.common.types import AtsType
from scout_careers.db.models import Company, Source
from scout_careers.db.session import session_scope
from scout_careers.registry.service import (
    RegistryError,
    SourceDeletion,
    SourceProbe,
    companies_by_id,
    config_identity,
    count_source_postings,
    delete_source,
    list_sources,
    open_posting_counts,
    probe_source,
    redact_config,
    require_source,
    resolve_company,
    retire_source,
    set_source_enabled,
    source_probe_url,
)

app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Boards attached to companies. `source retire` is the safe way to take a "
        "moved board out of service; `source rm` deletes its postings with it."
    ),
)


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


def _source_payload(source: Source, company: Company | None, open_postings: int) -> dict[str, Any]:
    """Render one source as JSON. The config is redacted, never raw."""
    return {
        "id": source.id,
        "company_id": source.company_id,
        "company_slug": company.slug if company is not None else None,
        "company_name": company.name if company is not None else None,
        "adapter": source.adapter.value,
        "config": redact_config(source.config or {}),
        "config_identity": config_identity(source.config or {}),
        "enabled": source.enabled,
        "poll_interval_minutes": source.poll_interval_minutes,
        "last_run_at": source.last_run_at,
        "last_status": source.last_status,
        "last_error": source.last_error,
        "consecutive_failures": source.consecutive_failures,
        "open_postings": open_postings,
    }


def _when(source: Source) -> str:
    """Render ``last_run_at`` for a table cell."""
    return source.last_run_at.isoformat(timespec="seconds") if source.last_run_at else "—"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def _list(
    *,
    company: str | None,
    adapter: AtsType | None,
    status: str | None,
    failing: bool,
    disabled: bool,
    as_json: bool,
) -> None:
    async with session_scope() as session:
        company_id = (await resolve_company(session, company)).id if company else None
        sources = await list_sources(
            session,
            company_id=company_id,
            adapter=adapter,
            last_status=status,
            failing=failing,
            enabled=False if disabled else None,
        )
        companies = await companies_by_id(session, [source.company_id for source in sources])
        open_counts = await open_posting_counts(session, [source.id for source in sources])

    if as_json:
        echo_json(
            [
                _source_payload(
                    source, companies.get(source.company_id), open_counts.get(source.id, 0)
                )
                for source in sources
            ]
        )
        return

    echo_table(
        ["id", "company", "adapter", "config", "on", "last run", "status", "fails", "open"],
        [
            [
                str(source.id),
                dash(
                    companies[source.company_id].slug
                    if source.company_id in companies
                    else source.company_id
                ),
                source.adapter.value,
                dash(config_identity(source.config or {})),
                "yes" if source.enabled else "no",
                _when(source),
                dash(source.last_status),
                str(source.consecutive_failures),
                str(open_counts.get(source.id, 0)),
            ]
            for source in sources
        ],
    )


@app.command("list")
def list_command(
    company: Annotated[
        str | None, typer.Option("--company", help="Restrict to one company: slug or id.")
    ] = None,
    adapter: Annotated[
        AtsType | None, typer.Option("--adapter", help="Restrict to one adapter type.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Restrict to one last_status value.")
    ] = None,
    failing: Annotated[
        bool, typer.Option("--failing", help="Only sources with consecutive failures.")
    ] = False,
    disabled: Annotated[bool, typer.Option("--disabled", help="Only disabled sources.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List sources with their health and their open-posting counts.

    The ``config`` column is the identifying value only — the board token, site
    or board name. That is the field that moves, and it is the field to compare
    against the employer's live careers page.
    """
    try:
        run_async(
            _list(
                company=company,
                adapter=adapter,
                status=status,
                failing=failing,
                disabled=disabled,
                as_json=as_json,
            )
        )
    except RegistryError as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


async def _show(source_id: int, as_json: bool) -> None:
    async with session_scope() as session:
        source = await require_source(session, source_id)
        companies = await companies_by_id(session, [source.company_id])
        open_postings = (await open_posting_counts(session, [source.id])).get(source.id, 0)
        total_postings = await count_source_postings(session, source.id)
        company = companies.get(source.company_id)
        payload = _source_payload(source, company, open_postings)
        payload["total_postings"] = total_postings
        payload["probe_url"] = source_probe_url(source.adapter, source.config or {})

    if as_json:
        echo_json(payload)
        return

    echo_table(
        ["field", "value"],
        [
            ["id", str(payload["id"])],
            ["company", f"{dash(payload['company_slug'])} ({payload['company_id']})"],
            ["adapter", str(payload["adapter"])],
            ["enabled", "yes" if payload["enabled"] else "no"],
            ["poll interval", f"{payload['poll_interval_minutes']}m"],
            ["last run", _when(source)],
            ["last status", dash(payload["last_status"])],
            ["consecutive failures", str(payload["consecutive_failures"])],
            ["open postings", str(open_postings)],
            ["postings a delete destroys", str(total_postings)],
            ["probe url", dash(payload["probe_url"])],
            ["last error", dash(payload["last_error"])],
        ],
    )
    echo()
    echo("config:")
    echo_table(
        ["key", "value"],
        [[key, dash(value)] for key, value in redact_config(source.config or {}).items()],
    )


@app.command("show")
def show(
    source_id: Annotated[int, typer.Argument(help="The source id.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show one source in full, including its config and its last error."""
    try:
        run_async(_show(source_id, as_json))
    except RegistryError as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# retire — the safe one
# ---------------------------------------------------------------------------


async def _preview(source_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        source = await require_source(session, source_id)
        companies = await companies_by_id(session, [source.company_id])
        company = companies.get(source.company_id)
        return {
            "id": source.id,
            "describe": f"{source.adapter.value} · {dash(config_identity(source.config or {}))}",
            "company": company.slug if company is not None else str(source.company_id),
            "enabled": source.enabled,
            "postings": await count_source_postings(session, source.id),
        }


async def _retire(source_id: int) -> None:
    async with session_scope() as session:
        await retire_source(session, source_id)


@app.command("retire")
def retire(
    source_id: Annotated[int, typer.Argument(help="The source id.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False,
) -> None:
    """Take a source out of service without deleting anything.

    What you want when a board moves. The source stops polling; its postings and
    their history survive, stay searchable, and close naturally under the two-run
    rule. Reversible with ``source enable``.
    """
    configure_logging(get_settings())
    try:
        preview = run_async(_preview(source_id))
    except RegistryError as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc

    echo(f"Source {preview['id']} — {preview['describe']} — on company {preview['company']}.")
    echo(
        f"Retiring it stops it polling. Its {preview['postings']} posting(s) are kept, "
        "with their history."
    )
    if not yes and not typer.confirm("Retire it?"):
        error("Nothing was changed.")
        raise typer.Exit(code=1)

    run_async(_retire(source_id))
    echo(
        f"Source {preview['id']} retired: disabled, marked `retired`, nothing deleted. "
        "Re-enable it with `scout-careers source enable`."
    )


# ---------------------------------------------------------------------------
# rm — the destructive one
# ---------------------------------------------------------------------------


async def _delete(source_id: int) -> SourceDeletion:
    async with session_scope() as session:
        return await delete_source(session, source_id)


@app.command("rm")
def rm(
    source_id: Annotated[int, typer.Argument(help="The source id.")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the confirmation. The only way to skip it.")
    ] = False,
) -> None:
    """Delete a source, and every posting ever discovered through it.

    ``job_posting.source_id`` is ``ON DELETE CASCADE``, so this destroys
    discovery history — ``first_seen_at`` dates, closed roles, the lot — that
    nothing can reconstruct. It is meant for a source that never successfully
    ran: the mistyped config. For a board that moved, use ``source retire``.
    """
    configure_logging(get_settings())
    try:
        preview = run_async(_preview(source_id))
    except RegistryError as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc

    echo(f"Source {preview['id']} — {preview['describe']} — on company {preview['company']}.")
    echo(
        f"Deleting it destroys {preview['postings']} posting(s) with it, by cascade. "
        "This cannot be undone."
    )
    if preview["postings"]:
        echo("`scout-careers source retire` stops it polling and keeps every one of them.")
    if not yes and not typer.confirm("Delete the source and its postings?"):
        error("Nothing was deleted.")
        raise typer.Exit(code=1)

    try:
        deletion = run_async(_delete(source_id))
    except RegistryError as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc

    echo(f"Source {deletion.source_id} deleted, destroying {deletion.postings_deleted} posting(s).")


# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------


async def _enable(source_id: int) -> Source:
    async with session_scope() as session:
        return await set_source_enabled(session, source_id, True)


@app.command("enable")
def enable(
    source_id: Annotated[int, typer.Argument(help="The source id.")],
) -> None:
    """Re-enable a source and clear its consecutive-failure count.

    The human act the auto-disable threshold waits for: the system never
    re-enables itself on a timer, because five consecutive daily failures almost
    always means the board moved (SOURCE_ADAPTERS.md §4.8).
    """
    configure_logging(get_settings())
    try:
        source = run_async(_enable(source_id))
    except RegistryError as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc
    echo(
        f"Source {source.id} enabled; its failure count is reset and it polls again "
        f"every {source.poll_interval_minutes} minutes."
    )


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


async def _test(source_id: int) -> SourceProbe:
    settings = get_settings()
    redis = await _redis_if_reachable(settings)
    try:
        async with session_scope() as session:
            return await probe_source(session, source_id, settings=settings, redis=redis)
    finally:
        if redis is not None:
            await redis.aclose()


@app.command("test")
def test(
    source_id: Annotated[int, typer.Argument(help="The source id.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Re-probe a source's board: one request, and report what came back.

    Read-only. A reachable board does not clear the failure count or re-enable
    anything — ``source enable`` does that, so that testing a source you
    deliberately retired cannot put it back into the nightly run behind your
    back.
    """
    configure_logging(get_settings())
    try:
        probed = run_async(_test(source_id))
    except DeniedByPolicy as exc:
        error(
            f"{exc.host} is on the never-fetch list. Scout never requests it, so this "
            "source was not probed. Set up a job alert to your alerts mailbox instead."
        )
        raise typer.Exit(code=2) from exc
    except AdapterConfigError as exc:
        error(f"The stored config no longer validates: {exc.message}")
        raise typer.Exit(code=1) from exc
    except RegistryError as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc

    probe = probed.probe
    if as_json:
        echo_json(
            {
                "source_id": probed.source_id,
                "adapter": probed.adapter.value,
                "describe": probed.describe,
                "url": probed.url,
                "probe": probe.model_dump(mode="json"),
            }
        )
    else:
        echo_table(
            ["field", "value"],
            [
                ["source", str(probed.source_id)],
                ["board", probed.describe],
                ["url", probed.url],
                ["reachable", "yes" if probe.reachable else "no"],
                ["postings seen", str(probe.sample_count)],
                ["latency", f"{probe.latency_ms} ms"],
                ["http status", dash(probe.http_status)],
                ["detail", dash(probe.detail)],
            ],
        )
    if not probe.reachable:
        raise typer.Exit(code=1)


__all__ = ["app"]
