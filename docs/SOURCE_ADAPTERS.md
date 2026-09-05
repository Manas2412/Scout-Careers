# SOURCE ADAPTERS — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the `SourceAdapter` protocol, the `RawPosting` DTO,
per-adapter config shapes, endpoint contracts, fidelity ranks, normalisation
rules and failure-isolation semantics. `ARCHITECTURE.md` wins on system-level
concerns and the invariants; `DATA_MODEL.md` wins on persisted columns; `API.md`
wins on endpoint paths and error codes. Everything else about a source adapter
is decided here.

Module: `backend/src/scout_careers/sources/`. This layer **never touches the
database**. It fetches, normalises and yields DTOs; `ingest/` persists them.

---

## 1. What an adapter is and is not

An adapter is a pure function from a validated config object to a stream of
`RawPosting` DTOs, plus a cheap liveness probe. That is the whole contract.

An adapter **is**:

- the only place in the system that knows an employer's ATS wire format,
- responsible for pagination, per-source rate limiting and its own retries,
- responsible for turning vendor-specific fields into the canonical DTO.

An adapter **is not**:

- a persistence layer — it never opens a session, never writes a row,
- a deduplicator — cross-source collapsing happens in `ingest/`,
- a filter — location and seniority gates are stage ④ of the pipeline, not the
  adapter's business (the one exception is server-side query narrowing on
  sources that require a search term, §6.9–§6.11),
- an LLM caller — extraction is stage ⑤ and runs on the persisted posting.

This separation is what makes an adapter testable against a recorded fixture
with no database, no Redis and no network.

### 1.1 Adapter taxonomy

| Class | Adapters | Property |
|---|---|---|
| **Multi-tenant ATS** | greenhouse, lever, ashby, workday, smartrecruiters, workable, recruitee | One implementation serves N employers; config carries the tenant key |
| **Single-employer career API** | google, amazon, microsoft | One implementation serves exactly one employer; config carries query narrowing |
| **Derived** | mail_alert | Not a fetch at all — parses mail already delivered to the operator's mailbox |
| **Human** | manual | Not an adapter; `POST /postings/import` writes the posting directly |

`manual` exists in the `ats_type` enum (`DATA_MODEL.md` §2) so a hand-entered
posting has a `source` row and therefore a stable `(source_id, external_id)`
identity like every other posting. There is no `ManualAdapter` class; the ingest
path for it is the import endpoint.

---

## 2. The protocol

### 2.1 `SourceAdapter`

```python
# sources/base.py
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

from scout_careers.common.types import AtsType


@runtime_checkable
class SourceAdapter(Protocol):
    """One employer-facing job source.

    Implementations are constructed per run, used once, and closed. They are
    not thread-safe and must not be cached across runs — the shared HTTP client
    and the rate-limit lease are bound at construction.
    """

    # ---- class-level identity -------------------------------------------
    name: ClassVar[AtsType]
    """Matches the `ats_type` enum value and the `source.adapter` column."""

    config_model: ClassVar[type[BaseModel]]
    """Pydantic model that validates `source.config` JSONB for this adapter."""

    fidelity_rank: ClassVar[int]
    """0-100. Higher wins when collapsing cross-source duplicates. See §7."""

    default_poll_interval_minutes: ClassVar[int]
    """Seeded into `source.poll_interval_minutes` on creation. Invariant 8."""

    requires_detail_fetch: ClassVar[bool]
    """True when the list endpoint omits the description (workday, microsoft)."""

    # ---- construction ----------------------------------------------------
    def __init__(
        self,
        *,
        source_id: int,
        config: BaseModel,
        http: SourceHttpClient,
    ) -> None: ...

    # ---- lifecycle -------------------------------------------------------
    @classmethod
    def parse_config(cls, raw: dict) -> BaseModel:
        """Validate raw JSONB into `config_model`.

        Raises `AdapterConfigError` (mapped to HTTP 422) on failure. Called by
        the registry on source creation AND at the top of every run, so a
        config that rotted since it was saved fails loudly, not silently.
        """

    async def probe(self) -> ProbeResult:
        """One cheap request. Confirms the endpoint exists and returns postings.

        Must complete within `settings.source_probe_timeout_s` (default 10) and
        must never page. Backs `POST /companies/detect` and
        `POST /sources/{id}/test` (`API.md` §2).
        """

    def fetch(self, *, since: datetime | None = None) -> AsyncIterator[RawPosting]:
        """Yield every currently-listed posting for this source.

        `since` is advisory: adapters whose upstream supports server-side date
        filtering use it; the rest ignore it and `ingest/` does change
        detection by `content_hash`. Adapters MUST NOT swallow their own
        exceptions — the runner owns failure isolation (§9).
        """

    async def aclose(self) -> None:
        """Release adapter-local resources. The HTTP client is NOT owned here."""

    # ---- presentation ----------------------------------------------------
    def describe(self) -> str:
        """Short human string for the Companies page and the digest,
        e.g. 'Workday · adobe / external_experienced'."""
```

`SourceAdapter` is a `Protocol`, not an ABC, deliberately: adapters are
independent modules with no shared state, and structural typing keeps the
import graph flat. A registry maps enum value to concrete class:

```python
ADAPTERS: dict[AtsType, type[SourceAdapter]] = {
    AtsType.GREENHOUSE: GreenhouseAdapter,
    AtsType.LEVER: LeverAdapter,
    AtsType.ASHBY: AshbyAdapter,
    AtsType.WORKDAY: WorkdayAdapter,
    AtsType.SMARTRECRUITERS: SmartRecruitersAdapter,
    AtsType.WORKABLE: WorkableAdapter,
    AtsType.RECRUITEE: RecruiteeAdapter,
    AtsType.GOOGLE: GoogleCareersAdapter,
    AtsType.AMAZON: AmazonJobsAdapter,
    AtsType.MICROSOFT: MicrosoftCareersAdapter,
    AtsType.MAIL_ALERT: MailAlertAdapter,
}
```

A startup assertion checks that every non-`manual` enum member has an entry and
that every registered class satisfies `isinstance(cls, SourceAdapter)`. A
missing adapter is a boot failure, not a runtime 500.

### 2.2 `RawPosting`

The DTO crossing the `sources/` → `ingest/` boundary. It is *raw* in the sense
that it has not been deduplicated, filtered, scored or persisted — but it is
already normalised: field names, HTML stripping, location parsing and timestamp
handling are the adapter's job, not `ingest/`'s.

```python
# sources/base.py
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

EmploymentType = Literal[
    "full_time", "part_time", "contract", "internship", "temporary", "unknown"
]
Seniority = Literal[
    "intern", "entry", "mid", "senior", "staff", "principal",
    "manager", "director", "executive", "unknown"
]


class RawPosting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- identity -------------------------------------------------------
    external_id: Annotated[str, Field(min_length=1, max_length=256)]
    """Stable upstream ID. Unique within a source, forever. See §2.3."""

    url: HttpUrl
    """Public, human-openable posting URL. This is what the operator clicks."""

    # --- content --------------------------------------------------------
    title: Annotated[str, Field(min_length=1, max_length=512)]
    description_html: str | None = None
    description_text: Annotated[str, Field(min_length=1)]
    """Already HTML-stripped and whitespace-normalised. Never None: a posting
    with no description is dropped by the adapter with a WARN, because an
    empty JD cannot be extracted or scored and would burn a filter slot."""

    # --- classification -------------------------------------------------
    department: str | None = None
    location_raw: str | None = None
    location_city: str | None = None
    location_country: str | None = None   # ISO-3166 alpha-2, upper case
    is_remote: bool = False
    employment_type: EmploymentType = "unknown"
    seniority_guess: Seniority = "unknown"

    # --- time -----------------------------------------------------------
    posted_at: datetime | None = None
    """UTC, tz-aware. None when upstream gives only a fuzzy string it cannot
    resolve (see §8.5). Never fabricated as now()."""

    # --- provenance -----------------------------------------------------
    raw: dict[str, Any] = Field(default_factory=dict)
    """The upstream object, trimmed to fields we might later want. Persisted to
    `job_posting.raw`. Must not contain auth material — see §4.6."""

    @field_validator("location_country")
    @classmethod
    def _iso2(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) != 2 or not v.isalpha():
            raise ValueError("location_country must be ISO-3166 alpha-2")
        return v.upper()
```

`content_hash` is **not** on the DTO. It is computed by `ingest/` as
`sha256(description_text)` (`DATA_MODEL.md` §4.1) so the hash algorithm lives in
exactly one place and an adapter cannot accidentally change change-detection
semantics.

`company_id` and `source_id` are not on the DTO either. The runner knows which
source it invoked; an adapter asserting its own company is an opportunity for
the two to disagree.

`extra="forbid"` and `frozen=True` are both load-bearing. Forbidding extras
turns "the vendor added a field and someone quietly started depending on it"
into a test failure. Freezing means `ingest/` cannot mutate an adapter's output
in place, which keeps the fixture-replay tests meaningful.

### 2.3 External ID rules

`(source_id, external_id)` is posting identity (`DATA_MODEL.md` §4.1), so the
choice of `external_id` per adapter is a permanent decision:

1. Prefer the upstream's own immutable primary key over a requisition number,
   which employers reuse across geographies.
2. Never derive it from the title, the location or the URL slug — all three are
   edited in place by recruiters, and an edit would create a phantom posting.
3. Never derive it from a hash of the description — that is `content_hash`'s
   job, and conflating the two makes every JD edit look like a new role.
4. Where the upstream truly has no stable key (`mail_alert`), synthesise one
   deterministically and document the recipe in that adapter's subsection.

The per-adapter choice is stated in each subsection's "Identity" line.

### 2.4 `ProbeResult`

```python
class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reachable: bool
    sample_count: int = 0          # postings visible in the probe response
    latency_ms: int
    http_status: int | None = None
    detail: str | None = None      # short, safe to display; never a raw body
    company_name_guess: str | None = None
```

This is the `probe` block returned verbatim by `POST /companies/detect` and
`POST /sources/{id}/test` (`API.md` §2). `detail` is a curated message —
`"Board token not found"`, not an upstream stack trace — because it is rendered
in the UI and upstream bodies are untrusted input (`ARCHITECTURE.md` §2).

### 2.5 Per-adapter config models

Every adapter declares a Pydantic model that validates the `source.config`
JSONB. This is the only schema `config` ever has; there is no free-form
fallback.

```python
class GreenhouseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_token: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class LeverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class AshbyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_name: Annotated[str, Field(min_length=1, max_length=64)]
    include_compensation: bool = True


class WorkdayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: Annotated[str, Field(pattern=r"^[a-z0-9.-]+\.myworkdayjobs\.com$")]
    tenant: Annotated[str, Field(min_length=1, max_length=64)]
    site: Annotated[str, Field(min_length=1, max_length=64)]
    locale: str = "en-US"
    applied_facets: dict[str, list[str]] = Field(default_factory=dict)
    max_pages: Annotated[int, Field(ge=1, le=200)] = 50


class SmartRecruitersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: Annotated[str, Field(min_length=1, max_length=64)]


class WorkableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")]


class RecruiteeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class KeywordSearchConfig(BaseModel):
    """Shared by google / amazon / microsoft: one employer, huge board,
    so the query is the config."""
    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=lambda: ["IN"])
    max_results_per_query: Annotated[int, Field(ge=1, le=2000)] = 500


class MailAlertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = "job-alerts"
    senders: list[str] = Field(
        default_factory=lambda: [
            "jobalerts-noreply@linkedin.com",
            "jobs-listings@linkedin.com",
            "info@naukri.com",
            "alert@indeed.com",
            "noreply@indeed.com",
        ]
    )
    lookback_hours: Annotated[int, Field(ge=1, le=168)] = 26
```

