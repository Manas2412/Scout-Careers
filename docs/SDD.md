# SOFTWARE DESIGN DOCUMENT — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for module internals below the package boundary — public
function signatures, class responsibilities, transaction boundaries, concurrency
limits, the exception hierarchy and the frontend component structure.
`ARCHITECTURE.md` wins on system-level concerns and the invariants;
`DATA_MODEL.md` wins on tables and columns; `API.md` wins on endpoint paths,
envelopes and error codes; each subsystem document (`SOURCE_ADAPTERS.md`,
`MATCH_SCORING.md`, `CLAIMS_LEDGER.md`, `DOCUMENT_GENERATION.md`,
`AI_ARCHITECTURE.md`, `APPLICATION_PIPELINE.md`, `EMAIL_INGESTION.md`,
`COMPANY_REGISTRY.md`) wins on its own algorithm. Where this file disagrees with
any of them, **this file is wrong and must be corrected.**

---

## 1. Purpose, audience and position

### 1.1 What this document is for

This is the document an engineer implements from. It assumes the reader has read
`ARCHITECTURE.md` and accepts its invariants, and it answers the questions that
document deliberately leaves open: what the modules actually expose, what the
functions are called, what types they take, where a transaction begins and ends,
what runs concurrently with what, and what happens when each thing fails.

The test of this document is narrow and checkable: **an engineer should be able
to create every file in `backend/src/scout_careers/` and `frontend/src/` with
correct signatures before writing a line of logic**, and the resulting skeleton
should type-check.

### 1.2 Audience

| Reader | What they should take from it |
|---|---|
| The implementing engineer (human or agent) | Module boundaries, signatures, error behaviour, transaction scope |
| A reviewer of a merge request | Whether the change sits in the right module and respects the layering rule |
| The operator, six months later | Why a thing is where it is, when a bug does not reproduce |

### 1.3 Altitude, and the relationship to the other design documents

```
ARCHITECTURE.md      what the system is, the invariants, module boundaries,
                     technology decisions, the pipeline as eleven stages
        │
HLD.md               runtime topology, deployment units, sequence diagrams
(companion)          per pipeline run, cross-cutting behaviour under failure
        │
▶ SDD.md             this file — package internals, signatures, algorithms,
                     concurrency, transactions, error mapping, the frontend
        │
code                 the implementation, whose tests assert what is here
```

`ARCHITECTURE.md` §10 lists `HLD.md` / `SOLUTION_ARCHITECTURE.md` / `SDD.md` as
"design at decreasing altitude". At the time of writing, `HLD.md` does not yet
exist; **`ARCHITECTURE.md` is this document's parent until it does**, and nothing
here depends on the HLD's contents. When the HLD is written it inherits the
module boundaries stated in `ARCHITECTURE.md` §5 and refined here; it does not
get to redefine them.

This document adds **no new invariants, entities, endpoints or configuration
keys.** Every `Settings` key in §9 already appears in a subsystem document. Every
endpoint referenced already appears in `API.md`. A signature here that implies an
endpoint `API.md` does not define is a defect in this file.

### 1.4 The invariants, restated as code obligations

Because this is the level at which they are either enforced or lost:

| Invariant (`ARCHITECTURE.md` §3) | Where it is enforced in this design |
|---|---|
| 1 — no automated submission | No module exposes a submit function. `review/service.py` `approve()` writes a row and returns download links. §2.10, §8.5 |
| 2 — no automated outbound mail to people | `mail/gmail.py` `GmailClient.send()` raises `OutboundPolicyViolation` for any recipient except `settings.mail_operator_address`; exactly one call site, in `mail/digest.py`. §2.11 |
| 3 — generation cites only the ledger | `ledger/validate.py` runs before any artifact is attachable, plus two DB triggers. §2.8, §3.5 |
| 4 — the never-scrape list is absolute | `sources/policy.py::assert_fetch_allowed` inside `SourceHttpClient`, on every request, after redirects. Not reachable from `Settings`. §2.3 |
| 5 — adapter failure is isolated | `ingest/runner.py` two-layer catch, `return_exceptions=True`. §2.5, §4.3 |
| 6 — no secrets in code or logs | `common/logging.py` processor chain drops known-sensitive keys; `common/secrets.py` owns the token file. §2.1 |
| 7 — every artifact is reproducible | `artifact` rows carry `model`, `prompt_version`, `variant_id`, `claim_usage`. §2.9 |
| 8 — robots and rate limits respected | `sources/http.py` robots cache + Redis token buckets. §2.3 |

---

## 2. Module-by-module design

The layering rule from `ARCHITECTURE.md` §5 is absolute and is the first thing a
review checks:

- **Routers are thin, services are thick.** No business logic in `api/`.
- **No HTTP concerns below `api/`.** No service returns a `Response`, raises
  `HTTPException`, or knows a status code. Services raise domain exceptions
  (§8); `api/errors.py` maps them.
- **`sources/` never touches the database.** It returns DTOs; `ingest/` persists.
- **Nothing below `api/` imports from `api/`.** Enforced by an
  `import-linter` contract in CI.

Dependency direction, which is acyclic:

```
common  ◀── everything
db      ◀── registry, ingest, extract, scoring, ledger, generate, review,
            mail, tracking, scheduler, api
llm     ◀── extract, scoring, ledger, generate, mail
sources ◀── ingest, registry                (sources → common only)
extract ◀── scoring
scoring ◀── generate, review
ledger  ◀── generate, scoring (read-only), api
generate◀── review
tracking◀── mail, api
api     ── depends on all services; nothing depends on api
```

---

### 2.1 `common/` — configuration, logging, types, time, hashing, IDs

**Responsibility.** Everything with no domain knowledge that every other module
needs. It imports nothing from the rest of the package.

```
common/
├── config.py     Settings (pydantic-settings), get_settings()
├── types.py      enums mirroring the Postgres ENUMs, shared aliases
├── logging.py    structlog configuration, run/correlation context
├── clock.py      Clock protocol, SystemClock, FrozenClock
├── hashing.py    content_hash, checksum
├── ids.py        ULID generation and validation
├── secrets.py    TokenStore (Fernet-encrypted OAuth token file)
├── errors.py     the exception hierarchy (§8)
└── text.py       whitespace/unicode normalisation shared by ingest and ledger
```

```python
# common/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="forbid", frozen=True,
    )
    # ... fields grouped in §9 ...


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. The ONLY construction site for Settings.

    `extra="forbid"` means an unknown environment key is a boot failure, which
    is how a renamed setting is caught at deploy rather than at 08:00.
    """
    return Settings()
```

`Settings` is `frozen=True`. Runtime-mutable settings — the small set exposed by
`PATCH /api/v1/settings` (`API.md` §7) — are **not** on this object; they live in
a single-row `app_setting` table read through `api/deps.py::runtime_settings()`.
Mixing the two is how a value ends up half-configurable.

```python
# common/clock.py
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test seam (§10.3). Advances only when told to."""

    def __init__(self, at: datetime) -> None: self._at = at
    def now(self) -> datetime: return self._at
    def advance(self, **delta) -> None: self._at += timedelta(**delta)
```

**Every module that needs the current time takes a `Clock`.** No service calls
`datetime.now()` directly. This is not fastidiousness: recency decay
(`MATCH_SCORING.md` §5.1), the relative-date parser (`SOURCE_ADAPTERS.md` §9.5,
which resolves against *run start*, not `now()`), `v_ghosted`, and the follow-up
quiet periods are all time-dependent and all need to be testable.

```python
# common/hashing.py
def content_hash(description_text: str) -> str:
    """sha256 of the normalised description. DATA_MODEL.md §4.1.

    The ONLY definition of change detection in the system. `sources/` does not
    compute it (SOURCE_ADAPTERS.md §2.2); neither does anything else.
    """
    return hashlib.sha256(normalise_for_hash(description_text).encode()).hexdigest()


def file_checksum(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()
```

```python
# common/ids.py
def new_ulid() -> str:
    """26-character Crockford base-32 ULID for externally addressable rows:
    job_posting, review_item, application, artifact, run_log."""
```

**Logging.** `structlog`, JSON renderer in production, one line per pipeline
stage per run, `run_id` bound in a `contextvars` context.

```python
# common/logging.py
SENSITIVE_KEYS = frozenset({
    "token", "access_token", "refresh_token", "authorization", "cookie",
    "set-cookie", "password", "api_key", "secret", "credentials", "body",
    "description_text", "email_body", "resume_content",
})


def scrub(_logger, _method, event: dict) -> dict:
    """Drops sensitive keys anywhere in the event dict. Invariant 6.

    Runs LAST in the processor chain, so nothing added by a later processor
    escapes it. A dropped key is replaced with the literal '[scrubbed]' rather
    than removed, so its absence is visible in the log line.
    """
```

**Error behaviour.** `common/` raises only `ConfigError` (boot) and
`ValueError`. It never catches.

---

### 2.2 `db/` — models, session lifecycle, migrations

```
db/
├── base.py        DeclarativeBase, TimestampMixin, naming convention
├── models.py      every table in DATA_MODEL.md, one class each
├── enums.py       SQLAlchemy ENUM bindings to common/types.py
├── session.py     engine, async_sessionmaker, session_scope()
├── views.py       read-only mappings for the DATA_MODEL.md §10 views
│                  (v_funnel, v_ghosted, v_mail_review_queue, v_followup_due)
└── migrations/    Alembic env.py + versions/
```

```python
# db/session.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,                 # postgresql+asyncpg://…
        pool_size=10, max_overflow=5, pool_pre_ping=True,
        pool_recycle=1800, echo=False,
        connect_args={"server_settings": {"application_name": "scout-careers",
                                          "statement_timeout": "30000"}},
    )


SessionFactory = async_sessionmaker[AsyncSession]


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """One unit of work. Commits on clean exit, rolls back on any exception.

    This is the ONLY way a service opens a session outside a request. It is
    deliberately a context manager rather than a decorator so that the extent
    of a transaction is visible as indentation — see §4.5 and §5.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
```

**Models.** `db/models.py` is a mechanical transcription of `DATA_MODEL.md`. Two
rules that are not mechanical:

1. **`Mapped[...]` annotations are exact**, including nullability. A column that
   is `NOT NULL` in `DATA_MODEL.md` is `Mapped[str]`, never `Mapped[str | None]`.
   The generated OpenAPI schema, and therefore the frontend client, inherits
   this.
2. **No relationship is `lazy="select"`.** Every relationship is
   `lazy="raise_on_sql"`. In an async session a lazy load is an
   `MissingGreenlet` at runtime, usually in production and never in the test
   that mattered; making it raise at development time converts the failure into
   an explicit `selectinload()` at the query site.

```python
# db/models.py (shape)
class JobPosting(Base, TimestampMixin):
    __tablename__ = "job_posting"

    id:               Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    company_id:       Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    source_id:        Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    external_id:      Mapped[str]
    title:            Mapped[str]
    description_text: Mapped[str]
    content_hash:     Mapped[str] = mapped_column(CHAR(64))
    posted_at:        Mapped[datetime | None]
    first_seen_at:    Mapped[datetime]
    last_seen_at:     Mapped[datetime]
    closed_at:        Mapped[datetime | None]
    filtered_out:     Mapped[bool] = mapped_column(default=False)
    filter_reason:    Mapped[str | None]
    raw:              Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    requirements: Mapped[list["Requirement"]] = relationship(lazy="raise_on_sql")
```

**Migrations.** One Alembic revision per schema change, ID ≤ 32 characters
(`DATA_MODEL.md` §11). Enum additions get a standalone revision because
`ALTER TYPE … ADD VALUE` cannot share a transaction. The four
triggers/functions — `assert_artifact_validated`,
`assert_attached_artifact_not_failed` (`CLAIMS_LEDGER.md` §6.1),
`application_status_rank`, `fn_application_status_refresh`
(`APPLICATION_PIPELINE.md` §5.2) — ship as `op.execute()` in revisions and are
tested by asserting the exception, not by reading the catalogue.

**Error behaviour.** `db/` raises SQLAlchemy exceptions unmodified. Translation
to `ConflictError` / `NotFoundError` happens in the service that knows what the
constraint means, not in a generic handler that guesses from a constraint name.

---

### 2.3 `sources/` — adapters, HTTP, policy, normalisation

Fully specified by `SOURCE_ADAPTERS.md`; restated here only where the SDD adds
something. **This layer never opens a database session.**

```
sources/
├── base.py        SourceAdapter Protocol, RawPosting, ProbeResult, SourceResult
├── http.py        build_client(), SourceHttpClient, robots cache, token buckets
├── policy.py      NEVER_FETCH_HOSTS, assert_fetch_allowed()   (frozen constants)
├── normalise.py   html_to_text, parse_location, infer_seniority, parse_posted_at
├── registry.py    ADAPTERS mapping + startup completeness assertion
├── greenhouse.py lever.py ashby.py workday.py smartrecruiters.py
├── workable.py recruitee.py google.py amazon.py microsoft.py
└── mail_alerts.py MailAlertAdapter (reads mail/, holds no HTTP client)
```

Public surface, beyond the `SourceAdapter` protocol in `SOURCE_ADAPTERS.md` §2.1:

```python
# sources/registry.py
ADAPTERS: dict[AtsType, type[SourceAdapter]]

def get_adapter_class(adapter: AtsType) -> type[SourceAdapter]:
    """Raises AdapterNotRegistered. Called by ingest and by registry/detect."""

def assert_registry_complete() -> None:
    """Every non-manual ats_type member has an entry and every entry satisfies
    isinstance(cls, SourceAdapter). Called from the FastAPI lifespan; a miss is
    a boot failure, not a 500 at 08:00."""
```

```python
# sources/http.py
class SourceHttpClient:
    def __init__(self, client: httpx.AsyncClient, *, source_id: int,
                 adapter: AtsType, bucket_key: str, redis: Redis,
                 breaker: InRunBreaker, settings: Settings, clock: Clock) -> None: ...

    async def get_json(self, url: str, *, params: dict | None = None,
                       headers: dict | None = None) -> Any: ...
    async def post_json(self, url: str, *, json: dict,
                        headers: dict | None = None) -> Any: ...
    async def get_text(self, url: str, *, params: dict | None = None) -> str: ...

    @property
    def stats(self) -> RequestStats:
        """requests, retries, rate_limit_wait_ms — copied into SourceResult."""
```

Every request passes through the same ordered gate. The order is load-bearing:

```python
async def _request(self, method: str, url: str, **kw) -> httpx.Response:
    assert_fetch_allowed(url)                 # 1. policy — before a socket exists
    await self._breaker.check(self.bucket_key)  # 2. in-run circuit breaker
    await self._robots.assert_allowed(url)    # 3. robots.txt (invariant 8)
    for attempt in range(MAX_ATTEMPTS):
        await self._bucket.acquire(deadline=self.settings.rate_limit_wait_s)  # 4.
        resp = await self._client.request(method, url, **kw)
        assert_fetch_allowed(str(resp.url))   # 5. post-redirect, mid-flight
        if self._retryable(resp):
            await asyncio.sleep(backoff_delay(attempt, retry_after(resp)))
            continue
        return self._capped(resp)             # 6. response-size cap
```

Steps 1 and 5 are the same check at two points because a 302 into LinkedIn is
the case a config-time check misses. `assert_fetch_allowed` takes no settings
argument and `policy.py` imports nothing from `common/config.py` — a test
asserts the constant is unreachable from `Settings`.

**Error behaviour.** Adapters never catch their own exceptions
(`SOURCE_ADAPTERS.md` §2.1). `SourceHttpClient` raises `AdapterTransportError`,
`AdapterHttpError`, `RateLimitTimeout`, `CircuitOpen`, `RobotsDenied`,
`DeniedByPolicy`; adapters raise `AdapterSchemaError` and `AdapterConfigError`.
`ingest/runner.py` classifies all of them into a `SourceResult` status.

