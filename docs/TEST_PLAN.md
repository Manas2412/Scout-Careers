# TEST PLAN — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for test strategy, the invariant test set, the
evaluation harness and the composition of the pre-merge gate. `ARCHITECTURE.md`
§3 is canonical for the invariants themselves; the acceptance-criteria tables in
`APPLICATION_PIPELINE.md` §15 and `EMAIL_INGESTION.md` §14 are canonical for
their subsystems and are reproduced here only by reference. `SOP.md` is canonical
for when the gate must run.

---

## 1. Testing philosophy

### 1.1 The highest-consequence failure is not downtime

Scout Careers has one user, one instance and no availability commitment. If it is
down for a day, the operator opens a browser and looks at job boards, which is
what they did before. The cost is an afternoon.

The failure that actually matters is different in kind:

> A generated document asserts something that is not true, the operator does not
> catch it, and it reaches an employer.

That failure is unrecoverable. It cannot be rolled back, hotfixed or apologised
away. It damages the operator's standing with a specific company permanently, it
is discovered by the person best placed to act on it, and — because the whole
point of the system is that the operator trusts the queue enough to spend ten
minutes a day on it rather than two hours — it is most likely to happen on
exactly the documents that received the least scrutiny.

Every priority in this plan follows from that ranking:

| Failure | Consequence | Test investment |
|---|---|---|
| A false claim reaches an employer | Unrecoverable reputational damage | **Maximum.** Invariant tests, ledger tests, eval gates at 1.00 |
| The system contacts a third party or submits on its own | Legal exposure, ATS standing, spam | **Maximum.** Invariant tests, static import-graph checks |
| A denied host is fetched | Terms violation, invariant breach | **Maximum.** Invariant tests at every entry point |
| A good role is missed | One lost opportunity among many | Moderate. Adapter contract tests, recall gates |
| A bad role is queued | Ten wasted seconds | Low. Precision gates, operator skip signal |
| The run is slow, or fails entirely | An afternoon | Low. Smoke test, budget assertion |

### 1.2 What follows from that

**Tests that prove a rule outrank tests that prove a feature.** A merge request
that adds an endpoint and forgets a happy-path test is incomplete. A merge
request that weakens an invariant test is rejected outright, and no amount of
"the feature needs it" changes that.

**Gates that protect invariants are set at 1.00, not at 0.95.** An invariant with
a 95% pass rate is not an invariant. Mismatch rejection, injection resistance and
evidence-span grounding are pass/fail, not thresholds.

**Absence is tested, not assumed.** The most reliable way to enforce a rule is to
give it nowhere to be called from (`API.md` §8), but "nowhere" decays under
refactoring. So the absence is asserted: no submit route in the generated schema,
exactly one call site of the mail sender, no constructible URL to a denied host.
These tests fail the day someone adds the thing, which is the only day it matters.

**Enforcement is tested at the layer that actually enforces it.** The artifact
guard is a database trigger, so its test writes to the database directly and
expects an exception — testing the service layer would prove only that the
service layer is currently well-behaved, which is the thing most likely to change.

**Determinism is a design requirement, not a testing convenience.** Nothing in
the offline suite calls a model, opens a socket or reads a clock it did not set.
A flaky invariant test gets skipped, and a skipped invariant test is an invariant
that no longer exists.

---

## 2. The pyramid, as actually applied

The classical pyramid assumes the risk lives in integration. Here it lives in
correctness of assertions and in policy enforcement, so the shape is different: a
broad, fast, offline base, a narrow integration band, almost no end-to-end
automation, and two structures the classical pyramid has no place for — the
**invariant band**, which cuts vertically through every layer, and the **eval
harness**, which is not a test suite at all but a measurement with gates.

```
                      ┌──────────────────────────┐
                      │  Manual checklist (§10)  │   what a machine cannot judge
                      ├──────────────────────────┤
                      │  Live smoke (§5.4)       │   scheduled, not in the gate
                      ├──────────────────────────┤
                      │  Integration (§8)        │   fixture Postgres + Redis
              ┌───────┼──────────────────────────┼───────┐
   INVARIANTS │       │  Contract / API (§4, §5) │       │ EVAL HARNESS
   (§4)       │       ├──────────────────────────┤       │ (§6)
   vertical,  │       │  Unit (§3)               │       │ golden set,
   pass/fail  │       │  fast, offline, pure     │       │ gated metrics
              └───────┴──────────────────────────┴───────┘
```

**Timing targets.** The offline suite (unit + invariant + contract) runs in under
90 seconds on the operator's machine. The integration suite adds under 3 minutes.
The eval costs roughly ₹35 per run and therefore runs on prompt, vocabulary,
formula and model-ID changes, not on every commit. If the offline suite ever
exceeds two minutes, that is a defect in the suite, because a suite nobody runs
before pushing is a suite that only reports failures after the fact.

---

## 3. Unit tests, by module

Offline, no network, no database, no model calls. Every module below has a
directory under `backend/tests/unit/` mirroring `backend/src/scout_careers/`.

### 3.1 `common/`

| Proves |
|---|
| ULID generation is monotonic within a millisecond and lexicographically sortable by creation time |
| `content_hash` is a stable SHA-256 of `description_text` and is whitespace-normalisation-stable — the same posting refetched with different HTML wrapping does not change the hash |
| Timezone helpers: everything stored is UTC; `Asia/Kolkata` scheduling resolves 08:00 IST correctly across a DST-free zone and across a host in any timezone |
| `Settings` rejects an unknown key, rejects a missing required secret, and never carries a default for a credential |
| The `structlog` redaction processor removes values by key name and by pattern (`AKIA…`, `Bearer …`, long base64) from nested structures, including inside lists |

### 3.2 `sources/`

Covered in depth in §5. Unit-level, additionally:

| Proves |
|---|
| Retry/backoff computes the expected delay sequence with jitter bounded, and gives up at the configured attempt count |
| Rate-limit buckets key on the shared resource, not the source ID — 40 Workday sources on one tenant share one bucket |
| The in-run circuit breaker opens after five consecutive transport-or-5xx failures on a key and short-circuits the remainder to `circuit_open` |
| `rate_limited` and `circuit_open` outcomes do **not** increment `source.consecutive_failures` |
| `record_source_outcome` auto-disables at five consecutive failures and resets the counter to zero on any success |
| robots.txt: `Crawl-delay` lowers a bucket rate and never raises it; a `Disallow` covering the endpoint disables the source; an unreachable robots.txt fails open only for the six named JSON API hosts and closed for everything else |

### 3.3 `registry/`

| Proves |
|---|
| Every URL pattern in the detection table maps to the right adapter and extracts the right config fields, over a table of real careers URLs including the awkward ones (a Workday host with a non-standard `wd` number, a Greenhouse embed on a company domain) |
| An ambiguous URL returns candidates rather than guessing |
| Company deduplication applies its signals in precedence order and stops at the first decisive one |
| A blacklisted-company collision surfaces the blacklist rather than silently creating a second row |
| CSV import phase 1 validates and reports without writing; phase 2 applies atomically |

### 3.4 `ingest/`

| Proves |
|---|
| Posting identity is `(source_id, external_id)`; a re-fetch with an unchanged `content_hash` is a no-op that only bumps `last_seen_at` |
| A changed `content_hash` updates the row and invalidates its `match_score` rows |
| Cross-source duplicates collapse on `(company_id, normalised_title, location_city)` keeping the higher-fidelity source |
| A posting unseen for two consecutive runs gets `closed_at`; unseen for one does not |
| A partial run does not close postings from sources that failed in that run |
| Deterministic filters kill the expected ~80% on a representative fixture batch, and every kill records a `filter_reason` |

### 3.5 `extract/` and `scoring/`

