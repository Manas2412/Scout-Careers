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

from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import dash, echo, echo_json, echo_table, error
from scout_careers.common.config import get_settings
from scout_careers.common.errors import ScoutError
from scout_careers.common.types import AtsType, ClaimConfidentiality, CompanyTier
from scout_careers.db.models import Claim, ResumeVariant
from scout_careers.db.session import session_scope
from scout_careers.extract.variants import (
    apply_bullet_tags,
    bullet_ids,
    bullet_tag_problems,
    unproven_skills,
    unresolved_items,
    untagged_bullets,
    variant_skill_set,
)
from scout_careers.extract.vocabulary import get_vocabulary
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


# ---------------------------------------------------------------------------
# Resume variants
# ---------------------------------------------------------------------------

#: ``backend/seeds/variants/`` in a source checkout.
DEFAULT_VARIANTS_DIR = Path(__file__).resolve().parents[3] / "seeds" / "variants"


class SeedVariant(BaseModel):
    """One ``seeds/variants/*.json`` file.

    ``skill_set`` is accepted but ignored. It is derived at seed time from the
    resume's skills line and its per-bullet tags, so that the stored array can
    never drift from the resume it claims to describe — and so that a
    ``skills.yaml`` edit is picked up by re-running this command, the mirror of
    ``extract reresolve`` on the requirement side.

    ``content`` is likewise the raw ``.docx`` transcription, with no bullet
    tags: those live in ``seeds/bullet_tags.yaml`` and are merged in here. The
    generator overwrites these JSON files wholesale on every resume edit, so a
    tag stored in one would be destroyed by an ordinary edit, in silence.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    target: str
    source_path: str | None = None
    content: dict[str, Any]
    skill_set: list[str] = Field(default_factory=list)


def load_variants(directory: Path) -> list[SeedVariant]:
    """Read every variant file in ``directory``, sorted by key."""
    files = sorted(directory.glob("*.json"))
    if not files:
        raise ValueError(
            f"no variant files in {directory}; "
            "run `python scripts/variants-from-docx.py <resume-dir>` first"
        )
    variants = [SeedVariant.model_validate_json(path.read_text(encoding="utf-8")) for path in files]
    keys = [variant.key for variant in variants]
    if len(set(keys)) != len(keys):
        raise ValueError(f"duplicate variant key among {keys}")
    return sorted(variants, key=lambda variant: variant.key)


#: ``backend/seeds/bullet_tags.yaml`` in a source checkout.
DEFAULT_BULLET_TAGS_PATH = Path(__file__).resolve().parents[3] / "seeds" / "bullet_tags.yaml"


class BulletTags(BaseModel):
    """What one resume line proves, and what backs the numbers in it."""

    model_config = ConfigDict(extra="forbid")

    skills: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)


class BulletTagFile(BaseModel):
    """``seeds/bullet_tags.yaml``, keyed by bullet ID."""

    model_config = ConfigDict(extra="forbid")

    bullets: dict[str, BulletTags] = Field(default_factory=dict)


def load_bullet_tags(path: Path) -> dict[str, dict[str, list[str]]]:
    """Read the tag file, or return nothing when it is absent.

    Absent is allowed: the variants are seedable without tags, they are simply
    inert — no bullet can be selected as proof of anything. Refusing here would
    make a fresh checkout unable to seed at all.
    """
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parsed = BulletTagFile.model_validate(raw)
    return {
        key: {"skills": tags.skills, "claims": tags.claims} for key, tags in parsed.bullets.items()
    }


def approved_claim_keys(path: Path) -> frozenset[str]:
    """The claim keys a bullet is allowed to cite.

    Read from ``claims.yaml`` rather than from the ``claim`` table, because the
    verdict in that file is the operator's live decision and the table is a
    snapshot of the last ``seed claims`` run. Flipping a claim to ``no`` does
    not delete its row — so gating on the table would let a bullet keep citing
    a claim the operator has since withdrawn.
    """
    if not path.exists():
        return frozenset()
    return frozenset(claim.key for claim in load_claims(path).claims if claim.approved)


def check_bullet_tags(
    variants: list[SeedVariant],
    tags: dict[str, dict[str, list[str]]],
    claims_path: Path,
) -> None:
    """Refuse the whole seed if any bullet tag does not resolve.

    Raises:
        ValueError: listing every problem across every variant.

    Before anything is written, and across all six files rather than the first
    that fails — the same pre-flight discipline as ``seed companies``, learned
    from the ``reserved`` tag that failed on row 44 after 43 inserts.

    A tag naming a bullet that no longer exists is a failure too, and the one
    most likely to happen: bullet IDs are positional, so reordering a resume
    renumbers them. Silently ignoring an orphaned tag would move its skills and
    its claims onto whichever line inherited the number.
    """
    vocabulary = get_vocabulary()
    approved = approved_claim_keys(claims_path)
    problems: list[str] = []
    known: set[str] = set()

    for variant in variants:
        merged = apply_bullet_tags(variant.content, tags)
        known |= bullet_ids(merged)
        problems += [
            f"{variant.key}/{problem}"
            for problem in bullet_tag_problems(
                merged, approved_claims=approved, vocabulary=vocabulary
            )
        ]

    problems += [f"tag {key!r} names no bullet in any variant" for key in sorted(set(tags) - known)]

    if problems:
        listed = "\n  ".join(problems)
        raise ValueError(f"{len(problems)} bullet tag problem(s):\n  {listed}")


async def _seed_variants(
    directory: Path, tags: dict[str, dict[str, list[str]]], as_json: bool
) -> int:
    """Upsert every variant, recomputing ``skill_set`` from the vocabulary."""
    vocabulary = get_vocabulary()
    variants = load_variants(directory)
    rows: list[dict[str, Any]] = []

    async with session_scope() as session:
        for variant in variants:
            content = apply_bullet_tags(variant.content, tags)
            groups = content.get("skills") or []
            tokens = variant_skill_set(content, vocabulary=vocabulary)
            missing = unresolved_items(groups, vocabulary=vocabulary)
            unproven = unproven_skills(content, vocabulary=vocabulary)

            existing = (
                await session.execute(select(ResumeVariant).where(ResumeVariant.key == variant.key))
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    ResumeVariant(
                        key=variant.key,
                        name=variant.name,
                        target=variant.target,
                        content=content,
                        skill_set=list(tokens),
                        source_path=variant.source_path,
                    )
                )
                action = "created"
            else:
                existing.name = variant.name
                existing.target = variant.target
                existing.content = content
                existing.skill_set = list(tokens)
                existing.source_path = variant.source_path
                action = "updated"

            rows.append(
                {
                    "key": variant.key,
                    "action": action,
                    "skills": len(tokens),
                    "unmatched": len(missing),
                    "bullets": _count_bullets(content),
                    "untagged": len(untagged_bullets(content)),
                    "unproven": list(unproven),
                    "missing": list(missing),
                }
            )
        await session.commit()

    if as_json:
        echo_json({"vocabulary": vocabulary.version, "variants": rows})
        return 0

    echo_table(
        ["variant", "action", "skill tokens", "unmatched items", "bullets", "untagged"],
        [
            [
                row["key"],
                row["action"],
                str(row["skills"]),
                str(row["unmatched"]),
                str(row["bullets"]),
                str(row["untagged"]),
            ]
            for row in rows
        ],
    )
    untagged = sum(int(row["untagged"]) for row in rows)
    if untagged:
        echo()
        echo(
            f"  {untagged} bullet(s) carry no skill tags. They are inert rather "
            "than wrong: nothing can select them as proof of a requirement."
        )
    undemonstrated = sorted({token for row in rows for token in row["unproven"]})
    if undemonstrated:
        echo()
        echo(
            f"  {len(undemonstrated)} token(s) are claimed on a skills line with "
            "no bullet proving them. They can score a requirement MET while "
            "leaving generation with no sentence to cite:"
        )
        echo(f"    {', '.join(undemonstrated)}")
    unmatched = sorted({item for row in rows for item in row["missing"]})
    if unmatched:
        echo()
        echo(
            f"  {len(unmatched)} distinct skill item(s) name nothing in "
            f"{vocabulary.version}. They can never match a requirement, however "
            "often employers ask for them:"
        )
        for item in unmatched[:25]:
            echo(f"    {item[:88]}")
        if len(unmatched) > 25:
            echo(f"    ... and {len(unmatched) - 25} more")
    return 0


def _count_bullets(content: dict[str, Any]) -> int:
    """Bullets across experience and projects, for the summary line."""
    total = 0
    for role in content.get("experience") or []:
        for block in role.get("blocks") or []:
            total += len(block.get("bullets") or [])
    for project in content.get("projects") or []:
        total += len(project.get("bullets") or [])
    return total


@app.command("variants")
def variants(
    directory: Annotated[
        Path | None,
        typer.Option("--dir", help="Variant directory; backend/seeds/variants by default."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Load the six resume variants, deriving ``skill_set`` from the vocabulary.

    Re-runnable. Re-run it after editing ``skills.yaml`` — the stored token set
    is recomputed from ``content.skills`` every time, so the supply half of the
    coverage intersection stays derived from the same file as the demand half.

    Per-bullet tags are merged in from ``seeds/bullet_tags.yaml``, so re-run it
    after editing that file too — and after ``seed claims``, since a claim the
    operator has withdrawn makes every bullet citing it a load failure here.
    """
    resolved = directory or DEFAULT_VARIANTS_DIR
    if not resolved.is_dir():
        error(f"No variant directory at {resolved}.")
        raise typer.Exit(code=1)
    # Every refusal the loader and the vocabulary can raise, raised before a
    # single row is written — the lesson of the `reserved` tag that failed on
    # row 44 of the company seed.
    try:
        get_vocabulary()
        tags = load_bullet_tags(DEFAULT_BULLET_TAGS_PATH)
        check_bullet_tags(load_variants(resolved), tags, DEFAULT_CLAIMS_PATH)
    except (ValidationError, ValueError, ScoutError) as exc:
        error(f"The variant files are not valid: {exc}")
        raise typer.Exit(code=1) from exc

    get_settings()
    raise SystemExit(run_async(_seed_variants(resolved, tags, as_json)))