---

### 2.4 `registry/` — companies, sources, ATS detection

**Responsibility.** CRUD for `company` and `source`, tag validation, per-company
defaults, and the paste-a-URL detection flow (`COMPANY_REGISTRY.md` §2).

```python
# registry/service.py
class CompanyService:
    def __init__(self, session: AsyncSession, clock: Clock, settings: Settings) -> None: ...

    async def list_companies(self, f: CompanyFilter, page: CursorPage) -> Page[CompanyRead]: ...
    async def get(self, company_id: int) -> CompanyRead: ...
    async def create(self, payload: CompanyCreate) -> CompanyRead: ...
    async def update(self, company_id: int, payload: CompanyPatch) -> CompanyRead: ...
    async def soft_delete(self, company_id: int) -> None: ...

    async def add_source(self, company_id: int, payload: SourceCreate) -> SourceRead:
        """Validates config through the adapter's config_model, canonicalises it
        (model_dump(mode='json'), sorted keys) so UNIQUE (company_id, adapter,
        config) collides as designed, seeds poll_interval_minutes from the
        adapter's ClassVar. Raises ConflictError('company.duplicate_source')."""

    async def set_source_enabled(self, source_id: int, enabled: bool) -> SourceRead:
        """Re-enabling resets consecutive_failures to 0 and clears last_error.
        The system never re-enables itself (SOURCE_ADAPTERS.md §4.8)."""
```

```python
# registry/detect.py
@dataclass(frozen=True, slots=True)
class DetectionCandidate:
    adapter: AtsType
    config: dict[str, Any]
    company_name_guess: str | None


async def detect(url: str, *, http: DetectHttpClient,
                 session: AsyncSession) -> DetectionResult:
    """COMPANY_REGISTRY.md §2.1, four steps in strict order:

      1. assert_fetch_allowed(url)         → DeniedByPolicy ⇒ 403
      2. match_patterns(url)               → 0 ⇒ UndetectableSource (422)
                                             >1 ⇒ candidates[], no probe
      3. adapter.probe()                   → ProbeResult, never skipped
      4. existing company / source lookup

    Read-only. Creates nothing. A pattern whose captures the config model
    rejects is a non-match, not an error.
    """


def match_patterns(url: str) -> list[DetectionCandidate]:
    """The twenty-row table in COMPANY_REGISTRY.md §2.2, tried in order,
    against the normalised URL (host lower-cased, query/fragment/trailing
    slash stripped, locale segment removed)."""


async def scan_body_for_board(html: str) -> list[DetectionCandidate]:
    """Last resort. One request already made, 512 KB cap, selectolax, NO
    JavaScript execution, no second hop (COMPANY_REGISTRY.md §2.2)."""
```

**Tag validation** is the one non-obvious write-path rule: `company.tags`
entries must be `axis:value` with `axis` in the known set; the value list is
open, the axis list is closed. Violation raises
`ValidationError('company.tag_invalid')` → 422.

**Error behaviour.** `NotFoundError`, `ConflictError`, `ValidationError`,
`DeniedByPolicy`. Detection never raises for an unreachable endpoint — an
unreachable probe is a 200 with `reachable: false`, because "I tried and it did
not answer" is a result, not a failure.

---

### 2.5 `ingest/` — run orchestration, persistence, dedup, filtering

**Responsibility.** Stages ①–④ and ⑩ of the pipeline. The only module that turns
a `RawPosting` into a row, and the only module that decides what a discovery run
does next.

```
ingest/
├── runner.py     run_discovery(), fetch_one(), the semaphore, failure isolation
├── persist.py    upsert_posting(), bump_last_seen(), invalidate_scores()
├── dedup.py      cross-source collapse (§3.1)
├── filters.py    the deterministic filter chain (§3.2)
├── close.py      the per-source two-run close rule
├── results.py    SourceResult, RunStats
└── pipeline.py   the stage orchestrator: discover → … → enqueue
```

```python
# ingest/runner.py
async def run_discovery(
    *, run_id: str, source_ids: list[int] | None,
    factory: SessionFactory, redis: Redis, settings: Settings, clock: Clock,
) -> RunSummary:
    """Stages ①–④. Owns failure isolation (ARCHITECTURE.md invariant 5).

    One httpx.AsyncClient for the whole run. asyncio.Semaphore(
    settings.source_concurrency) bounds parallel sources. asyncio.gather(
    ..., return_exceptions=True) guarantees the run completes. CancelledError
    is re-raised, never swallowed, so the whole-run timeout can stop the run.
    """


async def fetch_one(
    client: httpx.AsyncClient, src: SourceRow, *, run_id: str, ...
) -> SourceResult:
    """Fetch, normalise and persist one source. Never raises: converts every
    exception into a SourceResult with one of the eleven statuses in
    SOURCE_ADAPTERS.md §10.2. Two layers of catch — the inner classifies, the
    outer guarantees the classifier itself cannot abort the run."""
```

```python
# ingest/pipeline.py
async def run_pipeline(
    *, run_id: str, source_ids: list[int] | None, deps: Deps
) -> RunSummary:
    """The eleven stages, each an awaited call with its own transaction (§5).

        results  = await run_discovery(...)            # ① ② ③ per source
        survivors= await apply_filters(run_id)         # ④
        extracted= await extract_stage.run(survivors)  # ⑤
        scored   = await scoring_stage.run(extracted)  # ⑥ ⑦
        drafted  = await generate_stage.run(scored)    # ⑧ ⑨
        queued   = await enqueue_stage.run(drafted)    # ⑩

    Every stage reads its input from the database and writes its output there.
    No stage takes the previous stage's in-memory objects. This is what makes
    stage re-execution a no-op (§5.2) and what lets a failure in ⑧ be retried
    without re-fetching a single posting.
    """
```

```python
# ingest/persist.py
async def upsert_posting(
    session: AsyncSession, *, source: SourceRow, raw: RawPosting, clock: Clock
) -> UpsertOutcome:
    """Identity is (source_id, external_id). Returns NEW | UPDATED | UNCHANGED.
    On UPDATED, invalidates match_score rows for the posting (§3.1)."""


async def invalidate_scores(session: AsyncSession, posting_id: str) -> int:
    """DELETE FROM match_score WHERE posting_id = :id. Deleting rather than
    flagging is correct: prompt_version is part of the natural key, so a stale
    row would otherwise be indistinguishable from an A/B arm."""
```

**Error behaviour.** `ingest/` fails closed on integrity (a posting that cannot
be persisted is not counted as fetched) and open on enrichment (a posting whose
extraction fails stays unextracted and visible). It never raises out of
`run_discovery` except `CancelledError` and infrastructure failures — Postgres or
Redis unreachable — which produce `run_log.status = 'failed'`
(`SOURCE_ADAPTERS.md` §10.4).

---

### 2.6 `extract/` — JD → structured requirements

```
extract/
├── schema.py     ExtractedRequirement, RequirementExtraction (MATCH_SCORING §2.2)
├── prompt.py     build_extract_messages() (MATCH_SCORING §2.3)
├── weights.py    BANDS, final_weight()
├── normalise.py  the three-stage skill resolver (MATCH_SCORING §3.2)
├── vocab/        skills.yaml, aliases.yaml, adjacency.yaml
└── service.py    ExtractionService
```

```python
# extract/service.py
class ExtractionService:
    def __init__(self, llm: LLMClient, factory: SessionFactory,
                 settings: Settings, clock: Clock) -> None: ...

    async def extract_posting(self, posting_id: str) -> ExtractionOutcome:
        """Stage ⑤ for one posting. Sequence, and the session discipline in it:

          1. open session → load posting + company → CLOSE SESSION
          2. cache lookup by (content_hash, prompt_version, model_id)
          3. NO SESSION HELD → call_structured(...)          ← §4.5
          4. ground every evidence_span in description_text; retry once at
             temperature 0 on failure (MATCH_SCORING.md §2.2)
          5. resolve normalised_skill for each row (deterministic first)
          6. open session → replace requirement rows in one transaction
        """

    async def run(self, posting_ids: Sequence[str]) -> StageResult:
        """Bounded fan-out at settings.llm_max_concurrency (4). Per-item failure
        isolation: one posting's failure is counted in run_log.stats.llm_failures
        and leaves that posting unextracted for the next run."""
```

```python
# extract/normalise.py
def normalise(phrase: str, session: AsyncSession) -> str | None:
    """Exact alias → trigram (>= settings.skill_trigram_threshold) → constrained
    LLM fallback over a closed Literal. Returns None when unresolved, and writes
    the phrase to skill_proposal. It can never mint a vocabulary token."""
```

Skipping rules that must not be lost in implementation:

- **Mail-sourced postings are never extracted.** `job_posting.raw` carries the
  `needs_description` flag from `mail_alerts`; a five-line stub produces
  confident, worthless requirements (`SOURCE_ADAPTERS.md` §7.3).
- **Zero hard requirements is not a perfect match.** The posting is routed to
  `needs_manual_review` (`MATCH_SCORING.md` §13.2).

---

### 2.7 `scoring/` — coverage, composite, gaps, ranking

```
scoring/
├── variant.py     VariantView: skill_set, bullets(), evidence_for()
├── vocabulary.py  VOCAB (composites, labels), ADJACENCY map
├── coverage.py    level_for(), bucket_coverage()
├── composite.py   recency(), hard_gate(), composite()
├── gaps.py        build_gaps()
├── rank.py        the six tie-break rules (MATCH_SCORING.md §6.1)
└── service.py     ScoringService
```

```python
# scoring/service.py
class ScoringService:
    async def score_posting(self, posting_id: str) -> list[MatchScoreRow]:
        """Stage ⑥ for one posting against every active variant.

        Pure set arithmetic over rows already in the database — no token cost
        beyond the residual coverage-judgement call for requirements the
        vocabulary could not resolve. Writes one match_score row per variant
        with prompt_version = settings.scoring_prompt_version, then sets
        is_recommended on the winner under the partial unique index
        match_one_recommendation_idx.
        """

    async def rank_and_recommend(self, posting_id: str) -> MatchScoreRow: ...
```

Signatures reproduced verbatim from `MATCH_SCORING.md`, which is canonical:

```python
# scoring/coverage.py
def level_for(req: Requirement, variant: VariantView
              ) -> tuple[CoverageLevel, Evidence | None, str | None]: ...
def bucket_coverage(rows: Sequence[tuple[Decimal, CoverageLevel]]) -> Decimal: ...

# scoring/composite.py
def recency(posted_at: datetime, now: datetime) -> Decimal: ...
def hard_gate(h: Decimal) -> Decimal: ...
def composite(h: Decimal, n: Decimal, tier: CompanyTier,
              posted_at: datetime, now: datetime) -> tuple[Decimal, Decimal]: ...
```

**All arithmetic is `Decimal`.** A float anywhere in this package is a review
failure (`DATA_MODEL.md` §1).

**Error behaviour.** Per-variant failure isolation: a variant that raises is
skipped and logged; the remaining variants still produce a recommendation. A
posting where *every* variant fails goes to `needs_manual_review`. Coverage
judgement that fails enforcement records the affected requirements as `missing`
with `note: "judgement unavailable"` — **scoring fails closed, understating
coverage, never overstating it** (`AI_ARCHITECTURE.md` §6.3).

---

### 2.8 `ledger/` — the claims store, disclosure, validation

The most safety-critical module. `CLAIMS_LEDGER.md` is canonical for its logic.

```
ledger/
├── store.py       ClaimService: CRUD, usage query, expiry sweep
├── disclosure.py  permitted_tiers(), candidate_claims()
├── pairs.py       restricted → public_fallback resolution (pairs.yaml)
├── detect.py      the eight regex families, span de-overlapping
├── assertions.py  Assertion, AssertionExtraction, the LLM assertion pass
├── validate.py    resolve(), validate_text() — the enforcement entry point
└── usage.py       claim_usage writes, inside the artifact transaction
```

```python
# ledger/validate.py
@dataclass(frozen=True, slots=True)
class Resolution:
    resolved: bool
    claim: Claim | None = None
    stale: bool = False
    note: str | None = None


async def validate_text(
    session: AsyncSession, *, text: str, cited_claim_ids: list[int],
    company_slug: str | None, llm: LLMClient, settings: Settings,
) -> ValidationReport:
    """Stage ⑨ and POST /api/v1/claims/validate.

    Detection is the UNION of the regex families and the LLM assertion pass;
    spans are de-overlapped longest-first. Every assertion is then resolved
    through the six-step chain in CLAIMS_LEDGER.md §5.4.

    Fails closed: if the assertion-extraction call fails, or returns a span
    that is not a verbatim substring of `text`, the report is
    passed=False with note='Assertion extraction unavailable.' A document
    nobody checked is never treated as checked.
    """


def resolve(assertion: Assertion, cited: Sequence[Claim],
            session: AsyncSession, permitted: set[str]) -> Resolution: ...
```

Three properties the implementation must preserve, each of which is a real
failure mode rather than a nicety:

1. **Superlatives and prose facts resolve only by explicit citation** (step 1).
   There is no fuzzy path to "first" or "only".
2. **Step 6 repairs rather than punishes.** A true, uncited number resolves
   against the permitted ledger and the missing citation is written, so
   `claim_usage` stays complete.
3. **Resolution is scoped to cited claims before the whole ledger**, which is
   what catches the cross-project splice — a true number from one project
   attached to a true achievement from another (`CLAIMS_LEDGER.md` §5.5).

**Error behaviour.** `passed: false` is a return value, not an exception —
callers must handle it. `ledger/` raises only on infrastructure failure.
There is no bypass parameter on any function in this package.

---

### 2.9 `generate/` — tailoring plans, letters, rendering

```
generate/
├── plan_models.py     TailoringPlan and its parts (DOCUMENT_GENERATION §3.2)
├── plan_validator.py  the three DB-backed checks
├── bullet_selection.py shortlist(), rank_key(), uniqueness_score()
├── planner.py         TailoringPlanner — the strong-model call
├── letter.py          CoverLetterWriter
├── gap_paragraph.py   the honest-gap paragraph
├── similarity.py      MinHash against previous letters
├── apply_plan.py      apply_plan() — pure, no I/O, no model call
├── render_models.py   ResumeDocument, CoverLetterDocument
├── docx_builder.py    build_docx()
├── fit.py             page_count(), render_to_one_page()
├── storage.py         artifact path, checksum, write
└── service.py         GenerationService — the stage ⑧/⑨ orchestrator
```

```python
# generate/service.py
class GenerationService:
    async def generate_for_review_item(
        self, review_item_id: str, *, force_cover_letter: bool = False
    ) -> GenerationOutcome:
        """Stages ⑧ and ⑨ for one item.

          1. load posting, variant, match_score, requirements   [session]
          2. candidate_claims() — restricted claims filtered out BEFORE the
             model sees them (DOCUMENT_GENERATION.md §4.4)      [session]
          3. shortlist() per block — deterministic, no model    [no session]
          4. TailoringPlanner.plan(...)                          [no session]
          5. plan_validator.validate(plan, variant, ledger)      [session]
          6. cover letter branch, gated on company.cover_letter_worth
             or force_cover_letter                               [no session]
          7. validate_text() on the rendered text                [session]
          8. write artifact + claim_usage + attach, one transaction
        """
```

```python
# generate/apply_plan.py
def apply_plan(variant_content: dict, plan: TailoringPlan) -> ResumeDocument:
    """Deterministic and pure. Same variant + same plan ⇒ byte-identical
    output. No model call, no I/O. Every structural property — 'a plan never
    adds a block', 'a rephrase preserves the claim set' — is a unit test over
    this one function."""
```