| Proves |
|---|
| The structured-output schema rejects a response with a missing `kind`, an out-of-range `weight`, or an `evidence_span` absent from the source text |
| The three-stage skill resolver: exact vocabulary hit, trigram hit above threshold, and no-hit → `skill_proposal` written rather than silently dropped |
| Coverage levels compute correctly for met / partial / missing, including partial credit at `SCORING_PARTIAL_CREDIT` |
| The composite formula reproduces the worked arithmetic in `MATCH_SCORING.md` §5.2 exactly, to the stored precision |
| Tier weights, the recency decay with grace period, half-life and floor, and the hard-gate bands all apply as configured |
| Exactly one `match_score` row per posting carries `is_recommended` |
| A posting with **zero** extracted hard requirements is routed to `needs_manual_review` and is **not** scored as a perfect match |
| A scoring exception for one variant skips that variant and still produces a recommendation from the rest |
| Gap lists contain every unmet hard requirement, with the note text that the letter's honest-gap paragraph consumes |

### 3.6 `ledger/`

Covered in depth in §7.

### 3.7 `generate/`

| Proves |
|---|
| The `tailoring_plan` schema accepts every legal operation and rejects an op that would change a claim's meaning, invent a bullet, or edit a section the layer may not touch |
| `apply_plan` is deterministic and idempotent: applying the same plan twice yields byte-identical builder input |
| `apply_plan` ignores an op type the current flag set disables, without orphaning the rest of the plan |
| Every proposed bullet carries at least one claim ID; a plan with an empty `claim_ids` fails schema validation |
| The fit loop: `tight` mode then content trimming, and page count is **verified by rendering**, never assumed |
| The letter omits everything in `DOCUMENT_GENERATION.md` §6.5's never-list |
| The similarity check warns at `SIMILARITY_WARN` and blocks at `SIMILARITY_BLOCK` against a fixture corpus of previous letters |
| A company with `cover_letter_worth = false` produces no letter and no letter tokens are spent |

### 3.8 `review/` and `tracking/`

| Proves |
|---|
| The review state machine permits `pending_review → approved/skipped/needs_manual_review` and nothing else; a second decision returns the already-decided conflict |
| `may_append` rejects `rejected → acknowledged`, `interview → acknowledged`, `screening → screening`; accepts `interview → rejected`, `acknowledged → interview` |
| The same event twice — same `(application_id, status, email_message_id)` — creates one row |
| Events inserted out of chronological order yield the same `application.status` as in-order insertion |
| `ghosted` is absent from the `application_status` enum and assigned by no code path (a grep-level test) |
| An event on day 45 removes the application from `v_ghosted` with no compensating write |
| A rate with `n < MIN_N_FOR_RATE` returns null with the count present, never a computed percentage |
| Funnel grouping by variant defaults to `source_channel=direct` and returns `meta.n`, `meta.ci_low`, `meta.ci_high` on every row |
| The workbook export opens cleanly with zero applications, zero companies and zero runs |

### 3.9 `mail/`

| Proves |
|---|
| `unwrap()` performs no network I/O (asserted with a socket-blocking fixture) and returns the canonical URL and `external_id` for fixtures from all three alert platforms |
| The resolution chain links a message to an application in precedence order and leaves a genuinely ambiguous shared-ATS-domain message unresolved rather than guessing |
| A classification whose `excerpt` is not a verbatim substring of the body is rejected and the message is held |
| A body containing injection text yields confidence ≤ 0.50 and writes no event |
| The digest composes correctly from an empty run, a failed run, and a run with every section populated |

### 3.10 `llm/`

| Proves |
|---|
| The structured-output enforcement loop repairs a malformed response within the budget and hard-fails after it, rather than coercing |
| A repair retry logs Pydantic error **paths and messages** and never the offending payload |
| The prompt guard fences untrusted content with a nonce and the model output cannot terminate the fence |
| The extraction cache keys on `content_hash` + prompt version, and a prompt-version bump misses the cache |
| The daily budget circuit breaker trips at `LLM_DAILY_BUDGET_INR` and caps generation at `LLM_BUDGET_WARN_PCT` |
| Provider swap produces identical structured output shapes from both `bedrock` and `azure_openai` stubs |

---

## 4. The invariant tests

**These are the most important tests in the repository.** They live in
`backend/tests/invariants/`, they run first in the gate, and a failure among them
stops the run before anything else executes — there is no value in knowing
whether the funnel view is correct on a build that can email a stranger.

They differ from the rest of the suite in three ways. They assert **absence** as
often as behaviour. They assert at the **layer that enforces**, which is
sometimes the database and sometimes the import graph rather than a function.
And they are **not modifiable in a feature merge request**: changing one requires
its own spec and an operator decision recorded in `ARCHITECTURE.md` §3
(`SOP.md` §12).

### 4.1 Invariant 1 — no submit endpoint exists anywhere

The strongest available statement is about the generated schema, because the
schema is what the system actually exposes, independent of what any router file
appears to say.

```python
# backend/tests/invariants/test_no_submission_path.py
import re
import pytest
from scout_careers.api.app import create_app

FORBIDDEN_PATH_TOKENS = re.compile(
    r"submit|apply|application[s]?/send|autoapply|auto_apply", re.I)

# Routes whose name contains a forbidden token but which are legitimate.
ALLOWED_EXACT = {
    "/api/v1/applications",
    "/api/v1/applications/{id}",
    "/api/v1/applications/{id}/events",
}


def test_no_submit_route_in_openapi_schema():
    schema = create_app().openapi()
    offenders = [
        p for p in schema["paths"]
        if FORBIDDEN_PATH_TOKENS.search(p) and p not in ALLOWED_EXACT
    ]
    assert offenders == [], f"submission-shaped route(s) exposed: {offenders}"


def test_no_operation_describes_submission():
    schema = create_app().openapi()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            blob = " ".join(filter(None, [op.get("summary"), op.get("description")]))
            assert "submits the application" not in blob.lower(), (path, method)


@pytest.mark.parametrize("route", [
    "/api/v1/review/{id}/submit",
    "/api/v1/applications/{id}/submit",
    "/api/v1/postings/{id}/apply",
])
def test_named_submission_routes_are_404(client, route):
    assert client.post(route.replace("{id}", "01JB0000000000000000000000")).status_code == 404
```

A second test closes the non-HTTP route: **no module outside `sources/` and
`mail/` may construct an outbound request at all.** Playwright is importable only
from `sources/`, and a static check asserts that no module under `api/`,
`review/`, `generate/` or `tracking/` imports `httpx`, `requests`, `aiohttp` or
`playwright`.

```python
def test_no_outbound_client_in_decision_modules():
    forbidden = {"httpx", "requests", "aiohttp", "urllib.request", "playwright"}
    for module in walk_modules("scout_careers.api", "scout_careers.review",
                               "scout_careers.generate", "scout_careers.tracking"):
        assert not (imports_of(module) & forbidden), module
```

Finally, `POST /review/{id}/approve` is asserted to create exactly one
`application` row and issue **zero** outbound HTTP requests, under a transport
fixture that raises on any socket use (`APPLICATION_PIPELINE.md` §15, criteria 1
and 2).

### 4.2 Invariant 2 — no code path sends mail to a non-operator address

Three tests, because `EMAIL_INGESTION.md` §1.1 enforces in three layers and each
layer needs its own proof.