# ---------------------------------------------------------------------------
# The claims ledger
# ---------------------------------------------------------------------------

#: ``backend/seeds/claims.yaml`` in a source checkout.
DEFAULT_CLAIMS_PATH = Path(__file__).resolve().parents[3] / "seeds" / "claims.yaml"

#: Placeholders that mean "not looked at yet". Refused whenever an
#: ``evidence_ref`` is supplied at all.
#:
#: The guard cannot verify that a reference is true — nothing here can. What it
#: stops is the hurried version of getting past the gate, which is the failure
#: that actually happens: a row marked done in thirty seconds and never revisited.
EVIDENCE_PLACEHOLDERS: frozenset[str] = frozenset(
    {"todo", "tbd", "n/a", "na", "none", "?", "-", "--", "xxx", "tk", "fixme", "pending"}
)

#: An `evidence_ref` shorter than this is not a reference to anything.
MIN_EVIDENCE_REF = 10

#: Recorded when a claim is approved with no reference supplied. Distinguishable
#: at a glance from a real one, so a later pass can find the rows that still rest
#: on memory alone.
ATTESTED_PREFIX = "operator-attested"


class SeedClaim(BaseModel):
    """One row of ``seeds/claims.yaml``.

    ``verdict`` is the whole workflow: ``yes`` seeds the claim, ``no`` refuses
    it. Defaulting to ``no`` means an unreviewed row is never seeded — the
    ledger's job is to refuse what it cannot vouch for, and silence has to mean
    refusal rather than assent.

    ``evidence_ref`` is optional. A ``yes`` on its own records that the operator
    personally vouched for the figure; a reference records where anyone else can
    check it. The second is what still holds in a year, so it is worth supplying
    on anything a stranger might question — and required for nothing, because a
    gate people route around is worse than one they use.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    statement: str
    project: str
    verdict: Literal["yes", "no"] = "no"

    @field_validator("verdict", mode="before")
    @classmethod
    def _accept_yaml_booleans(cls, value: object) -> object:
        """Normalise what YAML hands us for a bare ``yes`` or ``no``.

        YAML 1.1 resolves unquoted ``yes``/``no`` to booleans — the same rule
        that turns Norway's ``NO`` into ``False``. The file and its instructions
        both say to write ``verdict: yes``, so refusing the boolean would mean
        rejecting exactly the input this workflow asks for, with an error message
        about ``True`` that explains nothing.

        Quoting in the file would work too, and would be a trap: the one row
        someone types without quotes fails, and it fails as an approval turning
        into an error rather than into a rejection.
        """
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, str):
            return value.strip().lower()
        return value

    evidence_ref: str | None = None
    metric_value: str | None = None
    metric_unit: str | None = None
    confidentiality: ClaimConfidentiality = ClaimConfidentiality.INTERNAL
    tags: list[str] = Field(default_factory=list)
    verified_at: date
    expires_at: date | None = None

    @property
    def approved(self) -> bool:
        """Whether this claim may be seeded."""
        return self.verdict == "yes"

    def resolved_evidence(self) -> str:
        """The reference to store, or an attestation when none was given."""
        if self.evidence_ref and self.evidence_ref.strip():
            return self.evidence_ref.strip()
        return f"{ATTESTED_PREFIX} {self.verified_at.isoformat()}"


class ClaimSeedFile(BaseModel):
    """The whole file."""

    model_config = ConfigDict(extra="forbid")

    claims: list[SeedClaim]


def load_claims(path: Path) -> ClaimSeedFile:
    """Read and validate the ledger seed file.

    Args:
        path: The YAML file.

    Returns:
        The parsed file, including rejected rows — the caller decides what to
        do with a ``no``, because "how many did I reject" is worth reporting.

    Raises:
        ValueError: On a duplicate key, or an approved row whose supplied
            ``evidence_ref`` is a placeholder or too short to be a reference.
    """
    seed = ClaimSeedFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    keys = [claim.key for claim in seed.claims]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate claim key(s): {', '.join(duplicates)}")

    # Only approved rows are checked. A rejected one is not going anywhere, so
    # its evidence_ref is nobody's problem.
    bad: list[str] = []
    for claim in seed.claims:
        if not claim.approved or claim.evidence_ref is None:
            continue
        supplied = claim.evidence_ref.strip()
        if supplied.lower() in EVIDENCE_PLACEHOLDERS or len(supplied) < MIN_EVIDENCE_REF:
            bad.append(f"{claim.key} ({supplied!r})")
    if bad:
        raise ValueError(
            f"{len(bad)} approved claim(s) have an evidence_ref that references nothing. "
            "Either give a real one — a dashboard, query, repo path, PR, report — or "
            "remove the field and let the `yes` stand as your own attestation. "
            + ", ".join(bad[:5])
        )
    return seed


async def _seed_claims(path: Path, as_json: bool) -> int:
    """Upsert every claim on its natural key."""
    seed = load_claims(path)
    approved = [claim for claim in seed.claims if claim.approved]
    rejected = [claim.key for claim in seed.claims if not claim.approved]
    rows: list[dict[str, Any]] = []

    async with session_scope() as session:
        for entry in approved:
            existing = (
                await session.execute(select(Claim).where(Claim.key == entry.key))
            ).scalar_one_or_none()
            values = {
                "statement": entry.statement,
                "metric_value": entry.metric_value,
                "metric_unit": entry.metric_unit,
                "project": entry.project,
                "evidence_ref": entry.resolved_evidence(),
                "confidentiality": entry.confidentiality,
                "tags": entry.tags,
                "verified_at": datetime.combine(entry.verified_at, time.min, tzinfo=UTC),
                "expires_at": (
                    datetime.combine(entry.expires_at, time.min, tzinfo=UTC)
                    if entry.expires_at
                    else None
                ),
            }
            if existing is None:
                session.add(Claim(key=entry.key, **values))
                action = "created"
            else:
                for field, value in values.items():
                    setattr(existing, field, value)
                action = "updated"
            rows.append(
                {
                    "key": entry.key,
                    "action": action,
                    "confidentiality": entry.confidentiality.value,
                    "attested_only": entry.evidence_ref is None,
                }
            )
        await session.commit()

    restricted = [row["key"] for row in rows if row["confidentiality"] == "restricted"]
    attested = [row["key"] for row in rows if row["attested_only"]]
    if as_json:
        echo_json(
            {"claims": rows, "rejected": rejected, "restricted": restricted, "attested": attested}
        )
        return 0

    created = sum(1 for row in rows if row["action"] == "created")
    echo(
        f"{len(rows)} approved claim(s): {created} created, {len(rows) - created} updated. "
        f"{len(rejected)} rejected and not seeded."
    )
    if attested:
        echo()
        echo(
            f"  {len(attested)} claim(s) rest on your attestation with no reference. That holds "
            "today; in a year it will not. Worth a real one on anything a stranger might question:"
        )
        for key in attested[:10]:
            echo(f"    {key}")
        if len(attested) > 10:
            echo(f"    ... and {len(attested) - 10} more")
    if restricted:
        echo()
        echo(
            f"  {len(restricted)} claim(s) are RESTRICTED. They earn coverage when scoring "
            "but are never emitted into a document sent outside an allow-listed employer:"
        )
        for key in restricted:
            echo(f"    {key}")
    return 0


@app.command("claims")
def claims(
    path: Annotated[
        Path | None,
        typer.Option("--file", help="Seed file; backend/seeds/claims.yaml by default."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Load the claims ledger.

    Each row carries ``verdict: yes`` or ``verdict: no``. Only ``yes`` is
    seeded; ``no`` is the default, so a row nobody has looked at is never
    written. The ledger's job is to refuse what it cannot vouch for, and that
    only works if silence means refusal.
    """
    resolved = path or DEFAULT_CLAIMS_PATH
    if not resolved.exists():
        error(f"No claims file at {resolved}.")
        raise typer.Exit(code=1)
    try:
        load_claims(resolved)
    except (ValidationError, ValueError, ScoutError) as exc:
        error(f"The claims file is not ready: {exc}")
        raise typer.Exit(code=1) from exc

    get_settings()
    raise SystemExit(run_async(_seed_claims(resolved, as_json)))


__all__ = [
    "DEFAULT_BULLET_TAGS_PATH",
    "DEFAULT_CLAIMS_PATH",
    "DEFAULT_SEED_PATH",
    "DEFAULT_VARIANTS_DIR",
    "BulletTagFile",
    "BulletTags",
    "ClaimSeedFile",
    "SeedClaim",
    "SeedCompany",
    "SeedFile",
    "SeedVariant",
    "app",
    "approved_claim_keys",
    "check_bullet_tags",
    "load_bullet_tags",
    "load_claims",
    "load_seed",
    "load_variants",
]