```python
# generate/fit.py
def render_to_one_page(doc: ResumeDocument, plan: TailoringPlan) -> FitResult:
    """The five-rung ladder, stopping at the first fit. Page count is measured
    by rendering through headless LibreOffice and counting PDF pages — never
    estimated (DOCUMENT_GENERATION.md §5.5). Exhausting the ladder raises
    DoesNotFit; it never ships two pages."""
```

`page_count` shells out to `soffice`, which is **blocking**. It is called from
async code through `asyncio.to_thread`, and never inside a session (§4.5). At
~1.5 s per attempt and at most five attempts it is the longest synchronous
operation in the system, which is why rendering happens on preview/approve
rather than in the nightly run.

**Error behaviour.** Enrichment-side, with one exception: the ledger gate fails
closed (invariant 3). Provider unavailable ⇒ the item enqueues with an empty
plan and no letter; the base variant is still a legitimate application.
`DoesNotFit` ⇒ `needs_manual_review`. LibreOffice failing twice is the single
permitted soft degradation: the `.docx` is offered with "page count not
verified" (`DOCUMENT_GENERATION.md` §12).

---

### 2.10 `review/` — the queue and the approval state machine

**Responsibility.** The human decision point, and the module where invariant 1
is visible as an absence.

```python
# review/service.py
class ReviewService:
    async def list_queue(self, f: ReviewFilter, page: CursorPage) -> Page[ReviewListItem]: ...
    async def get_detail(self, review_item_id: str) -> ReviewDetail:
        """One query set producing the whole API.md §5 payload: posting,
        company, recommended variant, score with gaps and evidence, tailoring
        plan, artifacts. The screen is designed to need exactly one request."""

    async def patch_plan(self, review_item_id: str, patch: PlanPatch) -> ReviewDetail:
        """Operator edits the plan, not the output (DOCUMENT_GENERATION.md
        §11.1). Edits are appended to plan.operator_edits so the correction is
        captured rather than worked around."""

    async def generate(self, review_item_id: str, req: GenerateRequest) -> GenerationOutcome: ...

    async def approve(self, review_item_id: str, note: str | None) -> ApprovalResult:
        """Creates the application row (status='submitted'), freezes the
        artifacts, returns download links. Issues zero outbound HTTP requests
        — asserted by a test (APPLICATION_PIPELINE.md §15, criterion 2).

        There is no submit(). There is no endpoint that submits. The absence is
        the enforcement (API.md §8).
        """

    async def skip(self, review_item_id: str, note: str | None) -> None: ...
```

State machine, exactly `APPLICATION_PIPELINE.md` §3.1:

```python
# review/states.py
ALLOWED: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.PENDING_REVIEW:      frozenset({ReviewStatus.APPROVED,
                                                 ReviewStatus.SKIPPED,
                                                 ReviewStatus.NEEDS_MANUAL_REVIEW}),
    ReviewStatus.NEEDS_MANUAL_REVIEW: frozenset({ReviewStatus.PENDING_REVIEW,
                                                 ReviewStatus.SKIPPED}),
    ReviewStatus.APPROVED:            frozenset(),   # terminal
    ReviewStatus.SKIPPED:             frozenset(),   # terminal
}

def assert_transition(current: ReviewStatus, proposed: ReviewStatus) -> None:
    """Raises AlreadyDecidedError → 409 review.already_decided."""
```

The transition check runs **inside** the same transaction as the write, against
a row read `WITH FOR UPDATE`. A single-user system still has two writers — the
browser and the scheduler — and "approve while the scheduler regenerates" is the
race that produces a decided item with a changed artifact.

---

### 2.11 `mail/` — Gmail read, alert parsing, classification, digest

```
mail/
├── gmail.py       GmailClient (read + the single send path), quota accounting
├── auth.py        OAuth flow, refresh, Redis-serialised refresh lock
├── sync.py        history cursor, the bounded full-sweep fallback
├── alerts/        base.py, linkedin.py, naukri.py, indeed.py, urls.py
├── classify.py    MailClassification schema, the fast-model call, post-checks
├── link.py        the R1–R5 resolution chain (§3.6)
├── digest.py      compose + send — the ONLY send call site in the codebase
└── service.py     MailService — the 'mail' run orchestrator
```

```python
# mail/gmail.py
class GmailClient:
    async def iter_messages(self, query: str) -> AsyncIterator[GmailMessage]: ...
    async def get_message(self, gmail_id: str) -> GmailMessage: ...
    async def history_since(self, history_id: str) -> AsyncIterator[GmailMessage]:
        """Raises HistoryCursorExpired on Gmail's 404 for a pruned cursor;
        mail/sync.py catches it and falls back to a bounded sweep."""

    async def send(self, *, to: str, subject: str, html: str) -> str:
        """Invariant 2, enforced here and nowhere else.

        Raises OutboundPolicyViolation unless `to` == settings.mail_operator_
        address, checked after address normalisation, and with Cc/Bcc
        structurally absent from the signature. Two tests guard this: the
        recipient check, and a static import-graph check asserting exactly one
        call site (EMAIL_INGESTION.md §14, criteria 1 and 2).
        """
```

```python
# mail/service.py
class MailService:
    async def run_poll(self, run_id: str) -> RunSummary:
        """The 'mail' run:
             ingest new messages (ON CONFLICT (gmail_id) DO NOTHING)
           → alert stream: parse cards → RawPosting → ingest.upsert_posting
           → reply stream: link (§3.6) → classify → transition
           → advance the cursor ONLY on success
        Re-processing is a no-op by construction (EMAIL_INGESTION.md §7.4)."""
```

```python
# mail/classify.py
class MailClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mail_class: MailClass
    confidence: Annotated[Decimal, Field(ge=0, le=1)]
    excerpt: Annotated[str, Field(max_length=200)]
    signals: Annotated[list[Annotated[str, Field(max_length=60)]], Field(max_length=5)]
    is_automated: bool


def post_validate(c: MailClassification, body: str) -> list[str]:
    """Four checks (EMAIL_INGESTION.md §7.2). The first is the one that matters:
    `excerpt` must be a verbatim substring of the normalised body. A
    non-verbatim excerpt means fabrication or manipulation; the message is
    demoted to held-for-review with mail.excerpt_not_verbatim. This single
    check defuses most of the injection surface."""
```

**Bodies are never persisted.** `classify` receives the body as a parameter and
the caller never assigns it to anything that outlives the call. A test
classifies a fixture containing a canary string and greps the database dump and
the log output for it (`EMAIL_INGESTION.md` §14, criterion 8).

**Error behaviour.** `invalid_grant` on refresh aborts the mail run immediately
and does not retry — a permanent failure retried is only burned quota. The
cursor does not advance, `GET /health` reports `gmail: "unauthenticated"`, and
the digest is written to disk instead of sent so no day's output is lost.

---

### 2.12 `tracking/` — events, transitions, metrics, export, follow-ups

```python
# tracking/transitions.py
RANK: dict[ApplicationStatus, int]
TERMINAL: frozenset[ApplicationStatus]

def may_append(current: ApplicationStatus, proposed: ApplicationStatus) -> bool:
    """The single authority on transition legality, used by BOTH the mail
    classifier and the manual-event endpoint. There is no second copy."""
```

```python
# tracking/events.py
async def append_event(
    session: AsyncSession, *, application_id: str, status: ApplicationStatus,
    occurred_at: datetime, email_message_id: int | None = None,
    confidence: Decimal | None = None, excerpt: str | None = None,
    is_manual: bool = False, force: bool = False,
) -> ApplicationEvent | None:
    """The ONLY writer to application_event.

    Returns None when the transition is dropped (illegal or duplicate); a drop
    is logged with the reason and counted as transitions_dropped, and is not an
    error. `force=True` is accepted only when is_manual is also True — the
    automated path is rejected with event.force_not_permitted (400).

    Dedup is on (application_id, status, email_message_id) before the insert;
    application.status is then recomputed by trigger as a fold over the whole
    event set, so out-of-order arrival and replay are both harmless.
    """
```

```python
# tracking/metrics.py
async def funnel(session: AsyncSession, *, group_by: FunnelGroup,
                 source_channel: str = "direct") -> list[FunnelRow]:
    """v_funnel plus the two API-layer corrections (APPLICATION_PIPELINE.md
    §8.3): withdrawn excluded from both numerator and denominator, and
    ever-reached counters computed over application_event rather than current
    status. Rates below settings.min_n_for_rate are returned as null with the
    count present — never as a computed percentage."""

async def ghosted(session: AsyncSession) -> list[GhostedRow]:
    """SELECT over v_ghosted. Nothing writes 'ghosted' anywhere (§3.7)."""
```

```python
# tracking/export.py
def build_workbook(session: Session, *, as_of: datetime) -> Path:
    """Four sheets, atomic write (tmp + replace), symlinked as
    scout-pipeline-latest.xlsx. Read-only, holds no long transaction. Runs
    on a synchronous session because openpyxl is synchronous and the whole
    build is well under a second at this scale."""
```

---

### 2.13 `scheduler/` — job definitions, locks, lifecycle

Covered in full in §6. Public surface:

```python
# scheduler/app.py
def build_scheduler(settings: Settings, deps: Deps) -> AsyncIOScheduler: ...
async def start(scheduler: AsyncIOScheduler) -> None: ...
async def shutdown(scheduler: AsyncIOScheduler, *, wait: bool = True) -> None: ...

# scheduler/locks.py
class RunLock:
    """Redis SET NX PX with a fencing token and a heartbeat."""
    async def acquire(self, name: str, *, ttl_s: int) -> LockToken | None: ...
    async def heartbeat(self, token: LockToken) -> bool: ...
    async def release(self, token: LockToken) -> None: ...
```

---

### 2.14 `llm/` — provider abstraction, prompts, enforcement, cost

Specified by `AI_ARCHITECTURE.md` §§3–9; not restated. The SDD adds two
implementation obligations:

1. **`llm/` never receives a `Session`.** No function in this package takes one,
   which makes §4.5's rule impossible to violate from inside it.
2. **`llm/cost.py` is the budget circuit breaker**, and it is checked *before*
   the call, not after:

```python
# llm/cost.py
class BudgetLedger:
    def __init__(self, settings: Settings, run_id: str) -> None: ...

    def check(self, family: str) -> BudgetVerdict:
        """OPEN at 100% of settings.llm_daily_budget_inr — no further calls in
        this run. WARN at settings.llm_budget_warn_pct, which caps generation
        to the top 5 items. Called before every model call, so a runaway is
        bounded by code rather than by a monthly invoice."""

    def record(self, resp: LLMResponse[Any]) -> None: ...
    def rollup(self) -> dict[str, Any]:  # → run_log.stats.llm_cost_inr
        ...
```

---

### 2.15 `api/` — routers, dependencies, envelope, errors

```
api/
├── main.py        FastAPI app, lifespan, middleware order
├── deps.py        session, settings, services, auth, idempotency
├── envelope.py    Envelope[T], ok(), created(), accepted(), no_content()
├── errors.py      exception → (status, code) mapping (§8)
├── pagination.py  CursorPage, encode/decode cursor
├── auth.py        the single-session cookie
└── routers/       companies.py sources.py postings.py variants.py claims.py
                   review.py applications.py metrics.py runs.py settings.py
                   health.py exports.py
```

```python
# api/envelope.py
class Envelope[T](BaseModel):
    data: T | None
    message: str = "OK"
    meta: dict[str, Any] | None = None
```

Every route returns `Envelope[...]`, including errors — `API.md` §1 admits no
exception, and a route that returns a bare model breaks the generated client.

```python
# api/routers/review.py — representative router, showing the intended thinness
@router.post("/{review_id}/approve", status_code=201,
             response_model=Envelope[ApprovalResult])
async def approve(
    review_id: str,
    body: ApproveRequest,
    svc: Annotated[ReviewService, Depends(get_review_service)],
) -> Envelope[ApprovalResult]:
    result = await svc.approve(review_id, note=body.note)
    return created(result, "Approved. Submit it yourself on the employer's site.")
```

Four lines, no logic. The message is deliberate: the UI copy for approval says
what the system does and does not do, every time.

```python
# api/deps.py
async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session (§4.5). Commits on a clean response, rolls back
    on an exception, and is closed before the response is serialised."""


def idempotent(header: str = "Idempotency-Key") -> Callable[..., Awaitable[str | None]]:
    """POST /runs/discovery and POST /review/{id}/generate (API.md §1).
    Key → response body cached in Redis for 24 h under
    `idem:{route}:{key}`; a repeat returns the original response."""
```

**Middleware order** (outermost first), because it is easy to get wrong and only
fails in production: `RequestIDMiddleware` → `StructlogContextMiddleware` →
`SessionAuthMiddleware` → `CORSMiddleware` → `GZipMiddleware`. Errors raised by
inner layers must still get a request ID, which is why the ID is outermost.

---

## 3. Key algorithms

### 3.1 Dedup and change detection

Three distinct mechanisms, applied in order, frequently conflated:

| # | Mechanism | Key | Answers |
|---|---|---|---|
| 1 | Posting identity | `(source_id, external_id)` | Is this the same posting I already have from this source? |
| 2 | Change detection | `content_hash` | Did its text change since I last saw it? |
| 3 | Cross-source collapse | `(company_id, normalised_title, location_city)` | Is this the same role I already have from a *different* source? |

```python
# ingest/persist.py
async def upsert_posting(session, *, source, raw: RawPosting, clock) -> UpsertOutcome:
    now = clock.now()
    chash = content_hash(raw.description_text)

    existing = await session.scalar(
        select(JobPosting)
        .where(JobPosting.source_id == source.id,
               JobPosting.external_id == raw.external_id)
        .with_for_update()
    )

    if existing is None:
        posting = JobPosting(id=new_ulid(), company_id=source.company_id,
                             source_id=source.id, content_hash=chash,
                             first_seen_at=now, last_seen_at=now,
                             **raw_to_columns(raw))
        session.add(posting)
        return UpsertOutcome(posting_id=posting.id, kind="new")

    if existing.content_hash == chash:
        existing.last_seen_at = now
        existing.closed_at = None            # a posting seen again is not closed
        return UpsertOutcome(posting_id=existing.id, kind="unchanged")

    # Text changed: update in place, keep identity and first_seen_at, and
    # invalidate scores so the posting is re-extracted and re-scored.
    apply_columns(existing, raw)
    existing.content_hash = chash
    existing.last_seen_at = now
    existing.closed_at = None
    await invalidate_scores(session, existing.id)
    await session.execute(
        delete(Requirement).where(Requirement.posting_id == existing.id))
    return UpsertOutcome(posting_id=existing.id, kind="updated")
```

`first_seen_at` is never rewritten on an update. It is what recency ranking
actually keys on when `posted_at` is absent or unreliable
(`SOURCE_ADAPTERS.md` §5.1), and resetting it on a recruiter's typo fix would
make an old requisition look new.

**Cross-source collapse** runs after all sources have been persisted, once per
run, because it is inherently cross-source:

```python
# ingest/dedup.py
async def collapse_duplicates(session, *, run_id: str) -> int:
    """Stage ③. For each (company_id, normalised_title, location_city) group
    with more than one open posting, keep the record whose source adapter has
    the higher fidelity_rank; ties break on first_seen_at ASCENDING so
    collapsing is deterministic and does not thrash between two boards listing
    the same role (SOURCE_ADAPTERS.md §8).

    The losers are NOT deleted — they are marked
        filtered_out = true,
        filter_reason = f'superseded_by:{winner_id}'
    which keeps the URL, the source attribution and the history, and lets the
    EMAIL_INGESTION.md §4.6 case ('mail lead superseded by the real board')
    resolve as a normal filter rather than a delete.
    """
```