```python
# backend/tests/invariants/test_outbound_policy.py
import inspect
import pytest
from scout_careers.mail.client import GmailClient, OutboundPolicyViolation
from scout_careers.mail import digest


@pytest.mark.parametrize("field,value", [
    ("to",  "recruiter@example.com"),
    ("cc",  "hiring.manager@example.com"),
    ("bcc", "someone@example.com"),
    ("to",  "Operator <operator@example.com>, recruiter@example.com"),
])
def test_send_rejects_foreign_recipient(gmail_client, field, value):
    with pytest.raises(OutboundPolicyViolation):
        gmail_client._send_raw(**{field: value}, subject="x", body="y")


def test_send_signature_takes_no_recipient():
    params = inspect.signature(GmailClient.send).parameters
    assert not ({"to", "recipient", "cc", "bcc"} & set(params)), (
        "the send API must not accept a recipient; it is resolved once at startup")


def test_exactly_one_call_site_of_send():
    sites = find_call_sites("scout_careers", attr="send", of_type=GmailClient)
    assert sites == [("scout_careers.mail.digest", "send_digest")], sites
```

The third is a static import-graph check over the whole package. It fails the
build the moment a second call site appears — including one added by an agent
that believed a follow-up email was helpful.

A fourth test asserts the OAuth scope constant is exactly
`{gmail.readonly, gmail.send}` and that `gmail.modify` and `gmail.compose` do not
appear anywhere in the codebase.

### 4.3 Invariant 4 — a denied host is refused at every entry point

The deny list is enforced inside the HTTP client after redirect resolution, so
the tests attack it from every direction a URL can enter the system.

```python
# backend/tests/invariants/test_never_scrape.py
import pytest
from scout_careers.sources.policy import (
    NEVER_FETCH_HOSTS, assert_fetch_allowed, DeniedByPolicy)

DENIED = [
    "https://www.linkedin.com/jobs/view/123",
    "https://in.linkedin.com/jobs/view/123",
    "https://careers.linkedin.com/x",          # subdomain of a listed host
    "https://LINKEDIN.COM/jobs",               # case
    "https://www.naukri.com/job-listings-x",
    "https://in.indeed.com/viewjob?jk=abc",
    "https://wellfound.com/jobs/1",
]


@pytest.mark.parametrize("url", DENIED)
def test_direct_client_refuses(url):
    with pytest.raises(DeniedByPolicy):
        assert_fetch_allowed(url)


@pytest.mark.parametrize("url", DENIED)
async def test_detect_endpoint_refuses(client, url):
    r = await client.post("/api/v1/companies/detect", json={"url": url})
    assert r.status_code == 403
    assert r.json()["meta"]["code"] == "source.denied_by_policy"


@pytest.mark.parametrize("url", DENIED)
async def test_manual_company_creation_refuses(client, url):
    r = await client.post("/api/v1/companies",
                          json={"name": "X", "careers_url": url})
    assert r.status_code == 403
    assert r.json()["meta"]["code"] == "source.denied_by_policy"


@pytest.mark.parametrize("url", DENIED)
async def test_manual_posting_import_refuses(client, url):
    r = await client.post("/api/v1/postings/import", json={"url": url})
    assert r.status_code == 403


async def test_redirect_into_denied_host_is_refused_midflight(respx_mock):
    respx_mock.get("https://jobs.example.com/x").respond(
        302, headers={"Location": "https://www.linkedin.com/jobs/view/9"})
    with pytest.raises(DeniedByPolicy):
        await source_http_client().get("https://jobs.example.com/x")


def test_deny_list_is_not_configuration():
    from scout_careers.common.config import Settings
    fields = set(Settings.model_fields)
    assert not any("never_fetch" in f or "deny" in f for f in fields)
    with pytest.raises(AttributeError):
        NEVER_FETCH_HOSTS.add("example.com")     # frozenset, not a set
```

A final test walks every adapter's constructible URL space — every config
permitted by its Pydantic model, over its fixture set — and asserts each URL
passes `assert_fetch_allowed`. This is the check that catches a new adapter
pointed at a denied host before it is ever run.

### 4.4 Invariant 3 — a failed artifact cannot be attached

Proven **against the database trigger**, not the service layer, because the
service layer is the layer most likely to be rewritten (`CLAIMS_LEDGER.md` §6.1).
These tests run in the integration band because they need a real Postgres, but
they belong to the invariant set and are reported as such.

```python
# backend/tests/invariants/test_artifact_guard.py
import pytest
from sqlalchemy.exc import IntegrityError, DBAPIError


async def test_trigger_blocks_attaching_failed_artifact_to_review_item(db):
    artifact_id = await insert_artifact(db, validation_status="failed")
    review_id   = await insert_review_item(db)
    with pytest.raises(DBAPIError) as exc:
        # Raw SQL: this is deliberately NOT going through the service layer.
        await db.execute(
            text("UPDATE review_item SET resume_artifact_id = :a WHERE id = :r"),
            {"a": artifact_id, "r": review_id})
    assert "failed ledger validation" in str(exc.value)


async def test_trigger_blocks_attaching_failed_artifact_to_application(db):
    artifact_id = await insert_artifact(db, validation_status="failed")
    with pytest.raises(DBAPIError):
        await db.execute(
            text("INSERT INTO application (id, posting_id, variant_id, "
                 "submitted_at, cover_letter_artifact_id) "
                 "VALUES (:i, :p, :v, now(), :a)"),
            {...})


async def test_trigger_blocks_flipping_an_attached_artifact_to_failed(db):
    artifact_id = await insert_artifact(db, validation_status="passed")
    await attach_to_review_item(db, artifact_id)
    with pytest.raises(DBAPIError) as exc:
        await db.execute(
            text("UPDATE artifact SET validation_status = 'failed' WHERE id = :a"),
            {"a": artifact_id})
    assert "detach before marking it failed" in str(exc.value)


async def test_bypassed_artifacts_may_attach(db):
    """`bypassed` means hand-authored, not overridden. It is allowed."""
    artifact_id = await insert_artifact(db, validation_status="bypassed")
    await attach_to_review_item(db, artifact_id)   # must not raise


async def test_generation_can_never_produce_bypassed(client, monkeypatch):
    """`bypassed` is settable only by the seed and import paths."""
    item = await seed_review_item(client)
    r = await client.post(f"/api/v1/review/{item}/generate")
    artifacts = (await client.get(f"/api/v1/review/{item}")).json()["data"]["artifacts"]
    assert all(a["validation_status"] in {"passed", "failed"}
               for a in artifacts.values())


def test_no_validation_override_route():
    paths = create_app().openapi()["paths"]
    assert not [p for p in paths if "override" in p or "force" in p]
```

The last one pairs the trigger with `API.md` §8: the trigger makes the bad state
unreachable from the database, the absent endpoint makes it unreachable from the
API, and neither is sufficient alone.

### 4.5 Invariant 5 — a run completes when an adapter throws

```python
# backend/tests/invariants/test_failure_isolation.py
import pytest


@pytest.mark.parametrize("failure", [
    ConnectionError("upstream reset"),
    TimeoutError(),
    ValueError("schema drift: 'jobs' key absent"),
    MemoryError(),          # deliberately not a "reasonable" exception
    KeyboardInterrupt,      # asserted to propagate, not be swallowed
])
async def test_one_adapter_failure_does_not_stop_the_run(runner, failure):
    sources = seed_sources(count=5)
    patch_adapter(sources[2], raises=failure)

    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            await runner.run(sources)
        return

    run = await runner.run(sources)

    assert run.status == "completed_with_errors"
    assert run.finished_at is not None
    assert len(run.source_results) == 5
    assert [r["status"] for r in run.source_results].count("error") == 1
    assert sum(r["fetched"] for r in run.source_results
               if r["status"] == "ok") > 0
    failed = next(r for r in run.source_results if r["status"] == "error")
    assert failed["error"]                              # reported, not hidden
    assert "token" not in failed["error"].lower()       # and not leaking secrets


async def test_every_adapter_failing_still_completes_the_run(runner):
    run = await runner.run(seed_sources(count=5), all_failing=True)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert len(run.source_results) == 5


async def test_failed_sources_do_not_close_their_postings(runner, db):
    """A source that failed twice must not be read as 'the roles are gone'."""
    ...
```

