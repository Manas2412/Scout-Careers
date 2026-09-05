# DEVELOPMENT — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for repository layout, coding standards, branch and
merge-request policy, the pre-merge gate, and the procedures for adding an
adapter, a migration, a prompt or a claim. `ARCHITECTURE.md` wins on system
design; `DATA_MODEL.md` on schema; `API.md` on endpoint contracts. Where a module
document states a checklist (`SOURCE_ADAPTERS.md` §12), this file references it
rather than restating it.

---

## 1. Before you write anything

Three rules that determine whether a change is worth making at all.

1. **The invariants are not negotiable** (`ARCHITECTURE.md` §3). No automated
   submission. No automated outbound mail to people. Generation cites only the
   ledger. The never-scrape list is absolute and is a code constant. Adapter
   failure is isolated. No secrets in code or logs. Every artifact is
   reproducible. Robots and rate limits are respected. A change that weakens any
   of these is rejected regardless of what it enables.

2. **Over-engineering is the main risk to this ever being finished**
   (`ARCHITECTURE.md` §9). One user, one Postgres, one container, one worker.
   Celery, Kubernetes and a vector database were considered and rejected with
   stated reasons. Re-proposing one requires new evidence, not a new preference.

3. **Fail closed on integrity, fail open on enrichment**
   (`ARCHITECTURE.md` §8). A scoring failure downgrades an item to
   `needs_manual_review`; it never silently ships an unscored draft. An empty
   queue slot costs the operator nothing. A fabricated number costs them the
   thing the whole system exists to build.

---

## 2. Repository layout

```
scout-careers/
├── backend/
│   ├── pyproject.toml            deps, ruff, mypy, pytest config — one file
│   ├── uv.lock
│   ├── alembic.ini
│   ├── Dockerfile
│   ├── migrations/
│   │   ├── env.py
│   │   └── versions/
│   ├── seeds/
│   │   ├── variants/*.json       the six resume variants
│   │   ├── claims.yaml           the 63-row starter ledger
│   │   └── companies.yaml        the ~40 starter companies
│   ├── src/scout_careers/
│   │   ├── common/               config, logging, types, time, hashing, ULIDs
│   │   ├── db/                   models.py, session.py, base.py
│   │   ├── sources/              SourceAdapter protocol + one module per ATS
│   │   ├── registry/             company CRUD, ATS auto-detection
│   │   ├── ingest/               run orchestration, normalise, dedup, change detect
│   │   ├── extract/              JD → Requirement[] (LLM, structured output)
│   │   ├── scoring/              coverage, gaps, composite, ranking
│   │   ├── ledger/               claims store, citation resolution, validation
│   │   ├── generate/             tailoring plans, cover letters, .docx render
│   │   ├── review/               queue, approval state machine
│   │   ├── mail/                 Gmail read + classify, digest
│   │   ├── tracking/             lifecycle, funnel, spreadsheet export
│   │   ├── scheduler/            APScheduler job definitions
│   │   ├── llm/                  provider client, prompt registry, guard, cost
│   │   │   └── prompts/{family}/{version}.md  +  PROMPTS.lock
│   │   ├── cli/                  scout-careers entry points
│   │   └── api/                  routers (thin), deps, main
│   └── tests/
│       ├── unit/                 offline, no network, no database
│       ├── integration/          against a throwaway Postgres
│       ├── fixtures/sources/     captured upstream responses, per adapter
│       └── eval/golden/          the 40 hand-labelled JDs
├── frontend/
│   ├── package.json
│   ├── package-lock.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── src/
│       ├── routes/               Dashboard Queue Jobs Companies Applications
│       │                         Claims Variants Settings
│       ├── components/           shared UI
│       ├── api/                  GENERATED from OpenAPI — never hand-edited
│       └── lib/                  formatting, hooks, state
├── ci/run-checks.sh              the single host-agnostic gate
├── docker-compose.yml
├── docker-compose.prod.yml
└── docs/
```

### 2.1 The layering rule

**Routers are thin, services are thick.** No business logic in `api/`. No HTTP
concerns below `api/`. A router validates input, calls one service function,
wraps the result in the envelope, and returns.

**`sources/` never touches the database.** It returns normalised DTOs; `ingest/`
persists them. This is what makes every adapter test runnable offline against a
fixture with no database in sight, and it is the reason all six required adapter
tests (§7) are unit tests.

Import direction, enforced by an `import-linter` contract in the gate:

```
api  →  review, registry, tracking, ingest, ledger, generate, scoring
        ↓
     extract, mail, scheduler
        ↓
     sources, llm, db
        ↓
     common
```

Nothing imports upward. `common/` imports nothing from the package. A cycle is a
gate failure, not a code-review discussion.

---

## 3. The local loop

Install once per `INSTALLATION_GUIDE.md` §3. Then, daily:

```bash
# terminal 1 — dependencies
docker compose up -d postgres redis

# terminal 2 — backend
cd backend && source .venv/bin/activate
uvicorn scout_careers.api.main:app --reload --port 8000 --workers 1

# terminal 3 — frontend
cd frontend && npm run dev
```

`--workers 1` is required. APScheduler runs in-process; a second worker is a
second scheduler.

Keep `SCHEDULER_ENABLED=false` locally. An 08:00 run firing mid-refactor spends
real tokens against real employer endpoints. Trigger runs explicitly.

### 3.1 Fast feedback

```bash
# format and lint, with fixes
ruff format . && ruff check --fix .

# types
mypy src/

# the tests that do not need a database
pytest tests/unit -q

# one test, verbose, with logs
pytest tests/unit/sources/test_greenhouse.py::test_fetch_maps_fixture -vv -s

# watch mode
ptw -- tests/unit -q
```

`pytest tests/unit` must complete in **under 20 seconds**. It has no network and
no database, both blocked by fixtures (§7). If it slows past that, something
acquired a dependency it should not have.

### 3.2 Working against real data without spending tokens

```bash
# Re-run one source, no LLM stages
scout-careers run discovery --source-ids 12 --stop-after normalise

# Re-extract one posting from cache (free if content_hash is unchanged)
scout-careers extract --posting-id 01JB… --dry-run

# Generate with the deterministic stub provider — zero tokens
LLM_PROVIDER=stub scout-careers generate --review-item 01JC…
```

The stub provider is §11.3. Use it by default; reach for a real provider only
when the model's output is the thing under test.

---

## 4. Coding standards

### 4.1 Python

**Type hints everywhere.** Every function signature, every attribute, every
return. `mypy` runs strict-ish and its configuration is the authority:

```toml
# backend/pyproject.toml
[tool.mypy]
python_version = "3.12"
strict = true
warn_unreachable = true
warn_return_any = true
disallow_untyped_defs = true
disallow_any_generics = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false      # fixtures stay readable

[[tool.mypy.overrides]]
module = ["docx.*", "apscheduler.*", "playwright.*"]
ignore_missing_imports = true      # named, not blanket
```

`Any` requires a comment naming the upstream shape that forces it. An
`ignore_missing_imports` entry is per-module and named — never blanket.

**Ruff for format and lint**, one tool, no black, no isort, no flake8:

```toml
[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = [
  "E", "F", "W",      # pycodestyle, pyflakes
  "I",                # isort
  "N",                # pep8-naming
  "UP",               # pyupgrade
  "B",                # bugbear
  "S",                # bandit — security
  "A",                # builtin shadowing
  "C4",               # comprehensions
  "DTZ",              # naive datetimes  ← load-bearing, see below
  "T20",              # print / pprint   ← load-bearing
  "SIM", "RUF",
  "ASYNC",            # blocking calls in async functions
]
ignore = ["E501"]     # the formatter owns line length

[tool.ruff.lint.per-file-ignores]
"tests/*"        = ["S101"]          # assert is the point
"migrations/*"   = ["E402", "F401"]  # alembic's generated shape
```

Four of those rule sets are load-bearing and must not be disabled:

- **`T20` — no `print`.** Structured logging only. A `print` in a pipeline stage
  is a line that carries no `run_id`, cannot be filtered, and cannot be
  correlated. It is also how untrusted content ends up in stdout unredacted.
- **`DTZ` — no naive datetimes.** Everything is stored and computed in UTC and
  displayed in `Asia/Kolkata` (`ARCHITECTURE.md` §8). A naive `datetime.now()` is
  a timezone bug waiting for a deploy to a differently-configured host.
- **`S` — bandit.** It is what catches `subprocess` with `shell=True`, weak
  hashing, and hard-coded credentials before review does.
- **`ASYNC`** catches a blocking `requests` call or a synchronous file read
  inside an async pipeline stage, which at 320 concurrent sources is a real
  stall, not a style point.

**Structured logging only:**

```python
# wrong — unfilterable, uncorrelated, and it leaks the body
print(f"failed source {src.id}: {resp.text}")

# right
log.warning(
    "source_fetch_failed",
    run_id=run_id,
    source_id=src.id,
    adapter=src.adapter,
    status_code=resp.status_code,
    error_code="adapter.transport",
)
```

Never log: credentials, OAuth tokens or any prefix, hash or length of one;
cookies; full email bodies; raw resume content; PII. Log lines about auth record
only `{"gmail_auth": "refreshed", "expires_in_s": 3599}`
(`EMAIL_INGESTION.md` §2.5).

**No magic constants.** Every tunable is a field on the single `Settings` object
built by `pydantic-settings` (`ARCHITECTURE.md` §8) and documented in
`CONFIGURATION.md`.

```python
# wrong
if source.consecutive_failures >= 5:
    source.enabled = False

# right
if source.consecutive_failures >= settings.auto_disable_threshold:
    source.enabled = False
```

The exception, and it is narrow: values that are **policy, not configuration**
are code constants precisely so they cannot be changed by environment.
`NEVER_FETCH_HOSTS` is the canonical case — invariant 4 says the deny list is a
code constant, not a config value, and making it configurable would give the
invariant somewhere to be switched off.

**Parameterised SQL only.** Never string concatenation, never f-strings into SQL.

```python
# wrong
await conn.execute(f"SELECT * FROM company WHERE slug = '{slug}'")

# right — ORM
stmt = select(Company).where(Company.slug == slug)

# right — raw, when the ORM is the wrong tool
await conn.execute(
    text("SELECT * FROM company WHERE name % :q ORDER BY similarity(name, :q) DESC"),
    {"q": query},
)
```

A dynamic identifier — a column name chosen at runtime for an ORDER BY — goes
through an allow-list, never through interpolation:

```python
_SORTABLE: Final[dict[str, ColumnElement[Any]]] = {
    "score":  MatchScore.composite_score,
    "seen":   JobPosting.first_seen_at,
    "title":  JobPosting.title,
}

def order_clause(key: str) -> ColumnElement[Any]:
    try:
        return _SORTABLE[key]
    except KeyError:
        raise ValidationError(f"Unsortable field: {key}") from None
```

**Money and percentages are `NUMERIC`, never float** (`DATA_MODEL.md` §1). In
Python that is `Decimal`. A cost meter accumulating floats across 100 calls a day
drifts, and the drift is against a budget the breaker enforces.

**Untrusted input is never interpolated into a prompt.** It goes into a delimited
envelope with the boundary tokens stripped from the content first
(`AI_ARCHITECTURE.md` §7.2). `user_template` interpolation is over named,
code-supplied fields only. This is enforced by review and by a test asserting no
prompt template contains a field bound to a `description_text` or an email body.

### 4.2 Async

- One `AsyncSession` per request or per task. Never module-level, never shared
  across tasks — `asyncpg` raises `another operation is in progress` and the
  error appears far from the cause.
- `asyncio.gather(..., return_exceptions=True)` in the source runner is the
  invariant-5 mechanism, in one keyword (`SOURCE_ADAPTERS.md` §10.1). Do not
  "clean it up" to a plain `gather`.
- `asyncio.CancelledError` is re-raised, never swallowed. The whole-run timeout
  must be able to stop the run.
- No adapter constructs its own `httpx.AsyncClient`. One client per run, injected
  (`SOURCE_ADAPTERS.md` §4.1). A locally-constructed client is a review failure —
  it bypasses rate limiting, robots enforcement, the circuit breaker and the UA
  policy simultaneously.

### 4.3 API layer

Every response, success or failure, is `{ data, message, meta }` (`API.md` §1).
This is a response model plus one exception handler, not a per-route concern:

```python
@router.get("/companies", response_model=Envelope[list[CompanyOut]])
async def list_companies(
    filters: Annotated[CompanyFilters, Depends()],
    svc: Annotated[CompanyService, Depends(get_company_service)],
) -> Envelope[list[CompanyOut]]:
    page = await svc.list(filters)
    return Envelope(
        data=page.items,
        message="OK",
        meta={"next_cursor": page.next_cursor, "total": page.total},
    )
```

Errors carry the correct status and a **stable machine code** in
`meta.code` — the frontend branches on the code, never on the message text, so
message wording is free to change and codes are not.

Pagination is cursor-based on every list endpoint. Offset pagination is not
offered, at any version.