`normalised_title` is `sources/normalise.py::normalise_title` — aggressive by
design, because a false merge is visible and a missed merge shows the operator
the same role twice.

**Closing.** The two-run rule is scoped **per source**
(`SOURCE_ADAPTERS.md` §10.5):

```python
# ingest/close.py
async def advance_close_counters(session, *, run_id: str,
                                 results: Sequence[SourceResult]) -> int:
    """A posting's not-seen counter advances ONLY on runs where its own source
    returned ok or empty. Any other status freezes the counter for that
    source's postings — otherwise two days of a flaky board closes an
    employer's entire live board."""
    healthy = [r.source_id for r in results if r.status in ("ok", "empty")]
    ...
```

### 3.2 The deterministic filter chain (stage ④)

The stage that makes the cost model work: it kills roughly 80% of postings
before a token is spent (`AI_ARCHITECTURE.md` §8.3). Ordered cheapest-first, and
**first rejection wins** so `filter_reason` names the *first* disqualifying
reason rather than an arbitrary one.

```python
# ingest/filters.py
@dataclass(frozen=True, slots=True)
class FilterVerdict:
    passed: bool
    reason: str | None = None      # written to job_posting.filter_reason


Predicate = Callable[[PostingView, CompanyView, Settings], FilterVerdict]

CHAIN: tuple[tuple[str, Predicate], ...] = (
    ("company_status",   _company_not_blacklisted),   # cheapest, hardest gate
    ("superseded",       _not_superseded),            # set by dedup (§3.1)
    ("closed",           _not_closed),
    ("no_description",   _has_usable_description),
    ("employment_type",  _employment_type_allowed),
    ("seniority",        _seniority_in_band),
    ("location",         _location_matches),
    ("title_denylist",   _title_not_denied),
    ("keyword_denylist", _description_not_denied),    # most expensive, last
)


def evaluate(posting: PostingView, company: CompanyView,
             settings: Settings) -> FilterVerdict:
    for name, predicate in CHAIN:
        verdict = predicate(posting, company, settings)
        if not verdict.passed:
            return FilterVerdict(False, verdict.reason or name)
    return FilterVerdict(True)
```

The three predicates worth stating precisely:

```python
def _location_matches(p, c, s) -> FilterVerdict:
    """COMPANY_REGISTRY.md §5.2. Positive tokens OR together; negative tokens
    (`!Chennai`) veto. A company's own location_filter OVERRIDES the global
    default entirely rather than intersecting with it.

    An unresolvable location (location_city IS NULL) PASSES a non-empty
    filter. Deliberate: a false positive costs two seconds of the operator's
    attention; a false negative is an invisible role they never see.
    """


def _seniority_in_band(p, c, s) -> FilterVerdict:
    """seniority_guess in settings.filter_seniority_allowed. 'unknown' passes —
    the inference resolves ambiguity to 'mid' already (SOURCE_ADAPTERS.md
    §9.3), so a residual 'unknown' means the source stated nothing, which is
    not grounds for dropping a role."""


def _description_not_denied(p, c, s) -> FilterVerdict:
    """Whole-word, case-insensitive match of settings.filter_keyword_denylist
    against description_text. Substring matching is wrong here: a deny-list
    entry 'sap' must not kill a posting containing 'sapling' or 'SAP HANA
    adjacent' phrasing in prose."""
```

Filtering **never deletes**. It writes `filtered_out = true` and a reason, so
`GET /postings?status=…` can show what was dropped and why, and so a filter
mistake is discoverable rather than silent.

### 3.3 Composite score computation

Canonical in `MATCH_SCORING.md` §5.1; reproduced here as the executable form the
implementation must match exactly, including quantisation points.

```python
# scoring/composite.py
TIER_WEIGHT: dict[CompanyTier, Decimal] = {
    CompanyTier.DREAM:  Decimal("1.10"),
    CompanyTier.STRONG: Decimal("1.00"),
    CompanyTier.VOLUME: Decimal("0.90"),
}
HARD_GATE = ((Decimal("0.60"), Decimal("1.00")),
             (Decimal("0.40"), Decimal("0.85")),
             (Decimal("0.00"), Decimal("0.65")))


def score_variant(posting: PostingView, variant: VariantView,
                  requirements: Sequence[Requirement],
                  settings: Settings, now: datetime) -> ScoredVariant:
    hard_rows: list[tuple[Decimal, CoverageLevel]] = []
    nice_rows: list[tuple[Decimal, CoverageLevel]] = []
    evidence: list[dict] = []
    scored: list[tuple[Requirement, CoverageLevel, Evidence | None, str | None]] = []

    for req in requirements:
        if req.kind is RequirementKind.RESPONSIBILITY:
            continue                              # extracted, never scored
        level, ev, note = level_for(req, variant)
        scored.append((req, level, ev, note))
        bucket = hard_rows if req.kind is RequirementKind.HARD else nice_rows
        bucket.append((req.weight, level))        # `tool` pools into `nice`
        if level is CoverageLevel.MET and ev is not None:
            evidence.append(ev.as_json(req))

    h = bucket_coverage(hard_rows)
    n = bucket_coverage(nice_rows)
    coverage_pct, composite_score = composite(
        h, n, posting.company_tier,
        posting.posted_at or posting.first_seen_at, now)

    return ScoredVariant(
        variant_id=variant.id,
        hard_met=sum(1 for w, l in hard_rows if l is CoverageLevel.MET),
        hard_total=len(hard_rows),
        nice_met=sum(1 for w, l in nice_rows if l is CoverageLevel.MET),
        nice_total=len(nice_rows),
        coverage_pct=coverage_pct,          # 100 · base — fit alone
        composite_score=composite_score,    # ranking number
        gaps=build_gaps(scored),
        evidence=evidence,
    )
```

The stored counts are **unweighted** and will disagree with the weighted
coverage. A variant can be 4/7 on hard requirements and score badly because the
three it missed carry most of the weight. That is the design working.

Winner selection applies the six tie-breaks in `MATCH_SCORING.md` §6.1 in order,
ending at `min(resume_variant.id)` so a rescore reproduces the same answer.

### 3.4 Tailoring-plan construction

```python
# generate/planner.py
class TailoringPlanner:
    async def plan(self, ctx: PlanContext) -> TailoringPlan:
        """
          1. DETERMINISTIC PRE-RANK.  For every block, shortlist() scores each
             bullet on hard-requirement coverage (3× a nice), uniqueness,
             quantification, recency, incumbency. No model call.

          2. FILTER BY DISCLOSURE.    candidate_claims() removes claims outside
             permitted_tiers(company, review_item); bullets whose claim set is
             then empty are dropped from the shortlist. A restricted fact
             cannot leak through a rephrasing because the model never sees it.

          3. MODEL CALL (`strong`).   Input: the shortlist with claim IDs and
             the requirement IDs each bullet answers, the gap list, the JD
             delimited as untrusted. Output: TailoringPlan — operation IDs and
             rationales. For promote/swap/demote the model returns only IDs; it
             composes text only under FF_TAILORING_REPHRASE.

          4. SCHEMA VALIDATION.       BulletOp._shape_matches_op, and the rule
             that promote/swap/rephrase must carry >= 1 claim_id.

          5. DB-BACKED VALIDATION.    plan_validator: reorder_blocks is exactly
             a permutation of the variant's block keys; every bullet ID exists
             in that block's bank; a rephrase preserves the original claim set.

          6. ONE REPAIR RETRY on failure, then needs_manual_review with the
             validator's messages in decision_note.

        A plan applies wholly or not at all. There is no partial application.
        """
```

Step 1 is why most plans are three operations rather than twelve: incumbency is
the last tie-break, so an already-correct base variant produces an almost-empty
plan, and churn for its own sake is treated as a cost.

Step 5's first check is how "invent a new experience block" is made structurally
impossible rather than discouraged.

### 3.5 The ledger validation pass

```python
# ledger/validate.py
async def validate_text(session, *, text, cited_claim_ids, company_slug,
                        llm, settings) -> ValidationReport:
    # ── 1. DETECT ────────────────────────────────────────────────────────
    spans: list[Span] = regex_spans(text)                 # eight families
    try:
        spans += await llm_assertion_spans(llm, text, settings)
    except (LLMError, SchemaEnforcementFailed):
        return ValidationReport.fail_closed(
            "Assertion extraction unavailable.")          # never 'passed'

    for s in spans:                                       # grounding
        if s.text not in normalise_ws(text):
            return ValidationReport.fail_closed(
                "Assertion extraction returned an ungrounded span.")

    spans = de_overlap(spans, prefer="longest")
    # '₹9.4L → ₹3.5L' resolves once as a `range`, not three times.

    # ── 2. RESOLVE ───────────────────────────────────────────────────────
    permitted = permitted_tiers(company, review_item)
    cited = await load_claims(session, cited_claim_ids)
    resolutions = [resolve(a, cited, session, permitted) for a in spans]

    # ── 3. VERDICT ───────────────────────────────────────────────────────
    passed = all(
        r.resolved and not (r.stale and settings.ledger_expired_claim_policy == "fail")
        for r in resolutions
    )
    return ValidationReport(passed=passed, assertions=[...])
```

The six-step `resolve` chain is `CLAIMS_LEDGER.md` §5.4 and is not restated. The
properties this code must not lose:

- The LLM pass failing produces `passed: false`, never `passed: true`. **Fail
  closed on anything touching what goes out.**
- De-overlapping happens before resolution, not after, or the same figure fails
  once and passes once.
- Confidentiality is checked here as well as at candidate selection. A resolved
  claim outside `permitted_tiers` fails the assertion with a disclosure note,
  and the generator retries with the public sibling from `ledger/pairs.yaml`.

On `passed`, `claim_usage` rows are written **in the same transaction** that sets
`artifact.validation_status = 'passed'`. An artifact cannot exist in a passed
state without its usage rows.

### 3.6 Mail-to-application linkage resolution

The system plants no tracking token, because it never submits. Linkage is
inference over evidence, designed to answer "I do not know"
(`EMAIL_INGESTION.md` §6).

```python
# mail/link.py
@dataclass(frozen=True, slots=True)
class Candidate:
    application_id: str
    confidence: Decimal
    rule: str


async def resolve_link(session, msg: GmailMessage, *,
                       settings: Settings, clock: Clock) -> LinkResult:
    window_start = clock.now() - timedelta(days=settings.mail_link_window_days)

    # R1 — THREAD. Deterministic. If it fires, nothing below is consulted.
    if (app_id := await by_thread(session, msg)) is not None:
        return LinkResult(app_id, Decimal("0.99"), "R1")

    live = await live_applications(session, since=window_start,
                                   include_terminal_targets=msg.may_be_rejection)

    cands: list[Candidate] = []

    # R2 — SENDER DOMAIN. Skipped entirely for shared ATS domains.
    if msg.from_domain not in SHARED_ATS_DOMAINS:
        cands += by_registrable_domain(live, msg, conf=Decimal("0.85"))

    # R3 — SHARED-ATS RESOLUTION.
    cands += by_reply_to_domain(live, msg, conf=Decimal("0.90"))          # R3a
    cands += by_display_name_trigram(                                      # R3b
        live, strip_via_ats(msg.display_name),
        threshold=settings.mail_company_trgm_threshold, conf=Decimal("0.80"))
    cands += by_list_id_tenant(live, msg, conf=Decimal("0.85"))            # R3c

    # R4 — REFERENCE MATCH. Combines; does not stand alone below 0.80.
    cands += by_external_id_literal(live, msg, conf=Decimal("0.80"))
    cands += by_title_token_set(live, msg, ratio=0.85, conf=Decimal("0.55"))

    merged = combine(cands)      # per application: 1 - Π(1 - cᵢ), capped 0.95
    strong = [c for c in merged if c.confidence >= settings.mail_link_min]

    # R5 — UNRESOLVED. More than one plausible candidate is NOT resolved by
    # picking the best. Two live applications at the same employer is the
    # common case, and guessing between them is what makes the pipeline data
    # untrustworthy.
    if len(strong) != 1:
        return LinkResult(None, max_conf(merged),
                          "R5", company_id=identified_company(merged))
    return LinkResult(strong[0].application_id, strong[0].confidence, strong[0].rule)
```

`email_message.company_id` is written even when `application_id` is not, so a
message from a tracked company that could not be tied to a specific application
still surfaces on that company's page. Unresolved messages appear in
`v_mail_review_queue`; the operator links one in a click and R1 remembers the
thread for every subsequent message, so the cost is paid once per conversation.

### 3.7 The ghosted derivation

`ghosted` is not in the `application_status` enum and **no code path assigns
it**. It is `v_ghosted` (`DATA_MODEL.md` §10), read like any other query:

```python
# tracking/metrics.py
GHOSTED_SQL = text("""
    SELECT a.id, a.posting_id, a.submitted_at,
           coalesce(max(e.occurred_at), a.submitted_at) AS last_event_at,
           (now() - coalesce(max(e.occurred_at), a.submitted_at)) AS silence
    FROM   application a
    LEFT   JOIN application_event e ON e.application_id = a.id
    WHERE  a.status IN ('submitted','acknowledged')
    GROUP  BY a.id
    HAVING coalesce(max(e.occurred_at), a.submitted_at)
             < now() - make_interval(days => :ghost_after_days)
    ORDER  BY last_event_at ASC
""")
```

Three consequences the implementation gets for free, and would have to hand-build
if `ghosted` were a stored status:

1. **Retroactivity.** A reply on day 45 removes the application from the set with
   no compensating write, because the view reads the event log.
2. **A parameterised threshold.** Changing `GHOST_AFTER_DAYS` reclassifies
   everything instantly and reversibly. A stored status bakes the old threshold
   into rows that are then wrong forever.
3. **Funnel correctness.** Ghosted applications sit in `submitted` /
   `acknowledged` where they belong and never count as a response.

The reason is stated plainly because it will be re-litigated: *absence of
evidence is not evidence*. Thirty days of silence is equally consistent with a
rejection in spam, a held-for-review classification, a genuinely slow enterprise
pipeline, a reply by phone, and a frozen headcount — five situations with five
different correct operator responses, which a stored status would flatten into
one.

---

## 4. Concurrency and async design

### 4.1 Where async is genuinely used, and where it is not

Async is justified only where the process is waiting on a socket. This system
does that in exactly four places, and pretending otherwise adds colour without
value.

| Work | Model | Why |
|---|---|---|
| Source fetching (~320 sources, thousands of HTTP round-trips) | **async**, bounded | The dominant wall-clock cost. Genuinely I/O-bound. |
| LLM calls (~90 per day) | **async**, separately bounded | Long, network-bound, and rate-limited by the provider. |
| Gmail API | **async** | Network-bound. |
| Database | **async** (`asyncpg`) | Because the request path and the runner are async; not because it is faster at this scale. |
| `.docx` build, LibreOffice page count, `.xlsx` export | **synchronous**, via `asyncio.to_thread` | CPU/subprocess work. Making them async would be a lie about what they do. |
| Scoring, filtering, dedup, ledger regex | **synchronous, pure functions** | No I/O. Async here buys nothing and costs testability. |

The rule that follows: **a pure function stays a pure function.** `apply_plan`,
`composite`, `level_for`, `may_append`, `evaluate` are all `def`, not
`async def`. They are the functions with the most tests and the least tolerance
for an event loop in the fixture.

### 4.2 The HTTP client and connection pooling

One `httpx.AsyncClient` per discovery run, built by
`sources/http.py::build_client` (`SOURCE_ADAPTERS.md` §4.1), shared by every
adapter, closed by the `async with` in `run_discovery`.

```python
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
```

Twenty connections against eight concurrent sources is deliberate headroom:
Workday's detail fan-out issues up to three concurrent requests inside one
source, so the worst case is 8 × 3 = 24 in-flight intentions against 20
connections, and the pool queues the difference rather than opening unbounded
sockets. The pool timeout (5 s) makes that queueing visible as latency rather
than as a hang.