`KeyboardInterrupt` is in the table deliberately: failure isolation must catch
`Exception`, not `BaseException`, or a Ctrl-C during a run becomes an
un-interruptible process that reports every source as failed.

### 4.6 Invariants 6, 7 and 8

| Invariant | Test |
|---|---|
| 6 — no secrets in code or logs | A canary secret is placed in every settings field, a full pipeline runs against stubs, and the captured log stream, the database dump and the artifact directory are searched for the canary. Zero hits required. A second test asserts the redaction processor is installed unconditionally, not behind a flag |
| 7 — every artifact is reproducible | Every `artifact` row written by any code path has non-null `model`, `prompt_version` and `variant_id`, and every resolved assertion has a `claim_usage` row. Asserted after a full stubbed pipeline run, by querying, not by inspecting the writer |
| 8 — robots and rate limits respected | Every adapter declares `default_poll_interval_minutes` and a `bucket_key`; a startup completeness assertion fails if one does not. A robots fixture with a `Disallow` disables the source and the run reports it |

---

## 5. Adapter contract tests

Adapters are the system's contact surface with a dozen third parties who change
their JSON without telling anyone. The strategy separates two questions that are
usually confused: *does my code map this payload correctly* (offline, in the
gate, always) and *is this still the payload* (live, scheduled, never in the
gate).

### 5.1 Fixtures are the specification

Per `SOURCE_ADAPTERS.md` §12, fixtures are captured **before** the adapter is
written:

```
backend/tests/fixtures/sources/{adapter}/
├── list_page_1.json          a real, unauthenticated public response
├── list_page_2.json          pagination continuation
├── list_page_last.json       terminal page
├── detail_{id}.json          where requires_detail_fetch is true
├── empty_board.json          a real board with zero open roles
├── malformed.json            a captured drift or a hand-crafted near-miss
└── expected.py               the exact RawPosting list the adapter must yield
```

`expected.py` holds the assertion as data, field by field, so a mapping
regression names the field it broke rather than saying "lists differ".

### 5.2 The six required tests

Every adapter has all six (`SOURCE_ADAPTERS.md` §12, step 11). A new adapter
without all six does not merge, and a parametrised meta-test asserts that each
registered adapter has each of the six named tests present.

| Test | Asserts |
|---|---|
| `test_parse_config_*` | Valid config passes; a bad host or token is rejected **at parse time**; canonical serialisation round-trips, because `UNIQUE (company_id, adapter, config)` depends on it |
| `test_fetch_maps_fixture` | Fixture in, exact expected `RawPosting` list out, field by field |
| `test_pagination` | A multi-page fixture is fully consumed and terminates; a cursor loop cannot run forever |
| `test_partial_failure` | A 500 on page 2 raises; the runner records `error`; **nothing partial is yielded** |
| `test_no_disallowed_host` | Every URL the adapter can construct passes `assert_fetch_allowed` |
| `test_normalisation_edges` | Empty description dropped; relative date parsed; remote location; multi-location; a title with the company name embedded |

### 5.3 The network is structurally unreachable

```python
# backend/tests/sources/conftest.py
import socket
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test under tests/sources/ may open a socket. Not mocked — blocked."""
    def deny(*args, **kwargs):
        raise RuntimeError(
            "network access in an offline test; use a fixture")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
```

This is stronger than mocking the HTTP client: it catches an adapter that
reaches for `urllib` directly, or a library that phones home during import.

### 5.4 The live smoke test detects endpoint drift

Fixtures rot. An adapter that maps a two-year-old payload perfectly is worthless
if the vendor renamed a field last Tuesday, and the failure mode is silent — an
empty board looks exactly like a company with no open roles.

A scheduled job, **outside the gate**, probes one real source per adapter:

```yaml
# ci/smoke-adapters.yml — runs 06:00 IST daily, never on a merge request
name: adapter-drift-smoke
schedule: "0 30 0 * * *"        # 06:00 IST
timeout_minutes: 10
steps:
  - probe:
      one_source_per_adapter: true
      assert:
        reachable: true
        sample_count_min: 1
        response_validates_against: adapter.response_model
        fields_present: [external_id, title, url, description_text]
      compare_to_fixture:
        mode: shape          # key set and types, not values
        on_difference: fail
  - on_failure:
      write: run_log
      notify: digest         # appears in the next morning's digest
      never: auto_disable    # a smoke failure is information, not a decision
```

Three properties make this worth having. It **validates against the response
model**, so a renamed field is a `schema_error` rather than an empty list. It
**compares shape to the committed fixture**, so drift is reported as a diff a
human can act on. And it **never changes system state** — it does not disable a
source, because "the vendor changed something" and "this source is dead" are
different conclusions and only a human should draw the second.

When a smoke test fails, the repair is: re-capture the fixture, update
`expected.py`, fix the mapping, and note the drift in the adapter's subsection of
`SOURCE_ADAPTERS.md` in the same merge request (`SOP.md` §12).

---

## 6. The LLM evaluation harness

The eval is not a test suite. It is a **measurement with gates**, run against a
hand-labelled corpus, and its output is a metric table with a pass/fail verdict.
It answers a question tests cannot: *is the model still doing the job well*, as
opposed to *is the code still doing what it did yesterday*.

**Authority:** `AI_ARCHITECTURE.md` §10 for the golden set composition, the
metric definitions and the gates; `MATCH_SCORING.md` §12 for the scorer-specific
tiers. This section describes how it runs and what it blocks.

### 6.1 The golden set

Forty job descriptions in `backend/tests/eval/golden/`, hand-labelled by the
operator, deliberately skewed toward hard cases:

| Slice | Count | Why it is in the set |
|---|---|---|
| Clear AI/ML product roles | 6 | The base case must not regress |
| Backend / distributed systems | 6 | Base case |
| Consulting / analyst / finance-adjacent | 6 | The `consulting` variant is the least exercised |
| Deliberate mismatches (firmware, hardware, sales) | 5 | Must score low and recommend nothing |
| Straddle roles (two variants plausible) | 5 | Where `combined` should win, and often does not |
| Verbose JDs with heavy boilerplate | 4 | Extraction precision under noise |
| Terse JDs (< 120 words) | 4 | Extraction recall with little to work with |
| Adversarial (injected instructions) | 4 | Injection resistance |

Each case carries the full JD text, the hand-labelled requirement list with kinds
and normalised skills, the expected recommended variant, and — for the
adversarial slice — the assertion that the injection did not affect the output.

```yaml
# backend/tests/eval/golden/031-seagate-analyst-ii.yaml
id: 031-seagate-analyst-ii
slice: consulting
source: { company: "Seagate Technology", captured_at: "2026-08-13" }
jd_text_file: 031-seagate-analyst-ii.txt
labels:
  requirements:
    - { text: "Advanced Excel model building", kind: hard,
        normalised_skill: excel, weight: 1.00 }
    - { text: "Power BI or similar visualisation", kind: hard,
        normalised_skill: power_bi, weight: 0.90 }
    - { text: "Automating reports and reconciliations", kind: responsibility,
        normalised_skill: automation, weight: 0.80 }
    - { text: "SAP / Anaplan / Hyperion exposure", kind: nice,
        normalised_skill: erp_planning, weight: 0.50 }
  expected_recommended_variant: consulting
  expected_top_2: [consulting, combined]
  expected_gaps_include: [excel, power_bi]
  expected_generation: allowed        # composite clears GENERATION_MIN_COMPOSITE
```

**Labelling is done once, by the operator, and revised only with a recorded
reason.** A golden set quietly adjusted to match current behaviour measures
nothing; it is a mirror with a test runner attached.

### 6.2 Expected variant recommendations, and why they are labelled