The regex on `host` in `WorkdayConfig` is a security control, not tidiness: the
Workday adapter builds a URL from config, so config must not be able to point it
at an arbitrary host. Every adapter that interpolates config into a URL
constrains it the same way, and §4.7 covers the SSRF allow-list that backstops
this.

`UNIQUE (company_id, adapter, config)` on `source` (`DATA_MODEL.md` §3.2) means
config models must serialise canonically. `parse_config` therefore round-trips
through `model_dump(mode="json", exclude_defaults=False)` with sorted keys
before the registry writes the row, so `{"site": "netflix"}` and
`{"site":"netflix"}` collide as they should.

---

## 3. Adapter roster

| Adapter | Class | Auth | Detail fetch | Fidelity | Default poll (min) |
|---|---|---|---|---|---|
| `greenhouse` | Multi-tenant ATS | none | no | 90 | 1440 |
| `ashby` | Multi-tenant ATS | none | no | 90 | 1440 |
| `lever` | Multi-tenant ATS | none | no | 88 | 1440 |
| `workday` | Multi-tenant ATS | none | **yes** | 85 | 1440 |
| `smartrecruiters` | Multi-tenant ATS | none | **yes** | 82 | 1440 |
| `workable` | Multi-tenant ATS | none | yes (cheap) | 80 | 1440 |
| `recruitee` | Multi-tenant ATS | none | no | 78 | 1440 |
| `google` | Single employer | none | no | 75 | 720 |
| `microsoft` | Single employer | none | **yes** | 74 | 720 |
| `amazon` | Single employer | none | no | 72 | 720 |
| `mail_alert` | Derived | Gmail OAuth | no | 20 | 60 |
| `manual` | Human import | n/a | n/a | 95 | n/a |

None of the ATS endpoints require authentication. This is not incidental — it
is the selection criterion. An adapter that would need a scraped session
cookie, a reverse-engineered token or a headless browser to log in does not get
written; see §11.

---

## 4. Shared infrastructure

Everything in this section lives in `sources/base.py` and `sources/http.py`, is
constructed once per run by `ingest/runner.py`, and is injected into every
adapter. No adapter constructs an `httpx.AsyncClient` of its own — a review
failure if one does.

### 4.1 The HTTP client

```python
# sources/http.py
import httpx

TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


def build_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        limits=LIMITS,
        follow_redirects=True,
        max_redirects=5,
        http2=True,
        headers={
            "User-Agent": settings.source_user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        },
        trust_env=False,
    )
```

One `AsyncClient` for the whole run, shared by every adapter, so connection
pooling and HTTP/2 multiplexing actually happen. `trust_env=False` stops an
ambient `HTTP_PROXY` in the container from silently rerouting traffic.

`SourceHttpClient` wraps it and is what adapters actually receive:

```python
class SourceHttpClient:
    """Per-source facade over the shared httpx client.

    Adds: rate-limit lease acquisition, retry/backoff, circuit-breaker checks,
    robots enforcement, response-size capping and structured logging. Adapters
    call `get_json` / `post_json` and nothing else.
    """

    async def get_json(self, url: str, *, params: dict | None = None,
                       headers: dict | None = None) -> Any: ...

    async def post_json(self, url: str, *, json: dict,
                        headers: dict | None = None) -> Any: ...

    async def get_text(self, url: str, *, params: dict | None = None) -> str: ...
```

Concurrency is bounded twice: `settings.source_concurrency` (default 8) sources
are fetched in parallel via a semaphore in the runner, and each source's own
requests are serialised — an adapter never issues two upstream calls at once.
Workday's detail fan-out is the sole exception and is bounded separately
(§6.4). At 320 sources this comfortably fits the 15-minute run budget
(`ARCHITECTURE.md` §9).

### 4.2 Retry, backoff and jitter

```python
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
RETRYABLE_EXC = (httpx.ConnectError, httpx.ReadTimeout,
                 httpx.WriteTimeout, httpx.RemoteProtocolError)

MAX_ATTEMPTS = 4          # 1 initial + 3 retries
BASE_DELAY_S = 1.0
MAX_DELAY_S = 30.0


def backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with full jitter (AWS 'Exponential Backoff and
    Jitter'). Full jitter, not equal jitter: with 320 sources on one daily
    trigger, decorrelating retries matters more than tightening variance."""
    if retry_after is not None:
        return min(retry_after, MAX_DELAY_S)
    ceiling = min(MAX_DELAY_S, BASE_DELAY_S * 2 ** attempt)
    return random.uniform(0.0, ceiling)
```

Rules:

- **Only idempotent-in-effect requests retry.** Workday and Workable use POST
  for *search*; those are reads with a POST body and are retryable. Nothing in
  this system performs a state-changing upstream request, so there is no
  non-retryable class — but the check is written as an explicit allow-list on
  the call site rather than assumed, so it survives someone later adding one.
- **`Retry-After` always wins** over computed backoff, capped at `MAX_DELAY_S`.
  Beyond the cap the attempt is abandoned and the source is failed for this run
  rather than blocking the run budget.
- **4xx other than 408/425/429 never retry.** A 404 on a board token means the
  board moved; retrying it three times is 3× the noise and 0× the information.
- **Retries do not reset the per-source rate-limit lease.** A retry re-acquires
  a token like any other request. Backing off and then bursting is how a
  well-behaved client becomes a badly-behaved one.
- Total retry budget per source is capped at `settings.source_retry_budget_s`
  (default 90). Past it the source fails fast so one sick upstream cannot eat
  the run.

### 4.3 Rate limiting

Per-source token buckets in Redis, so limits survive a process restart mid-run
and are shared if a manual `POST /runs/discovery` overlaps the scheduled one.

```
Key:  rl:{adapter}:{bucket_key}
Type: Lua-scripted token bucket (atomic check-and-decrement)
TTL:  2 × refill period
```

`bucket_key` is the **rate-limiting domain**, not the source ID — several Adobe
Workday sites share one Workday tenant host and must share one bucket:

| Adapter | `bucket_key` | Rate | Reason |
|---|---|---|---|
| greenhouse | `boards-api.greenhouse.io` | 5 req/s, burst 10 | Shared public API across all boards |
| lever | `api.lever.co` | 5 req/s, burst 10 | Shared public API |
| ashby | `api.ashbyhq.com` | 4 req/s, burst 8 | Shared public API |
| workday | `{host}` | 1 req/s, burst 3 | Per-tenant host; detail fan-out is the load |
| smartrecruiters | `api.smartrecruiters.com` | 3 req/s, burst 6 | Shared, documented as rate-limited |
| workable | `apply.workable.com` | 3 req/s, burst 6 | Shared |
| recruitee | `{company}.recruitee.com` | 2 req/s, burst 4 | Per-tenant subdomain |
| google | `careers.google.com` | 1 req/s, burst 2 | Single employer, no need for speed |
| amazon | `www.amazon.jobs` | 1 req/s, burst 2 | Single employer |
| microsoft | `gcsservices.careers.microsoft.com` | 1 req/s, burst 2 | Single employer |
| mail_alert | `gmail` | Gmail API quota units | Not HTTP to an employer at all |

Acquisition blocks with a deadline (`settings.rate_limit_wait_s`, default 20);
past it the request raises `RateLimitTimeout`, the source is marked
`rate_limited` for the run, and the runner moves on. Waiting forever on a
crowded bucket is how a 15-minute run becomes a 50-minute one.

The bucket rates above are conservative floors chosen without reference to any
published quota, because most of these APIs publish none. Where a vendor does
document a limit, the documented limit is recorded in
`DATA_SOURCES_AND_COMPLIANCE.md` and the bucket is set to the lower of the two.
The system never probes for a limit by exceeding it.

### 4.4 Timeouts

| Phase | Budget | Note |
|---|---|---|
| Connect | 5 s | |
| Read (list page) | 20 s | Workday list pages are the slowest legitimate case |
| Read (detail) | 15 s | |
| Probe (whole call) | 10 s | Probe is user-facing; `POST /companies/detect` must feel instant |
| Whole source | 180 s | Hard ceiling incl. retries and rate-limit waits |
| Whole run | 900 s | `ARCHITECTURE.md` §9; the runner cancels stragglers |

A source cancelled by the 180-second ceiling is recorded as `status: "timeout"`
with whatever it had already yielded discarded. Partial ingestion is
specifically not allowed: a half-fetched board looks like "everything else
closed" to the two-run `closed_at` rule (`DATA_MODEL.md` §4.1) and would close
live postings.

### 4.5 User-agent policy

One identifying UA for all outbound traffic, from `settings.source_user_agent`:

```
ScoutCareers/1.0 (personal job-search agent; +mailto:<operator address>)
```

Decisions, stated:

- **The UA is honest.** It is never set to a browser string. Impersonating a
  browser to evade bot detection is the same act as scraping a source that does
  not want to be scraped, and this system does not do that (invariant 4, §11).
- **It carries a contact address** so an operator on the other end can ask us to
  stop, and we can comply, without having to block an anonymous client first.
- **It never varies per source.** A rotating UA is an evasion technique.
- The address is configuration, not a code constant, so it is not committed.

If a source returns 403 to this UA, that is a policy signal and the adapter is
retired or the source is disabled — not a prompt to change the UA. This rule is
what stops the "just spoof Chrome" pull request.

### 4.6 What never leaves and never lands in `raw`

`RawPosting.raw` is persisted to `job_posting.raw` (JSONB) and shown in the
posting inspector. Adapters trim it to a documented per-adapter field list
before it is set. Categorically excluded:

- any `Set-Cookie`, `Authorization`, CSRF or session value,
- recruiter names, personal emails and phone numbers appearing in vendor
  metadata (`smartrecruiters` `creator`, `lever` owner fields) — the system has
  no use for them and invariant 2 means it will never contact them,