### 4.4 Frontend

**TypeScript strict.** `strict: true`, `noUncheckedIndexedAccess: true`,
`noImplicitOverride: true`. No unjustified `any`.

**The API client is generated.** `frontend/src/api/` is produced from
`/api/v1/openapi.json` by `npm run generate:api` and is committed so a
regeneration produces a reviewable diff.

> **Hand-written request or response types in the frontend are a review
> failure** (`API.md`, preamble). Not a preference — a failure. A hand-written
> type is a second, unversioned copy of the contract that drifts silently and is
> discovered in production. If the generated type is wrong, the schema is wrong;
> fix the Pydantic model.

The gate enforces it:

```bash
npm run generate:api && git diff --exit-code src/api/
```

A dirty diff means the committed client does not match the schema, and the merge
request is incomplete.

**No `console.log`** in application code. A thin `lib/log.ts` wrapper that no-ops
in production is the only channel.

**No hardcoded colours or spacing** outside the token layer.

**TanStack Query owns server state.** Component state is for what is on the
screen. A `useEffect` that fetches is a code smell with one legitimate exception —
an imperative action outside the query lifecycle — and it needs a comment saying
which.

---

## 5. Branches and merge requests

### 5.1 Naming

```
feature/<ticket>-<short-description>      feature/SC-41-ashby-adapter
bugfix/<ticket>-<short-description>       bugfix/SC-58-workday-relative-date
hotfix/<ticket>-<short-description>       hotfix/SC-63-token-refresh-race
chore/<short-description>                 chore/bump-fastapi-0116
```

Delete the branch after merge. A repository whose branch list is a history of
everything ever attempted is a repository nobody can find anything in.

### 5.2 Promotion

```
feature/* ──▶ dev ──▶ main
hotfix/*  ──▶ main ──▶ (back-merged to dev, same day)
```

Two long-lived branches. A single-user personal system does not need `qa` and
`uat` — it needs `dev` to be continuously green and `main` to be what is
deployed. Adding staging tiers for one operator is the over-engineering
`ARCHITECTURE.md` §9 warns against.

`main` is deployable at every commit. `dev` is green at every commit — a red
`dev` is fixed or reverted before anything else merges onto it.

### 5.3 Work is proposed, never pushed

**No direct pushes to `main` or `dev`.** Every change — including the operator's
own, including a one-line typo fix — goes through a merge request.

This is not ceremony, and it is not about a second pair of eyes that a
single-operator project does not have. It is about **the gate**. A merge request
is the only place `ci/run-checks.sh` is guaranteed to have run against the change
in isolation. A direct push is a change that entered the deployed branch without
the gate, and the first time that happens is the time it was the change that
broke the ledger validation.

Where the host enforces it, configure: protect `main` and `dev`; require a merge
request; require the CI check to pass; disallow force-push. Where it does not, a
pre-push hook is the fallback:

```bash
# .git/hooks/pre-push
#!/usr/bin/env bash
branch=$(git rev-parse --abbrev-ref HEAD)
case "$branch" in
  main|dev)
    echo "Refusing direct push to $branch. Open a merge request." >&2
    exit 1 ;;
esac
```

**`--no-verify` is never used.** Neither is skipping CI. A gate that can be
waived under time pressure is not a gate, and time pressure is exactly when the
mistakes happen.

### 5.4 What a merge request must contain

| Section | Content |
|---|---|
| **What changed** | Two or three sentences. Not a restatement of the diff. |
| **Why** | The problem, and the alternative rejected. |
| **Ticket** | `SC-41`, or `none — <reason>`. |
| **Test evidence** | The gate output, plus anything the gate cannot assert: a run ID, a `source_results` block, a rendered `.docx`, an eval delta table. |
| **Impact** | Schema change? New config key? New cost? A flag? A behaviour change on the generation path? Each named explicitly. |
| **Invariant check** | Which of `ARCHITECTURE.md` §3 the change touches, and how it still holds. Absent means "none", and reviewers will check that claim. |

A merge request touching `sources/`, `llm/prompts/`, `ledger/` or `generate/`
carries the invariant section filled in. Those four directories are where a
plausible change can quietly break a guarantee.

### 5.5 Commit messages

Conventional Commits, because the type prefix is what makes `git log` scannable
in a repository where most commits are small.

```
<type>(<scope>): <imperative summary, ≤ 72 chars>

<body: why, not what — the diff already says what>

<footer: SC-41, BREAKING CHANGE:, Refs:>
```

| Type | Use |
|---|---|
| `feat` | New capability |
| `fix` | Bug fix |
| `refactor` | No behaviour change |
| `perf` | Measured improvement — include the measurement |
| `test` | Tests only |
| `docs` | Documentation only |
| `chore` | Deps, tooling, CI |
| `migrate` | Contains an Alembic revision. Its own type because it is the one thing a reviewer must never miss. |
| `prompt` | Contains a prompt version bump. Its own type for the same reason. |

Scopes are module names: `sources`, `ingest`, `extract`, `scoring`, `ledger`,
`generate`, `review`, `mail`, `tracking`, `llm`, `api`, `db`, `frontend`.

```
feat(sources): add Recruitee adapter

Recruitee exposes an unauthenticated /api/offers/ endpoint per tenant
subdomain, so this is a plain list-page adapter with no detail fetch.
Fidelity rank 78 — descriptions are reliable but thinner than Greenhouse.

Bucket is per-tenant ({company}.recruitee.com) at 2 req/s, since the
subdomain is the rate-limiting domain, not the shared API host.

SC-41
```

```
prompt(llm): bump cover_letter to 2026-09-14.1

Gap paragraph was landing in the closing rather than paragraph three on
terse JDs. Instruction reordered; schema unchanged.

Eval: agreement 0.84 → 0.86, validation pass rate 0.91 → 0.93,
no metric regressed. Run 01JF7K…, table in PROMPTS.lock.

SC-52
```

One logical change per commit. A commit that changes a prompt *and* a scoring
weight cannot be reverted without reverting both, and those are exactly the two
things that need to be bisectable independently.

---

## 6. The pre-merge gate

One host-agnostic script, `ci/run-checks.sh`. The CI runner invokes it and does
nothing else, so the gate is identical locally and in CI and cannot drift between
them. This is deliberate: a CI configuration file that duplicates the checks is a
second definition that eventually disagrees with the first.

```bash
bash ci/run-checks.sh all         # both
bash ci/run-checks.sh backend
bash ci/run-checks.sh frontend
```

### 6.1 The script