The recommendation label is the one an operator would defend in conversation, not
the one the current formula produces. The distinction is the whole value of the
set: the straddle slice exists because `combined` *should* often win and
frequently does not, and the only way to see that is to have written down what
right looks like before measuring.

The mismatch slice carries `expected_recommended_variant: none` and
`expected_generation: blocked`. Those five cases are the ones that catch a
formula change that inflates coverage — a system that recommends a firmware role
to a variant with no firmware evidence has failed in the direction that produces
an embarrassing application, not merely a wasted one.

### 6.3 The metrics and their gates

| Metric | Definition | Gate |
|---|---|---|
| Extraction precision | Extracted requirements matching a labelled one (fuzzy on text, exact on `kind`) | ≥ 0.85 |
| Extraction recall | Labelled requirements found | ≥ 0.80 |
| Hard-requirement recall | Recall restricted to `kind = hard` | ≥ 0.92 |
| `normalised_skill` accuracy | Correct vocabulary assignment | ≥ 0.90 |
| Recommendation agreement | Top-1 recommended variant matches the label | ≥ 0.80 |
| Recommendation top-2 | Labelled variant in the top 2 | ≥ 0.95 |
| Mismatch rejection | Deliberate mismatches scoring below the generation threshold | **1.00** |
| Validation pass rate | Generated artifacts passing the ledger gate first time | ≥ 0.90 |
| Schema enforcement rate | Structured calls succeeding within the repair budget | ≥ 0.98 |
| Injection resistance | Adversarial cases with no output deviation | **1.00** |
| Evidence-span grounding | `evidence_span` found verbatim in the source text | **1.00** (zero violations) |
| Coverage-level agreement | Cohen's κ against the operator's met/partial/missing labels | ≥ 0.70 |
| Variant-ranking correlation | Spearman ρ between scorer order and operator order, per JD | ≥ 0.75 |
| Cost per posting | Mean tokens × price | ≤ ₹0.80 |

**Hard-requirement recall is gated hardest** because a missed hard requirement
inflates coverage, which promotes a bad match into a queue the operator trusts.
**Mismatch rejection, injection resistance and grounding are gated at 1.00**
because they are invariant-adjacent, and an invariant with a 95% pass rate is not
an invariant. A fabricated evidence span is the same class of failure as an
uncited claim in a generated document.

### 6.4 The rule

**A prompt change ships only if the eval holds.**

```bash
cd backend && python -m scout_careers.eval run \
    --family requirement_extraction \
    --prompt-version 2026-09-14.1 \
    --baseline      2026-09-01.4
```

The runner executes both versions over the golden set, prints a per-metric delta
table, and exits non-zero if:

1. any absolute gate fails, **or**
2. any metric regresses by more than **2 points** against the baseline, even
   while still passing.

The second condition is the one that earns its keep. A change that trades four
points of recall for one point of precision clears every absolute gate and is
still a bad change; without the regression check it would ship, and the loss
would only surface as a slow decline in queue quality that nobody could attribute.

The eval costs roughly **₹35 per run**, so it runs on prompt changes, vocabulary
changes, scoring-formula changes and model-ID changes — not on every commit. The
gate enforces this by requiring an eval artifact whenever the diff touches
`llm/prompts/`, `extract/`, `scoring/` or a pinned model setting.

Generation families — tailoring plan and cover letter — are additionally
**reviewed by hand on ten cases**, because "is this letter good" is not a metric,
and pretending otherwise would be the most expensive mistake in this plan.

Eval results are committed alongside the prompt version. `PROMPTS.lock` records,
per version: the content hash, the eval run ID, the metric table and the date. A
prompt in production always has a recorded eval behind it.

---

## 7. Claims ledger tests

The ledger is the control that stands between the model and a false statement
reaching an employer. Its tests are second in priority only to the invariant set,
and structurally they are the invariant set's evidence base — invariant 3 is only
as strong as the validation pass it depends on.

### 7.1 Citation resolution

The resolution chain (`CLAIMS_LEDGER.md` §5.4) has six steps with different
semantics, and each needs its own proof — a chain tested only end to end will
pass while resolving everything by the permissive last step.

| Case | Expected |
|---|---|
| Exact `(value, unit)` match against a cited claim | resolved, step 2 |
| `~60%` against a claim of 60 | resolved, step 3, note records the tolerance |
| `62%` against a claim of 60 | **unresolved** — tolerance is granted only to spans the prose marked approximate (`~`, `about`, `roughly`, `approximately`, `over`, `nearly`) |
| `about 62%` against a claim of 60 | resolved — 3.3% is inside the ±5% band. A companion case at `about 64%` (6.7%) is unresolved, so the test pins the boundary from both sides rather than only from the safe one |
| `₹9.4L → ₹3.5L` against a claim encoding the range | resolved, step 4, once — not three times as two currencies plus a range |
| A subordinate figure inside a cited claim's canonical statement | resolved, step 5 |
| A true figure present in the ledger but not cited | resolved by step 6, **and the missing citation is written** so `claim_usage` stays complete, **and** the omission is logged |
| A true figure in the ledger under a different project, spliced onto this achievement | **unresolved** — the composition is false even though both halves are true |
| A superlative with no citation | **unresolved**. Superlatives never resolve by search |
| A prose fact with a citation whose statement does not support it | **unresolved** |
| A resolved claim outside `permitted_tiers` | **unresolved**, with the restricted-claim note |

The splice case is the one worth naming: `CLAIMS_LEDGER.md` §5.5's worked example
has a true 60% cost reduction from one project joined to a true "14 centres" from
another. Both halves verify; the sentence is false. Resolution scoped to cited
claims catches exactly this, which is the most likely way an LLM produces a
falsehood out of entirely true inputs. It gets its own named test, not a row in a
parametrised table.

### 7.2 Expiry

| Case | Expected |
|---|---|
| A claim with `expires_at` in the future | resolves normally |
| A claim with `expires_at` in the past, cited | `resolved=True, stale=True`; generation blocks; the item is flagged |
| An expired claim as the only evidence for a requirement | coverage degrades `met` → `partial` |
| A claim expiring **between** scoring and generation | generation blocks; the stale citation is named in the failure notes |
| A soft-deleted claim (`deleted_at` set) | never resolves, at any step, including step 6 |

### 7.3 Confidentiality gating

| Case | Expected |
|---|---|
| `public` claim, any employer | permitted |
| `internal` claim, employer not on the allow-list | permitted (default tier) |
| `restricted` claim, employer **on** the disclosure allow-list | permitted |
| `restricted` claim, employer **not** on the allow-list | assertion fails with the restricted note; the artifact fails validation |
| A paired claim where the restricted member is gated | the public member is substituted and the document still generates |
| Allow-list membership is per-application, not per-file | two applications to different employers from the same variant produce different permitted sets |

The paired-claim substitution test matters more than it looks: it is what makes
gating painless rather than a source of empty documents, and if it regresses the
symptom is a validation-failure rate that climbs for no visible reason.

### 7.4 The validation pass over tricky assertion strings

A single parametrised table, run against the full pass (regex families ∪ LLM
assertion extraction, with the model stubbed deterministically per §11). This
table is the highest-value dozen lines in the repository: every row is a real way
a document can lie.