`trust_env=False` is a security control, not tidiness: an ambient `HTTP_PROXY`
in the container would silently reroute every outbound request, including the
post-redirect policy check's view of the final URL.

### 4.3 Bounded concurrency for source fetching

```python
sem = asyncio.Semaphore(settings.source_concurrency)      # default 8
```

**Why 8.** Three constraints intersect:

1. The run budget is 900 s for ~320 sources (`ARCHITECTURE.md` §9). At 8
   concurrent that is 40 sequential slots, so the mean source must complete in
   ~22 s. Measured against the per-source ceiling of 180 s, that is the right
   order of magnitude with room for a long tail.
2. Rate limits are enforced per `bucket_key`, not per source, and most sources
   share a bucket (all Greenhouse boards share `boards-api.greenhouse.io` at
   5 req/s). Raising concurrency past the bucket rate converts parallelism into
   queueing inside `RateLimitTimeout` — more concurrency, identical throughput,
   more failures.
3. One process, one worker. Eight concurrent adapters plus their JSON parsing is
   comfortably inside one event loop; forty is where GIL-bound parsing starts to
   delay the loop's I/O callbacks.

Inside a source, requests are **serialised** — an adapter never issues two
upstream calls at once. Workday's detail fan-out is the single documented
exception, bounded at 3 by its own `TaskGroup`, and it removes round-trip
latency from the critical path without exceeding the tenant bucket (which
serialises anyway at 1 req/s).

Failure isolation, restated as code, is one keyword:

```python
outcomes = await asyncio.gather(*(one(s) for s in sources), return_exceptions=True)
```

`asyncio.CancelledError` is re-raised in every handler, never swallowed, so the
whole-run timeout can actually stop the run.

### 4.4 Why LLM calls are bounded separately

```python
llm_sem = asyncio.Semaphore(settings.llm_max_concurrency)   # default 4
```

They are a different resource with a different failure mode, and sharing the
source semaphore would couple them wrongly in both directions.

- **Different scarcity.** Source concurrency is limited by politeness to
  employers. LLM concurrency is limited by provider throttling and by cost.
- **Different cost of a retry.** A retried HTTP GET is free and idempotent. A
  retried model call costs money and counts against
  `LLM_DAILY_BUDGET_INR`.
- **Higher concurrency buys nothing.** The model work in a run is under four
  minutes against a fifteen-minute budget (`AI_ARCHITECTURE.md` §3.3). Raising
  the limit shortens the run imperceptibly and makes 429s materially more
  likely, which converts a cheap serial wait into an expensive backoff storm.
- **The budget breaker needs a chokepoint.** `BudgetLedger.check()` runs before
  each call; with unbounded concurrency, dozens of calls are already in flight
  when the breaker opens and the budget is exceeded by the amount in flight.

The two semaphores are therefore never the same object and never derived from
one another.

### 4.5 Session lifecycle and scope — and the rule about LLM calls

Three scopes, and no fourth:

| Scope | Created by | Lifetime |
|---|---|---|
| Request | `api/deps.py::get_session` | One HTTP request; commits on a clean response |
| Unit of work | `db/session.py::session_scope` | One explicit block inside a background stage |
| Export | synchronous `Session` | The `.xlsx` build, read-only |

**The rule, stated as an invariant of this design:**

> **A database session is never held across a long-running call.** Not an LLM
> call, not an outbound HTTP fetch, not a LibreOffice render, not a Gmail
> round-trip.

The mechanics, for one extraction:

```python
# extract/service.py — the shape every stage follows
async def extract_posting(self, posting_id: str) -> ExtractionOutcome:
    async with session_scope(self._factory) as s:           # ── session open
        posting = await load_posting_view(s, posting_id)
        company = await load_company_view(s, posting.company_id)
        cached  = await lookup_extraction_cache(s, posting.content_hash)
    if cached is not None:                                  # ── session closed
        return cached

    system, user = build_extract_messages(posting, company)
    async with self._llm_sem:
        resp = await call_structured(                       # ── NO SESSION HELD
            self._llm, family="requirement_extraction",
            prompt=self._prompt, schema=RequirementExtraction,
            fields={...}, untrusted={"description_text": posting.description_text},
            post_validate=partial(ground_spans, source=posting.description_text),
        )

    rows = [to_requirement_row(r, posting.id, resp) for r in resp.value.requirements]
    async with session_scope(self._factory) as s:           # ── session reopened
        await replace_requirements(s, posting.id, rows)
        await record_llm_usage(s, resp)
```

Why this is a rule and not a preference:

- A Bedrock `structured()` call has a 25–60 s policy timeout
  (`AI_ARCHITECTURE.md` §3.4). Ten of those in flight, each pinning a pooled
  connection, exhausts a pool of ten and stalls the API process that shares it.
- Postgres `idle in transaction` for 60 s blocks vacuum and holds row locks
  taken by `with_for_update`, which the queue's approve path needs.
- It makes the retry story simple. The model call is outside the transaction, so
  a repair-retry does not extend a transaction, and a failed call rolls back
  nothing because nothing was open.

Enforcement is not left to discipline: `llm/` functions do not accept a
`Session` (§2.14), and a CI check asserts that no `await` on an `llm.*`,
`httpx`, or `soffice` call appears lexically inside a `session_scope` block.

### 4.6 The single-writer assumption, and where it does not hold

One user, one API process, one scheduler. That justifies the absence of a
distributed task queue (`ARCHITECTURE.md` §4.1) but it does **not** justify
assuming a single writer, because there are always two: the browser and the
scheduler.

The three places that must take a lock or a row lock:

| Race | Mechanism |
|---|---|
| Manual `POST /runs/discovery` overlapping the 08:00 run | Redis run lock; second caller gets 409 (`API.md` §7) |
| Approve during a regeneration of the same review item | `SELECT … FOR UPDATE` on `review_item` inside the transaction |
| Gmail token refresh during a digest send | Redis `lock:gmail:refresh`, 30 s TTL (`EMAIL_INGESTION.md` §2.5) |

---

## 5. Transaction boundaries and re-runnability

### 5.1 One transaction per stage per item

The unit of work is deliberately small: one source's postings, one posting's
requirements, one posting's scores, one artifact. A run-long transaction would
convert any failure into a total rollback and would make partial progress
impossible — which is precisely the property that lets stage ⑧ be retried
without re-fetching.

| Stage | Transaction boundary | Contents | Isolation |
|---|---|---|---|
| ① Discover | none | Pure HTTP. `sources/` opens no session. | — |
| ② ③ Persist | **one per source** | All `upsert_posting` calls for that source, plus `record_source_outcome`. A source that fails mid-fetch commits nothing — partial ingestion would close live postings via the two-run rule. | Read Committed |
| ③ Collapse | **one per run** | `collapse_duplicates` marks the losers `superseded_by:`. Small, run-wide, after every source. | Read Committed |
| ④ Filter | **one per batch of 500** | `filtered_out` + `filter_reason` updates. Batched because it is a pure UPDATE over rows already read. | Read Committed |
| ⑤ Extract | **two per posting** | Read (posting + cache), then — after the model call — replace requirement rows. Never one transaction spanning both. | Read Committed |
| ⑥ ⑦ Score | **one per posting** | Delete prior `match_score` rows for the active `prompt_version`, insert one row per variant, set `is_recommended`. Atomic because the partial unique index must never see two recommendations. | Read Committed |
| ⑧ Generate | **two per item** | Read (variant, score, permitted claims), then — after the model calls and the render — write artifact + `claim_usage` + attach. | Read Committed |
| ⑨ Validate | inside the ⑧ write | `artifact.validation_status` and `claim_usage` are written together. An artifact cannot be `passed` without its usage rows. | Read Committed |
| ⑩ Enqueue | **one per run** | `review_item` inserts for items clearing `GENERATION_MIN_COMPOSITE`, capped at `GENERATION_DAILY_CAP`. | Read Committed |
| ⑪ Digest | **read-only** | Compose from committed state; the send is outside any transaction. | Read Committed |
| Mail poll | **one per message** | Insert `email_message`, link, classify, append event. One message's failure does not lose the others. | Read Committed |
| Approve | **one** | `review_item` status (row-locked) + `application` insert + first `application_event`. | Read Committed |

Read Committed throughout. Nothing here needs repeatable reads; the two places
that could race take an explicit row lock instead, which is cheaper and states
the intent.

### 5.2 What is safe to re-run

| Operation | Safe to re-run? | Why |
|---|---|---|
| A whole discovery run | **Yes** | Posting identity is `(source_id, external_id)`; unchanged content is an `UNCHANGED` no-op. `Idempotency-Key` on `POST /runs/discovery` returns the original response inside 24 h. |
| One source | **Yes** | Same reason, scoped. |
| Stage ④ filter | **Yes** | Pure function of posting + company + settings. Re-evaluation overwrites `filter_reason` with the same value. |
| Stage ⑤ extract | **Yes** | Requirement rows are replaced, not appended. The `content_hash` cache makes a repeat free. |
| Stage ⑥ score | **Yes** | Rows are keyed `(posting_id, variant_id, prompt_version)`; a re-score deletes and reinserts the active version's family and leaves other versions alone. |
| Stage ⑧ generate | **Yes, with a caveat** | A new artifact row is created each time (artifacts are immutable and retained for provenance). Re-running produces a *new* artifact, not a duplicate one; the old one is detached, never deleted. |
| Stage ⑩ enqueue | **Yes** | `UNIQUE (posting_id)` on `review_item` makes a repeat a no-op for already-queued postings. |
| Mail poll | **Yes** | `gmail_id` is UNIQUE with `ON CONFLICT DO NOTHING`; event dedup on `(application_id, status, email_message_id)`; status is a fold over all events. The cursor advances only on success, so the failure mode is "process twice", which is harmless — never "skip", which is silent loss. |
| Approve | **No** | Terminal. A second call is 409 `review.already_decided`. |
| Export | **Yes** | Read-only; the file is written atomically and overwrites. |

The general property being maintained: **every stage is a function of committed
state, not of the previous stage's memory.** That is what makes "re-run stage ⑧
after fixing the prompt" a one-line operation rather than a re-fetch of 4,000
postings.

---

## 6. The scheduler

### 6.1 Job definitions

APScheduler `AsyncIOScheduler`, in-process with the FastAPI app, `Asia/Kolkata`
as the scheduler timezone so the daily run fires at 08:00 IST regardless of host
timezone (`ARCHITECTURE.md` §8).

| Job ID | Trigger (IST) | Function | `run_type` | Lock |
|---|---|---|---|---|
| `discovery_daily` | `cron 0 8 * * *` | `ingest.pipeline.run_pipeline` | `discovery` | `lock:run:discovery` |
| `mail_morning` | `cron 10 8 * * *` | `mail.service.run_poll` | `mail` | `lock:run:mail` |
| `mail_poll` | `cron */30 9-22 * * *` | `mail.service.run_poll` | `mail` | `lock:run:mail` |
| `digest_daily` | `cron 15 8 * * *` | `mail.digest.compose_and_send` | `mail` | `lock:run:digest` |
| `export_nightly` | `cron 30 23 * * *` | `tracking.export.build_workbook` | `export` | `lock:run:export` |
| `rescore_sweep` | `cron 0 3 * * *` | `scoring.service.sweep_stale` | `discovery` | `lock:run:discovery` |
| `claim_expiry_scan` | `cron 30 3 * * *` | `ledger.store.scan_expiring` | — | — |
| `prune_weekly` | `cron 0 4 * * 0` | `tracking.retention.prune` | — | `lock:run:prune` |

```python
# scheduler/app.py
def build_scheduler(settings: Settings, deps: Deps) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=IST,
        jobstores={"default": SQLAlchemyJobStore(
            url=settings.database_url_sync,       # psycopg, NOT asyncpg
            tablename="apscheduler_job")},
        job_defaults={
            "coalesce": True,          # a missed 09:00 and 09:30 fire once
            "max_instances": 1,        # never two of the same job concurrently
            "misfire_grace_time": 900, # 15 min: a late run is still useful
        },
    )
    for spec in JOB_SPECS:
        scheduler.add_job(id=spec.id, func=spec.func, trigger=spec.trigger,
                          replace_existing=True, kwargs={"deps": deps})
    return scheduler
```

`replace_existing=True` means the code is the source of truth for schedules: a
changed cron in `JOB_SPECS` takes effect on deploy rather than requiring the
persisted row to be deleted. Job definitions are never edited in the database.

### 6.2 The Postgres job store

`SQLAlchemyJobStore` against the same database, in table `apscheduler_job`. This
is the only reason the scheduler is durable, and it is the entire justification
for choosing APScheduler over Celery (`ARCHITECTURE.md` §4.1): the sole
durability property needed is "a restart does not lose the schedule".

Two implementation consequences:

- **The job store is synchronous.** APScheduler's SQLAlchemy job store uses a
  blocking driver, so `settings.database_url_sync` is a second URL over
  `psycopg` alongside the async one. Two engines, one database.
- **`apscheduler_job` is not in `db/models.py` and not in Alembic.** APScheduler
  owns its own table and creates it. Modelling a library's private table is how
  a migration ends up fighting a library upgrade.

### 6.3 The Redis run lock

APScheduler's `max_instances=1` prevents a job overlapping *itself* in one
process. It does nothing about a manual `POST /runs/discovery` issued from the
browser while the scheduled run is in flight, which is the actual race
(`API.md` §7 requires a 409, "a Redis lock, not an advisory convention").

```python
# scheduler/locks.py
class RunLock:
    async def acquire(self, name: str, *, ttl_s: int) -> LockToken | None:
        """SET lock:run:{name} <random-token> NX PX ttl_s.
        Returns None if held — the caller raises RunAlreadyInFlight → 409
        run.already_running."""

    async def heartbeat(self, token: LockToken) -> bool:
        """Extends the TTL only if the value still equals our token. Called
        every ttl_s/3 by a background task for the run's duration."""

    async def release(self, token: LockToken) -> None:
        """Lua compare-and-delete. A lock whose TTL expired mid-run is NOT
        deleted by us — deleting a lock we no longer own would release
        somebody else's."""
```

TTL is 300 s with a heartbeat every 100 s, rather than a single TTL covering the
900 s run budget. A process killed mid-run then frees the lock in five minutes
instead of fifteen, and a run that is genuinely still working keeps the lock
alive indefinitely. A fixed long TTL gets those two cases exactly backwards.

### 6.4 Misfire handling

`misfire_grace_time = 900` for every job, with `coalesce=True`.

| Situation | Behaviour |
|---|---|
| Host asleep 07:50–08:05, discovery due 08:00 | Fires at 08:05, inside the grace window. The digest at 08:15 still has content. |
| Host down 07:00–09:00 | Discovery is **skipped** — past grace. `POST /runs/discovery` runs it on demand, and the digest reports the missed run. Silently running yesterday's schedule two hours late is worse than not running it. |
| Six 30-minute mail polls missed during an outage | `coalesce=True` fires **once**. The history cursor makes one catch-up poll equivalent to six sequential ones (`EMAIL_INGESTION.md` §3.1). |
| Digest missed | `last_digest_at` is the `finished_at` of the last run whose `stats.digest_sent` is true, so the next digest rolls the missed content forward rather than losing it. |

### 6.5 Behaviour on restart mid-run

A process killed at 08:07, mid-discovery, leaves three artefacts. Startup
reconciles all three, in order, in the FastAPI lifespan **before** the scheduler
starts:

```python
# scheduler/recovery.py
async def reconcile_on_startup(factory: SessionFactory, redis: Redis,
                               clock: Clock) -> ReconciliationReport:
    """1. run_log rows with status='running' and started_at older than the run
          budget are marked 'failed' with error='process_restart'. They are NOT
          resumed: a half-executed pipeline whose in-memory stage boundary is
          lost cannot be safely continued, and every stage is re-runnable
          anyway (§5.2).

       2. Orphaned run locks are left alone. The TTL expires them; deleting a
          lock we may not own is the one thing a lock implementation must
          never do.

       3. Postings from the interrupted run are already committed per source
          (§5.1), so nothing is half-persisted. Their not-seen counters were
          only advanced for sources that completed, so no live posting is
          closed by the interruption.
    """
```

The operator's recovery action is one call to `POST /runs/discovery`. Automatic
resumption is deliberately not implemented: it is the feature most likely to
double-charge the LLM budget for the sake of saving a manual click that happens
perhaps twice a year.

---

## 7. Frontend design

React 18 + Vite 5 + TypeScript 5.6 (strict) + TanStack Query v5 + React Router.
No global state library: the server is the state, and TanStack Query is the
cache. The only client state is ephemeral UI state, held in component state.

### 7.1 Route-by-route component structure

```
frontend/src/
├── routes/
│   ├── Dashboard.tsx        /
│   ├── Queue.tsx            /queue           · QueueDetail.tsx  /queue/:id
│   ├── Jobs.tsx             /jobs            · JobDetail.tsx    /jobs/:id
│   ├── Companies.tsx        /companies       · CompanyDetail.tsx /companies/:id
│   ├── Applications.tsx     /applications    · ApplicationDetail.tsx /applications/:id
│   ├── Claims.tsx           /claims
│   ├── Variants.tsx         /variants
│   └── Settings.tsx         /settings
├── components/              shared UI (below)
├── api/                     GENERATED from the OpenAPI schema — never edited
└── lib/                     queryKeys.ts, format.ts, hooks/
```

| Route | Composition |
|---|---|
| `/` Dashboard | `<RunHealthBanner>` · `<QueueSummaryCard>` · `<FunnelStrip>` · `<GhostedList>` · `<NeedsDecisionList>` · `<SourceFailureTable>` |
| `/queue` | `<QueueFilters>` · `<QueueList>` → `<QueueCard>` (score chip, top-3 gaps, variant badge, artifact status) |
| `/queue/:id` | `<PostingHeader>` · `<ScorePanel>` (coverage bar, hard/nice counts) · `<GapList>` · `<EvidenceList>` · `<TailoringPlanDiff>` · `<ArtifactPanel>` · `<DecisionBar>` (Approve · Skip · Regenerate) |
| `/jobs` | `<JobFilters>` (company, tier, location, remote, min coverage, variant, full-text) · `<JobTable>` |
| `/jobs/:id` | `<PostingHeader>` · `<RequirementTable>` (document order, kind, weight, normalised skill) · `<VariantScoreTable>` (all six) · `<RawInspector>` |
| `/companies` | `<CompanyFilters>` · `<CompanyTable>` (tier, status, tags, source health, open/new/applications) · `<AddCompanyDialog>` wrapping `<DetectUrlFlow>` |
| `/companies/:id` | `<CompanyHeader>` · `<SourceHealthTable>` · `<CompanyPostings>` · `<CompanyApplications>` · `<CompanyDefaultsForm>` |
| `/applications` | `<ApplicationFilters>` · `<ApplicationTable>` · `<GhostedFilterToggle>` |
| `/applications/:id` | `<ApplicationHeader>` · `<EventTimeline>` (excerpts, manual vs observed) · `<AddManualEventForm>` · `<ArtifactLinks>` |
| `/claims` | `<ClaimTable>` (project, tier, verified, expiry countdown) · `<ClaimEditor>` · `<ValidateTextPanel>` (a live `POST /claims/validate` scratchpad) |
| `/variants` | `<VariantList>` · `<VariantContentViewer>` · `<RenderButton>` |
| `/settings` | `<SourceHealthTable>` · `<RunHistory>` · `<RuntimeSettingsForm>` · `<HealthPanel>` |

### 7.2 The generated API client

```
openapi.json ──▶ openapi-typescript ──▶ src/api/schema.d.ts   (types)
             └─▶ handwritten thin wrapper src/api/client.ts    (fetch + envelope)
```

`pnpm gen:api` regenerates `schema.d.ts` from `/api/v1/openapi.json`. The file is
committed, and CI regenerates it and fails on a diff — so a backend contract
change that the frontend has not absorbed is caught in the backend's own
pipeline, not at runtime.

**A hand-written request or response type in the frontend is a review failure**
(`API.md` §1). `client.ts` is the only hand-written file in `src/api/`, and it
does exactly three things: attach credentials, unwrap the envelope, and convert a
non-2xx into a typed `ApiError` carrying `meta.code`.

```ts
// src/api/client.ts
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string | undefined,   // meta.code from API.md §1
    readonly field: string | undefined,
    message: string,
  ) { super(message); }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/v1${path}`, { credentials: "include", ...init });
  const body = (await res.json()) as Envelope<T>;
  if (!res.ok) throw new ApiError(res.status, body.meta?.code, body.meta?.field, body.message);
  return body.data as T;
}
```

Components never see the envelope. They see `T`, or an `ApiError` whose `code`
they can branch on — which is what makes `review.already_decided` render as
"someone already decided this" rather than a generic toast.

### 7.3 Query keys and invalidation

One key factory. Ad-hoc key arrays are the source of every stale-cache bug in a
TanStack app.

```ts
// src/lib/queryKeys.ts
export const qk = {
  companies: {
    all:    ["companies"] as const,
    list:   (f: CompanyFilter) => ["companies", "list", f] as const,
    detail: (id: number)       => ["companies", "detail", id] as const,
  },
  postings: {
    all:    ["postings"] as const,
    list:   (f: PostingFilter) => ["postings", "list", f] as const,
    detail: (id: string)       => ["postings", "detail", id] as const,
    scores: (id: string)       => ["postings", "detail", id, "scores"] as const,
  },
  review: {
    all:    ["review"] as const,
    list:   (f: ReviewFilter) => ["review", "list", f] as const,
    detail: (id: string)      => ["review", "detail", id] as const,
  },
  applications: {
    all:    ["applications"] as const,
    detail: (id: string) => ["applications", "detail", id] as const,
    events: (id: string) => ["applications", "detail", id, "events"] as const,
  },
  metrics: { funnel: (g: FunnelGroup) => ["metrics", "funnel", g] as const },
  runs:    { all: ["runs"] as const, detail: (id: string) => ["runs", id] as const },
  claims:  { all: ["claims"] as const, usage: (id: number) => ["claims", id, "usage"] as const },
  health:  ["health"] as const,
} as const;
```

Invalidation is declared next to each mutation, and every entry is justified by a
real server-side write:

| Mutation | Invalidates | Because |
|---|---|---|
| `approve(reviewId)` | `review.all`, `applications.all`, `metrics.funnel`, `postings.detail(postingId)` | Creates an application, removes the item from the queue, moves the funnel |
| `skip(reviewId)` | `review.all` | Queue only |
| `generate(reviewId)` | `review.detail(id)` | Artifacts and plan change; the list row's score does not |
| `patchPlan(reviewId)` | `review.detail(id)` | Same |
| `createCompany` / `addSource` | `companies.all` | List counts and the health table |
| `patchCompany` | `companies.detail(id)`, `companies.list` | Tier and status change ranking and filtering |
| `rescore(postingId)` | `postings.detail(id)`, `postings.scores(id)`, `review.all` | 202; the queue may gain or lose the item |
| `startDiscoveryRun` | `runs.all`, and after completion `postings.all` + `review.all` | See below |
| `addManualEvent(appId)` | `applications.detail(id)`, `applications.events(id)`, `metrics.funnel` | The status trigger recomputes |
| `patchClaim` / `deleteClaim` | `claims.all`, `review.all` | A ledger change can invalidate a queued draft's evidence |

Two policies that matter more than the table:

- **202 responses are polled, not optimistically applied.** `POST /runs/discovery`
  and `POST /postings/{id}/rescore` return 202. The UI switches
  `qk.runs.detail(runId)` to `refetchInterval: 3000` until `status !== "running"`,
  then invalidates the broad keys once. Optimistically mutating the queue for a
  run that has not finished shows the operator a state the server never had.
- **Default `staleTime` is 30 s; the queue's is 0.** The queue is the screen where
  a stale row causes a wasted decision. Everything else tolerates half a minute.
  `GET /health` polls at 60 s and never blocks a render.

### 7.4 Queue — the screen the product lives on

The queue is where ten minutes a day is spent, so its information order is a
product decision, not a layout one.

**Order of information on a card, top to bottom:**

1. Title, company, tier chip, location, age.
2. Composite score and coverage bar — one number to order by, one to judge fit.
3. **The top three gaps, named in full.** Not a count. "missing: advanced Excel
   model building, Power BI, SAP/Anaplan" is the sentence the operator decides
   on. `MATCH_SCORING.md` §8.2 is explicit that the gap list, not the score, is
   what decides an item, and the UI reflects that ordering literally.
4. Recommended variant.
5. Artifact validation status, with `failed` and `needs_manual_review` visually
   distinct from `passed`.

**Detail screen.** One request (`GET /review/{id}`) returns everything
(`API.md` §5), so there is no waterfall and no spinner cascade. The tailoring
plan renders as a **diff against the base variant** — promoted bullets in green,
demoted in grey, rephrased with both texts side by side and the claim IDs shown
as chips — because a diff is reviewable at a glance and a regenerated document is
not (`DOCUMENT_GENERATION.md` §2.2).

**Decision bar.** Three actions. `Approve` is a primary button whose confirmation
copy reads *"Creates the application record. You submit it on the employer's
site — Scout never submits."* The wording is part of the design, not
decoration: invariant 1 is a promise to the operator and the UI is where the
promise is kept visible.

Keyboard: `j`/`k` move, `a` approve, `s` skip, `g` regenerate, `?` help. Ten
minutes a day is a keyboard workflow.

### 7.5 Companies — the screen that keeps 300 sources alive

The registry is the system's input, and a dead source is invisible until
something surfaces it. This screen is that surface.

- **`<SourceHealthTable>` is the primary element**, not a footnote: adapter,
  `last_run_at`, `last_status`, `consecutive_failures`, last error, and a
  **Test** button hitting `POST /sources/{id}/test` which renders the `ProbeResult`
  inline. Auto-disabled sources (≥5 failures) are pinned to the top in a
  distinct row style, because a silently auto-disabled source is exactly the
  failure this screen exists to prevent.
- **`<DetectUrlFlow>` is the add path.** Paste a URL → `POST /companies/detect`
  → show adapter, config and probe result → confirm. A 403
  `source.denied_by_policy` renders as a plain explanation that the host is on
  the never-scrape list and that this is not configurable, with no retry
  affordance. An affordance to retry a policy refusal implies the refusal is
  negotiable.
- Filters mirror `GET /companies` exactly: `status`, `tier`, `tag`, `adapter`,
  `q`, `has_new`. Filter state lives in the URL query string, so a filtered view
  is a bookmark.
- Tier and status are editable inline. They are the two fields the operator
  changes most often (a rejection makes a company `blacklisted`; a good
  conversation makes it `dream`), and both feed ranking.

### 7.6 Dashboard — ten minutes, in order

The dashboard answers four questions in the order they need answering, and shows
nothing else:

1. **Did the machine work?** `<RunHealthBanner>` — last run status, wall clock,
   source failures, LLM cost. Red when the run failed or Gmail is
   unauthenticated. This is first because everything below is meaningless if the
   run did not happen.
2. **What needs me?** `<QueueSummaryCard>` (new items, highest score) and
   `<NeedsDecisionList>` (held-for-review mail, follow-up prompts) — capped at
   five each, matching the digest.
3. **What is going stale?** `<GhostedList>`, ordered by silence descending. The
   most actionable population the operator has.
4. **Is any of this working?** `<FunnelStrip>` — submitted, responded, advanced,
   interviewed, offered, with the observed rates.

The funnel strip carries `meta.note` verbatim: *"Observed rates from your own
history. Not a prediction."* Rates with `n < MIN_N_FOR_RATE` render as the raw
count with the rate suppressed — never as a percentage
(`APPLICATION_PIPELINE.md` §15, criterion 11). A percentage computed from seven
applications is a lie the operator will act on, and the UI is the last place it
can be stopped.

---

## 8. Error taxonomy

### 8.1 The hierarchy

```python
# common/errors.py
class ScoutError(Exception):
    """Root. Every domain exception in the system descends from this.
    Carries the machine code that API.md §1 returns in meta.code."""
    code: ClassVar[str] = "internal.error"
    http_status: ClassVar[int] = 500
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, field: str | None = None,
                 detail: dict[str, Any] | None = None) -> None: ...


# ── request-shaped ───────────────────────────────────────────────────────
class NotFoundError(ScoutError):        code = "resource.not_found";      http_status = 404
class ValidationError(ScoutError):      code = "request.invalid";         http_status = 422
class ConflictError(ScoutError):        code = "resource.conflict";       http_status = 409
class AlreadyDecidedError(ConflictError): code = "review.already_decided"
class RunAlreadyInFlight(ConflictError): code = "run.already_running"
class LocalRateLimited(ScoutError):     code = "request.rate_limited";    http_status = 429

# ── policy ───────────────────────────────────────────────────────────────
class PolicyError(ScoutError):          http_status = 403
class DeniedByPolicy(PolicyError):      code = "source.denied_by_policy"
class OutboundPolicyViolation(PolicyError): code = "mail.outbound_denied"

# ── adapters (SOURCE_ADAPTERS.md §10.2/§10.3) ────────────────────────────
class AdapterError(ScoutError):         code = "adapter.unknown";         http_status = 502
class AdapterConfigError(AdapterError):  code = "adapter.config_invalid";  http_status = 422
class AdapterSchemaError(AdapterError):  code = "adapter.schema_drift"
class AdapterHttpError(AdapterError):    code = "adapter.board_not_found"
class AdapterTransportError(AdapterError): code = "adapter.transport";     retryable = True
class AdapterTimeout(AdapterError):      code = "adapter.timeout";         retryable = True
class RateLimitTimeout(AdapterError):    code = "adapter.rate_limited";    retryable = True
class CircuitOpen(AdapterError):         code = "adapter.circuit_open";    retryable = True
class RobotsDenied(AdapterError):        code = "adapter.robots_denied"
class UndetectableSource(ValidationError): code = "source.undetectable"

# ── LLM (AI_ARCHITECTURE.md §3.1) ────────────────────────────────────────
class LLMError(ScoutError):             code = "llm.error";               http_status = 502
class LLMTimeout(LLMError):             code = "llm.timeout";             retryable = True
class LLMRateLimited(LLMError):         code = "llm.rate_limited";        retryable = True
class LLMProviderUnavailable(LLMError):  code = "llm.unavailable";         retryable = True
class SchemaViolation(LLMError):        code = "llm.schema_violation"
class SchemaEnforcementFailed(LLMError):  code = "llm.enforcement_failed"
class BudgetExhausted(LLMError):        code = "llm.budget_exhausted";    http_status = 429

# ── ledger and generation ────────────────────────────────────────────────
class LedgerError(ScoutError):          code = "ledger.error"
class ValidationFailed(LedgerError):    code = "ledger.validation_failed"; http_status = 422
class ClaimExpired(LedgerError):        code = "ledger.claim_expired";     http_status = 422
class DisclosureDenied(LedgerError):    code = "ledger.disclosure_denied"; http_status = 422
class GenerationError(ScoutError):      code = "generation.failed";        http_status = 502
class DoesNotFit(GenerationError):      code = "generation.does_not_fit"
class PlanInvalid(GenerationError):     code = "generation.plan_invalid"