```bash
#!/usr/bin/env bash
# ci/run-checks.sh — the single pre-merge gate.
# Host-agnostic by design: CI invokes this and nothing else, so what runs in
# CI and what runs on a laptop cannot drift apart.
#
# Usage: run-checks.sh [all|backend|frontend]   (default: all)

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-all}"
FAILED=()

c_reset=$'\033[0m'; c_bold=$'\033[1m'
c_red=$'\033[31m';  c_green=$'\033[32m'; c_yellow=$'\033[33m'

step() { printf '\n%s▶ %s%s\n' "$c_bold" "$1" "$c_reset"; }

run() {   # run <label> <command...>
  local label="$1"; shift
  printf '  %-38s' "$label"
  if "$@" >"/tmp/scout-check-$$.log" 2>&1; then
    printf '%s✓%s\n' "$c_green" "$c_reset"
  else
    printf '%s✗%s\n' "$c_red" "$c_reset"
    sed 's/^/      /' "/tmp/scout-check-$$.log" | tail -60
    FAILED+=("$label")
  fi
  rm -f "/tmp/scout-check-$$.log"
}

# ─────────────────────────────────────────────────────────────── backend ──
check_backend() {
  step "Backend"
  cd "$ROOT/backend"

  run "ruff format --check"   ruff format --check .
  run "ruff check"            ruff check .
  run "mypy"                  mypy src/
  run "import contracts"      lint-imports --config pyproject.toml

  # Compile every module. Catches syntax and import-time errors in code the
  # test suite does not reach — CLI entry points, adapters not yet wired up.
  run "compile"               python -m compileall -q src/

  # Offline unit tests. No network (a socket-blocking fixture), no database.
  run "pytest unit"           python -m pytest tests/unit -q \
                                --timeout=60 --maxfail=1

  # Integration tests, only when a throwaway Postgres is reachable.
  if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
    run "pytest integration"  python -m pytest tests/integration -q --timeout=180
  else
    printf '  %-38s%sskipped (TEST_DATABASE_URL unset)%s\n' \
      "pytest integration" "$c_yellow" "$c_reset"
  fi

  # Migrations must be linear and must match the models. A model change with
  # no revision is the single most common cause of a broken deploy.
  run "alembic single head"   bash -c \
    '[ "$(alembic heads | wc -l)" -eq 1 ]'
  if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
    run "alembic no drift"    bash -c \
      'alembic upgrade head && alembic check'
  fi

  # Prompt lockfile: every prompt file hash matches PROMPTS.lock, so an edited
  # prompt cannot ship without a version bump (AI_ARCHITECTURE.md §5.1).
  run "prompt lockfile"       python -m scout_careers.llm.registry --verify-lock

  # Dependency advisories. HIGH and CRITICAL fail; the ignore list is
  # explicit, dated and reviewed monthly.
  run "pip-audit"             pip-audit \
                                --requirement requirements.lock \
                                --strict \
                                --ignore-vuln GHSA-0000-0000-0000

  cd "$ROOT"
}

# ────────────────────────────────────────────────────────────── frontend ──
check_frontend() {
  step "Frontend"
  cd "$ROOT/frontend"

  run "npm ci"                npm ci --no-audit --no-fund
  run "tsc --noEmit"          npx tsc --noEmit
  run "eslint"                npx eslint . --max-warnings 0
  run "prettier --check"      npx prettier --check "src/**/*.{ts,tsx,css}"

  # The generated client must match the committed one. A dirty diff means a
  # hand-edit or a stale regeneration — both review failures (API.md).
  if [[ -f "$ROOT/backend/openapi.json" ]]; then
    run "api client in sync"  bash -c \
      'npm run generate:api --silent -- --schema ../backend/openapi.json \
       && git diff --exit-code -- src/api/'
  fi

  run "vitest"                npx vitest run --reporter=dot
  run "build"                 npm run build
  run "npm audit"             npm audit --omit=dev --audit-level=high

  cd "$ROOT"
}

# ────────────────────────────────────────────────────────── invariants ──
check_invariants() {
  step "Invariants (ARCHITECTURE.md §3)"
  cd "$ROOT/backend"

  run "no submit endpoint"    bash -c \
    '! grep -rEni "def .*(submit_application|auto_apply|post_application)" src/'

  run "never-scrape is const"  python -m pytest \
    tests/unit/test_invariants.py -q --timeout=30

  run "no print statements"   bash -c \
    '! grep -rn --include="*.py" -E "^\s*print\(" src/'

  cd "$ROOT"
}

# ─────────────────────────────────────────────────────────────── driver ──
case "$TARGET" in
  backend)  check_backend;  check_invariants ;;
  frontend) check_frontend ;;
  all)      check_backend;  check_frontend; check_invariants ;;
  *) echo "Usage: $0 [all|backend|frontend]" >&2; exit 2 ;;
esac

if ((${#FAILED[@]})); then
  printf '\n%s✗ %d check(s) failed:%s\n' "$c_red" "${#FAILED[@]}" "$c_reset"
  printf '    %s\n' "${FAILED[@]}"
  exit 1
fi

printf '\n%s✓ All checks passed.%s\n' "$c_green" "$c_reset"
```

### 6.2 What the gate does not cover

Honesty about the boundary matters more than the list of checks.

| Not covered | Why | How it is covered instead |
|---|---|---|
| The answer-quality eval | ~₹35 per run in real tokens (`AI_ARCHITECTURE.md` §10.3) | Run on prompt and model-ID changes, not per-commit. Result committed to `PROMPTS.lock`. |
| Live adapter behaviour | No test in `sources/` may touch the network | Seed a real source and run it (`SOURCE_ADAPTERS.md` §12, step 13) |
| "Is this cover letter good" | Not a metric, and pretending otherwise would be the most expensive mistake available | Ten hand-reviewed cases per generation-family change |
| Page count of a rendered `.docx` | Needs LibreOffice | `RENDER_VERIFY_PAGES` at runtime; verified manually on generation changes |

### 6.3 Definition of done

A change is done when **all** of these hold. Not "looks right", not "works on my
machine".

```
[ ] ci/run-checks.sh all is green
[ ] New behaviour has a test that fails without the change
[ ] Schema change ships an Alembic revision, id ≤ 32 chars, with a downgrade
[ ] New tunable is a Settings field, in CONFIGURATION.md, with a default
[ ] Prompt change has a version bump, a lockfile entry and a passing eval
[ ] Adapter change has all six required tests (SOURCE_ADAPTERS.md §12)
[ ] Generation-path behaviour ships behind a flag, defaulting off
[ ] No new secret in code, config table, log line or error message
[ ] The invariants it touches are named in the MR and still hold
[ ] The merge request describes what, why, evidence and impact
```