```python
# backend/tests/ledger/test_validation_table.py
import pytest

# (draft text, cited claim keys, expect_passed, note)
CASES = [
    # --- must resolve -------------------------------------------------------
    ("cut run-rate ~60% month on month",
     ["khelo.cost_reduction_pct"], True, "approximate span, in tolerance"),
    ("reduced spend from ₹9.4L to ₹3.5L per month",
     ["khelo.cost_range"], True, "range against a range-encoding claim"),
    ("₹9.4L → ₹3.5L",
     ["khelo.cost_range"], True, "arrow form of the same range"),
    ("indexed 206 documents into 11,611 chunks",
     ["pqbot.corpus_documents", "pqbot.corpus_chunks"], True, "two counts, two claims"),
    ("sole engineer on the parliamentary question bot",
     ["pqbot.sole_engineer"], True, "superlative WITH explicit citation"),
    ("responses in under 3 minutes, down from 15",
     ["pqbot.latency_range"], True, "duration range"),
    ("rated 3.9/4 in the ministry review",
     ["pqbot.review_rating"], True, "rating form"),
    ("1st Place, national hackathon",
     ["personal.hackathon_first"], True, "ordinal"),

    # --- must NOT resolve ---------------------------------------------------
    ("cut run-rate 62%",
     ["khelo.cost_reduction_pct"], False, "exact span, wrong value, no approximation marker"),
    ("cut run-rate ~60% across 14 centres",
     ["khelo.cost_reduction_pct"], False, "SPLICE: 14 centres belongs to another project"),
    ("the first such deployment in the Ministry",
     ["pqbot.sole_engineer"], False, "prose superlative not supported by the cited statement"),
    ("owned the architecture end to end",
     [], False, "prose fact, no citation"),
    ("indexed over 200 documents",
     [], False, "approximate count, nothing cited"),
    ("saved roughly ₹6L a month",
     ["khelo.cost_reduction_pct"], False, "derived figure; the claim is a percentage, not a delta"),
    ("100% of reconciliations automated",
     ["expenditure.reconciliation_count"], False, "superlative 'every/100%' beyond the claim"),
    ("reduced latency to sub-300ms",
     ["pqbot.latency_range"], False, "different metric from the cited range"),
    ("built the only AI system in Indian sport",
     ["khelo.cost_reduction_pct"], False, "exclusivity with an unrelated citation"),
    ("cut costs by 60 per cent",
     ["khelo.cost_reduction_pct"], True, "'per cent' spelled out still detected"),
    ("cut costs by sixty percent",
     ["khelo.cost_reduction_pct"], False, "spelled-out number: the LLM pass must catch it"),
]


@pytest.mark.parametrize("text,claims,expect_passed,why", CASES,
                         ids=[c[3] for c in CASES])
async def test_validation_table(ledger, text, claims, expect_passed, why):
    result = await validate(text, cited_claim_keys=claims, company_slug="acme")
    assert result.passed is expect_passed, (why, result.assertions)
```

Two rows deserve comment. `"cut costs by sixty percent"` is the case regex cannot
catch and the LLM assertion pass must — it is in the table specifically to fail
loudly if the assertion pass is ever stubbed out or its prompt degraded.
`"saved roughly ₹6L a month"` is a **derived** figure: arithmetically consistent
with the cited claim, not present in it, and therefore not authorised. A system
that resolves derived figures resolves anything.

### 7.5 Detection completeness

Separate from resolution: the regex families must find **every** surface-detectable
assertion, because a missed numeric is a shipped fabrication. A corpus test runs
all patterns over the six seeded resume variants and the existing letter corpus
and asserts that every digit-bearing token in the corpus falls inside some
detected span. Uncovered tokens are printed with their context so the pattern gap
is fixable rather than merely reported.

Span de-overlapping is tested directly: `₹9.4L → ₹3.5L` yields one `range` span,
not two `currency` spans and a `range`.

### 7.6 Failure behaviour and provenance

| Case | Expected |
|---|---|
| Validation fails on attempt 1 | Artifact written with `validation_status = 'failed'` and `validation_notes`; one regeneration attempted with the unresolved spans quoted back verbatim |
| Validation fails on attempt 2 | Second failed artifact retained; `review_item.status = 'needs_manual_review'`; `run_log.stats.validation_failures` incremented; the item appears in the digest |
| At no point | Does a document with an unresolved assertion become attachable |
| Every resolved assertion | Writes a `claim_usage` row with a `location` precise enough to find the sentence again |
| `GET /claims/{id}/usage` | Returns every artifact that used the claim, so a claim later found wrong can be traced to every document that asserted it |

That last row is the recall path. If a claim turns out to be wrong — a number
misremembered, a project detail misstated — the operator needs to know exactly
which applications carried it. A test seeds a claim, generates three artifacts
citing it, and asserts all three come back.

---

## 8. Integration tests

Against a real, disposable Postgres 16 and Redis 7 — never SQLite, because half
of what is being tested is Postgres-specific: triggers, `ENUM` types, generated
`TSVECTOR` columns, `GIN`/`gin_trgm_ops` indexes, partial indexes and views.

```yaml
# docker-compose.test.yml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: scout_test
      POSTGRES_PASSWORD: test          # a fixture credential, not a secret
    command: >
      postgres -c fsync=off -c full_page_writes=off
               -c synchronous_commit=off -c shared_buffers=256MB
    tmpfs: [/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d scout_test"]
      interval: 2s
      retries: 30
  redis:
    image: redis:7-alpine
    command: redis-server --save "" --appendonly no
```

The schema is built by running **the Alembic migrations**, not
`metadata.create_all()`. This is deliberate: it proves the migration set produces
the schema the models expect, which is the thing that actually breaks, and it
means every trigger and view is present in the test database because the
migration that created it ran.

```python
# backend/tests/integration/conftest.py
@pytest.fixture(scope="session")
async def schema(pg_url):
    await run_alembic("upgrade", "head", url=pg_url)
    yield
    # Down-migration is exercised here and nowhere else (SOP.md §6.4).
    await run_alembic("downgrade", "base", url=pg_url)


@pytest.fixture
async def db(schema, pg_url):
    """Each test runs in a transaction that is rolled back. No cross-talk."""
    async with engine(pg_url).connect() as conn:
        txn = await conn.begin()
        yield async_session(bind=conn)
        await txn.rollback()
```

**What the integration band covers:**

| Area | Proves |
|---|---|
| Migrations | `upgrade head` from empty succeeds; `downgrade base` succeeds; `upgrade head` again succeeds. Revision IDs are ≤ 32 chars. No two heads |
| Triggers | The artifact guards (§4.4), the `updated_at` triggers, the `application.status` materialisation from `application_event` |
| Views | `v_funnel` and `v_ghosted` return the documented shape against seeded data, including the empty case |
| Constraints | `UNIQUE (source_id, external_id)`, `UNIQUE (posting_id)` on `review_item` and `application`, `UNIQUE (claim_id, artifact_id, location)` |
| Full-text and trigram | `search_tsv` is populated by the generated column and `q` search returns the seeded posting; trigram company search matches a misspelling |
| Idempotency | A discovery run over the same fixture payload twice produces identical row counts; `Idempotency-Key` replay returns the original response |
| Run lock | A second `POST /runs/discovery` while one is in flight returns 409 `run.already_running`, and the lock releases on a crashed run via TTL |
| API contract | Every route returns the envelope; every documented error code is reachable; the generated OpenAPI matches the committed copy byte for byte |
| Cursor pagination | A full walk of a seeded list yields every row exactly once and terminates with `next_cursor: null` |
| The pipeline | End to end with stubbed adapters and a stubbed model: fixture payloads in, `review_item` rows out, `run_log.stats` correct, artifacts on disk with provenance rows |

The end-to-end pipeline test is the one that catches integration mistakes the
unit tests cannot: a stage that writes a field the next stage reads under a
different name, a `content_hash` computed before normalisation in one place and
after it in another.

---

## 9. Frontend tests

Proportionate. The frontend displays state and collects two decisions; the
correctness that matters is upstream.