# ── mail ─────────────────────────────────────────────────────────────────
class MailError(ScoutError):            code = "mail.error";              http_status = 502
class GmailUnauthenticated(MailError):  code = "mail.unauthenticated"
class HistoryCursorExpired(MailError):  code = "mail.cursor_expired";     retryable = True
class DelimiterInjectionAttempt(ScoutError): code = "content.delimiter_injection"; http_status = 422
```

### 8.2 Retryable versus not

`retryable` is a class attribute because the decision belongs to the exception
type, not to the call site that happens to catch it.

| Class | Retryable | Retried by | Ceiling |
|---|---|---|---|
| `AdapterTransportError`, `AdapterTimeout` | **Yes** | `SourceHttpClient`, exponential backoff with full jitter | 4 attempts; 90 s per-source budget |
| `RateLimitTimeout`, `CircuitOpen` | **Yes, next run** | Not retried in-run — the source is marked and the runner moves on. Does **not** increment `consecutive_failures`: it is our own back-pressure, not the source's fault | — |
| `AdapterHttpError` (404/410/403) | **No** | — | A 404 on a board token means the board moved; retrying is 3× the noise, 0× the information |
| `AdapterSchemaError`, `AdapterConfigError` | **No** | — | Needs a code or config change |
| `LLMTimeout`, `LLMRateLimited`, `LLMProviderUnavailable` | **Yes** | `with_transport_retries`, counted separately from repair retries | `max_transport_retries` per family; `LLMTimeout`'s second attempt gets 1.5× the timeout, capped at 90 s; no third |
| `SchemaViolation` | **Yes, as a repair** | `call_structured`'s repair loop with the errors fed back — never the same request again | `max_repair_retries` (1 or 2) |
| `SchemaEnforcementFailed` | **No** | — | Fatal for that item; handled per stage |
| `ValidationFailed` (ledger) | **Once, as a regeneration** | `GenerationService`, with unresolved spans quoted back | `GENERATION_VALIDATION_RETRIES` (1) |
| `DoesNotFit` | **No** | — | `needs_manual_review`; never ships two pages |
| `HistoryCursorExpired` | **Yes, as a fallback** | `mail/sync.py`, bounded full sweep | One sweep |
| `GmailUnauthenticated` | **No** | — | `invalid_grant` is permanent; retrying burns quota |
| `DeniedByPolicy`, `OutboundPolicyViolation` | **Never** | — | Not a failure to recover from. It is the system working |
| `BudgetExhausted` | **No** | — | Remaining items wait for tomorrow |

### 8.3 HTTP mapping

`api/errors.py` registers one handler for `ScoutError` and one for
`RequestValidationError`. Both emit the `API.md` §1 envelope.

```python
# api/errors.py
@app.exception_handler(ScoutError)
async def handle_scout_error(request: Request, exc: ScoutError) -> JSONResponse:
    log.warning("api_error", code=exc.code, status=exc.http_status,
                path=request.url.path, request_id=request_id_var.get())
    return JSONResponse(
        status_code=exc.http_status,
        content=Envelope[None](
            data=None, message=str(exc),
            meta={"code": exc.code} | ({"field": exc.field} if exc.field else {}),
        ).model_dump(),
    )
```

| Status | Raised by | Example code |
|---|---|---|
| 400 | Malformed request the router itself rejects | `request.malformed` |
| 403 | `DeniedByPolicy` on `POST /companies/detect` | `source.denied_by_policy` |
| 404 | `NotFoundError` | `resource.not_found` |
| 409 | `AlreadyDecidedError`, `RunAlreadyInFlight`, duplicate source | `review.already_decided`, `run.already_running`, `company.duplicate_source` |
| 422 | `ValidationError` and its subclasses, FastAPI validation wrapped in the envelope | `request.invalid`, `company.tag_invalid`, `ledger.validation_failed`, `source.undetectable` |
| 429 | `LocalRateLimited`, `BudgetExhausted` | `request.rate_limited`, `llm.budget_exhausted` |
| 502 | `AdapterError`, `LLMError`, `MailError` reaching a request | `adapter.transport`, `llm.unavailable` |

Two rules that are easy to get wrong:

- **An unhandled exception is a 500 with `code: "internal.error"` and a generic
  message.** Never the exception text — an upstream body or a JD fragment is
  untrusted input and must not be echoed to a browser
  (`ARCHITECTURE.md` §2). The detail goes to the structured log with the
  request ID.
- **`GET /health` never returns 500 for a degraded dependency**
  (`API.md` §7). It returns 200 with a per-dependency status map. A degraded LLM
  provider must not take the UI down, and a health endpoint that 500s takes the
  container down with it.

---

## 9. Configuration surface

Every key below already appears in a subsystem document; this is the assembled
`Settings` object, grouped. `CONFIGURATION.md` is canonical for defaults and
prose; this table is canonical for which module reads which key.

| Group | Keys | Read by |
|---|---|---|
| Core | `ENVIRONMENT`, `DATABASE_URL`, `DATABASE_URL_SYNC`, `REDIS_URL`, `SESSION_SECRET`, `OPERATOR_PASSWORD_HASH`, `TZ_DISPLAY` | `db`, `api`, `scheduler` |
| Sources | `SOURCE_CONCURRENCY` (8), `SOURCE_USER_AGENT`, `SOURCE_PROBE_TIMEOUT_S` (10), `SOURCE_RETRY_BUDGET_S` (90), `SOURCE_TIMEOUT_S` (180), `RUN_TIMEOUT_S` (900), `RATE_LIMIT_WAIT_S` (20), `MAX_DESCRIPTION_CHARS` (40000), `AUTO_DISABLE_THRESHOLD` (5) | `sources`, `ingest` |
| Filtering | `DEFAULT_LOCATION_FILTER`, `FILTER_SENIORITY_ALLOWED`, `FILTER_KEYWORD_DENYLIST`, `FILTER_TITLE_DENYLIST`, `FILTER_EMPLOYMENT_TYPES` | `ingest/filters.py` |
| Extraction | `EXTRACTION_PROMPT_VERSION`, `SKILL_VOCAB_VERSION`, `SKILL_TRIGRAM_THRESHOLD` (0.62), `SKILL_ADJACENCY_MIN` (0.40), `EXTRACTION_MAX_JD_TOKENS` (4000) | `extract` |
| Scoring | `SCORING_PROMPT_VERSION`, `SCORING_BLEND_HARD` (0.80), `SCORING_PARTIAL_CREDIT` (0.50), `SCORING_TIER_WEIGHTS`, `SCORING_RECENCY_GRACE_DAYS` (14), `SCORING_RECENCY_HALF_LIFE_DAYS` (45), `SCORING_RECENCY_FLOOR` (0.65), `SCORING_HARD_GATE_BANDS`, `RESCORE_MAX_AGE_DAYS` (30) | `scoring` |
| Generation | `GENERATION_ENABLED`, `GENERATION_MIN_COMPOSITE` (30.0), `GENERATION_MIN_COVERAGE_PCT` (45.0), `GENERATION_DAILY_CAP` (10), `COVER_LETTER_ENABLED`, `COVER_LETTER_TARGET_WORDS` (400), `LETTER_MAX_JD_TOKENS` (900), `RESUME_MAX_PAGES` (1), `RENDER_VERIFY_PAGES`, `SIMILARITY_WARN` (0.55), `SIMILARITY_BLOCK` (0.72), `ARTIFACT_DIR` | `generate` |
| Ledger | `LEDGER_DEFAULT_TTL_DAYS` (180), `LEDGER_EXPIRED_CLAIM_POLICY` (`fail`), `LEDGER_RESTRICTED_DISCLOSURE_COMPANIES` (`[]`), `LEDGER_APPROXIMATION_TOLERANCE` (0.05), `LEDGER_VALIDATION_PROMPT_VERSION`, `GENERATION_VALIDATION_RETRIES` (1), `LEDGER_EXPIRY_WARNING_DAYS` (30) | `ledger`, `generate` |
| LLM | `LLM_PROVIDER`, `LLM_MODEL_FAST`, `LLM_MODEL_STRONG`, `LLM_MAX_CONCURRENCY` (4), `LLM_DAILY_BUDGET_INR` (80), `LLM_BUDGET_WARN_PCT` (80), `LLM_INR_PER_USD` (88), `LLM_PRICE_*`, `LLM_CACHE_ENABLED` | `llm` |
| Mail | `MAIL_ENABLED`, `MAIL_OPERATOR_ADDRESS`, `MAIL_ALERT_ADDRESS`, `MAIL_TOKEN_PATH`, `MAIL_TOKEN_KEY`, `MAIL_POLL_CRON`, `MAIL_LOOKBACK_DAYS` (14), `MAIL_ALERT_LOOKBACK_DAYS` (2), `MAIL_LINK_WINDOW_DAYS` (120), `MAIL_CONF_THRESHOLD_TERMINAL` (0.90), `MAIL_CONF_THRESHOLD_DEFAULT` (0.80), `MAIL_COMPANY_TRGM_THRESHOLD` (0.75), `MAIL_STORE_SUBJECT`, `MAIL_MAX_BODY_CHARS` (5000), `MAIL_RATE_UNITS_PER_SEC` (20) | `mail` |
| Digest | `DIGEST_ENABLED`, `DIGEST_SEND_AT` (08:15 IST), `DIGEST_MAX_QUEUE_ITEMS` (10), `DIGEST_MAX_ALERT_ITEMS` (10), `SCOUT_BASE_URL` | `mail/digest.py` |
| Tracking | `GHOST_AFTER_DAYS` (30), `SUBMIT_CONFIRM_DAYS` (3), `MIN_N_FOR_RATE` (15), `MIN_N_FOR_COMPARISON` (30), `FOLLOWUP_MAX_PER_DAY` (3), `FOLLOWUP_MAX_PER_APP` (2), `FOLLOWUP_QUIET_DAYS_*`, `EXPORT_DIR`, `EXPORT_CRON`, `PRUNE_CRON`, `RETENTION_POSTING_DAYS`, `RETENTION_MAIL_MONTHS` | `tracking` |
| Feature flags | `FF_TAILORING_REPHRASE` (false), `FF_GAP_IN_OPENING` (false) | `generate` |

Rules governing this surface:

1. **No magic constants in modules.** A number that could reasonably differ
   between environments is a setting. A number whose change would invalidate
   stored data — the Workday page size of 20, the ULID length, adapter fidelity
   ranks — is a **code constant** and is deliberately not configurable.
2. **`NEVER_FETCH_HOSTS` is not on this object** and is not reachable from it. A
   test asserts that (`SOURCE_ADAPTERS.md` §4.7).
3. **Changing any scoring key bumps `SCORING_PROMPT_VERSION` in the same
   commit** (`MATCH_SCORING.md` §13.1). A formula change reusing a version
   string makes two incomparable score families indistinguishable.
4. **New generation behaviour ships flagged off** and is proven on a slice
   (`ARCHITECTURE.md` §8).
5. Secrets — AWS/Azure credentials, `MAIL_TOKEN_KEY`, `SESSION_SECRET` — come
   from the environment or a secret store, appear in no log line and no error
   message, and are `SecretStr` on the model so an accidental `repr` prints
   `**********`.

---

## 10. Testing hooks

The seams below exist because they were designed in, not because they were
discovered later. Each corresponds to a specific class of test that would
otherwise need the network, a clock, or a wallet.

### 10.1 Adapter fakes and recorded fixtures

`SourceAdapter` is a `Protocol`, so a fake is a class, not a mock:

```python
# tests/fakes/adapters.py
class FakeAdapter:
    """Satisfies SourceAdapter structurally. No HTTP, no fixtures on disk."""
    name = AtsType.GREENHOUSE
    config_model = GreenhouseConfig
    fidelity_rank = 90
    default_poll_interval_minutes = 1440
    requires_detail_fetch = False

    def __init__(self, *, postings: list[RawPosting] | None = None,
                 raises: Exception | None = None, delay_s: float = 0.0) -> None: ...

    async def fetch(self, *, since=None) -> AsyncIterator[RawPosting]:
        if self._raises: raise self._raises
        for p in self._postings:
            if self._delay: await asyncio.sleep(self._delay)
            yield p
```

`raises` and `delay_s` are what make failure-isolation and timeout tests
possible: "one adapter raises, the run still completes and reports it", and "one
adapter exceeds the 180 s ceiling, the others are unaffected".

Real adapters are tested against **recorded fixtures**, which are the
specification (`SOURCE_ADAPTERS.md` §12): `tests/fixtures/sources/{adapter}/`.
All six required per-adapter tests run offline, enforced by an autouse fixture
that patches the transport to raise:

```python
# tests/conftest.py
@pytest.fixture(autouse=True, scope="function")
def _no_network(monkeypatch, request):
    """Any socket in a sources/ test is a failure. Opt out explicitly with
    @pytest.mark.network, which is excluded from the default CI selection."""
```

### 10.2 The deterministic LLM stub

```python
# tests/fakes/llm.py
class StubLLM:
    """Implements LLMClient. Returns canned, schema-valid responses keyed by
    (family, a hash of the untrusted input)."""

    def __init__(self, responses: dict[str, BaseModel] | None = None,
                 *, fail_with: Exception | None = None,
                 violate_schema_times: int = 0) -> None: ...

    async def structured[T: BaseModel](self, *, model, system, user, schema,
                                       **kw) -> LLMResponse[T]:
        self.calls.append(RecordedCall(model=model, system=system, user=user,
                                       schema=schema.__name__))
        if self._fail_with: raise self._fail_with
        if self._violations:
            self._violations -= 1
            raise SchemaViolation(errors=[{"msg": "stubbed"}], raw="{}")
        return LLMResponse(value=self._lookup(schema), raw_text="{}",
                           model_id="stub", prompt_version="stub.v0",
                           usage=Usage(100, 50), latency_ms=1, attempts=1,
                           stop_reason="end_turn")
```

`self.calls` is the assertion surface for the tests that matter most:

- **`RecordedCall.user` proves the untrusted content was enveloped**, not
  concatenated — the prompt-injection defence is testable as a string property.
- **`violate_schema_times=1`** proves the repair loop runs once and then
  succeeds; `=3` proves it gives up rather than looping.
- **`fail_with=LLMProviderUnavailable`** proves each stage's documented
  degradation: extraction leaves the posting unextracted, coverage judgement
  fails *closed* to `missing`, generation enqueues without a letter, mail
  classification writes no event.
- The call count proves the `content_hash` cache: two identical postings, one
  call.

### 10.3 Frozen clock

`FrozenClock` (§2.1) is injected everywhere a time is read. It is what makes
these testable at all:

| Test | Needs |
|---|---|
| Recency decay at exactly 14, 15 and 59 days | A clock that can sit on a boundary |
| `v_ghosted` at 29, 30 and 31 days, and its retroactive removal on day 45 | A clock plus `pg_sleep`-free date arithmetic |
| The relative-date parser resolving against **run start**, not `now()` | A clock passed into `parse_posted_at` |
| Claim expiry flipping `met` → `partial` | A clock crossing `expires_at` |
| Follow-up quiet periods | A clock advancing past each threshold |

For the SQL views, the same effect is achieved by parameterising `now()` —
`GHOSTED_SQL` takes `:ghost_after_days` and the test inserts events at computed
offsets rather than mocking the database clock.

### 10.4 Fixture Postgres

Real Postgres, never SQLite. The schema uses `TSVECTOR` generated columns, `GIN`
indexes, `pg_trgm`, native `ENUM`s, partial unique indexes and four PL/pgSQL
triggers; a SQLite test suite would pass while testing none of them.

```python
# tests/conftest.py
@pytest.fixture(scope="session")
def pg_container() -> PostgresContainer:
    """testcontainers, postgres:16 with pg_trgm. Alembic is run to head ONCE
    per session — running migrations rather than create_all is what proves the
    migrations themselves work."""