The line that matters most: **"done" means the executable acceptance criteria
pass.** Every module document ends with a table of them. They are the
specification; the code is an implementation of it.

---

## 7. Adding a source adapter

`SOURCE_ADAPTERS.md` §12 is the authoritative thirteen-step checklist and is not
restated here. This section is the shape of the work and the three steps people
get wrong.

### 7.1 The order that matters

1. **Confirm the source is permissible — before writing a line.** The endpoint
   must be a documented or front-end-public JSON API reachable **without
   authentication, without a browser session, and without defeating bot
   protection**, on a host absent from `NEVER_FETCH_HOSTS`. Check robots.txt and
   terms. Record the finding in `DATA_SOURCES_AND_COMPLIANCE.md` **whether the
   answer is yes or no** — a recorded "no" is what stops the question being
   reopened every six months.

   If any of that fails, stop. The answer is `mail_alert` or manual import, not
   a cleverer adapter. Meta and Apple are the worked precedents
   (`SOURCE_ADAPTERS.md` §11): the cost of not having them is bounded and known;
   the cost of proceeding is unbounded, and an invariant that has one exception
   is not an invariant.

2. **Capture fixtures before writing code.** `curl` one list page and one detail
   page into `tests/fixtures/sources/{adapter}/`. **The fixtures are the
   specification**; the adapter is an implementation of them. Writing the adapter
   first produces an adapter that passes tests derived from the adapter.

3. Enum value + standalone Alembic revision (`ALTER TYPE … ADD VALUE` cannot
   share a transaction with other DDL).
4. Config model, `extra="forbid"`, constraining pattern on any field
   interpolated into a URL, plus a canonical-serialisation round-trip test —
   `UNIQUE (company_id, adapter, config)` depends on it.
5. Response model. **This is what turns silent vendor drift into `schema_error`
   instead of an empty board.**
6. The adapter: `parse_config`, `probe`, `fetch`, `aclose`, `describe`. Use only
   `SourceHttpClient`. Never catch your own exceptions — the runner classifies
   them.
7. Fidelity rank with a one-line reason. No new adapter gets 90+ without full JD
   text in the primary response.
8. Rate-limit bucket keyed on the **shared resource**, not the source ID.
   Conservative is never the wrong first guess.
9. Detection patterns in `COMPANY_REGISTRY.md` §2. An adapter that cannot be
   auto-detected is an adapter nobody will use.
10. Register in `ADAPTERS`.
11. All six required tests, offline.
12. A documentation subsection in the established shape.
13. **Seed a real source and run it.** `status: "ok"` with a plausible `fetched`
    count is the acceptance criterion.

### 7.2 The six required tests

All offline. No test in `sources/` may touch the network — enforced by a
`pytest` fixture that patches the transport to raise.

| Test | Asserts |
|---|---|
| `test_parse_config_*` | Valid config passes; bad host or token rejected; canonical round-trip |
| `test_fetch_maps_fixture` | Fixture in, exact `RawPosting` list out, field by field |
| `test_pagination` | Multi-page fixture fully consumed, and terminates |
| `test_partial_failure` | A 500 on page 2 raises; the runner records `error`; **nothing partial is yielded** |
| `test_no_disallowed_host` | Every URL the adapter can construct passes `assert_fetch_allowed` |
| `test_normalisation_edges` | Empty description dropped; relative date; remote location; multi-location |

`test_partial_failure` is the one most often written wrongly. A half-fetched
board looks like "everything else closed" to the two-run `closed_at` rule and
would close an employer's live postings. Partial ingestion is not allowed, and
the test must assert that nothing was yielded, not merely that an exception
propagated.

### 7.3 Skeleton

```python
# src/scout_careers/sources/recruitee.py
from typing import ClassVar, Final
from pydantic import BaseModel, ConfigDict, Field

from scout_careers.sources.base import RawPosting, SourceAdapter, ProbeResult
from scout_careers.sources.http import SourceHttpClient
from scout_careers.sources.normalise import html_to_text, parse_location

_SUBDOMAIN: Final = r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$"


class RecruiteeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    company: str = Field(pattern=_SUBDOMAIN)   # interpolated into the host


class RecruiteeAdapter(SourceAdapter):
    name: ClassVar[str] = "recruitee"
    config_model: ClassVar[type[BaseModel]] = RecruiteeConfig
    fidelity_rank: ClassVar[int] = 78          # §8: reliable, thinner JDs
    default_poll_interval_minutes: ClassVar[int] = 1440
    requires_detail_fetch: ClassVar[bool] = False

    def describe(self, cfg: RecruiteeConfig) -> str:
        return f"Recruitee · {cfg.company}"

    async def probe(self, client: SourceHttpClient, cfg: RecruiteeConfig) -> ProbeResult:
        payload = await client.get_json(self._url(cfg))
        return ProbeResult(reachable=True, sample_count=len(payload["offers"]))

    async def fetch(
        self, client: SourceHttpClient, cfg: RecruiteeConfig
    ) -> list[RawPosting]:
        # No try/except. The runner classifies failures (§10.1); catching here
        # would turn a schema_error into a silently empty board.
        payload = _OffersPage.model_validate(await client.get_json(self._url(cfg)))
        return [self._map(o, cfg) for o in payload.offers if o.description]

    @staticmethod
    def _url(cfg: RecruiteeConfig) -> str:
        return f"https://{cfg.company}.recruitee.com/api/offers/"
```

---

## 8. Adding a database migration

### 8.1 Procedure