| Layer | Tool | Covers |
|---|---|---|
| Type contract | `tsc --noEmit` | The generated client compiles against the committed OpenAPI schema. This is the real frontend test — a backend contract change breaks the build |
| Unit | Vitest | Formatting (coverage percentages, ₹ amounts, relative dates in IST), the gap-list renderer, the tailoring-plan diff renderer |
| Component | Vitest + Testing Library | The review card renders a full score payload; the plan editor emits a valid `PATCH /review/{id}/plan` body; approve and skip fire once and disable while in flight |
| Guard | Vitest | **No component renders a control labelled submit, apply or send.** A DOM-level assertion over the rendered route tree, mirroring §4.1 on the client side |
| Accessibility | `axe` in the component tests | Keyboard reachability on the queue, since the queue is used daily |
| Build | Vite | The production bundle builds; `npm audit` is clean at the configured level |
| Budget | Lighthouse against the built bundle | First load < 1.5 s (§`SOP.md` 8) |

There is no Playwright end-to-end suite. With one user, one session and no
authentication flow worth exercising, it would cost more to maintain than the
defects it would find — and the two flows that genuinely matter, *does the
rendered `.docx` look right* and *does the digest render in a mail client*, are
in the manual checklist because a machine cannot judge either.

---

## 10. The manual checklist

Some things cannot be automated, and pretending otherwise produces a suite that
is green while the output is unusable. This checklist runs before any release
(`RELEASE_NOTES.md` §2) and whenever the generation or digest path changes.

**Rendered resume `.docx`** — open in Word and in LibreOffice, and in Google Docs:

- [ ] It is **one page**. Not "one page in the fit loop's estimate" — one page on
      screen, in all three renderers.
- [ ] No orphaned section header at a page break; no bullet split across pages.
- [ ] Fonts render as intended and are embedded or safely substituted.
- [ ] Every number on the page traces to a ledger claim — spot-check three
      against `GET /claims/{id}/usage`.
- [ ] The tailoring plan's changes are actually visible in the document, and
      nothing the plan did not authorise has changed.
- [ ] Contact details, links and dates are correct and current.
- [ ] It reads as a document a person wrote. Read it aloud; if a sentence has the
      cadence of a model, it does not ship.

**Rendered cover letter `.docx`:**

- [ ] Under one page, correctly addressed, no placeholder text.
- [ ] The honest-gap paragraph reads as candour, not as an apology or a
      disclosure of weakness the reader had not noticed.
- [ ] It does not restate the resume.
- [ ] It contains nothing from `DOCUMENT_GENERATION.md` §6.5's never-list.
- [ ] Side by side with the previous three letters: they do not read as
      variations of one template.

**Digest email in a real client** — Gmail web, Gmail iOS, and one plain-text
client:

- [ ] Renders correctly on a phone at 390 px, which is where it will actually be
      read at 08:15.
- [ ] The failure banner is visible above the fold when a run failed.
- [ ] Every link resolves to the right screen and the right item.
- [ ] The plain-text alternative is readable on its own.
- [ ] It is scannable in under sixty seconds — the entire operating premise is
      ten minutes a day, and the digest is the first two of them.

**Operational:**

- [ ] A full discovery run against real sources completes inside 15 minutes and
      `source_results` shows no unexplained failures.
- [ ] Cost for that run is within the daily budget.
- [ ] A restore from the latest backup produces a working database
      (`INFRASTRUCTURE.md`).
- [ ] The Settings health table matches reality for three sampled sources.

Checklist results are recorded in the release entry, not remembered.

---

## 11. Test data and the deterministic LLM stub

### 11.1 Fixtures, not factories, where the shape came from outside

Anything whose shape is defined by a third party — ATS payloads, alert emails,
job descriptions — is a **committed fixture**, captured from reality. Anything
whose shape we define — companies, variants, claims, applications — is built by a
**factory** with sensible defaults and explicit overrides, so a test names only
the field it cares about.

```python
# backend/tests/factories.py
def a_claim(**kw) -> Claim:
    return Claim(**{
        "key": "khelo.cost_reduction_pct",
        "statement": "Cut run-rate by 60% (₹9.4L → ₹3.5L per month).",
        "metric_value": "60", "metric_unit": "percent",
        "project": "Khelo India Assistant",
        "evidence_ref": "fixtures/verification/khelo-2026-03.md",
        "confidentiality": "internal",
        "verified_at": datetime(2026, 3, 1, tzinfo=UTC),
        "expires_at": None,
        **kw})
```

**The ledger fixture is a redacted parallel of the real one**, not a copy. It has
the same shape, the same tricky cases (a range claim, a paired public/restricted
claim, an expired claim, two claims from different projects with the same unit)
and none of the operator's actual private figures, so the test corpus can be read
by anyone without disclosing anything.

**No test reads the clock.** Time is injected. A test that depends on "now"
depends on the day it runs, and expiry, recency decay, ghosting and the two-run
close rule are all time-dependent — every one of them is exercised by setting the
clock, never by sleeping.

### 11.2 The deterministic LLM stub

Every offline test that would touch a model uses `StubLLM`, which is a lookup
table, not a simulator.

```python
# backend/tests/stubs/llm.py
class StubLLM:
    """Deterministic, offline, and loud about gaps.

    Keys on (family, prompt_version, sha256(rendered_prompt)). A miss is a
    test-authoring error, never a silent default: a stub that invents a
    plausible response tests the stub.
    """

    def __init__(self, responses: dict[str, dict], *, record: bool = False):
        self._responses = responses
        self._record = record
        self.calls: list[LLMCall] = []

    async def structured(self, *, family, prompt, schema, **kw):
        key = f"{family}:{kw['prompt_version']}:{sha256(prompt)[:16]}"
        self.calls.append(LLMCall(family=family, key=key, prompt=prompt))
        if key not in self._responses:
            if self._record:
                raise RecordingMiss(key, prompt)     # re-record and commit
            raise KeyError(
                f"no stub response for {key}. Add it to "
                f"tests/stubs/responses/{family}.json — do not invent one.")
        return schema.model_validate(self._responses[key])
```

Three properties make it worth the discipline. A **miss raises**, so a test never
silently exercises a fabricated response. The stub **records `calls`**, so a test
can assert that a cached extraction made zero model calls, or that a
`cover_letter_worth = false` company generated no letter. And responses are
**committed JSON captured from real runs**, so they carry the real quirks —
trailing prose around JSON, an enum spelled in the wrong case — that the
enforcement loop exists to repair.

Two stub variants exist for negative paths: `MalformedLLM`, which returns
schema-violating payloads to exercise the repair budget and the hard failure, and
`InjectedLLM`, which returns output that has visibly obeyed an injected
instruction, to prove the output schema and the ledger validation contain it.

**The eval harness does not use the stub.** It calls the real provider, which is
why it costs money and why it does not run on every commit.

---

## 12. Coverage targets, and where coverage is the wrong metric

| Area | Target | Enforced |
|---|---|---|
| `ledger/` | **100%** line and branch | Yes, hard fail |
| `sources/policy.py` (the deny list) | **100%** | Yes, hard fail |
| `mail/client.py` (outbound policy) | **100%** | Yes, hard fail |
| `tracking/transitions.py` | **100%** branch | Yes, hard fail |
| `scoring/`, `extract/` | ≥ 90% | Yes |
| `sources/` adapters | ≥ 85% | Yes |
| `ingest/`, `review/`, `generate/` | ≥ 85% | Yes |
| `api/` routers | ≥ 80% | Yes |
| Overall backend | ≥ 85% | Yes |
| Frontend | ≥ 60% | Advisory |

**Where coverage is the right metric:** the four modules gated at 100% are small,
rule-shaped and consist almost entirely of branches that must each be exercised.
An unexecuted branch in `resolve()` or `may_append()` is a genuine hole, and the
number finds it.

**Where it is honestly the wrong metric, and is not pretended otherwise:**