@pytest.fixture(scope="function")
async def session(pg_container) -> AsyncIterator[AsyncSession]:
    """Each test runs inside a transaction that is ROLLED BACK at teardown, via
    a nested SAVEPOINT so service code can commit freely. Fast, and no test can
    leak a row into another."""
```

The trigger tests are the important ones and cannot be written any other way:
attaching a `failed` artifact must raise `IntegrityError` with SQLSTATE 23514;
inserting events out of order must yield the same `application.status` as
inserting them in order; two `is_recommended` rows for one
`(posting_id, prompt_version)` must violate `match_one_recommendation_idx`.

### 10.5 The other seams

| Seam | Purpose |
|---|---|
| `Deps` dataclass (session factory, redis, llm, clock, settings, http) constructed once and passed down | No module-level singletons; a test builds a `Deps` with fakes and never patches an import |
| `RunLock` behind a Protocol | An in-memory implementation lets concurrency tests run without Redis |
| `page_count()` injectable | `FakePageCounter(pages=2)` drives the fit ladder deterministically without LibreOffice in the unit suite |
| `GmailClient` behind a Protocol | `FakeGmail(messages=[...])` replays fixture `.eml` files; the send path's `OutboundPolicyViolation` is asserted directly |
| Golden sets in `tests/golden/scoring/` (40 labelled JDs) | `scripts/eval_scoring.py` gates every prompt, vocabulary or formula change in CI, with grounding violations gating at **zero** |

---

## 11. Performance budget

Target: a complete discovery run in **under 15 minutes** wall clock
(`ARCHITECTURE.md` §9), at 320 sources / ~4,000 raw postings / ~150 new / ~30
surviving the filter / ≤10 drafts.

### 11.1 Per-stage budget

| Stage | Work | Budget | Dominated by |
|---|---|---|---|
| ① Discover | 320 sources, 8 concurrent, ~40 sequential slots | **600 s** | Network round-trips and rate-limit buckets. Mean source ≤ 15 s. |
| ② Normalise | HTML → text for ~4,000 postings | **25 s** | `selectolax` parsing; ~6 ms per posting, in-process, inside ①'s wall clock |
| ③ Dedupe | 4,000 identity lookups + one collapse pass | **20 s** | Indexed lookups on `(source_id, external_id)`; one grouped query for the collapse |
| ④ Filter | ~150 new postings through nine predicates | **2 s** | Pure Python over rows already loaded |
| ⑤ Extract | ~30 LLM calls, 4 concurrent, ~6 s each | **60 s** | Provider latency. The `content_hash` cache removes 15–25% of these. |
| ⑥ Score | 30 postings × 6 variants = 180 scorings | **10 s** | `Decimal` set arithmetic; no I/O beyond loading variants once |
| ⑦ Rank | Ordering + `is_recommended` writes | **2 s** | One UPDATE per posting |
| ⑧ Generate | ≤10 plans + ≤6 letters on the `strong` model, 4 concurrent | **150 s** | Provider latency at 60 s policy timeout; rendering is **not** here (§11.2) |
| ⑨ Validate | ≤16 validations: regex + one `fast` call each | **40 s** | The assertion pass |
| ⑩ Enqueue | ≤10 inserts | **1 s** | — |
| ⑪ Digest | Six queries + one Gmail send | **8 s** | Gmail round-trip |
| | **Total** | **~918 s worst case, ~660 s expected** | |

The worst case exceeds 900 s, which is stated rather than hidden. It is reached
only when stage ① consumes its entire 600 s budget *and* every generation call
runs to its timeout. The `RUN_TIMEOUT_S` cancellation therefore lands on stage ①
stragglers first — which is the correct place for it, since a cancelled source is
recorded as `timeout` and retried tomorrow, whereas a cancelled generation wastes
tokens already spent.

### 11.2 What is deliberately outside the run budget

| Work | Where it runs | Why |
|---|---|---|
| `.docx` rendering and the LibreOffice page-count ladder (~8 s worst case per document) | On **preview/approve**, request-scoped | It is human-triggered, the human is waiting, and putting five subprocess launches per document inside a batch run buys nothing |
| `.xlsx` export | 23:30 nightly, separate job | Read-only, sub-second, no reason to share the run |
| Rescore sweep | 03:00 nightly, separate job | Refreshes `R`, which decays with wall-clock time; not part of discovery |

### 11.3 The one place the budget does not close

Workday is the highest-coverage adapter and the dominant cost
(`SOURCE_ADAPTERS.md` §5.4). Its arithmetic does not fit, and the design must
say so rather than discover it in production:

```
Per-tenant bucket:      1 req/s
Per-source ceiling:     180 s  ⇒  ~180 requests
Page size:              20 (server-clamped)
Detail fetch:           1 request per posting

Requests for a board of N postings = ceil(N/20) + N
⇒ the 180 s ceiling is reached at N ≈ 170.
⇒ WorkdayConfig.max_pages = 50 (1,000 postings) is unreachable in one run.
```

A 1,483-posting Adobe board therefore hits the ceiling, is recorded as
`timeout`, and — because partial ingestion is forbidden (`SOURCE_ADAPTERS.md`
§4.4) — yields nothing. This is a real defect in the current numbers, not a
rounding issue, and §12.1 states the options. The interim mitigation is the one
the schema already supports: split a large tenant into several `source` rows with
different `applied_facets`, which is exactly what
`UNIQUE (company_id, adapter, config)` was designed for.

### 11.4 API latency targets

| Endpoint | Target (p95) | Note |
|---|---|---|
| `GET /review`, `GET /postings` | < 150 ms | Cursor pagination, covering indexes |
| `GET /review/{id}` | < 200 ms | One composed query; the whole decision screen |
| `GET /metrics/funnel` | < 300 ms | View over low-thousands of rows |
| `POST /companies/detect` | < 1.5 s | One probe at a 10 s cap; must feel instant |
| `POST /postings/import` | < 15 s | Synchronous extract → score → generate; 202 fallback past the cap |
| `POST /review/{id}/approve` | < 400 ms | One transaction, zero outbound requests |

---

## 12. Open design questions

Genuine ones. Each states the tension, what is being done in the meantime, and
what would settle it.

### 12.1 Workday's request budget does not fit the per-source ceiling

**The problem.** §11.3: a Workday board over ~170 postings cannot complete inside
180 s at 1 req/s, and partial ingestion is forbidden because a half-fetched board
looks like mass closure to the two-run rule. Adobe, Nvidia, Salesforce, Dell,
Cisco and HPE are all Workday, and all have boards well over that.

**Options, none free.**

| Option | Cost |
|---|---|
| Raise the per-tenant bucket above 1 req/s | Contradicts §4.3's conservative-floor policy, against a vendor that publishes no limit |
| Persist a per-source paging cursor and fetch the board across several days | Breaks the "one run sees the whole board" assumption that `closed_at` depends on; needs a per-source `last_complete_sweep_at` and a close rule keyed on that instead |
| Raise the per-source ceiling for `requires_detail_fetch` adapters only | Pushes one source toward 20 minutes, exceeding the run budget on its own |
| Skip the detail fetch and score from the list stub | Workday stubs have no description; the posting would be unscoreable, which is the `mail_alert` problem again |

**Interim.** Split large tenants into several `source` rows by `applied_facets`
(location, job family). This works, it is what the unique constraint anticipated,
and it is manual.

**What settles it.** Measuring a real Adobe sweep: requests, wall clock, and
whether the tenant tolerates 2 req/s without a 429. Until that number exists,
every option above is a guess.

### 12.2 The two-run close rule versus intermittently-failing sources

The counter freezes on any source status other than `ok`/`empty`
(`SOURCE_ADAPTERS.md` §10.5), which is right. But a source that alternates
`ok` / `rate_limited` daily advances its counter every other run, so a posting
genuinely removed from a board takes four days to close instead of two. A source
that fails every third run takes three. The close latency is therefore a function
of source health, and nothing measures it.

**Candidate fix:** close on "not seen in the last two runs *in which this source
succeeded*, and at least N days have passed", making the rule time-bounded as
well as run-bounded. Not adopted yet because it adds a second parameter to a rule
whose simplicity is its main virtue.

### 12.3 Cross-source collapse is a filter, not a merge

§3.1 marks the loser `filtered_out` with `superseded_by:`. Simple and reversible.
But the two postings are genuinely one role, and the loser may carry information
the winner lacks — a compensation band from Ashby, an alternative apply URL, an
earlier `first_seen_at` that is the *true* discovery date. Today that information
is reachable only by querying the superseded row directly, and `first_seen_at` on
the winner overstates how long the role has been known, which biases recency
scoring slightly in the wrong direction.

**The alternative** is a `posting_alias` table making one canonical posting with
several source records. That is a schema change, a migration, and a change to
every query that assumes one posting per source. It is not obviously worth it at
this scale, and it is the sort of change that is much cheaper to make before
there are 40,000 postings than after.

### 12.4 The scheduler shares a process with the API

APScheduler runs in the FastAPI process (`ARCHITECTURE.md` §4). At this scale
that is right. Two consequences are unresolved:

1. A discovery run holding the event loop for 15 minutes competes with request
   handling. The run is I/O-bound so this is *mostly* fine, but JSON parsing of
   4,000 postings is CPU work on the same loop, and a p95 API latency of 150 ms
   during a run has never been measured.
2. Scaling the API to two replicas would run the scheduler twice. The Redis run
   lock prevents duplicate *runs*, but not duplicate mail polls racing on the
   cursor, and the job store would have two schedulers competing for the same
   jobs.

**Not solved because it is not yet a problem** — one user, one container. The
cheap guard is a `SCHEDULER_ENABLED` setting so a second replica can run with it
off. Adding that setting now, before it is needed, is probably correct; it is
listed here rather than in §9 because it has not been decided.

### 12.5 The skill vocabulary's growth path

`skill_proposal` collects phrases the three-stage resolver could not place
(`MATCH_SCORING.md` §3.2), and new tokens enter through a reviewed commit. That
is right for auditability, and it means **the vocabulary only improves when the
operator does a chore**. If the queue is empty for a month, unresolved
requirements score `missing`, coverage is understated, and good roles rank below
where they should — silently, because an understated score looks exactly like a
genuine gap.

**What would help:** a digest line ("11 unmapped skill phrases, appearing in 34
postings"), ordered by how many postings each blocks. That converts a chore into
a ranked decision. Whether it belongs in the digest or on the Dashboard is
undecided; the digest is already long.

### 12.6 Mail linkage confidence is unvalidated

The R1–R5 chain (§3.6) has plausible confidence values — 0.99, 0.90, 0.85, 0.80,
0.55 — and a combination rule of `1 - Π(1 - cᵢ)`. **None of these numbers comes
from data.** They come from judgement about which signals are reliable. The
combination rule additionally assumes the signals are independent, which
Reply-To domain and display name plainly are not (a well-configured ATS sets
both).

The design is safe against this — an ambiguous set is held for review rather than
guessed — so the failure mode is extra manual linking, not wrong data. But the
thresholds should be re-derived from the first hundred real messages, and there
is no mechanism yet for capturing "the operator's manual link disagreed with what
the chain would have chosen". Adding that is cheap and has not been specified.

### 12.7 `bypassed` is defined twice, slightly differently

`CLAIMS_LEDGER.md` §6.3 says `bypassed` marks an artifact that was **never
machine-generated** — hand-authored or seed-imported.
`DOCUMENT_GENERATION.md` §8.3 says it applies to an artifact rendered from an
**unmodified base variant with an empty tailoring plan**, whose content was
validated at seed time.

These overlap but are not the same set: the second admits a machine-rendered
document, which the first excludes. Both agree `bypassed` may attach and that
nothing *generated* can be `bypassed`, so the invariant is intact either way, and
the implementation currently follows the narrower reading (the ledger document,
which is canonical for the ledger). **The two documents should be reconciled**,
and the reconciliation is a documentation change, not a code change — recorded
here because it will otherwise be found by someone implementing the base-variant
render path.

### 12.8 There is no measurement of what the filter throws away

Stage ④ removes ~80% of postings and is the reason the cost model works
(`AI_ARCHITECTURE.md` §8.3). Every rejection stores a `filter_reason`, so the
distribution is queryable. What is **not** measured is the false-negative rate:
how often a filter dropped a role the operator would have applied to. By
construction that is unobservable — the operator never sees it.

**A partial answer:** sample. Once a week, admit 20 randomly-chosen filtered
postings into scoring anyway, flag them, and see whether any score above
`GENERATION_MIN_COMPOSITE`. That costs ~20 extra extractions a week (~₹5) and
would turn an unmeasurable risk into a number. It is not designed in because the
sampling would need its own flag, its own digest line and a rule preventing
sampled items from consuming queue slots — and because the honest position is
that the filter's false-negative rate matters less in month one than getting the
system running at all.

### 12.9 Six variants scored against every posting will not stay cheap

180 scorings per run is trivial. The rescore sweep is not: 30 days of open
postings × 6 variants, re-run nightly, is a few thousand scorings — still fine,
but the growth is `open_postings × active_variants`, and both grow. The variant
count is the sharper edge: adding a seventh and eighth variant is a natural
thing to want, and it is a 33% increase in scoring work and in
`match_score` rows *per prompt version*, with the A/B mechanism keeping old
versions forever.

No pruning policy for `match_score` exists. `RETENTION_POSTING_DAYS` prunes
closed postings and cascades, but scores for *open* postings under superseded
prompt versions accumulate without limit. A retention rule — "keep the active
version plus one" — is probably right, and it interacts with §11 of
`MATCH_SCORING.md`'s A/B comparison, which needs both versions present. Undecided.

---

## 13. Related documents

| Document | Relationship to this file |
|---|---|
| `ARCHITECTURE.md` | Parent. Invariants, module boundaries, pipeline stages, scale envelope. Wins on all of them. |
| `DATA_MODEL.md` | Canonical for every table, column, index and trigger this document's signatures read and write |
| `API.md` | Canonical for endpoint paths, the envelope, status codes and the machine codes in §8 |
| `SOURCE_ADAPTERS.md` | Canonical for the `SourceAdapter` protocol, `RawPosting`, rate limits, failure isolation. §2.3, §3.1 and §11.3 elaborate; they do not override |
| `COMPANY_REGISTRY.md` | Canonical for ATS detection, tags, location filters, company status. Backs §2.4 and §3.2 |
| `MATCH_SCORING.md` | Canonical for extraction, normalisation, coverage, the composite formula, gaps. Backs §2.6, §2.7, §3.3 |
| `CLAIMS_LEDGER.md` | Canonical for the ledger, disclosure, the validation pass, the DB triggers. Backs §2.8, §3.5 |
| `DOCUMENT_GENERATION.md` | Canonical for the tailoring plan, bullet selection, rendering, the fit ladder. Backs §2.9, §3.4 |
| `AI_ARCHITECTURE.md` | Canonical for the provider abstraction, routing, prompt registry, enforcement, cost. Backs §2.14, §4.4 |
| `EMAIL_INGESTION.md` | Canonical for Gmail access, alert parsing, linkage, classification, the digest. Backs §2.11, §3.6 |
| `APPLICATION_PIPELINE.md` | Canonical for the lifecycle, the event log, the funnel, `ghosted`, the export. Backs §2.12, §3.7 |
| `SECURITY_ARCHITECTURE.md` | Threat model and secret handling. Constrains §2.1, §2.3, §8.3 |
| `CONFIGURATION.md` | Canonical for defaults and prose on every key in §9 |
| `INFRASTRUCTURE.md` | Runtime topology, the pinned LibreOffice version §2.9 depends on, backups |
