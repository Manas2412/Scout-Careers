"""``scout-careers seed companies`` — the starting registry.

Idempotent on ``company.slug`` and on ``(company_id, adapter, config)``, so
re-running it updates nothing the operator has since edited and inserts only
what is missing (COMPANY_REGISTRY.md §9).

Seeding does **not** probe. The seed file already names the adapter and its
config, so a probe would only be a liveness check — and one that fires forty
requests at forty employers before the operator has decided they want any of
them. The first discovery run answers the same question and records the answer
in ``source.last_status``, where the health table and ``company list`` read it.
The config is still validated against each adapter's config model before
anything is written, so a malformed board token fails at seed time, not at 02:30.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import dash, echo, echo_json, echo_table, error
from scout_careers.common.config import get_settings
from scout_careers.common.errors import ScoutError
from scout_careers.common.types import AtsType, CompanyTier
from scout_careers.db.session import session_scope
from scout_careers.registry.service import (
    create_company,
    create_source,
    find_source,
    validate_tags,
)
from scout_careers.sources.registry import get_adapter

app = typer.Typer(no_args_is_help=True, help="Load seed data.")

#: ``backend/seeds/companies.yaml`` in a source checkout.
DEFAULT_SEED_PATH = Path(__file__).resolve().parents[3] / "seeds" / "companies.yaml"


class SeedCompany(BaseModel):
    """One row of the seed file.

    ``extra="forbid"`` so a typo in a key is a load failure rather than a
    silently ignored field — a seed file is edited by hand more often than any
    other file in this repository.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    adapter: AtsType
    config: dict[str, Any] = Field(default_factory=dict)
    tier: CompanyTier = CompanyTier.VOLUME
    tags: list[str] = Field(default_factory=list)
    careers_url: str | None = None
    website: str | None = None
    hq_location: str | None = None


class SeedFile(BaseModel):
    """The seed document."""

    model_config = ConfigDict(extra="forbid")

    companies: list[SeedCompany]


def load_seed(path: Path) -> SeedFile:
    """Read and validate the seed file.

    Args:
        path: The YAML file.

    Returns:
        The parsed document.

    Raises:
        FileNotFoundError: When the file is missing.
        ValidationError: When a row does not match :class:`SeedCompany`.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SeedFile.model_validate(document)


async def _seed(path: Path, as_json: bool) -> int:
    seed = load_seed(path)
    rows: list[dict[str, Any]] = []

    async with session_scope() as session:
        for entry in seed.companies:
            company = await create_company(
                session,
                name=entry.name,
                slug=entry.slug,
                tier=entry.tier,
                tags=[*entry.tags, "origin:seed"],
                careers_url=entry.careers_url,
                website=entry.website,
                hq_location=entry.hq_location,
            )
            existing = await find_source(
                session,
                company_id=company.id,
                adapter=entry.adapter,
                config=entry.config,
            )
            source = await create_source(
                session,
                company_id=company.id,
                adapter=entry.adapter,
                config=entry.config,
            )
            rows.append(
                {
                    "slug": entry.slug,
                    "name": entry.name,
                    "company_id": company.id,
                    "tier": entry.tier.value,
                    "adapter": entry.adapter.value,
                    "source_id": source.id,
                    "source_action": "existing" if existing is not None else "created",
                }
            )

    if as_json:
        echo_json(rows)
        return 0

    echo_table(
        ["slug", "name", "tier", "adapter", "company", "source", "source state"],
        [
            [
                row["slug"],
                row["name"],
                row["tier"],
                row["adapter"],
                str(row["company_id"]),
                str(row["source_id"]),
                dash(row["source_action"]),
            ]
            for row in rows
        ],
    )
    created = sum(1 for row in rows if row["source_action"] == "created")
    echo()
    echo(
        f"{len(rows)} companies in the seed file; {created} source(s) created, rest already present."
    )
    return 0


@app.command("companies")
def companies(
    path: Annotated[
        Path | None,
        typer.Option("--file", help="Seed file; backend/seeds/companies.yaml by default."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Load ``backend/seeds/companies.yaml`` into the registry."""
    resolved = path or DEFAULT_SEED_PATH
    if not resolved.exists():
        error(f"No seed file at {resolved}.")
        raise typer.Exit(code=1)
    # Everything `create_company` and `create_source` can refuse, refused here
    # first, before a single row is written.
    #
    # The adapter check was here from the start; the tag check was not, and a
    # bare `reserved` on the last row of the file failed on row 44 — after 43
    # companies had been created. The transaction rolled them back, so nothing
    # was corrupted, but the operator got a stack trace from inside the write
    # loop for a mistake that is visible in the YAML without a database at all.
    # A seed file is edited by hand more often than anything else here, so every
    # rule it can break belongs in this block.
    try:
        seed = load_seed(resolved)
        for entry in seed.companies:
            get_adapter(entry.adapter).parse_config(entry.config)
            validate_tags(entry.tags)
    except (ValidationError, ValueError, ScoutError) as exc:
        error(f"The seed file is not valid: {exc}")
        raise typer.Exit(code=1) from exc

    get_settings()
    code = run_async(_seed(resolved, as_json))
    if code:
        raise typer.Exit(code=code)


__all__ = ["DEFAULT_SEED_PATH", "SeedCompany", "SeedFile", "app", "load_seed"]