```bash
cd backend
# 1. Edit db/models.py
# 2. Autogenerate — a starting point, never the final artefact
alembic revision --autogenerate -m "add source last_probe_at"
# 3. READ AND EDIT the generated file
# 4. Apply, then verify the downgrade actually works
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

### 8.2 Rules

- **One revision per schema change. Revision ID ≤ 32 characters**
  (`DATA_MODEL.md` §11). Set it explicitly rather than accepting the generated
  hash:

  ```bash
  alembic revision --autogenerate -m "add source last_probe_at" \
    --rev-id "0021_source_last_probe_at"
  ```

- **Always read the autogenerated file.** Alembic reliably misses: server
  defaults, `USING` clauses on type changes, partial-index predicates, GIN
  operator classes, and every enum change. It also reliably proposes dropping
  anything created outside the models — extensions, views, trigger functions.
  Delete those lines.

- **Enum additions are their own revision.** `ALTER TYPE … ADD VALUE` cannot run
  inside a transaction block alongside other DDL.

  ```python
  revision = "0022_ats_type_recruitee"
  down_revision = "0021_source_last_probe_at"

  def upgrade() -> None:
      op.execute("COMMIT")           # leave the migration transaction
      op.execute("ALTER TYPE ats_type ADD VALUE IF NOT EXISTS 'recruitee'")

  def downgrade() -> None:
      # Postgres cannot remove an enum value. Documented, not silently omitted.
      pass
  ```

- **A downgrade is always written**, even when it is a documented no-op with a
  reason. An empty `pass` with no comment is a review failure; the comment is the
  artefact.

- **Never orphan provenance.** Any migration touching `claim` or `artifact` must
  preserve `claim_usage` integrity (`DATA_MODEL.md` §11). Assert it in the
  migration itself:

  ```python
  def upgrade() -> None:
      op.alter_column("claim", "statement", nullable=False)
      orphans = op.get_bind().scalar(
          sa.text("SELECT count(*) FROM claim_usage cu "
                  "LEFT JOIN claim c ON c.id = cu.claim_id WHERE c.id IS NULL")
      )
      if orphans:
          raise RuntimeError(f"Refusing to migrate: {orphans} orphaned claim_usage rows")
  ```

- **Indexes on populated tables use `CONCURRENTLY`**, which also requires leaving
  the transaction:

  ```python
  def upgrade() -> None:
      op.execute("COMMIT")
      op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                 "posting_dept_idx ON job_posting (company_id, department)")
  ```

- **Seed data is never a migration.** The six variants, the starter ledger and
  the never-scrape list ship as an idempotent seed script
  (`DATA_MODEL.md` §11). A migration is a fact about structure; a seed is an
  opinion about content, and opinions get edited.

- **One head, always.** The gate asserts `alembic heads | wc -l == 1`. Two heads
  after a merge means an `alembic merge` revision, reviewed like any other.

- **Backfills are separate.** A revision that adds a column and backfills 50,000
  rows in one statement locks the table for the duration. Add the column
  nullable, ship a backfill script, then a second revision to set `NOT NULL`.

---

## 9. Adding or changing a prompt

### 9.1 The mechanism

Prompts live at `llm/prompts/{family}/{version}.md`, loaded at startup
(`AI_ARCHITECTURE.md` §5.1). Four families: `requirement_extraction`,
`coverage_judgement`, `tailoring_plan`, `cover_letter`.

**The registry refuses to start if a file's content hash does not match
`PROMPTS.lock`.** That is what makes an edited prompt impossible to ship without
a version bump, and therefore what makes `artifact.prompt_version` mean anything.
Invariant 7 — every artifact is reproducible — reduces to that hash check.

Version strings are dated with a within-day counter: `2026-09-14.1`.

**Old versions are never deleted.** An artifact recording
`cover_letter@2026-09-01.4` must remain explicable in eighteen months.

### 9.2 Procedure

```bash
cd backend

# 1. Copy, never edit in place
cp src/scout_careers/llm/prompts/cover_letter/2026-09-01.4.md \
   src/scout_careers/llm/prompts/cover_letter/2026-09-14.1.md

# 2. Edit the new file

# 3. Run the eval against the current production version as baseline
python -m scout_careers.eval run \
  --family cover_letter \
  --prompt-version 2026-09-14.1 \
  --baseline 2026-09-01.4

# 4. Only if it holds: record the hash and the eval result
python -m scout_careers.llm.registry --lock \
  --family cover_letter --version 2026-09-14.1 --eval-run 01JF7K…

# 5. Point configuration at it
#    COVER_LETTER_PROMPT_VERSION=2026-09-14.1
```

### 9.3 The eval gate

**A prompt change ships only if the eval holds.** Two conditions, and the second
is the one that catches bad changes that look good:

1. Every absolute gate in `AI_ARCHITECTURE.md` §10.2 passes.
2. **No metric regresses by more than 2 points against the baseline, even while
   passing.** A change trading 4 points of recall for 1 point of precision clears
   every absolute gate and is still a bad change.

The runner exits non-zero on either failure. The gates worth memorising:

| Metric | Gate |
|---|---|
| Hard-requirement recall | ≥ 0.92 |
| Extraction precision / recall | ≥ 0.85 / ≥ 0.80 |
| Recommendation agreement (top-1) | ≥ 0.80 |
| Validation pass rate | ≥ 0.90 |
| Schema enforcement rate | ≥ 0.98 |
| **Mismatch rejection** | **1.00** |
| **Injection resistance** | **1.00** |

The last two are gated at 1.00 because they are invariant-adjacent, and an
invariant with a 95% pass rate is not an invariant.

The eval costs about ₹35 per run, so it runs on prompt changes and model-ID
changes, not on every commit. **Generation families are additionally reviewed by
hand on ten cases**, because "is this letter good" is not a metric.

Eval results are committed alongside the version. `PROMPTS.lock` records, per
version: content hash, eval run ID, the metric table and the date. **A prompt in
production always has a recorded eval behind it.**

### 9.4 The golden set

Forty hand-labelled JDs in `tests/eval/golden/`, deliberately skewed toward hard
cases (`AI_ARCHITECTURE.md` §10.1): clear matches, deliberate mismatches,
straddle roles, verbose and terse JDs, and four adversarial cases carrying
injected instructions.

**Labelling is by the operator, once, and revised only with a recorded reason.**
A golden set quietly adjusted to match current behaviour measures nothing. If a
prompt change makes a case fail and the label looks wrong, that is a separate
commit with its own justification — never bundled with the change it excuses.

### 9.5 A model-ID change is a prompt change

Same procedure, same gate, same lockfile entry. Never an auto-upgrade, never a
`-latest` alias. An artifact whose model cannot be named violates invariant 7
(`AI_ARCHITECTURE.md` §4.1, §12).

### 9.6 Rules for the text itself

- The instruction precedes the data; the schema description is repeated after it.
- Untrusted content appears **exactly once**, in one envelope, in one message.
- `user_template` interpolates **named, code-supplied fields only**. Untrusted
  text is never interpolated — it is enveloped by `llm/guard.py`.
- **No tools are exposed to any prompt** except the schema-emitting `emit` tool.
  There is no fetch tool, no email tool, no SQL tool. An injected instruction to
  fetch a URL has literally no mechanism to invoke, which is a stronger property
  than a refusal.
- Every prompt taking untrusted input carries the standing data-boundary clause
  verbatim (`AI_ARCHITECTURE.md` §7.3). Do not paraphrase it per family.

---

## 10. Adding a claim to the ledger

### 10.1 The discipline

**Write the claim before writing the bullet.** A number that arrives because a
sentence needed it is the number most likely to be wrong. No validator can
enforce this; it is the whole discipline.

**State the evidence reference before the statement.** If the reference cannot be
written, the claim is not ready.

### 10.2 Procedure

```http
POST /api/v1/claims
```

```jsonc
{
  "key": "khelo.eval_bank_cases",
  "statement": "The evaluation bank holds 213 cases.",
  "metric_value": "213",
  "metric_unit": "cases",
  "project": "Khelo India Assistant",
  "evidence_ref": "repo:khelo-assistant@7f31ac0 · evals/bank.jsonl · wc -l",
  "confidentiality": "public",
  "tags": ["llm-eval"],
  "verified_at": "2026-08-31T00:00:00Z"
}
```

Or in `seeds/claims.yaml` for a seeded row, which is where anything the six
variants cite belongs.

### 10.3 What is rejected, and why each rule exists

422 when (`CLAIMS_LEDGER.md` §9.1):

| Rejection | Reason |
|---|---|
| `key` fails `^[a-z0-9]+(?:_[a-z0-9]+)*\.[a-z0-9]+(?:_[a-z0-9]+)*$` | `project.fact` keys are what make the table scannable |
| Project slug outside the fixed set | Stops a typo creating a parallel project |
| `metric_unit` outside the closed vocabulary | Unit separation is what lets "top 15 percentile" resolve while "15% faster" does not |
| `evidence_ref` under 12 chars, or `n/a` / `tbd` / `see repo` | An unverifiable claim is not a claim |
| `verified_at` in the future | |
| `metric_value` present without `metric_unit` | A bare number cannot be checked for unit agreement |

### 10.4 Writing a good claim

**Statement.** Canonical phrasing, in the operator's voice, defensible verbatim
in an interview. Not marketing.

```yaml
# wrong — unbounded, unverifiable, reads as flattery
statement: "Dramatically reduced infrastructure costs through expert optimisation."