- salary strings are kept (they are the employer's own public statement),
- full raw HTML beyond `description_html`, which has its own column.

Combined with `ARCHITECTURE.md` §8's logging rule, this means an adapter never
logs a response body. It logs `{source_id, adapter, url_template, status,
duration_ms, item_count}`. `url_template` is the pattern, not the interpolated
URL, so board tokens do not leak into logs either.

### 4.7 robots.txt and the never-scrape list

Two distinct mechanisms, often confused. Both apply.

**The never-scrape list (invariant 4)** is a frozen code constant in
`sources/policy.py`:

```python
NEVER_FETCH_HOSTS: frozenset[str] = frozenset({
    "linkedin.com", "www.linkedin.com", "in.linkedin.com",
    "naukri.com", "www.naukri.com",
    "indeed.com", "in.indeed.com", "www.indeed.com",
    "glassdoor.com", "www.glassdoor.co.in",
    "monsterindia.com", "shine.com", "instahyre.com",
    "angel.co", "wellfound.com",
    "facebook.com", "www.facebook.com",
})


def assert_fetch_allowed(url: str) -> None:
    host = httpx.URL(url).host.lower()
    if host in NEVER_FETCH_HOSTS or any(
        host.endswith("." + h) for h in NEVER_FETCH_HOSTS
    ):
        raise DeniedByPolicy(host)
```

`assert_fetch_allowed` is called inside `SourceHttpClient`, on every request,
after redirects are resolved — not at config time. A source that 302s to
LinkedIn is refused mid-flight. There is no setting, environment variable or
admin toggle that disables the check; `POST /companies/detect` surfaces it as
**403 `source.denied_by_policy`** (`API.md` §2) and there is no override path
(`API.md` §8). A test asserts that no code path constructs a request to a
listed host, and a second test asserts the constant is not reachable from
`Settings`.

**robots.txt (invariant 8)** applies to everything not on the deny list:

- Fetched once per host per run, cached in Redis for 24 h under
  `robots:{scheme}://{host}`, parsed with `urllib.robotparser`.
- Evaluated against our real UA and against `*`.
- A `Crawl-delay` directive, if present, **lowers** that host's bucket rate for
  the run; it never raises it.
- **Fetch failure is fail-open, but only for known JSON API hosts.** For
  `boards-api.greenhouse.io`, `api.lever.co`, `api.ashbyhq.com`,
  `api.smartrecruiters.com`, `apply.workable.com` and
  `gcsservices.careers.microsoft.com` — documented public APIs whose contract is
  the API itself — an unreachable robots.txt does not block the run. For every
  other host, an unreachable robots.txt fails the source closed for that run.
  This asymmetry is a decision made here, and the reasoning is that a
  documented public JSON API is an invitation whose absence of a robots file is
  not a refusal, while an employer careers host that will not serve robots.txt
  has told us nothing and the conservative reading wins.
- A `Disallow` covering the endpoint disables the source and reports it in the
  digest. It is never bypassed.

### 4.8 Circuit breaking

Two layers, one per timescale.

**In-run breaker (fast).** Per `bucket_key`, in memory. Five consecutive
transport-or-5xx failures within a run open the breaker for that key for the
rest of the run; every remaining source on that key is short-circuited to
`status: "circuit_open"` without a request. This stops 40 Workday sources on a
down tenant from each burning 90 seconds of retry budget.

**Cross-run breaker (durable).** `source.consecutive_failures` in Postgres
(`DATA_MODEL.md` §3.2), incremented on any failed run for that source and reset
to zero on any success. At `>= 5` the source is set `enabled = false`,
`last_status = 'auto_disabled'`, and appears in the digest's failures section
and the Settings health table.

```python
async def record_source_outcome(session, source_id: int, result: SourceResult) -> None:
    if result.status == "ok":
        # success clears history: a source that works today is not on probation
        # for a transient outage last week
        await session.execute(
            update(Source).where(Source.id == source_id).values(
                consecutive_failures=0, last_status="ok",
                last_error=None, last_run_at=result.finished_at))
        return

    src = await session.get(Source, source_id)
    failures = src.consecutive_failures + 1
    await session.execute(
        update(Source).where(Source.id == source_id).values(
            consecutive_failures=failures,
            enabled=src.enabled and failures < AUTO_DISABLE_THRESHOLD,   # 5
            last_status="auto_disabled" if failures >= AUTO_DISABLE_THRESHOLD
                        else result.status,
            last_error=truncate(result.error, 500),
            last_run_at=result.finished_at))
```

Re-enabling is a human act: `PATCH /sources/{id}` with `enabled: true`, which
also resets the counter. The system never re-enables itself on a timer, because
five consecutive daily failures almost always means the board moved and needs a
new config, not that the network was unlucky five times.

`rate_limited` and `circuit_open` outcomes **do not** increment
`consecutive_failures`. They are our own back-pressure, not the source's fault,
and counting them would auto-disable healthy boards during a busy run.

---

## 5. Multi-tenant ATS adapters

Each subsection states: endpoint, auth, pagination, identity, the real response
shape, the field mapping, and anything specific that will bite.

### 5.1 Greenhouse

**Endpoint** `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
**Auth** none. Public job board API.
**Pagination** none — one response, whole board.
**Identity** `external_id = str(job["id"])` (Greenhouse's own job board post ID,
stable). Not `internal_job_id`, which is the requisition and is shared across
several posts for one multi-location role.
**Detail fetch** not required; `content=true` embeds the full description.
**Config** `{ "board_token": "stripe" }`

```jsonc
// GET https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true
{
  "jobs": [
    {
      "absolute_url": "https://boards.greenhouse.io/stripe/jobs/6789012",
      "data_compliance": [
        { "type": "gdpr", "requires_consent": false,
          "requires_processing_consent": false,
          "requires_retention_consent": false, "retention_period": null }
      ],
      "internal_job_id": 5544332,
      "id": 6789012,
      "location": { "name": "Bengaluru, India" },
      "metadata": [
        { "id": 1234, "name": "Employment Type", "value": "Full time",
          "value_type": "single_select" }
      ],
      "requisition_id": "JR-2026-4471",
      "title": "Software Engineer, Payments Infrastructure",
      "updated_at": "2026-08-29T11:04:33-04:00",
      "content": "&lt;p&gt;Stripe builds the economic infrastructure…&lt;/p&gt;",
      "departments": [ { "id": 41, "name": "Engineering", "parent_id": null,
                         "child_ids": [] } ],
      "offices": [ { "id": 9, "name": "Bengaluru", "location": "Bengaluru, India",
                     "parent_id": 1, "child_ids": [] } ]
    }
  ],
  "meta": { "total": 214 }
}
```

**Mapping**

| `RawPosting` | Source |
|---|---|
| `external_id` | `id` |
| `url` | `absolute_url` |
| `title` | `title` |
| `description_html` | `html.unescape(content)` |
| `description_text` | `html_to_text(unescaped)` |
| `department` | `departments[0].name` (deepest non-null wins if nested) |
| `location_raw` | `location.name` |
| `location_city` / `location_country` | parsed from `location.name` (§8.2) |
| `is_remote` | `"remote" in location.name.lower()` or an office named Remote |
| `employment_type` | `metadata` entry named `Employment Type`, else `unknown` |
| `posted_at` | `updated_at` — **see caveat** |
| `raw` | `{id, internal_job_id, requisition_id, updated_at, departments, offices, metadata}` |

**Caveats.**

- `content` is **HTML-entity-escaped**. `html.unescape` before parsing, or every
  description becomes a wall of `&lt;p&gt;`. This is the single most common
  Greenhouse integration bug.
- There is **no `created_at`**. `updated_at` moves when a recruiter edits the
  post, so `posted_at` derived from it drifts later over a role's life. It is
  still the best available signal, so we use it, and `job_posting.first_seen_at`
  is what recency ranking actually keys on (`MATCH_SCORING.md`). Recorded here
  so nobody later "fixes" the mapping.
- Some boards live on `job-boards.greenhouse.io` for the human URL while the API
  stays on `boards-api.greenhouse.io`. Only the API host is ours to construct;
  the human URL comes from `absolute_url` and is never assembled.
- A board with `embed`-only publication returns 404 on this endpoint. That is a
  detection failure, surfaced by the probe, not a retry case.

### 5.2 Lever

**Endpoint** `GET https://api.lever.co/v0/postings/{site}?mode=json`
**Auth** none.
**Pagination** none by default; `?limit=` and `?skip=` exist and are used only
if `meta` indicates truncation. Most boards return everything.
**Identity** `external_id = posting["id"]` (a UUID, stable).
**Config** `{ "site": "netflix" }`

```jsonc
// GET https://api.lever.co/v0/postings/netflix?mode=json
[
  {
    "id": "6f1a2c8e-4b7d-4a11-9d2e-0f3a5b6c7d8e",
    "text": "Senior Software Engineer, Streaming Platform",
    "hostedUrl": "https://jobs.lever.co/netflix/6f1a2c8e-4b7d-4a11-9d2e-0f3a5b6c7d8e",
    "applyUrl": "https://jobs.lever.co/netflix/6f1a2c8e-…/apply",
    "createdAt": 1756288800000,
    "workplaceType": "hybrid",
    "categories": {
      "commitment": "Full-time",
      "department": "Engineering",
      "team": "Streaming Platform",
      "location": "Bengaluru, India",
      "allLocations": ["Bengaluru, India", "Remote - India"]
    },
    "description": "<p>Netflix is one of the world's leading…</p>",
    "descriptionPlain": "Netflix is one of the world's leading…",
    "lists": [
      { "text": "What you will do",
        "content": "<li>Design and operate services…</li>" },
      { "text": "What we are looking for",
        "content": "<li>7+ years building distributed systems…</li>" }
    ],
    "additional": "<p>Netflix is an equal opportunity employer…</p>",
    "additionalPlain": "Netflix is an equal opportunity employer…"
  }
]
```

**Mapping**

| `RawPosting` | Source |
|---|---|
| `external_id` | `id` |
| `url` | `hostedUrl` |
| `title` | `text` |
| `description_html` | `description` + each `lists[].text` as `<h3>` + `lists[].content` + `additional` |
| `description_text` | assembled from `descriptionPlain`, the flattened lists, `additionalPlain` |
| `department` | `categories.team` if present, else `categories.department` |
| `location_raw` | `categories.location` |
| `is_remote` | `workplaceType == "remote"` or location matches the remote pattern |
| `employment_type` | `categories.commitment` mapped via §8.4 |
| `posted_at` | `createdAt` — **epoch milliseconds**, `datetime.fromtimestamp(v / 1000, tz=UTC)` |
| `raw` | `{id, categories, workplaceType, createdAt}` |

**Caveats.**

- The response is a **bare JSON array**, not an object with a `jobs` key. Code
  that assumes an envelope breaks here and only here.
- **The requirements live in `lists`, not in `description`.** A Lever adapter
  that maps only `descriptionPlain` throws away exactly the text that stage ⑤
  extracts requirements from, and every Lever posting then scores near zero.
  This is the highest-value line in this subsection.
- `createdAt` is milliseconds. Treating it as seconds dates every posting to
  1970 and recency ranking silently inverts.
- `allLocations` may hold several cities for one posting. We keep the first as
  the canonical location, store the full list in `raw`, and do not fan one
  posting out into several — one Lever posting is one application.

### 5.3 Ashby

**Endpoint** `GET https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true`
**Auth** none for the public job board API. (Ashby's authenticated `/api/*`
endpoints exist and are not used — we have no API key and do not need one.)
**Pagination** none.
**Identity** `external_id = job["id"]` (UUID).
**Config** `{ "board_name": "openai", "include_compensation": true }`

```jsonc
// GET https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true
{
  "apiVersion": "1",
  "jobs": [
    {
      "id": "b3d9f0c1-2e44-4a6b-8c7d-9e0f1a2b3c4d",
      "title": "Member of Technical Staff, Applied AI",
      "department": "Applied",
      "team": "Applied Engineering",
      "employmentType": "FullTime",
      "location": "Bengaluru, India",
      "secondaryLocations": [
        { "location": "Remote - India", "address": { "postalAddress": {
            "addressRegion": "KA", "addressCountry": "IN" } } }
      ],
      "publishedAt": "2026-08-21T09:12:44.000Z",
      "isListed": true,
      "isRemote": false,
      "descriptionHtml": "<div><p>About the team…</p><ul><li>…</li></ul></div>",
      "descriptionPlain": "About the team…",
      "jobUrl": "https://jobs.ashbyhq.com/openai/b3d9f0c1-2e44-4a6b-8c7d-9e0f1a2b3c4d",
      "applyUrl": "https://jobs.ashbyhq.com/openai/b3d9f0c1-…/application",
      "compensation": {
        "compensationTierSummary": "₹45L – ₹70L",
        "scrapeableCompensationSalarySummary": "₹45L – ₹70L",
        "compensationTiers": [
          { "id": "tier-1", "title": "India",
            "tierSummary": "₹45L – ₹70L",
            "components": [ { "summary": "₹45L – ₹70L", "componentType": "Salary",
                              "interval": "1 YEAR", "currencyCode": "INR",
                              "minValue": 4500000, "maxValue": 7000000 } ] }
        ]
      }
    }
  ]
}
```

**Mapping**

| `RawPosting` | Source |
|---|---|
| `external_id` | `id` |
| `url` | `jobUrl` |
| `title` | `title` |
| `description_html` | `descriptionHtml` |
| `description_text` | `descriptionPlain` (already clean; §8.1 normalisation still applied) |
| `department` | `team` or `department` |
| `location_raw` | `location` |
| `is_remote` | `isRemote` or a secondary location matching the remote pattern |
| `employment_type` | `employmentType` (`FullTime` → `full_time`, §8.4) |
| `posted_at` | `publishedAt` (ISO 8601, already UTC) |
| `raw` | `{id, department, team, employmentType, isRemote, secondaryLocations, compensation}` |

**Caveats.**

- **Skip `isListed: false`.** Ashby returns unlisted postings on this endpoint;
  they are drafts or confidential searches. Yielding them puts roles in the
  queue that the employer has not published. The adapter drops them and counts
  them under `skipped_unlisted` in the source result.
- `descriptionPlain` is genuinely plain and is the best JD text of any adapter.
  Ashby earns its 90 fidelity here.
- The compensation block is the only place any adapter reliably surfaces salary.
  It is stored in `raw` and rendered on the posting page; it is never used in
  scoring, because a salary band says nothing about requirement coverage.

### 5.4 Workday

**The highest-coverage adapter in the system.** Adobe, Nvidia, Salesforce, Dell,
Cisco and HPE all run Workday, and so do a long tail of large enterprises. Every
hour spent making this adapter robust buys more coverage than any other adapter
in this document. It is also the only ATS adapter that needs a second request
per posting, which makes it the dominant cost in the run budget and the reason
the per-tenant bucket is 1 req/s.

**List endpoint** `POST https://{host}/wday/cxs/{tenant}/{site}/jobs`
**Detail endpoint** `GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}`
**Auth** none. The CXS endpoints back Workday's own public careers front end.
**Pagination** `offset` + `limit`, `limit` capped at 20 by the server.
**Identity** `external_id = jobPostingInfo.id`, falling back to the
`externalPath` trailing requisition token when the detail fetch failed.
**Config** `{ "host": "adobe.wd5.myworkdayjobs.com", "tenant": "adobe",
"site": "external_experienced" }`

```jsonc
// POST https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/jobs
// Content-Type: application/json    Accept: application/json
// body:
{ "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "" }

// 200
{
  "total": 1483,
  "jobPostings": [
    {
      "title": "Machine Learning Engineer",
      "externalPath": "/job/Bangalore/Machine-Learning-Engineer_R156789",
      "locationsText": "Bangalore, India",
      "postedOn": "Posted 3 Days Ago",
      "bulletFields": ["R156789"]
    }
  ],
  "userAuthenticated": false
}
```

```jsonc
// GET https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced
//     /job/Bangalore/Machine-Learning-Engineer_R156789
{
  "jobPostingInfo": {
    "id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
    "title": "Machine Learning Engineer",
    "jobDescription": "<p><b>Our Company</b></p><p>Changing the world through…</p><ul><li>5+ years…</li></ul>",
    "location": "Bangalore, India",
    "additionalLocations": [],
    "postedOn": "Posted 3 Days Ago",
    "startDate": "2026-09-02",
    "timeType": "Full time",
    "jobReqId": "R156789",
    "jobPostingId": "Machine-Learning-Engineer_R156789",
    "country": { "descriptor": "India", "id": "c4f78be1a8f14da0ab49ce1162348a5e" },
    "jobRequisitionLocation": {
      "descriptor": "Bangalore",
      "country": { "descriptor": "India", "alpha2Code": "IN" }
    },
    "remoteType": "On-site",
    "canApply": true,
    "posted": true,
    "includeResumeParsing": true,
    "externalUrl": "https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/Bangalore/Machine-Learning-Engineer_R156789"
  },
  "hiringOrganization": { "name": "Adobe",
    "url": "https://adobe.wd5.myworkdayjobs.com/external_experienced" }
}
```

**Fetch algorithm**

```python
async def fetch(self, *, since=None):
    offset, seen, total = 0, 0, None
    while offset < self.cfg.max_pages * PAGE:            # PAGE = 20
        page = await self.http.post_json(
            f"https://{self.cfg.host}/wday/cxs/{self.cfg.tenant}/{self.cfg.site}/jobs",
            json={"appliedFacets": self.cfg.applied_facets,
                  "limit": PAGE, "offset": offset, "searchText": ""},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        total = total if total is not None else page.get("total", 0)
        stubs = page.get("jobPostings", [])
        if not stubs:
            break

        # Detail fan-out, bounded. 3 concurrent, still inside the 1 req/s
        # tenant bucket — the bucket serialises; the gather only removes
        # round-trip latency from the critical path.
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(self._detail(s)) for s in stubs]
        for t in tasks:
            posting = t.result()
            if posting is not None:
                seen += 1
                yield posting

        offset += PAGE
        if total is not None and offset >= total:
            break
```

**Mapping**

| `RawPosting` | Source |
|---|---|
| `external_id` | `jobPostingInfo.id` |
| `url` | `jobPostingInfo.externalUrl`, else `https://{host}/{locale}/{site}{externalPath}` |
| `title` | `jobPostingInfo.title` (fall back to the stub's `title`) |
| `description_html` | `jobPostingInfo.jobDescription` |
| `description_text` | `html_to_text(jobDescription)` |
| `location_raw` | `jobPostingInfo.location`, else stub `locationsText` |
| `location_country` | `jobRequisitionLocation.country.alpha2Code`, else geocoded from text |
| `is_remote` | `remoteType` contains `remote` |
| `employment_type` | `timeType` (`Full time` → `full_time`) |
| `posted_at` | `startDate` if parseable, else `postedOn` relative phrase (§8.5) |
| `raw` | `{id, jobReqId, jobPostingId, timeType, remoteType, country, externalPath}` |

**Caveats.**

- **`limit` above 20 is silently clamped.** Sending 100 does not fail; it returns
  20 and an adapter that advances `offset` by 100 skips 80% of the board. Page
  size is a constant, never config.
- **`Accept: application/json` is mandatory.** Without it some tenants return the
  SPA HTML shell with a 200, and a JSON parse error looks like a broken adapter
  rather than a missing header.
- `postedOn` is a relative phrase — `"Posted Today"`, `"Posted 3 Days Ago"`,
  `"Posted 30+ Days Ago"`. §8.5 covers the parse and the deliberate `None` for
  `30+`.
- `total` is capped by some tenants (commonly at 1,000 or 2,000) regardless of
  the real board size. `max_pages` bounds us at 50 pages / 1,000 postings per
  site by default. A tenant needing more is split into several `source` rows
  with different `applied_facets` — which is exactly the case `source`'s
  `UNIQUE (company_id, adapter, config)` was designed for.
- `applied_facets` accepts Workday's own facet IDs, e.g.
  `{"locationCountry": ["c4f78be1a8f14da0ab49ce1162348a5e"]}` for India. Those
  IDs are tenant-specific opaque GUIDs; they are discovered once from the
  tenant's facet response and stored in config. They are never guessed.
- A detail fetch that 404s means the requisition closed between list and detail.
  The stub is dropped silently and counted under `skipped_gone`; it is not an
  error and must not touch `consecutive_failures`.
- Some tenants use `wd1`/`wd3`/`wd5`/`wd12` hosts and some front the board on a
  vanity domain that CNAMEs to Workday. The vanity case is handled by detection
  (`COMPANY_REGISTRY.md` §2), which resolves it to the real `myworkdayjobs.com`
  host before writing config — the `host` regex in `WorkdayConfig` requires it.

### 5.5 SmartRecruiters

**List** `GET https://api.smartrecruiters.com/v1/companies/{company_id}/postings?limit=100&offset=0`
**Detail** `GET https://api.smartrecruiters.com/v1/companies/{company_id}/postings/{posting_id}`
**Auth** none for the public postings API.
**Pagination** `offset` + `limit` (100 max), driven by `totalFound`.
**Identity** `external_id = posting["id"]`.
**Config** `{ "company_id": "Visa" }` — case-sensitive, as SmartRecruiters
issues it.

```jsonc
// GET https://api.smartrecruiters.com/v1/companies/Visa/postings?limit=100&offset=0
{
  "offset": 0,
  "limit": 100,
  "totalFound": 372,
  "content": [
    {
      "id": "744000012345678",
      "uuid": "0f2b1c9d-6e7a-4b3c-8d9e-1a2b3c4d5e6f",
      "name": "Senior Data Engineer",
      "refNumber": "REF12345R",
      "company": { "identifier": "Visa", "name": "Visa" },
      "location": { "city": "Bengaluru", "region": "Karnataka",
                    "country": "in", "remote": false },
      "industry": { "id": "financial_services", "label": "Financial Services" },
      "department": { "id": "tech", "label": "Technology" },
      "function": { "id": "engineering", "label": "Engineering" },
      "typeOfEmployment": { "label": "Full-time" },
      "experienceLevel": { "id": "mid_senior_level", "label": "Mid-Senior Level" },
      "customField": [],
      "ref": "https://api.smartrecruiters.com/v1/companies/Visa/postings/744000012345678",
      "releasedDate": "2026-08-25T06:41:10.000Z"
    }
  ]
}
```

```jsonc
// GET .../postings/744000012345678
{
  "id": "744000012345678",
  "name": "Senior Data Engineer",
  "applyUrl": "https://jobs.smartrecruiters.com/Visa/744000012345678",
  "postingUrl": "https://jobs.smartrecruiters.com/Visa/744000012345678",
  "jobAd": {
    "sections": {
      "companyDescription": { "title": "Company Description",
        "text": "<p>Visa is a world leader in payments…</p>" },
      "jobDescription": { "title": "Job Description",
        "text": "<p>You will…</p><ul><li>Build streaming pipelines…</li></ul>" },
      "qualifications": { "title": "Qualifications",
        "text": "<ul><li>6+ years of data engineering…</li></ul>" },
      "additionalInformation": { "title": "Additional Information",
        "text": "<p>Visa is an EEO employer…</p>" }
    }
  }
}
```

**Mapping**

| `RawPosting` | Source |
|---|---|
| `external_id` | `id` |
| `url` | detail `postingUrl`, else `https://jobs.smartrecruiters.com/{company_id}/{id}` |
| `title` | `name` |
| `description_html` | concatenated `jobAd.sections.*.text` in the order company → job → qualifications → additional |
| `description_text` | `html_to_text` of the above |
| `department` | `department.label` |
| `location_raw` | `"{city}, {region}, {country}"` compacted |
| `location_city` | `location.city` |
| `location_country` | `location.country.upper()` (it arrives lower-case) |
| `is_remote` | `location.remote` |
| `employment_type` | `typeOfEmployment.label` |
| `seniority_guess` | `experienceLevel.id` mapped, then §8.3 title inference as a check |
| `posted_at` | `releasedDate` |
| `raw` | `{id, refNumber, department, function, experienceLevel, typeOfEmployment, location}` |

**Caveats.**

- **The list response has no description at all.** Every posting costs a second
  request. At 100 postings that is 101 requests against a shared 3 req/s bucket
  — roughly 35 seconds. Acceptable, but it is why SmartRecruiters companies are
  worth tiering carefully.
- `location.country` is lower-case ISO-3166 alpha-2. The `RawPosting` validator
  upper-cases it; the adapter should not do it twice.
- `companyDescription` is boilerplate repeated on every posting for that
  employer. It is included in `description_html` for completeness but the
  extraction prompt is instructed to ignore a leading company-boilerplate
  section (`AI_ARCHITECTURE.md`); stripping it in the adapter would be a
  heuristic guess about section semantics that belongs upstream of the model,
  not inside it.
- `experienceLevel` is the only ATS field in this document that states seniority
  directly. Where present it overrides title inference.

### 5.6 Workable

**List** `POST https://apply.workable.com/api/v3/accounts/{account}/jobs`
**Detail** `GET https://apply.workable.com/api/v3/accounts/{account}/jobs/{shortcode}`
**Auth** none for a published board.
**Pagination** cursor — the response's `nextPage` token is posted back as
`{"token": "<nextPage>"}`.
**Identity** `external_id = job["shortcode"]` (Workable's stable short code, and
what the public URL uses).
**Config** `{ "account": "acme-inc" }`

```jsonc
// POST https://apply.workable.com/api/v3/accounts/acme-inc/jobs
// body:
{ "query": "", "location": [], "department": [], "worktype": [], "remote": [] }

// 200
{
  "total": 47,
  "results": [
    {
      "id": "c1d2e3f4a5b6",
      "shortcode": "A1B2C3D4E5",
      "title": "Backend Engineer (Python)",
      "description": "<p>We are looking for…</p>",
      "requirements": "<ul><li>4+ years Python…</li></ul>",
      "benefits": "<ul><li>Health cover…</li></ul>",
      "department": ["Engineering"],
      "employment_type": "Full-time",
      "experience": "mid",
      "function": "Engineering",
      "industry": "Software",
      "location": {
        "country": "India", "countryCode": "IN",
        "city": "Pune", "region": "Maharashtra",
        "workplace": "hybrid", "zip": null
      },
      "published_on": "2026-08-30",
      "created_at": "2026-08-30T04:22:19Z",
      "url": "https://apply.workable.com/acme-inc/j/A1B2C3D4E5/",
      "application_url": "https://apply.workable.com/acme-inc/j/A1B2C3D4E5/apply/"
    }
  ],
  "nextPage": "eyJvZmZzZXQiOjEwfQ=="
}
```

**Mapping**

| `RawPosting` | Source |
|---|---|
| `external_id` | `shortcode` |
| `url` | `url` |
| `title` | `title` |
| `description_html` | `description` + `requirements` + `benefits`, in that order, each under an `<h3>` |
| `description_text` | `html_to_text` of the above |
| `department` | `department[0]` |
| `location_city` / `location_country` | `location.city` / `location.countryCode` |
| `is_remote` | `location.workplace == "remote"` |
| `employment_type` | `employment_type` |
| `seniority_guess` | `experience` where present |
| `posted_at` | `created_at`, else `published_on` at 00:00 UTC |
| `raw` | `{id, shortcode, department, function, experience, location}` |

**Caveats.**

- **The requirements are a separate field.** Same failure mode as Lever §5.2:
  mapping only `description` drops the requirement text and every Workable role
  scores near zero.
- The list response usually carries the full description already. The detail
  fetch is issued **only** when `description` is absent or under 200 characters,
  which is why Workable's detail cost is marked "cheap" in §3.
- `nextPage` is an opaque base64 cursor. It is never decoded, incremented or
  reconstructed — it is echoed back verbatim, and a missing/null `nextPage`
  terminates the loop.
- A very small number of Workable accounts publish only through the v1 widget
  endpoint (`GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true`).
  The adapter falls back to it on a 404 from v3, maps the same fields, and
  records `variant: "widget_v1"` in the source result so the fallback is visible
  rather than invisible.

### 5.7 Recruitee

**Endpoint** `GET https://{company}.recruitee.com/api/offers/`
**Auth** none.
**Pagination** none.
**Identity** `external_id = str(offer["id"])`.
**Config** `{ "company": "acme" }`

```jsonc
// GET https://acme.recruitee.com/api/offers/
{
  "offers": [
    {
      "id": 1284471,
      "title": "Data Analyst",
      "slug": "data-analyst",
      "status": "published",
      "description": "<p>As a Data Analyst you will…</p>",
      "requirements": "<ul><li>3+ years SQL…</li></ul>",
      "location": "Bengaluru, India",
      "city": "Bengaluru",
      "country_code": "IN",
      "postal_code": null,
      "department": "Data",
      "employment_type_code": "fulltime",
      "min_hours": 40,
      "max_hours": 40,
      "remote": false,
      "careers_url": "https://acme.recruitee.com/o/data-analyst",
      "careers_apply_url": "https://acme.recruitee.com/o/data-analyst/c/new",
      "published_at": "2026-08-18T10:05:00.000+02:00",
      "created_at": "2026-08-17T15:40:12.000+02:00"
    }
  ]
}
```

**Mapping** is direct: `description` + `requirements` concatenated,
`country_code` straight to `location_country`, `employment_type_code` through
§8.4, `published_at` (offset-aware ISO 8601, converted to UTC) to `posted_at`,
`careers_url` to `url`.

**Caveats.**

- **Filter to `status == "published"`.** Recruitee returns internal and closed
  offers on this endpoint.
- `published_at` carries a non-UTC offset. Convert; do not truncate the string.
- Recruitee tenants are small European and Indian mid-market employers. Boards
  are typically under 50 postings, so this adapter is cheap and its 78 fidelity
  reflects only that the JD text is thinner than Ashby's, not that it is
  unreliable.

---

## 6. Single-employer career APIs

These three employers do not use a third-party ATS for their public board. Each
has a JSON endpoint that its own front end calls. Using the same endpoint the
site's own JavaScript uses, at a human-scale request rate, with an honest UA, is
the least intrusive way to read a public careers page — strictly less load than
rendering the page in a headless browser would be.

Because these boards are enormous (Amazon alone lists tens of thousands of
roles), config carries **query narrowing**. This is the one place an adapter
filters, and it is server-side filtering to keep the fetch bounded, not the
pipeline's stage-④ filter.

### 6.1 Google Careers

**Endpoint** `GET https://careers.google.com/api/v3/search/`
**Params** `page`, `page_size` (100 max), `q`, `location`, `employment_type`
**Auth** none.
**Identity** `external_id = job["id"].removeprefix("jobs/")`.
**Config** `{ "queries": ["machine learning", "product manager"],
"countries": ["IN"], "max_results_per_query": 500 }`

```jsonc
// GET https://careers.google.com/api/v3/search/?page=1&page_size=100
//     &q=machine%20learning&location=India
{
  "count": 218,
  "next_page": 2,
  "jobs": [
    {
      "id": "jobs/117482093344118982",
      "job_title": "Software Engineer III, Machine Learning, Google Cloud",
      "company_name": "Google",
      "company_id": "Google",
      "locations": [ { "display": "Bengaluru, Karnataka, India",
                       "country_code": "IN", "city": "Bengaluru" } ],
      "description": "<p>Google's software engineers develop…</p>",
      "qualifications": "<p><b>Minimum qualifications:</b></p><ul><li>Bachelor's degree…</li></ul>",
      "responsibilities": "<ul><li>Write product or system development code.</li></ul>",
      "apply_url": "https://www.google.com/about/careers/applications/jobs/results/117482093344118982",
      "publish_date": "2026-08-27",
      "job_level": "MID",
      "employment_type": "FULL_TIME"
    }
  ]
}
```

`description_html` is `description` + `responsibilities` + `qualifications`, in
that order — qualifications last so the requirement text ends the document,
which is where the extraction prompt expects the densest requirement signal.
`job_level` maps directly to `seniority_guess`. `publish_date` is a bare date;
it becomes 00:00 UTC.

**Caveat.** The response shape has changed at least once historically. The
adapter validates the page against a Pydantic response model and raises
`AdapterSchemaError` on mismatch rather than silently yielding empty postings —
a schema drift shows up as a red source in the health table, not as "Google
posted nothing today".

### 6.2 Amazon Jobs

**Endpoint** `GET https://www.amazon.jobs/search.json`
**Params** `base_query`, `offset`, `result_limit` (100 max), `sort=recent`,
`country` (ISO-3166 alpha-3, e.g. `IND`), `normalized_country_code[]`
**Auth** none.
**Identity** `external_id = job["id_icims"]`.
**Config** `{ "queries": ["machine learning", "solutions architect"],
"countries": ["IN"], "max_results_per_query": 500 }`

```jsonc
// GET https://www.amazon.jobs/search.json?base_query=machine+learning
//     &offset=0&result_limit=100&sort=recent&normalized_country_code[]=IND
{
  "error": null,
  "hits": 412,
  "jobs": [
    {
      "id_icims": "2891734",
      "title": "Machine Learning Engineer II, Alexa Shopping",
      "company_name": "Amazon Development Centre India Pvt Ltd",
      "job_path": "/en/jobs/2891734/machine-learning-engineer-ii-alexa-shopping",
      "location": "IN, KA, Bengaluru",
      "normalized_location": "Bengaluru, Karnataka, IND",
      "city": "Bengaluru", "state": "Karnataka",
      "country_code": "IN",
      "posted_date": "August 28, 2026",
      "updated_time": "8 days",
      "job_category": "Software Development",
      "job_family": "Machine Learning Science",
      "job_schedule_type": "Full Time",
      "business_category": "Alexa",
      "department_name": "Alexa Shopping",
      "description": "<p>Come build the future of shopping…</p>",
      "basic_qualifications": "<ul><li>3+ years of non-internship professional software development experience</li></ul>",
      "preferred_qualifications": "<ul><li>Experience with distributed training</li></ul>",
      "url_next_step": null
    }
  ]
}
```

`description_html` is `description` + `basic_qualifications` +
`preferred_qualifications`. That split is unusually useful: it maps almost
exactly onto `requirement_kind` `hard` versus `nice` (`DATA_MODEL.md` §2), so
the adapter emits section markers (`<h3>Basic qualifications</h3>`) that the
extraction prompt keys on, raising extraction precision for Amazon above the
baseline.

`url` is `https://www.amazon.jobs` + `job_path`. `posted_date` is a long-form
English date (`"August 28, 2026"`) and parses cleanly; `updated_time` is a
relative phrase and is ignored.

**Caveats.**

- The country parameter is **alpha-3** (`IND`), unlike everything else in this
  document. The config stores alpha-2 and the adapter converts, so config stays
  consistent across adapters.
- `hits` can exceed the reachable result set. The loop terminates on an empty
  `jobs` array or `max_results_per_query`, whichever comes first.
- Multiple `queries` will return overlapping roles. The adapter deduplicates by
  `id_icims` within a single fetch before yielding, so `ingest/` never sees the
  same `(source_id, external_id)` twice in one run.

### 6.3 Microsoft Careers

**Search** `GET https://gcsservices.careers.microsoft.com/search/api/v1/search`
**Params** `q`, `lc` (location), `l=en_us`, `pg`, `pgSz` (20 max), `o=Relevance`, `flt=true`
**Detail** `GET https://gcsservices.careers.microsoft.com/search/api/v1/job/{jobId}?lang=en_us`
**Auth** none.
**Identity** `external_id = jobId`.
**Config** `{ "queries": ["software engineer", "applied scientist"],
"countries": ["IN"], "max_results_per_query": 400 }`

```jsonc
// GET .../search/api/v1/search?q=software%20engineer&lc=India&l=en_us&pg=1&pgSz=20&o=Relevance&flt=true
{
  "operationResult": {
    "result": {
      "jobs": [
        {
          "jobId": "1812345",
          "title": "Senior Software Engineer",
          "postingDate": "2026-08-26T00:00:00+00:00",
          "properties": {
            "primaryLocation": "Hyderabad, Telangana, India",
            "locations": ["Hyderabad, Telangana, India", "Bangalore, Karnataka, India"],
            "profession": "Software Engineering",
            "discipline": "Software Engineering",
            "employmentType": "Full-Time",
            "workSiteFlexibility": "Up to 50% work from home",
            "roleType": "Individual Contributor",
            "description": "Come build community, explore your passions…"
          }
        }
      ],
      "totalJobs": 316
    },
    "status": 200
  }
}
```

```jsonc
// GET .../search/api/v1/job/1812345?lang=en_us
{
  "operationResult": {
    "result": {
      "jobId": "1812345",
      "title": "Senior Software Engineer",
      "description": "<p>Come build community…</p>",
      "qualifications": "<p><b>Required Qualifications:</b></p><ul><li>6+ years…</li></ul>",
      "responsibilities": "<ul><li>Design, build and ship…</li></ul>",
      "primaryWorkLocation": { "city": "Hyderabad", "state": "Telangana",
                               "country": "India" },
      "employmentType": "Full-Time",
      "postingDate": "2026-08-26T00:00:00+00:00"
    },
    "status": 200
  }
}
```

`url` is `https://jobs.careers.microsoft.com/global/en/job/{jobId}`.
`description_html` is `description` + `responsibilities` + `qualifications`.
`is_remote` is derived from `workSiteFlexibility` containing `100%`.

**Caveats.**

- **`pgSz` is capped at 20**, like Workday. Same trap, same rule: page size is a
  constant.
- **The list `description` is a truncated teaser.** The detail fetch is
  mandatory, which is why `requires_detail_fetch = True`.
- The `operationResult.result` envelope is two levels deep and `status` inside
  the envelope can report an application-level failure while the HTTP status is
  200. The adapter checks the inner status and raises on a non-200 — otherwise a
  Microsoft outage looks like an empty board and closes every live posting after
  two runs.

---

## 7. `mail_alerts` — how LinkedIn roles enter without LinkedIn ever being fetched

This is the most important distinction in this document and the one most likely
to be misread by a future reader, so it is stated plainly.

### 7.1 The distinction

**Scout Careers never fetches linkedin.com.** Not through an API, not through a
browser, not through a proxy, not with a cookie, not once. `linkedin.com` and
its subdomains are in `NEVER_FETCH_HOSTS` (§4.7), the check runs inside the HTTP
client on every request including post-redirect, and there is no configuration
that disables it (invariant 4; `API.md` §8).

**What the system does instead:** the operator, as a LinkedIn user, configures
job alerts in their own LinkedIn account and points them at a dedicated mailbox.
LinkedIn then sends job-alert emails to that mailbox, as a normal product
feature, at the operator's own request. The `mail_alerts` adapter reads **that
mailbox** — the operator's own inbox, over the Gmail API, with the operator's own
OAuth grant — and parses mail the operator already received.

The same applies to Naukri and Indeed alerts.

### 7.2 Why the distinction matters

It is not a technicality, and it is not a loophole. Four reasons, each
independently sufficient:

1. **Terms of service.** LinkedIn's User Agreement prohibits automated access to
   its site and the use of scrapers. It does not, and could not, prohibit a user
   from reading email that LinkedIn deliberately sent them. Reading your own
   inbox is not accessing LinkedIn's service.
2. **Technical access boundary.** Scraping requires an authenticated session
   whose credentials belong to a person, replayed by a machine. The mail path
   requires no LinkedIn credential at all — the system holds a Gmail OAuth token
   for the operator's own mailbox and nothing else. There is no session to
   replay, no rate limit to evade, no bot detection to defeat, and nothing that
   breaks when LinkedIn changes its front end.
3. **Consent and volume.** LinkedIn chose what to send and how often. The
   ingestion rate is set by LinkedIn's own alert cadence, which is by
   construction a rate LinkedIn is happy to serve. There is no way for this path
   to impose load on anyone.
4. **Durability.** A scraper is a permanent maintenance liability that breaks on
   every markup change and eventually gets the account restricted. The mail path
   has been stable for a decade because the email format is a product surface
   with its own compatibility pressure.

The design cost of this choice is real and accepted: an alert email carries a
title, a company, a location and a link — not a job description. That is why
`mail_alert` has the lowest fidelity rank in the system (20) and why a
mail-sourced posting is always superseded by the same role arriving from the
company's own ATS (§8).

### 7.3 What it actually does

**Not an HTTP adapter.** It uses `mail/` (`EMAIL_INGESTION.md`) to read messages,
so it holds no `SourceHttpClient` and touches no employer host. It satisfies the
`SourceAdapter` protocol so the runner, the run log and the health table treat it
identically to every other source.

```python
class MailAlertAdapter:
    name = AtsType.MAIL_ALERT
    config_model = MailAlertConfig
    fidelity_rank = 20
    default_poll_interval_minutes = 60
    requires_detail_fetch = False

    async def fetch(self, *, since=None):
        query = self._gmail_query()      # label + from: + newer_than:
        async for msg in self.mail.iter_messages(query):
            for card in parse_alert(msg):     # per-provider parser
                if card.apply_url_host in NEVER_FETCH_HOSTS:
                    # the link is stored, never followed
                    pass
                yield self._to_raw_posting(card, msg)
```

**Per-provider parsers.** One parser per sender family, selected by
`from_domain`, each returning `AlertCard(title, company, location, url,
posted_hint)`:

| Sender | Structure keyed on |
|---|---|
| `jobalerts-noreply@linkedin.com`, `jobs-listings@linkedin.com` | Repeated job-card table blocks; title in the anchor text, company and location in the following text nodes |
| `info@naukri.com` | Table rows with a title anchor plus experience and location cells |
| `alert@indeed.com`, `noreply@indeed.com` | Job blocks with a title anchor and a company/location line |

Parsers work on the **HTML part**, via `selectolax`, using structural selectors
only. They never execute anything, never load remote images (which would ping
the sender's tracking pixel), and treat every extracted string as untrusted text
(`ARCHITECTURE.md` §2). A parser that matches zero cards on a message that
matched the sender filter emits a WARN with the message ID and increments
`parse_misses` in the source result — that counter going non-zero is the early
warning that a provider changed its template.

**Identity.** There is no upstream ID, so one is synthesised deterministically:

```python
external_id = hashlib.sha256(
    "|".join([
        provider,                       # 'linkedin' | 'naukri' | 'indeed'
        normalise_company(card.company),
        normalise_title(card.title),
        normalise_city(card.location),
    ]).encode()
).hexdigest()[:32]
```

Deliberately **not** including the URL: alert links carry per-send tracking
parameters, so a URL-derived ID would make the same role look new in every
digest. Deliberately **not** including the date: the same role legitimately
appears in several alerts and must collapse to one posting.

**Description.** The alert has none. `description_text` is set to a structured
stub:

```
Discovered via <provider> job alert email received 2026-09-04.
Title: Staff Machine Learning Engineer
Company: Acme Corp
Location: Bengaluru, India
No job description available from this source. Open the posting to read it.
```

The stub is deliberately recognisable. `ingest/` marks mail-sourced postings
`filtered_out = false` but flags them `needs_description`, and stage ⑤ **skips
extraction** on them — running the extractor on a five-line stub would produce
confident, worthless requirements and then score against them, which is worse
than not scoring at all. Mail-sourced postings therefore surface on the Jobs page
as discovery leads with no score, and the operator's action is either to open the
link manually or, far better, to add that company to the registry so its real
board is polled and the role arrives at full fidelity.

**Company resolution.** The parsed company name is matched against `company` by
trigram similarity on `name` (`company_name_trgm_idx`, `DATA_MODEL.md` §3.1) at
a 0.45 threshold. A confident match attaches the posting to the existing
company; anything below threshold creates nothing and the posting is attached to
a reserved `unmatched` company row so it stays visible instead of being dropped.
`COMPANY_REGISTRY.md` §8 covers promoting an unmatched alert into a tracked
company in one click — which is the main way the registry is expected to grow
after seeding.

**What it never does.** It never follows the alert link. It never opens the
LinkedIn/Naukri/Indeed posting page to enrich the stub. It never replies to the
alert, and it never mails anyone (invariant 2). The alert URL is stored and
rendered as a link for the human to click.

---

## 8. Fidelity ranking

Cross-source duplicates are collapsed at ingest by
`(company_id, normalised_title, location_city)` keeping the record whose source
has the higher fidelity rank (`DATA_MODEL.md` §4.1). The ranks:

| Rank | Adapter | Why it sits here |
|---|---|---|
| 95 | `manual` | A human read it and pasted it. Highest possible confidence in the text; created only deliberately. |
| 90 | `ashby` | Clean `descriptionPlain`, explicit `isRemote`, real ISO timestamps, compensation. Nothing to reconstruct. |
| 90 | `greenhouse` | Complete JD in one request, stable IDs, structured departments and offices. Loses nothing to Ashby except the timestamp caveat. |
| 88 | `lever` | Complete once `lists` are assembled; the assembly is a reconstruction step, so a notch below. |
| 85 | `workday` | Full JD, but only after a second request, with a relative `postedOn` and tenant-specific facet quirks. |
| 82 | `smartrecruiters` | Full JD after a second request; section structure is good; company boilerplate dilutes the text. |
| 80 | `workable` | Good structure, occasionally thin descriptions, a v1 fallback path. |
| 78 | `recruitee` | Reliable but thinner JDs; small boards. |
| 75 | `google` | Excellent structure (`qualifications` split out) but a shape that has drifted before. |
| 74 | `microsoft` | Good detail, but a two-level envelope with an inner status and a mandatory detail fetch. |
| 72 | `amazon` | Excellent qualification split; heavy duplication across queries and a long-form date. |
| 20 | `mail_alert` | Title, company, location, link. No description. A lead, not a posting. |

Two ranks are equal (`ashby` and `greenhouse` at 90). Ties break on
`first_seen_at` ascending — the record we saw first wins — so collapsing is
deterministic and does not thrash when both boards list the same role.

The 50-point gap between `mail_alert` and everything else is the point of the
scale. It guarantees that the moment a mail-discovered role also appears on the
employer's own board, the full-fidelity record wins and the stub is superseded.

Rank is a `ClassVar` on the adapter, not a config value. Making it configurable
would let a per-source setting override an ordering that exists to protect data
quality.

---

## 9. Normalisation

Adapters own normalisation. `ingest/` receives DTOs that are already canonical,
which is what makes stage ② cheap and adapter tests meaningful.

### 9.1 HTML to text

```python
# sources/normalise.py
from selectolax.parser import HTMLParser

BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
DROP  = {"script", "style", "noscript", "iframe", "svg", "form", "template"}


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    tree = HTMLParser(html.unescape(raw))          # unescape FIRST — §5.1
    for node in tree.css(",".join(DROP)):
        node.decompose()
    for node in tree.css("li"):
        node.insert_before("\n• ")
    for node in tree.css(",".join(BLOCK - {"li"})):
        node.insert_before("\n")
    text = tree.text(separator="")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()
```

Rules, and the reasoning:

- **`html.unescape` runs before parsing, always.** Greenhouse double-escapes;
  applying it universally costs nothing on already-clean input.
- **List structure is preserved as `• `.** Requirements are almost always `<li>`
  elements, and flattening them into a paragraph measurably degrades stage ⑤
  extraction. Bullets are the single formatting feature worth keeping.
- **Block elements become newlines; nothing else does.** No Markdown conversion,
  no heading syntax, no tables. The extractor reads prose, not Markdown.
- `script`/`style`/`iframe`/`form` are removed before text extraction — partly
  for cleanliness, mostly because their content is attacker-controllable and
  must never reach a prompt (`SECURITY_ARCHITECTURE.md`).
- Unicode is NFKC-normalised and zero-width characters are stripped, so
  `content_hash` does not change when a recruiter pastes from a different editor.
- **Truncation.** `description_text` longer than
  `settings.max_description_chars` (default 40,000) is truncated at a paragraph
  boundary with a marker, and the truncation is recorded in `raw`. An unbounded
  JD is a token-cost hazard and, more importantly, a prompt-injection surface.

### 9.2 Location parsing

Input is free text and varies wildly: `"Bengaluru, India"`, `"IN, KA, Bengaluru"`,
`"Remote - India"`, `"San Jose, California, United States of America"`,
`"Multiple Locations"`, `"Bangalore, Karnataka, India | Hyderabad, Telangana, India"`.

Deterministic pipeline, no LLM:

1. **Split multi-location strings** on `|`, `;`, ` and `, and `/` where both
   sides parse as locations. Keep the first as canonical; the whole list goes to
   `raw.all_locations`. One posting stays one posting (§5.2 caveat).
2. **Remote detection first.** Case-insensitive match against
   `{"remote", "work from home", "wfh", "virtual", "anywhere", "distributed"}`
   sets `is_remote = True`. `"Remote - India"` is both remote and India; the
   remote marker is stripped and parsing continues on the remainder.
3. **Country resolution** against a static table of names, ISO alpha-2, alpha-3
   and common aliases (`"USA"`, `"U.S."`, `"United States of America"`, `"UK"`,
   `"Bharat"`). Country is matched from the **rightmost** token, since every
   observed format puts it last, except Amazon's `"IN, KA, Bengaluru"` which is
   detected by a leading-alpha-2 pattern and reversed first.
4. **City resolution** against a curated table of ~400 cities covering India
   comprehensively and the top 200 global tech hubs, with aliases mapping
   `Bangalore → Bengaluru`, `Bombay → Mumbai`, `Gurgaon → Gurugram`,
   `Calcutta → Kolkata`, `Trivandrum → Thiruvananthapuram`. The canonical form is
   stored so `location` filters and the cross-source dedup key are stable.
5. **Unresolvable input** leaves `location_city` and `location_country` null and
   preserves `location_raw` verbatim. `"Multiple Locations"` is exactly this
   case. It is never guessed, because a wrong city silently removes the posting
   from the operator's location filter — a false negative that is invisible.

No geocoding service is called. A network dependency to normalise a city name is
not worth the failure mode, and the curated table covers the operator's actual
search geography completely.

### 9.3 Seniority inference

`seniority_guess` is a cheap deterministic hint used by stage ④'s filter, not a
scoring input. Scoring reads requirements, not titles.

Where the source states seniority (`smartrecruiters.experienceLevel`,
`workable.experience`, `google.job_level`), that value is mapped and used
directly. Otherwise, ordered title regexes, first match wins:

| Pattern (case-insensitive, word-bounded) | Result |
|---|---|
| `intern|internship|trainee|apprentice` | `intern` |
| `\b(vp|vice president|chief|cxo|c[te]o|head of)\b` | `executive` |
| `\bdirector\b` | `director` |
| `\b(manager|mgr|lead engineering manager)\b` | `manager` |
| `\bprincipal|distinguished|fellow\b` | `principal` |
| `\bstaff\b|\bsde\s*(iii|3)\b|\bl[67]\b` | `staff` |
| `\b(senior|sr\.?|snr)\b|\bsde\s*(ii|2)\b|\bii\b` | `senior` |
| `\b(junior|jr\.?|associate|graduate|entry|campus|university)\b|\bi\b` | `entry` |
| anything else | `mid` |

Order matters and is load-bearing: `"Senior Engineering Manager"` must resolve to
`manager`, not `senior`, so manager patterns are tested before seniority
adjectives. `"Lead"` alone is not matched — it means an IC track at some
employers and a people-management track at others, and a wrong guess here feeds
the filter. Ambiguity resolves to `mid`, the permissive value, because a false
`unknown` or a wrong high band would drop a role before a human ever saw it.

### 9.4 Employment type

Case-insensitive, punctuation- and whitespace-insensitive lookup:

| Upstream values | `employment_type` |
|---|---|
| `Full time`, `Full-Time`, `FullTime`, `FULL_TIME`, `fulltime`, `Permanent`, `Regular` | `full_time` |
| `Part time`, `Part-Time`, `PartTime`, `PART_TIME`, `parttime` | `part_time` |
| `Contract`, `Contractor`, `Fixed Term`, `FixedTerm`, `Temporary Contract` | `contract` |
| `Intern`, `Internship`, `INTERN`, `Apprenticeship` | `internship` |
| `Temporary`, `Temp`, `Seasonal` | `temporary` |
| anything unmatched | `unknown` |

An unmatched value is logged once per run per distinct string, so a new vendor
vocabulary shows up as a log line rather than silently becoming `unknown`.

### 9.5 `posted_at`

Rules, in order:

1. **A real timestamp is used as-is**, converted to UTC, tz-aware. Ashby,
   SmartRecruiters, Recruitee, Workable, Microsoft.
2. **Epoch milliseconds are divided by 1000** (Lever). A sanity check rejects any
   resulting year outside 2000–2100 rather than storing a 1970 date.
3. **Bare dates become 00:00 UTC** (Google `publish_date`, Amazon `posted_date`,
   Workday `startDate`). This is a deliberate small backdating; it is uniform, so
   recency ordering is unaffected.
4. **Relative phrases are resolved against the run start**, not `now()`, so every
   posting in a run is dated on the same clock:

   | Phrase | Result |
   |---|---|
   | `Posted Today` | run start, midnight UTC |
   | `Posted Yesterday` | run start − 1 day |
   | `Posted N Days Ago` | run start − N days |
   | `Posted 30+ Days Ago` | **`None`** |

   `30+` is refused because it means "at least 30 days" — an unbounded lower
   bound. Storing it as exactly 30 would make a nine-month-old requisition look
   three weeks old to recency ranking. `None` is honest and the ranker handles it
   (`MATCH_SCORING.md`).
5. **`posted_at` is never defaulted to `now()`.** `first_seen_at` already records
   when we saw it (`DATA_MODEL.md` §4.1); fabricating a posting date would make
   every backfilled role look brand new and would poison the one recency signal
   the ranker has.

### 9.6 Title normalisation

Used for the cross-source dedup key only; `job_posting.title` stores the
original. Lower-cased; trailing location and requisition parentheticals removed
(`"(Bengaluru)"`, `"(R156789)"`, `"- Remote"`); punctuation collapsed; roman
numerals mapped to digits (`III` → `3`); a small synonym table applied
(`sde` → `software engineer`, `swe` → `software engineer`,
`ml` → `machine learning`). Aggressive normalisation is acceptable here because a
false merge across sources is cheap to spot and a missed merge shows the operator
the same role twice.

---

## 10. Failure isolation

Invariant 5: one broken source degrades that source only, and a run always
completes and always reports which sources failed.

### 10.1 The runner

```python
async def run_discovery(session, run_id: str, source_ids: list[int] | None):
    sources = await load_due_sources(session, source_ids)
    sem = asyncio.Semaphore(settings.source_concurrency)      # 8
    results: list[SourceResult] = []

    async with build_client(settings) as client:
        async def one(src: Source) -> None:
            async with sem:
                results.append(await fetch_one(client, src, run_id))

        # return_exceptions=True is the invariant, in one keyword.
        outcomes = await asyncio.gather(
            *(one(s) for s in sources), return_exceptions=True
        )
        for src, outcome in zip(sources, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # a bug in fetch_one itself — should be impossible; recorded, not raised
                results.append(SourceResult.crashed(src, outcome))
                log.exception("source_runner_crashed", source_id=src.id)

    await finalise_run(session, run_id, results)
```

`fetch_one` wraps everything in a `try/except Exception` and converts any
exception into a `SourceResult`. Two layers of catching is intentional: the
inner one classifies known failures, the outer one guarantees that an unknown
failure in the classifier itself still cannot abort the run.

`asyncio.CancelledError` is re-raised, never swallowed — the whole-run timeout
must be able to stop the run.

### 10.2 Status vocabulary

| `status` | Meaning | Increments `consecutive_failures` |
|---|---|---|
| `ok` | Completed, postings yielded (possibly zero) | No — resets to 0 |
| `empty` | Completed, zero postings, endpoint healthy | No — resets to 0 |
| `error` | Transport or 5xx after all retries | Yes |
| `http_error` | Non-retryable 4xx (404, 410, 403) | Yes |
| `schema_error` | Response parsed but did not match the model | Yes |
| `timeout` | Hit the 180 s per-source ceiling | Yes |
| `rate_limited` | Could not acquire a token within the wait deadline | **No** |
| `circuit_open` | Skipped, in-run breaker open for its bucket | **No** |
| `robots_denied` | robots.txt disallows the endpoint | Yes, and disables immediately |
| `denied_by_policy` | Never-scrape host — should be unreachable | Yes, and disables immediately |
| `disabled` | `enabled = false`; not attempted | No |

`empty` is separate from `ok` deliberately. A board that returns zero postings is
usually fine (a small company with nothing open) but occasionally means a token
changed and the vendor returns an empty array instead of a 404. Separating them
lets the health table show "empty for 7 consecutive runs" as a distinct,
actionable signal without failing the source.

### 10.3 `run_log.source_results`

One entry per attempted source, matching `DATA_MODEL.md` §9.1 and the shape
`API.md` §7 returns:

```jsonc
{
  "source_id": 77,
  "company_id": 42,
  "adapter": "workday",
  "describe": "Workday · adobe / external_experienced",
  "status": "ok",
  "fetched": 214,
  "new": 6,
  "updated": 3,
  "unchanged": 205,
  "skipped": { "unlisted": 0, "gone": 2, "no_description": 1 },
  "duration_ms": 21043,
  "requests": 12,
  "retries": 1,
  "rate_limit_wait_ms": 4100,
  "error": null,
  "error_code": null
}
```

```jsonc
{
  "source_id": 91,
  "company_id": 55,
  "adapter": "greenhouse",
  "describe": "Greenhouse · acme-corp",
  "status": "http_error",
  "fetched": 0, "new": 0, "updated": 0, "unchanged": 0,
  "skipped": {},
  "duration_ms": 890,
  "requests": 1,
  "retries": 0,
  "rate_limit_wait_ms": 0,
  "error": "HTTP 404 — board token not found",
  "error_code": "adapter.board_not_found"
}
```

`error` is a curated, human-readable, non-sensitive string, capped at 500
characters. It is never a raw upstream body and never a stack trace — it is
rendered in the digest and the Settings health table, and upstream bodies are
untrusted (`ARCHITECTURE.md` §2). The stack trace goes to structured logs, keyed
by `run_id` and `source_id`.

`error_code` is a stable machine code for the UI to branch on:
`adapter.board_not_found`, `adapter.schema_drift`, `adapter.timeout`,
`adapter.rate_limited`, `adapter.circuit_open`, `adapter.robots_denied`,
`source.denied_by_policy`, `adapter.transport`, `adapter.unknown`.

### 10.4 Run status

`run_log.status` (`DATA_MODEL.md` §2) resolves as:

- **`completed`** — every attempted source ended `ok`, `empty` or `disabled`.
- **`completed_with_errors`** — at least one failing status, but the pipeline
  reached stage ⑪. This is the normal state of a healthy 320-source system; some
  board somewhere changes most weeks.
- **`failed`** — the run could not proceed: Postgres or Redis unavailable, the
  run lock could not be held, or the runner itself raised. Adapter failures never
  produce `failed`. That is invariant 5 expressed as a state machine.

### 10.5 The two-run close rule and partial failures

`closed_at` is set on a posting not seen in two consecutive runs
(`DATA_MODEL.md` §4.1). A failed source must not count as a run in which its
postings "were not seen" — otherwise two days of a flaky board closes an
employer's entire live board.

The rule is therefore scoped per source: a posting's not-seen counter advances
only on runs where **its own source** returned `ok` or `empty`. Any other status
freezes the counter for that source's postings. This is stated here because it is
an adapter-layer consequence and is easy to implement wrongly in `ingest/`.

---

## 11. Deferred adapters: Meta and Apple

Both are deliberately not implemented. This is a decision with a stated
rationale, not a backlog item that was forgotten.

**Meta** (`metacareers.com`) serves its board from a GraphQL endpoint requiring a
`doc_id` (a server-registered persisted-query hash), an `fb_dtsg` CSRF token and
a session-derived `lsd` token, all obtained by loading and executing the page's
JavaScript. Every one of those rotates. There is no documented public JSON API.
Reaching it means running a headless browser that impersonates a signed-out user
session — precisely the class of access the UA policy (§4.5) and invariant 4 rule
out. `facebook.com` is already in `NEVER_FETCH_HOSTS`.

**Apple** (`jobs.apple.com`) has a JSON search endpoint behind a CSRF token
issued by the page and an aggressive bot-protection layer that fingerprints TLS
and behaviour. Sustained programmatic access requires defeating that layer. A
system whose stated policy is that a 403 to an honest UA is a refusal to be
respected cannot then engineer around a 403.

The engineering judgement, stated plainly:

- The cost is bounded and known. Two employers are missing from automated
  discovery. Both are covered by the `mail_alert` path if the operator sets up
  alerts, and by `POST /postings/import` (`API.md` §3) for any specific role.
- The cost of proceeding is unbounded. A browser-automation adapter for a
  hostile endpoint is permanent maintenance, adds Playwright to the runtime for
  two sources, and — decisively — requires the system to behave in a way it
  tells the operator it does not behave. An invariant that has one exception is
  not an invariant, and invariant 4's value comes entirely from being absolute.

**The condition under which this is revisited:** either employer publishing a
documented public job-board API, or migrating to a supported ATS. Neither is a
code change we can make; both are detectable by re-running
`POST /companies/detect` against their careers URL, which is why the detection
path is the right place to notice it (`COMPANY_REGISTRY.md` §10).

Until then, `meta` and `apple` do not appear in the `ats_type` enum. Adding a
value to an enum for an adapter that policy forbids would be a standing
invitation to write it.

---

## 12. Adding a new adapter

A checklist, in order. Every step has an artefact; nothing here is optional.

**1. Confirm the source is permissible.**
Read `DATA_SOURCES_AND_COMPLIANCE.md`. The endpoint must be a documented or
front-end-public JSON API reachable **without authentication, without a browser
session, and without defeating bot protection**, on a host absent from
`NEVER_FETCH_HOSTS`. Check its robots.txt and terms. If any of that fails, stop —
the answer is `mail_alert` or manual import, not a cleverer adapter. Record the
finding in `DATA_SOURCES_AND_COMPLIANCE.md` whether the answer is yes or no.

**2. Capture fixtures before writing code.**
`curl` one list page and one detail page. Redact nothing (there is nothing to
redact — these are unauthenticated public responses) except any host-identifying
token you would rather not commit. Save to
`backend/tests/fixtures/sources/{adapter}/list_page_1.json` and
`detail_{id}.json`. Fixtures are the specification; the code that follows is an
implementation of them.

**3. Add the enum value.**
`ats_type` in `DATA_MODEL.md` §2, plus a standalone Alembic revision using
`ALTER TYPE ... ADD VALUE` — it cannot share a transaction with other DDL
(`DATA_MODEL.md` §11). Mirror it in `common/types.AtsType`.

**4. Write the config model.**
In `sources/{adapter}.py`, `extra="forbid"`, with a constraining pattern on any
field interpolated into a URL (§2.5). Add a round-trip test proving canonical
serialisation, since `UNIQUE (company_id, adapter, config)` depends on it.

**5. Write a response model.**
A Pydantic model for the upstream page shape. This is what turns silent vendor
drift into `schema_error` instead of an empty board (§6.1, §6.3).

**6. Implement the adapter.**
`parse_config`, `probe`, `fetch`, `aclose`, `describe`. Set `name`,
`config_model`, `fidelity_rank`, `default_poll_interval_minutes`,
`requires_detail_fetch`. Use only `SourceHttpClient`. Never catch your own
exceptions.

**7. Justify the fidelity rank.**
Add a row to §8 with a one-line reason. A new adapter cannot be assigned 90+
without full JD text in the primary response.

**8. Add rate-limit bucket configuration.**
A row in §4.3, with the `bucket_key` chosen for the *shared resource*, not the
source ID. Start conservative; conservative is never the wrong first guess.

**9. Add detection patterns.**
The URL-pattern table in `COMPANY_REGISTRY.md` §2, plus the probe wiring, so
`POST /companies/detect` can produce a config from a pasted careers URL. An
adapter that cannot be auto-detected is an adapter nobody will use.

**10. Register it.**
Add to `ADAPTERS`. The startup completeness assertion will now pass.

**11. Write the tests.** All six are required:

| Test | Asserts |
|---|---|
| `test_parse_config_*` | Valid config passes; bad host/token rejected; canonical round-trip |
| `test_fetch_maps_fixture` | Fixture in, exact expected `RawPosting` list out, field by field |
| `test_pagination` | Multi-page fixture is fully consumed and terminates |
| `test_partial_failure` | A 500 on page 2 raises; the runner records `error`; nothing partial is yielded |
| `test_no_disallowed_host` | Every URL the adapter can construct passes `assert_fetch_allowed` |
| `test_normalisation_edges` | Empty description dropped; relative date; remote location; multi-location |

All six run offline against fixtures. No test in `sources/` may touch the
network — enforced by a `pytest` fixture that patches the transport to raise.

**12. Document it.**
A subsection in §5 or §6 of this file, in the established shape: endpoint, auth,
pagination, identity, config, real JSON, mapping table, caveats. An adapter
whose caveats are only in the code will be broken by the next person, and the
caveats are the reason this document is long.

**13. Seed a real source and run it.**
Add one real company using the new adapter, run `POST /runs/discovery` with
`source_ids` scoped to it, and read `source_results`. `status: "ok"` with a
plausible `fetched` count is the acceptance criterion. Not "it compiles".

---

## 13. Employer-to-ATS reference

A starting point for seeding the registry (`COMPANY_REGISTRY.md` §9), not a
source of truth. **ATS assignments drift** — employers migrate, and large
employers frequently run two systems at once (an ATS for experienced hire, a
separate campus site). The authority is always
`POST /companies/detect` against the employer's live careers URL, which probes
before it commits.

| Employer | Expected adapter | Config shape |
|---|---|---|
| Adobe | `workday` | `adobe.wd5.myworkdayjobs.com` / `adobe` / `external_experienced` |
| Nvidia | `workday` | `nvidia.wd5.myworkdayjobs.com` / `nvidia` / `NVIDIAExternalCareerSite` |
| Salesforce | `workday` | `salesforce.wd12.myworkdayjobs.com` / `salesforce` / `External_Career_Site` |
| Dell Technologies | `workday` | `dell.wd1.myworkdayjobs.com` / `dell` / `External` |
| Cisco | `workday` | `cisco.wd1.myworkdayjobs.com` / `cisco` / `at_cisco` |
| HPE | `workday` | `hpe.wd5.myworkdayjobs.com` / `hpe` / `Jobsathpe` |
| Workday | `workday` | Its own tenant |
| Walmart / Walmart Global Tech | `workday` | `walmart.wd5.myworkdayjobs.com` |
| Stripe | `greenhouse` | `board_token: stripe` |
| Databricks | `greenhouse` | `board_token: databricks` |
| Figma | `greenhouse` | `board_token: figma` |
| Anthropic | `greenhouse` | `board_token: anthropic` |
| Reddit | `greenhouse` | `board_token: reddit` |
| Coinbase | `greenhouse` | `board_token: coinbase` |
| Dropbox | `greenhouse` | `board_token: dropbox` |
| DoorDash | `greenhouse` | `board_token: doordash` |
| Netflix | `lever` | `site: netflix` |
| Palantir | `lever` | `site: palantir` |
| OpenAI | `ashby` | `board_name: openai` |
| Perplexity | `ashby` | `board_name: perplexity-ai` |
| Linear | `ashby` | `board_name: linear` |
| Ramp | `ashby` | `board_name: ramp` |
| Vanta | `ashby` | `board_name: vanta` |
| Visa | `smartrecruiters` | `company_id: Visa` |
| Bosch | `smartrecruiters` | `company_id: BoschGroup` |
| IKEA | `smartrecruiters` | `company_id: IKEA` |
| Ubisoft | `smartrecruiters` | `company_id: Ubisoft` |
| Google | `google` | Keyword + country narrowing |
| Amazon / AWS | `amazon` | Keyword + country narrowing |
| Microsoft | `microsoft` | Keyword + country narrowing |
| Meta | **deferred** | §11 |
| Apple | **deferred** | §11 |

Mid-market employers of roughly 50–500 people, in India and Europe, cluster on
`workable` and `recruitee`; both are best discovered by pasting the careers URL
rather than guessed from a list. Large Indian IT services and product companies
are split between `workday` and bespoke portals; the bespoke ones are handled by
`mail_alert` plus manual import until they migrate.

---

## 14. Related documents

| Document | Covers |
|---|---|
| `ARCHITECTURE.md` | Invariants, pipeline stages, module boundaries |
| `DATA_MODEL.md` | `source`, `job_posting`, `run_log` columns and indexes |
| `API.md` | `/companies/detect`, `/sources/{id}/test`, `/runs/discovery` |
| `COMPANY_REGISTRY.md` | ATS auto-detection, tiering, per-company defaults, seeding |
| `DATA_SOURCES_AND_COMPLIANCE.md` | Legal basis per source, the deny list, documented rate limits |
| `EMAIL_INGESTION.md` | Gmail access, the mailbox the `mail_alert` adapter reads |
| `MATCH_SCORING.md` | What stage ④ filters on and how recency is ranked |
| `SECURITY_ARCHITECTURE.md` | Untrusted-input handling, SSRF, secret handling |