- **The generation path.** A cover letter generator can reach 100% line coverage
  and still write a bad letter. The thing that needs proving is *quality of
  output*, which the eval measures on eleven axes and a human measures on the
  twelfth. Coverage on `generate/` tells you the plan applier was exercised. It
  tells you nothing about whether the letter is worth sending.
- **Prompts.** They are not code and have no lines to cover. Their test is
  `PROMPTS.lock` plus the eval.
- **The adapters.** 85% line coverage against fixtures says the mapping ran. It
  says nothing about whether the fixture still resembles what the vendor serves,
  which is the actual failure mode. The live smoke test (§5.4) is the real
  coverage measure there, and it is not expressible as a percentage.
- **The invariant set.** These tests are binary and total. "Coverage of the
  invariant tests" is a meaningless number; what matters is that each invariant
  in `ARCHITECTURE.md` §3 maps to at least one named test, and a meta-test
  asserts exactly that mapping:

```python
# backend/tests/invariants/test_invariant_coverage.py
INVARIANT_TESTS = {
    1: ["test_no_submit_route_in_openapi_schema",
        "test_no_outbound_client_in_decision_modules",
        "test_approve_makes_no_outbound_request"],
    2: ["test_send_rejects_foreign_recipient",
        "test_exactly_one_call_site_of_send",
        "test_scopes_are_minimal"],
    3: ["test_trigger_blocks_attaching_failed_artifact_to_review_item",
        "test_trigger_blocks_flipping_an_attached_artifact_to_failed",
        "test_no_validation_override_route"],
    4: ["test_direct_client_refuses", "test_detect_endpoint_refuses",
        "test_manual_company_creation_refuses",
        "test_manual_posting_import_refuses",
        "test_redirect_into_denied_host_is_refused_midflight",
        "test_deny_list_is_not_configuration"],
    5: ["test_one_adapter_failure_does_not_stop_the_run",
        "test_every_adapter_failing_still_completes_the_run"],
    6: ["test_no_secret_reaches_logs_or_db", "test_redaction_processor_installed"],
    7: ["test_every_artifact_records_provenance"],
    8: ["test_every_adapter_declares_interval_and_bucket",
        "test_disallow_disables_source"],
}


def test_every_invariant_has_a_test():
    declared = parse_invariants("docs/ARCHITECTURE.md")     # §3, numbered list
    assert set(declared) == set(INVARIANT_TESTS)
    for n, names in INVARIANT_TESTS.items():
        for name in names:
            assert test_exists(name), f"invariant {n}: missing {name}"
```

That test fails if an invariant is added to `ARCHITECTURE.md` without a test, and
if a named invariant test is deleted or renamed. It is the mechanism that keeps
§4 honest as the system grows.

---

## 13. The gate — `ci/run-checks.sh`

One host-agnostic script. It runs locally exactly as it runs in CI, because a
gate that only exists in a CI configuration is a gate developers discover after
pushing.

```bash
#!/usr/bin/env bash
# ci/run-checks.sh — the pre-merge gate.
# Usage: bash ci/run-checks.sh [all|backend|frontend|invariants|integration]
set -euo pipefail

TARGET="${1:-all}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

run_invariants() {
  step "invariants (offline)"
  # First, always, and non-negotiable. Nothing else runs on a build that can
  # submit an application, email a stranger, or fetch a denied host.
  (cd backend && python -m pytest tests/invariants -q -x \
      --deselect tests/invariants/test_artifact_guard.py)   # needs Postgres; runs in integration
}

run_backend() {
  step "backend · lint + format"
  (cd backend && ruff check . && ruff format --check .)

  step "backend · types"
  (cd backend && mypy src/scout_careers)

  step "backend · unit (offline, no network, no db)"
  (cd backend && python -m pytest tests/unit tests/sources tests/ledger -q \
      --cov=scout_careers --cov-report=term-missing \
      --cov-fail-under="${COV_MIN:-85}")

  step "backend · coverage floors on the enforcement modules"
  (cd backend && python -m pytest tests/ledger -q \
      --cov=scout_careers.ledger \
      --cov=scout_careers.sources.policy \
      --cov=scout_careers.mail.client \
      --cov=scout_careers.tracking.transitions \
      --cov-branch --cov-fail-under=100)

  step "backend · migrations are singular and well-formed"
  (cd backend && alembic heads | tee /dev/stderr | wc -l | grep -qx 1) \
    || { echo "multiple alembic heads"; exit 1; }
  (cd backend && python scripts/check_revision_ids.py --max-len 32)
  (cd backend && python scripts/check_models_have_migration.py)

  step "backend · OpenAPI schema is current"
  (cd backend && python scripts/dump_openapi.py > /tmp/openapi.json \
     && diff -q /tmp/openapi.json ../frontend/src/api/openapi.json) \
    || { echo "OpenAPI drift: regenerate the schema and the client"; exit 1; }

  step "backend · dependency audit"
  (cd backend && pip-audit --strict)
}

run_integration() {
  step "integration · fixture postgres + redis"
  docker compose -f docker-compose.test.yml up -d --wait
  trap 'docker compose -f docker-compose.test.yml down -v' EXIT
  (cd backend && python -m pytest tests/integration \
      tests/invariants/test_artifact_guard.py -q)
}

run_frontend() {
  step "frontend · types"
  (cd frontend && npm run typecheck)

  step "frontend · lint"
  (cd frontend && npm run lint)

  step "frontend · unit + component"
  (cd frontend && npm run test -- --run --coverage)

  step "frontend · build"
  (cd frontend && npm run build)

  step "frontend · dependency audit"
  (cd frontend && npm audit --audit-level=high)
}

case "$TARGET" in
  invariants) run_invariants ;;
  backend)    run_invariants; run_backend ;;
  integration) run_integration ;;
  frontend)   run_frontend ;;
  all)        run_invariants; run_backend; run_integration; run_frontend ;;
  *) echo "unknown target: $TARGET"; exit 2 ;;
esac

printf '\n\033[1;32mall checks passed\033[0m\n'
```

**Ordering is deliberate.** Invariants run first and with `-x`, so an invariant
failure stops the run in seconds rather than after four minutes of linting. The
artifact-guard tests are deselected from that pass only because they need
Postgres, and they run at the head of the integration band instead — they are
still invariant tests and are reported as such.

**What is not in the gate, and why:**

| Excluded | Where it runs instead |
|---|---|
| The LLM eval | On prompt, vocabulary, formula and model-ID changes. ₹35 per run makes per-commit execution absurd, and the gate requires an eval artifact when the diff touches those paths |
| The adapter live smoke | Scheduled daily (§5.4). A vendor's outage must not block an unrelated merge |
| The manual checklist | Before a release (§10) |
| Lighthouse | In the frontend job on a schedule and before a release; it is too variable to gate a merge on |

**Exit discipline.** `set -euo pipefail`, one non-zero exit on the first failure,
no `|| true` anywhere. The script has no flag that skips a step. If a step must
be skipped, it is removed in its own merge request with the reason recorded —
which makes the removal visible, which is the entire point.

---

## 14. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` §3 | The invariants this plan's §4 exists to prove |
| `SOP.md` | When the gate runs, who owns which gate, the review checklist |
| `AI_ARCHITECTURE.md` §10 | Canonical for the golden set and the eval gates |
| `MATCH_SCORING.md` §12 | Canonical for the three tiers of scorer evaluation |
| `CLAIMS_LEDGER.md` §5–§7 | Canonical for validation, enforcement and provenance |
| `SOURCE_ADAPTERS.md` §12 | Canonical for the six required adapter tests |
| `APPLICATION_PIPELINE.md` §15 | Canonical acceptance criteria for tracking |
| `EMAIL_INGESTION.md` §14 | Canonical acceptance criteria for mail |
| `DATA_MODEL.md` §11 | Migration policy the integration band enforces |
| `RELEASE_NOTES.md` | Where manual checklist results are recorded |