# right — bounded, attributable, checkable
statement: >-
  The Khelo India Assistant's monthly run-rate was reduced by about 60 percent
  after a serving-path and model-routing rebuild.
```

**Evidence reference.** Where it was verified from, precisely enough to re-run.

```yaml
evidence_ref: "aws:cost-explorer ap-south-1, 2026-02 baseline vs 2026-07 actual"
evidence_ref: "repo:pq-panel@c19be4d · pytest --collect-only -q | tail -1"
evidence_ref: "openforge:pq-panel · git shortlog -sn · single author across 33 migrations"
```

**Confidentiality.** `public` unless there is a reason. `restricted` means the
claim is emitted only for employers on
`LEDGER_RESTRICTED_DISCLOSURE_COMPANIES` — absolute rupee figures are the
canonical case. **Pair every restricted claim with a public sibling** in
`ledger/pairs.yaml` so the generator degrades gracefully rather than failing:

```yaml
# ledger/pairs.yaml
- restricted: khelo.cost_reduction_run_rate_inr    # "₹9.4L → ₹3.5L"
  public:     khelo.cost_reduction_pct             # "about 60 percent"
```

**Expiry.** `expires_at = null` for frozen facts ("sole engineer on X"). For
anything that drifts — a test count, a corpus size, a user count — set the
default 180-day TTL. Facts are true on a date.

**Superlatives need a row.** "Sole", "first", "only", "largest" are exactly the
class of word the validator detects and refuses unless it resolves. A superlative
claim is legitimate; it needs a statement and an evidence reference like any
other.

### 10.5 Changing a claim

Covered operationally in `OPERATIONS_MAINTENANCE.md` §4.2. The rule to carry into
code: **a changed number is a new claim, not an edit.** `claim_usage` references
`claim.id`, not `claim.key`, so a resume sent in March still explains itself
against the March row. Retire the old key with a date suffix and soft-delete it;
insert the new fact under the canonical key. One transaction.

This is the same reasoning that makes `application_event` append-only: the log is
the truth, and the current value is a projection of it.

### 10.6 Never loosen the check

When generation fails validation, there are exactly two correct actions
(`CLAIMS_LEDGER.md` §9.4):

- The fact is true and the ledger lacks it → **add the claim**.
- The generator invented it → **a prompt version bump**.

There is no third. There is deliberately no bypass endpoint (`API.md` §8), and
`LEDGER_EXPIRED_CLAIM_POLICY=warn` is not the answer to an expired claim. Any
merge request that widens a detection regex, lowers
`LEDGER_APPROXIMATION_TOLERANCE`, or adds a validation exemption is rejected on
sight unless it is fixing a demonstrated false positive with a test.

---

## 11. Debugging recipes

### 11.1 Replay a single source fetch

Offline, against the committed fixture — no network, no database:

```bash
python -m scout_careers.sources.replay \
  --adapter greenhouse \
  --fixture tests/fixtures/sources/greenhouse/list_page_1.json \
  --config '{"board_token": "stripe"}' \
  --show normalised
```

```
47 postings

[0] external_id=4019283  title="Staff Engineer, Payments"
    location_raw="San Francisco, CA"  → city=San Francisco country=US remote=False
    seniority_guess=staff  employment_type=full_time
    posted_at=2026-08-28T00:00:00Z
    description_text: 4,812 chars  content_hash=9f2c…d41b
```

Against the live endpoint, still without persisting:

```bash
python -m scout_careers.sources.replay \
  --adapter greenhouse --config '{"board_token": "stripe"}' \
  --live --limit 3 --show raw
```

`--live` honours the rate-limit bucket, robots and the never-scrape list, exactly
as a run does. There is no bypass flag.

When the vendor changed shape, capture and diff:

```bash
curl -sS 'https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true' \
  > /tmp/live.json
python -m scout_careers.sources.replay --adapter greenhouse \
  --fixture /tmp/live.json --config '{"board_token":"stripe"}' --show errors

diff <(jq -S 'paths(scalars) | join(".")' tests/fixtures/sources/greenhouse/list_page_1.json | sort -u) \
     <(jq -S 'paths(scalars) | join(".")' /tmp/live.json | sort -u)
```

The path diff is the fastest read on schema drift: added paths are new fields
(harmless), removed paths are what broke the response model.

### 11.2 Re-score one posting

```bash
# Re-run scoring only, against the requirements already extracted (free)
scout-careers score --posting-id 01JB7K… --explain

# Force re-extraction too — costs tokens
scout-careers score --posting-id 01JB7K… --re-extract

# Score against one variant, showing the coverage decision per requirement
scout-careers score --posting-id 01JB7K… --variant consulting --explain
```

```
Posting  01JB7K…  "Analyst II, Financial Modeling & AI"  Seagate (strong)
Variant  consulting  (skill_set: 34 tokens)

HARD  (7)
  ✓ met      Financial modelling and forecasting
             ← requirement.normalised_skill=financial_modelling ∈ skill_set
             ← evidence: bullet "Built a ₹924 Cr variance model…" claims [38,39]
  ~ partial  SQL and data manipulation
             ← adjacency sql↔postgres = 0.71 ≥ SKILL_ADJACENCY_MIN (0.40)
  ✗ missing  Advanced Excel model building
             ← no vocabulary match; no ledger evidence of Excel modelling
  …
  4/7 met, 1 partial → weighted hard coverage 0.607

NICE  (6)   5/6 met
coverage_pct     47.50
hard gate G      0.85   (band 0.40–0.60)
tier T           1.00   (strong)
recency R        0.93   (posted 21d, grace 14, half-life 45)
composite_score  38.42
is_recommended   True   (best of 6 variants; next: combined 31.10)
```

`--explain` is where a scoring bug becomes visible. Every level has its reason on
the line below it, so "why is this missing" never requires reading `scoring/`.

Via the API, which also invalidates and rescores downstream:

```bash
curl -sS -X POST "http://localhost:8000/api/v1/postings/01JB7K…/rescore"   # 202
```

### 11.3 Dry-run generation with the deterministic stub

The most useful tool in this section. `LLM_PROVIDER=stub` swaps in a provider
that returns **deterministic, schema-valid, ledger-cited** responses from
fixtures, keyed by prompt family and a hash of the input. Zero tokens, zero
network, identical output every run.

```bash
LLM_PROVIDER=stub scout-careers generate --review-item 01JC9M… --dry-run
```

```
Provider: stub (deterministic)   Cost: ₹0.00   Calls: 2 (0 network)

TAILORING PLAN  (tailoring_plan@2026-09-01.2)
  reorder_blocks   expenditure_tracker · khelo_assistant · pmis
  bullet_swaps     summary: "…" → "…"                        claims [12, 31]
                   experience.1.bullet.2: "…" → "…"          claims [38]
  skills_line      Data & Tooling  +Excel

COVER LETTER  (cover_letter@2026-09-01.4)   412 words, 4 paragraphs
  gap paragraph at ¶3, naming: Advanced Excel, Power BI
  similarity vs last 20 letters: max 0.31  (warn 0.55, block 0.72)

VALIDATION
  6 assertions, 6 resolved, 0 unresolved   → passed
  claims cited: 12, 13(→public sibling 12), 31, 38, 39, 21

RENDER  (--dry-run: not written)
  resume        1 page (tight=False, 2 trims)
  cover letter  1 page
```

What the stub is for and what it is not:

| Good for | Not for |
|---|---|
| Plan-application logic, `apply_plan` op handling | Whether the model produces a good plan |
| The validation gate — including making it fail on demand | Prompt quality |
| `.docx` rendering, the fit loop, page counting | Anything the eval measures |
| Similarity checking against the letter corpus | |
| The whole review → approve → artifact path | |
| Every integration test in CI | |

**Every integration test uses the stub.** No test at any level uses a live
provider — that is what keeps the gate free and deterministic. The stub also
carries deliberate failure fixtures, so a test can assert the failure paths:

```bash
LLM_STUB_SCENARIO=uncited_number   scout-careers generate --review-item 01JC9M… --dry-run
# → validation failed: "40 engineers" does not resolve. Artifact not attached.

LLM_STUB_SCENARIO=schema_violation scout-careers generate --review-item 01JC9M… --dry-run
# → repair retry 1 … repair retry 2 … needs_manual_review

LLM_STUB_SCENARIO=injected_jd      scout-careers score --posting-id 01JB7K…
# → injection recorded in notes; coverage unaffected; no output deviation
```

A path that has never been seen to fail has not been tested. The scenarios exist
so it has been.

### 11.4 Inspect a run end to end

```bash
RUN=$(curl -sS 'http://localhost:8000/api/v1/runs?limit=1' | jq -r '.data[0].id')

curl -sS "http://localhost:8000/api/v1/runs/$RUN" | jq '.data.stats'

# Failing sources only
curl -sS "http://localhost:8000/api/v1/runs/$RUN" \
  | jq '.data.source_results[] | select(.status != "ok" and .status != "empty")'

# Correlated logs for one source in that run
docker compose logs api --no-log-prefix \
  | jq -c "select(.run_id == \"$RUN\" and .source_id == 77)"
```

### 11.5 Reproduce a validation failure as a test

The right response to any ledger validation failure, before fixing it:

```python
# tests/unit/ledger/test_validation_regressions.py
@pytest.mark.parametrize(
    ("text", "expect_passed", "unresolved"),
    [
        # Regression: SC-58. "14 centres" had no ledger row and slipped through
        # because the numeral-plus-noun family did not match a bare cardinal.
        ("cut run-rate ~60% (₹9.4L → ₹3.5L per month) across 14 centres",
         False, ["14 centres"]),
        # Control: the same sentence without the uncited span must pass.
        ("cut run-rate ~60% (₹9.4L → ₹3.5L per month)", True, []),
    ],
)
async def test_validation_regression(validator, text, expect_passed, unresolved):
    result = await validator.validate(text)
    assert result.passed is expect_passed
    assert [a.span for a in result.assertions if not a.resolved] == unresolved
```

The control case matters as much as the failing one. A widened regex that catches
the bug and also rejects every legitimate sentence is a worse outcome than the
bug.

---

## 12. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | Invariants, module structure, layering rule, scale envelope |
| `INSTALLATION_GUIDE.md` | Getting the loop running; provider setup |
| `OPERATIONS_MAINTENANCE.md` | Running it; health thresholds; the maintenance cadence |
| `DATA_MODEL.md` | §11 migration policy — the authority for §8 here |
| `API.md` | Envelope, error codes, the generated-client rule |
| `SOURCE_ADAPTERS.md` | §12 the adapter checklist — the authority for §7 here |
| `AI_ARCHITECTURE.md` | §5.1 prompt registry, §10 eval, §12 flags — the authority for §9 |
| `CLAIMS_LEDGER.md` | §9 ledger maintenance — the authority for §10 here |
| `MATCH_SCORING.md` | §12 evaluating the scorer, §13 settings |
| `DOCUMENT_GENERATION.md` | §13 settings, §14 rollout discipline |
| `SECURITY_ARCHITECTURE.md` | Threat model, secret handling, what review looks for |
| `DATA_SOURCES_AND_COMPLIANCE.md` | Legal basis per source; where §7 step 1 is recorded |
