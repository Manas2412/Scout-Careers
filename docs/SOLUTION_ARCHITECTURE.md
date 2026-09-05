# SOLUTION ARCHITECTURE — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** derived. `ARCHITECTURE.md` is canonical for the invariants, module
boundaries and the scale envelope; `DATA_MODEL.md` for schema; `API.md` for
endpoint contracts; the domain documents for their own subjects. This file is
canonical for nothing except the ADR index (§12), the technical-debt register
(§13) and the evolution path (§14), which exist nowhere else. Where it appears
to contradict a canonical document, this file is wrong.

`HLD.md` describes the system component by component. This document describes the
same system capability by capability, integration by integration, and lifecycle
by lifecycle — the view needed to decide whether the design holds together, not
what each part does.

---

## 1. How to read this document

Five views, in the order an architect needs them.

| § | View | Question it answers |
|---|---|---|
| 2 | Business capability map | What does the system *do*, in the operator's language, and what implements each part |
| 3–5 | Logical, application, technology layers | What are the conceptual pieces, what are the deployable pieces, what are they built on |
| 6 | Integration architecture | What does the system talk to, how, and what happens when each one fails |
| 7 | State and data lifecycle | Where does every entity come from, how does it change, when does it die |
| 8 | Sequence views | What actually happens, step by step, for the four flows that matter |
| 9 | Deployment | What runs where, and what the alternative topology is |
| 10–11 | Cross-cutting | Configuration, logging, errors, idempotency, time, feature flags; and the security posture that sits across all of it |
| 12–14 | Decisions, debt, evolution | What was decided and why, what was knowingly deferred, what would have to change |

---

## 2. Business capability map

Five capabilities. Every module in `ARCHITECTURE.md` §5 serves exactly one of
them as its primary purpose; the map is a partition, not an overlay.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            SCOUT CAREERS                                     │
├──────────────┬──────────────┬──────────────┬──────────────┬─────────────────┤
│  DISCOVER    │   QUALIFY    │   PREPARE    │    TRACK     │      LEARN      │
│              │              │              │              │                 │
│ Find every   │ Decide which │ Draft what   │ Know where   │ Know what       │
│ role that    │ roles are    │ would be     │ every        │ actually        │
│ might matter │ worth an     │ submitted,   │ application  │ works, from     │
│              │ evening      │ truthfully   │ stands       │ observed data   │
├──────────────┼──────────────┼──────────────┼──────────────┼─────────────────┤
│ registry/    │ extract/     │ generate/    │ tracking/    │ tracking/       │
│ sources/     │ scoring/     │ ledger/      │ mail/ (read) │   (metrics)     │
│ ingest/      │              │ review/      │              │ mail/ (digest)  │
│ mail/ (alerts)│             │              │              │                 │
├──────────────┼──────────────┼──────────────┼──────────────┼─────────────────┤
│ company      │ requirement  │ claim        │ application  │ v_funnel        │
│ source       │ match_score  │ claim_usage  │ application_ │ v_ghosted       │
│ job_posting  │ resume_      │ review_item  │   event      │ run_log         │
│ run_log      │   variant    │ artifact     │ email_message│                 │
└──────────────┴──────────────┴──────────────┴──────────────┴─────────────────┘
       ①②③④          ⑤⑥⑦            ⑧⑨⑩         post-gate-2        ⑪ + weekly
```

### 2.1 Capability detail

| Capability | Business outcome | Components | Entities owned | Success measure |
|---|---|---|---|---|
| **Discover** | Every relevant role appears within 24 hours of being posted, without the operator visiting a career site | `registry/` (which employers, which boards), `sources/` (how to read each ATS), `ingest/` (identity, dedup, filtering), `mail/` alert branch (what cannot be fetched) | `company`, `source`, `job_posting`, `run_log` | Sources healthy; ~150 new postings/run; zero postings lost to a silent adapter failure |
| **Qualify** | The operator's attention goes to the thirty roles a day that could work, ranked, with the reasoning visible | `extract/` (JD → typed requirements), `scoring/` (coverage, composite, gaps) | `requirement`, `match_score`, `resume_variant` | Precision@10 ≥ 0.5 on approvals; zero grounding violations; every score reconstructible by hand |
| **Prepare** | A tailored resume plan and a truthful cover letter exist for the roles worth applying to, and nothing untrue can reach a document | `generate/` (plan, letter, render), `ledger/` (claims, validation, provenance), `review/` (the queue and the approval machine) | `claim`, `claim_usage`, `review_item`, `artifact` | Zero attachable artifacts with an unresolved assertion; ≤ 10 drafts/day; one page, verified |
| **Track** | The operator always knows the state of every application without maintaining a spreadsheet by hand | `tracking/` (state machine, event log), `mail/` reply branch (classification, linkage) | `application`, `application_event`, `email_message` | Status changes detected from mail within 30 minutes; the event log is append-only and complete |
| **Learn** | After forty applications the operator knows which variant and which tier convert, and what to fix | `tracking/` (funnel, follow-up prompts, export), `mail/` (the digest) | `v_funnel`, `v_ghosted`, the `.xlsx` workbook | Rates reported only above the minimum sample size; referrals segmented out; the number is observed, never predicted |

### 2.2 What the map makes visible

Three things worth naming.

**Prepare is where the invariants concentrate.** Two of the four hard invariants
(no auto-submit, ledger-only citation) and the entirety of the confidentiality
model sit in this one capability. That is why it carries three modules for what
looks like one job: `generate/` may compose, `ledger/` may permit, and `review/`
may attach — and no module holds two of those powers.

**Discover is the only capability with an external failure surface.** Everything
it touches is untrusted and unreliable; everything downstream operates on rows
already in the database. This is why per-source isolation is an invariant rather
than an implementation detail, and why the entire fetch layer is forbidden from
touching the database.

**Learn produces no automation.** Its outputs are read by a human and feed exactly
one machine decision — tie-break rule 5 in variant selection, gated at twenty
submissions for that variant. Everything else it produces is a report. That
restraint is the same decision as "no selection probability", applied to the one
dataset the system genuinely owns.

---

## 3. Logical architecture

Layers, top to bottom, with a strict dependency direction: a layer may depend on
the layer below it and never on the one above.

```
┌────────────────────────────────────────────────────────────────────────┐
│  PRESENTATION                                                           │
│  React SPA (Dashboard · Queue · Jobs · Companies · Applications ·       │
│  Claims · Variants · Settings)  ·  Daily digest email  ·  .xlsx export  │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │  typed client generated from OpenAPI 3.1
┌──────────────────────────────▼─────────────────────────────────────────┐
│  API — thin                                                             │
│  FastAPI routers · {data, message, meta} envelope · cursor pagination · │
│  stable error codes · Idempotency-Key handling · dependency wiring      │
│  NO business logic. NO submit endpoint. NO recipient parameter.         │
└──────────────────────────────┬─────────────────────────────────────────┘
┌──────────────────────────────▼─────────────────────────────────────────┐
│  DOMAIN SERVICES — thick                                                │
│  registry · ingest · extract · scoring · ledger · generate · review ·   │
│  tracking · mail                                                        │
│  All policy lives here: filtering, weights, coverage, citation,         │
│  transition legality, retention                                         │
└───────────┬──────────────────────────────────┬─────────────────────────┘
            │                                  │
┌───────────▼──────────────┐      ┌────────────▼────────────────────────┐
│  INTEGRATION             │      │  PERSISTENCE                         │
│  sources/ (11 adapters   │      │  db/ — SQLAlchemy 2.0 async models,  │
│  + shared HTTP client)   │      │  Alembic migrations, session mgmt    │
│  llm/ (provider-agnostic)│      │  Views: v_funnel, v_ghosted          │
│  mail/gmail client       │      │  Artifact files on a volume          │
└───────────┬──────────────┘      └────────────┬────────────────────────┘
            │                                  │
┌───────────▼──────────────────────────────────▼─────────────────────────┐
│  PLATFORM                                                               │
│  common/ — Settings, structlog, ULID, hashing, UTC/IST, TokenStore      │
│  scheduler/ — APScheduler (Postgres job store) + Redis run lock         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Two rules make this hold.** No HTTP concern appears below `api/` — a domain
service raises `AdapterConfigError`, not `HTTPException`, and the API layer maps
it to 422. And `sources/` never opens a database session: it returns DTOs and
`ingest/` persists them, which is what allows every adapter to be tested against
a recorded fixture with no database, no Redis and no network.

---

## 4. Application architecture

### 4.1 Deployable units

| Unit | Contents | Process model | Why not split further |
|---|---|---|---|
| **`scout-api`** | FastAPI app, all domain services, all adapters, the LLM client, and the APScheduler instance | One uvicorn process; pipeline work runs as `asyncio` tasks inside it, bounded by semaphores | Splitting the scheduler into its own container buys nothing at one user and doubles the deploy surface. The seam exists — the scheduler is a separate module reading the same `Settings` — so the split is a compose-file change if a run ever competes with the UI |
| **`scout-web`** | Built static assets, served by the reverse proxy | Static | — |
| **`postgres`** | Postgres 16 with `pg_trgm` | Single instance | §13.3 |
| **`redis`** | Redis 7 | Single instance, no persistence required | Every Redis value is a cache, a lease or a lock, and each is rebuildable from Postgres |
| **`proxy`** | Caddy or nginx: TLS, static serving, `/api/*` reverse proxy | Single instance | — |

### 4.2 Internal invocation paths

There are exactly three ways work starts.

| Trigger | Path | Concurrency control |
|---|---|---|
| **Scheduled** — 08:00 discovery, 08:10 + half-hourly mail, 23:30 export, Sunday 04:00 prune | APScheduler job → service entry point | Redis run lock per `run_type`; a second discovery run returns 409 rather than queueing |
| **Interactive** — every UI action | Router → service → repository | None needed beyond the database |
| **Accepted-async** — `POST /runs/discovery`, `POST /review/{id}/generate`, `POST /postings/{id}/rescore`, `POST /exports/spreadsheet` | Router returns 202 with a run or job reference; work continues in a background task | `Idempotency-Key` on the first two: a repeated key within 24 hours returns the original response rather than starting a second run |

`POST /postings/import` is the deliberate exception: it runs extract → score →
generate **synchronously** and returns the resulting `review_item`, because the
operator pasted a URL and is waiting. It is exempt from the daily generation cap
but not from the composite floor.

### 4.3 The frontend

Eight routes, one job each. The client is generated from the OpenAPI schema —
hand-written request types are a review failure, because the envelope and the
error codes are the contract and duplicating them by hand is how the two drift.

| Route | Purpose | Primary endpoints |
|---|---|---|
| Dashboard | Today's state in one screen: queue depth, recent status changes, run health, budget | `/runs`, `/metrics/funnel`, `/health` |
| Queue | The ten-minute loop. Gap list first, plan as a diff, artifacts last | `/review`, `/review/{id}`, `/approve`, `/skip`, `/plan` |
| Jobs | Everything discovered, filtered and searched, including below-floor postings | `/postings` |
| Companies | The registry: add by URL paste, tier, tags, defaults, source health | `/companies`, `/companies/detect`, `/sources/{id}/test` |
| Applications | Pipeline state, event history, manual events | `/applications`, `/applications/{id}/events` |
| Claims | The ledger: add, verify, deprecate, see usage | `/claims`, `/claims/{id}/usage`, `/claims/validate` |
| Variants | The six resume variants and their skill sets | `/variants`, `/variants/{id}/render` |
| Settings | Feature flags, thresholds, source health table, Gmail auth state | `/settings`, `/health` |

---

## 5. Technology architecture

| Concern | Choice | Version | Rationale |
|---|---|---|---|
| Language | Python | 3.12 | Strongest ecosystem for parsing, document generation and LLM tooling; matches the operator's deepest production experience |
| Web framework | FastAPI | 0.115 | Async, Pydantic-native, OpenAPI 3.1 for free — and the generated schema is what the frontend client is built from |
| Validation | Pydantic | v2 | One model family shared by the API boundary, adapter config, LLM structured output and `Settings` |
| ORM / migrations | SQLAlchemy async + Alembic | 2.0 | Typed, async, and migration discipline enforced (one revision per schema change, ID ≤ 32 chars) |
| Database | PostgreSQL | 16 | JSONB for adapter config and raw payloads, `tsvector` for posting search, `pg_trgm` for fuzzy company and alias matching, native enums, generated columns, partial indexes, triggers for the artifact-attachment invariant |
| Cache / coordination | Redis | 7 | Rate-limit token buckets (Lua, atomic), run locks, robots cache, Gmail cursor cache, Gmail refresh lock |
| Scheduler | APScheduler | — | In-process, Postgres job store; survives restart, which is the only durability property needed |
| HTTP client | httpx | — | One `AsyncClient` per run, HTTP/2, `trust_env=False` so an ambient proxy cannot silently reroute traffic |
| LLM | AWS Bedrock (default) / Azure OpenAI (alternate) | — | Both provisioned; one interface, one config key, no lock-in. Model IDs pinned, never `-latest` |
| HTML parsing | selectolax | — | Fast, no JS execution — which is the point, not a limitation |
| Documents | python-docx + LibreOffice headless | — | The same toolchain that produced the resume set; LibreOffice only to verify page count by rendering |
| Spreadsheet | openpyxl | — | The nightly workbook |
| Browser automation | Playwright | — | Present for the narrow cases where no API exists. **Never used for discovery, detection or submission** |
| Frontend | React + Vite + TypeScript + TanStack Query | 18 / 5 / 5.6 | Matches existing frontend experience; strict mode, no unjustified `any` |
| Logging | structlog | — | One structured line per pipeline stage per run, correlated by `run_id` |
| Runtime | Docker Compose on one VM | — | ECS Fargate documented as the alternative (§9.3) |

---

## 6. Integration architecture

### 6.1 Every external system

| System | Protocol | Auth | Direction | Rate limit (self-imposed) | Failure mode | Recovery |
|---|---|---|---|---|---|---|
| **Greenhouse** `boards-api.greenhouse.io` | HTTPS GET, JSON | None | Out (read) | 5 req/s, burst 10, shared bucket | 404 = board moved (no retry); 5xx retried 3× with full jitter | Auto-disable at 5 consecutive daily failures; human re-enables after fixing config |
| **Lever** `api.lever.co` | HTTPS GET, JSON | None | Out (read) | 5 req/s, burst 10, shared | As above | As above |
| **Ashby** `api.ashbyhq.com` | HTTPS GET, JSON | None | Out (read) | 4 req/s, burst 8, shared | As above | As above |
| **Workday** `{tenant}.wdN.myworkdayjobs.com` | HTTPS POST (search) + GET (detail), JSON | None | Out (read) | **1 req/s, burst 3, per tenant host** — the detail fan-out is the load | Detail 404 = requisition closed between list and detail; counted `skipped_gone`, not an error | Per-tenant in-run circuit breaker after 5 consecutive failures |
| **SmartRecruiters** `api.smartrecruiters.com` | HTTPS GET, JSON | None | Out (read) | 3 req/s, burst 6, shared | List carries no description, so every posting costs a second request | As Greenhouse |
| **Workable** `apply.workable.com` | HTTPS POST (cursor), JSON | None | Out (read) | 3 req/s, burst 6, shared | Cursor token invalid ⇒ restart the page walk | As Greenhouse |
| **Recruitee** `{company}.recruitee.com` | HTTPS GET, JSON | None | Out (read) | 2 req/s, burst 4, per tenant | As Greenhouse | As Greenhouse |
| **Google Careers** `careers.google.com` | HTTPS GET, JSON | None | Out (read) | 1 req/s, burst 2 | Query-narrowed; a bad query returns nothing rather than erroring | As Greenhouse |
| **Amazon Jobs** `www.amazon.jobs` | HTTPS GET, JSON | None | Out (read) | 1 req/s, burst 2 | As above | As above |
| **Microsoft Careers** `gcsservices.careers.microsoft.com` | HTTPS GET, JSON | None | Out (read) | 1 req/s, burst 2 | Requires a detail fetch | As above |
| **Employer careers hosts** (detection, redirects, robots.txt) | HTTPS GET | None | Out (read) | One probe ≤ 10 s; one redirect chain ≤ 3 hops; body scan capped at 512 KB, no JS | `reachable: false` returned to the UI rather than an error; unreachable robots fails the source closed except on six documented public API hosts | Operator pastes a different URL, or adds a `manual` source |
| **Gmail API — read** | HTTPS, google-api-python-client | OAuth 2.0, `gmail.readonly` | In (read) | Self-capped 20 units/s against a 250/s ceiling; ~8,300 units/day against a 1e9 quota | `invalid_grant` = permanent; the run aborts without retry and the cursor is **not** advanced | `scout-careers auth gmail` + one browser consent |
| **Gmail API — send** | HTTPS | OAuth 2.0, `gmail.send` | Out (send) | 1 message/day | Send failure ⇒ digest written to `exports/digest-YYYY-MM-DD.html` and shown in-app | Next day's digest carries the rolled-forward window |
| **AWS Bedrock** | HTTPS, boto3 | IAM role or environment credentials | Out | `LLM_MAX_CONCURRENCY = 4`; daily ₹80 budget circuit breaker | Throttling retried with backoff; hard failure leaves postings unscored, never partially scored | Next run retries; the item is visible and flagged meanwhile |
| **Azure OpenAI** | HTTPS | API key from environment | Out | As Bedrock | As Bedrock | Provider swap is one configuration key |

### 6.2 Cross-cutting integration controls

Applied to every outbound call without exception, in this order:

```
   caller
     │
     ▼
  ① NEVER-SCRAPE GATE      host ∈ NEVER_FETCH_HOSTS (frozen constant) ⇒ refuse
     │                     re-evaluated after EVERY redirect hop
     ▼
  ② ROBOTS CHECK           cached 24 h per host; Crawl-delay may only LOWER the
     │                     bucket rate; Disallow disables the source
     ▼
  ③ RATE-LIMIT LEASE       Redis Lua token bucket keyed on the rate-limiting
     │                     DOMAIN, not the source id; deadline 20 s then
     │                     RateLimitTimeout (does NOT count toward auto-disable)
     ▼
  ④ CIRCUIT BREAKER        in-run, per bucket key, opens after 5 consecutive
     │                     transport/5xx failures; remaining sources on that key
     │                     short-circuit without a request
     ▼
  ⑤ REQUEST                honest UA with a contact address, identical for every
     │                     source, never a browser string; trust_env=False
     ▼
  ⑥ RETRY                  4 attempts max, full jitter, Retry-After always wins,
     │                     4xx other than 408/425/429 never retried,
     │                     90 s per-source retry budget
     ▼
  ⑦ RESPONSE HANDLING      size cap; body never logged; log line is
                           {source_id, adapter, url_template, status,
                            duration_ms, item_count} — the template, not the
                            interpolated URL, so board tokens do not leak
```

**Two timescales of breaker.** The in-run breaker is memory-resident and stops
one down tenant from consuming forty sources' worth of retry budget. The
cross-run breaker is `source.consecutive_failures` in Postgres, auto-disabling at
five and never re-enabling itself, because five consecutive daily failures almost
always means the board moved and needs a new config rather than that the network
was unlucky five times.

**SSRF containment.** Every adapter config field that is interpolated into a URL
is constrained by a regex in its Pydantic model — `WorkdayConfig.host` must match
`^[a-z0-9.-]+\.myworkdayjobs\.com$` — so a config value cannot point the client
at an arbitrary host. The allow-list backstops that, and the never-scrape gate
backstops both.

**Untrusted data never becomes instruction.** Job descriptions and email bodies
are attacker-controllable. They are never executed, never concatenated into SQL,
and never placed in an LLM instruction region — they arrive inside a
nonce-delimited data block, with the system prompt stating that the block is a
third-party document that cannot issue instructions, and a document that already
contains the delimiter form is refused outright and logged.

---

## 7. State and data lifecycle

### 7.1 Entity lifecycle table

| Entity | Created by | Mutated by | Terminal state | Retention |
|---|---|---|---|---|
| `company` | Operator, via detect → confirm, or CSV import | Operator only (tier, tags, defaults, status) | `status = 'blacklisted'` or soft-deleted | Soft delete; never hard-deleted |
| `source` | Operator confirming a probed detection | Runner (`last_run_at`, `last_status`, `consecutive_failures`, auto-disable) | `enabled = false` | Disabled, never deleted — deleting would orphan postings and lose the history of what was tried |
| `job_posting` | `ingest/` on first sight | `ingest/` on re-fetch (`last_seen_at` always; row + score invalidation when `content_hash` changes); `closed_at` after two consecutive misses | `closed_at` set | 90 d after close if never scored; 180 d if scored but never queued; **forever** if referenced by a `review_item` or `application`. `raw` and `description_html` nulled at 30 d |
| `requirement` | `extract/` | Never — a new extraction writes a new row family under a new prompt version | — | Life of the posting (cascade) |
| `resume_variant` | Seed (six variants) | Operator | Soft delete | Forever; soft delete only |
| `claim` | Operator | Operator (re-verify updates `verified_at`; deprecate sets `deleted_at`) | Soft-deleted or expired | **Forever.** Provenance is never orphaned |
| `claim_usage` | `ledger/` on a resolved assertion | Never — append only | — | Forever |
| `match_score` | `scoring/` | Never — rescoring inserts under a new `prompt_version` | Superseded | Latest 2 prompt versions per posting |
| `review_item` | `ingest/` stage ⑩, or `POST /postings/import` | Operator (plan edits, approve, skip); system (artifact attachment, `needs_manual_review`) | `approved`, `skipped` — both terminal | **Forever.** Skip decisions are the only record of what the operator declined and why |
| `artifact` | `generate/` (`passed`/`failed`), seed and import paths (`bypassed`) | Frozen on approval | — | Forever if attached to an application; 90 d file retention if only on a skipped item (row kept, `path` nulled); 30 d if `failed` |
| `application` | `POST /review/{id}/approve`, in the same transaction as the approval | Status materialised by trigger from `application_event`; operator may PATCH `submitted_at`, notes, channel | `rejected`, `withdrawn`, `offer` | **Forever** |
| `application_event` | Mail classification, or operator manual entry (`is_manual = true`) | **Never.** Append-only, no update, no delete | — | **Forever.** The entire evidential basis of every metric |
| `email_message` | Mail poll | `classified_as`, `confidence`, `application_id`, `processed_at` set once | Processed | 24 months — **except** rows referenced by an `application_event`, which are kept forever so the audit trail cannot break |
| `run_log` | Every scheduled or manual run | `finished_at`, `stats`, `source_results` on completion | Completed | 12 months; `source_results` reset to `[]` at 90 days |

### 7.2 Two lifecycle rules that override the table

1. **Nothing referenced by an `application` or `application_event` is ever
   pruned.** The prune job is written as `DELETE … WHERE NOT EXISTS (…)` and
   every statement has a test that inserts a referenced row and asserts it
   survives.
2. **Pruning is never cascading-by-accident.** `job_posting` deletion cascades to
   `requirement` and `match_score` by design, and the prune predicate guarantees
   such a posting has no application. `ON DELETE CASCADE` is never the safety
   mechanism; the `WHERE` clause is.

### 7.3 What is deliberately not stored

| Not stored | Why |
|---|---|
| Email bodies | Classification runs in memory; only the class, the confidence and a short excerpt on the event row persist. Keeps the blast radius of a database leak small and avoids retaining correspondence the operator did not choose to keep |
| Recruiter names, personal emails, phone numbers from vendor metadata | Invariant 2 means they will never be contacted, so the system has no use for them. Stripped before `raw` is persisted |
| Cookies, `Authorization` headers, CSRF or session values | Categorically excluded from `raw` and from every log line |
| A predicted selection probability | Not computable (`MATCH_SCORING.md` §7); a displayed number would become the thing the operator optimises |
| `ghosted` as a status | Absence of evidence is not evidence. It is a view over `application_event`, so an event arriving on day 45 removes the application from it with no compensating write |
| A "safe" copy of the resume with sensitive figures removed | Confidentiality gates emission per application, not per file. Two files drift and the wrong one gets attached |

### 7.4 State transitions at a glance

```
job_posting        discovered ──▶ filtered_out ──▶ (visible, terminal)
                        │
                        └──▶ scored ──▶ below floor ──▶ (visible, no queue entry)
                                   └──▶ above floor ──▶ review_item

review_item        pending_review ──▶ needs_manual_review   (generation/validation failed)
                        │
                        ├──▶ skipped     (terminal, never generates)
                        └──▶ approved    (terminal) ──┐
                                                       │ same transaction
application                             submitted ◀────┘
                        ├──▶ acknowledged ──▶ screening ──▶ interview ──▶ offer
                        ├──▶ rejected     (from any live state)
                        └──▶ withdrawn    (operator only)

                   v_ghosted: submitted|acknowledged with no event for
                              GHOST_AFTER_DAYS — computed, never written
```

`drafted` is not a stored status. It is `pending_review` plus the presence of
validated artifacts. Adding an enum value for it would create a state the
operator cannot act on and a second place for the artifact-attachment invariant
to be violated.

---

## 8. Sequence views

### 8.1 Daily discovery run

```
Scheduler   Runner      Redis      Adapter    ATS        LLM        DB       Digest
    │          │          │           │        │          │          │          │
 08:00 ─────▶ start                                                              
    │          │─ acquire lock ─────▶│                                           
    │          │◀── ok (else 409) ───│                                           
    │          │─────────────────────────────────────────────────▶ run_log INSERT
    │          │                     │                             (status=running)
    │          │  ┌─ for each of ~320 sources, ≤8 concurrent ─────────────────┐
    │          │  │  parse_config (fails loudly if it rotted since saved)     │
    │          │  │─ robots (24h cache) ─▶│                                   │
    │          │  │─ rate lease ─────────▶│                                   │
    │          │  │─ breaker check ──────▶│                                   │
    │          │  │────── fetch() ───────────────▶│ HTTPS                     │
    │          │  │                              │────▶ list + detail        │
    │          │  │◀───── RawPosting[] ──────────│                            │
    │          │  │  on error: isolate, record, CONTINUE — never abort the run│
    │          │  └───────────────────────────────────────────────────────────┘
    │          │  ② normalise · ③ dedupe (identity, hash, cross-source) ──────▶ job_posting
    │          │  ④ FILTER — deterministic, no model. ~150 → ~30              
    │          │  ⑤ extract ─────────────────────────────▶│ fast alias        
    │          │     grounding check: every evidence_span verbatim in the JD  
    │          │     fail ⇒ retry once @ temp 0 ⇒ needs_manual_review         
    │          │  ◀── RequirementExtraction ──────────────│                   
    │          │  normalise skills (vocabulary lookup — no call) ────────────▶ requirement
    │          │  ⑥ score 6 variants × 30 postings — pure code ─────────────▶ match_score
    │          │  ⑦ rank; select is_recommended per posting                   
    │          │  ⑧ generate top ≤10, dream tier first ──▶│ strong alias      
    │          │  ◀── tailoring_plan + letter draft ──────│                   
    │          │  ⑨ validate every assertion against the ledger               
    │          │     unresolved ⇒ artifact FAILED ⇒ 1 regeneration ⇒ manual   
    │          │  ⑩ ──────────────────────────────────────────────────────────▶ review_item
    │          │─────────────────────────────────────────────────▶ run_log UPDATE
    │          │                                       (completed | with_errors)
    │          │─ release lock ─────▶│                                          
 08:10 ──── mail poll ───────────────────────────────────────────────────────▶
 08:15 ──── compose + send ─────────────────────────────────────────────────▶ digest
```

**The load-bearing detail** is the `CONTINUE` in the per-source loop. A run always
completes and always reports which sources failed; there is no path where one
broken adapter prevents the other 319 from being seen.

### 8.2 Add a company by pasting a URL

```
Operator      UI          API           registry/       policy      Adapter     DB
    │          │           │                │             │            │         │
 paste ──────▶│                                                                   
    │          │─ POST /companies/detect ──▶│                                     
    │          │           │                │─ ① host check ─▶│                   
    │          │           │                │   on NEVER_FETCH_HOSTS?             
    │          │           │                │◀── DENIED ──────│                   
    │          │◀───────── 403 source.denied_by_policy ───────│  (no request made,
    │          │           │                │                    no override path) │
    │          │           │                │─ ② pattern table (20 rules, in order)
    │          │           │                │   0 matches ⇒ single GET, ≤3 hops,
    │          │           │                │              policy re-checked per hop
    │          │           │                │              then re-run the table
    │          │           │                │   still 0 ⇒ body scan, 512 KB,
    │          │           │                │              selectolax, NO JS
    │          │           │                │   still 0 ⇒ 422 source.undetectable
    │          │           │                │   >1 ⇒ 200 with candidates[]
    │          │           │                │─ config_model.validate() — a captured
    │          │           │                │   value the model rejects is a NON-MATCH
    │          │           │                │─ ③ probe ──────────────▶│ one request
    │          │           │                │                         │ ≤10 s, no paging
    │          │           │                │◀─ ProbeResult ──────────│              
    │          │           │                │─ ④ dedup: existing company / source? ──▶│
    │          │◀── 200 {adapter, config, company_name_guess, probe, existing_id} ────│
 review ─────▶│                                                                       
 confirm ────▶│─ POST /companies (or /companies/{id}/sources) ──────────────────────▶│
    │          │◀── 201 ──────────────────────────────────────────────────────────────│
```

**Detection never returns a config it has not probed**, and detection creates
nothing — a pattern match is a guess about a URL shape, a probe is evidence, and
a registry that writes unprobed sources accumulates dead rows nobody notices for
a month.

### 8.3 Review and approve

```
Operator      UI            API          review/      ledger/     generate/     DB
    │          │             │              │            │            │          │
 open item ──▶│─ GET /review/{id} ─────────▶│                                     
    │          │             │              │─ posting + score + gaps + plan
    │          │             │              │  + artifact validation status ─────▶│
    │          │◀── one screen, GAP LIST RENDERED FIRST ───────────────────────────│
    │                                                                              
 ── path A: skip ──                                                                
 skip ───────▶│─ POST /review/{id}/skip ───▶│ status=skipped, decided_at, note ──▶│
    │          │◀── 200 ── terminal. Never generates. Costs one click, zero tokens.
    │                                                                              
 ── path B: edit the plan ──                                                       
 edit ───────▶│─ PATCH /review/{id}/plan ──▶│ operator correction CAPTURED, not
    │          │                            │ worked around ────────────────────▶│
 regen ──────▶│─ POST /review/{id}/generate ▶│──────────────────────▶│ strong     
    │          │                            │            │◀─ draft ──│            
    │          │                            │─ validate ▶│ resolve every assertion
    │          │                            │            │ unresolved ⇒ FAILED,
    │          │                            │            │ 1 regeneration, then
    │          │                            │            │ needs_manual_review
    │          │◀── 202 ─────────────────────│            │ (no override endpoint)
    │                                                                              
 ── path C: approve ──                                                             
 approve ────▶│─ POST /review/{id}/approve ▶│                                      
    │          │                            │  ┌── ONE TRANSACTION ──────────────┐ 
    │          │                            │  │ render .docx (page count        │ 
    │          │                            │  │   VERIFIED by rendering)        │ 
    │          │                            │  │ trigger refuses any artifact    │ 
    │          │                            │  │   with validation_status=failed │ 
    │          │                            │  │ review_item.status = approved   │ 
    │          │                            │  │ application INSERT, submitted   │ 
    │          │                            │  │ application_event INSERT        │ 
    │          │                            │  │ claim_usage rows written        │ 
    │          │                            │  └─────────────────────────────────┘ 
    │          │◀── 201 + download links ────│  ZERO outbound HTTP requests.        
    │                                                                              
 ══════════════════════════════════════════════════════════════════════════════════
   HUMAN GATE 2 — the operator opens the employer's site and submits. The system
   does not observe this and has no endpoint that could perform it.
 ══════════════════════════════════════════════════════════════════════════════════
```

### 8.4 Mail-driven status update

```
Scheduler    mail/       Redis       Gmail        LLM          DB        Digest
    │          │           │           │           │            │          │
 :10/:40 ────▶ poll                                                        
    │          │─ history_id ─▶│                                           
    │          │◀── cursor ────│  (Postgres-backed, Redis-cached)          
    │          │─ users.history.list(startHistoryId) ─▶│                    
    │          │   404 (history pruned) ⇒ bounded full sweep, cursor rebuilt,
    │          │   logged WARN and reported — expected after an outage      
    │          │◀── messagesAdded ────────────────────│                     
    │          │  for each message (concurrency 5, ≤20 quota units/s):      
    │          │─ messages.get(format=full) ─────────▶│                     
    │          │  ┌─ LINKAGE ────────────────────────────────────────────┐  
    │          │  │ 1. thread_id matches a known application thread      │  
    │          │  │ 2. sender domain → company → open application        │  
    │          │  │ 3. shared ATS domain ⇒ disambiguate or HOLD for the  │  
    │          │  │    operator (v_mail_review_queue) — never guess      │  
    │          │  └──────────────────────────────────────────────────────┘  
    │          │─ classify (body as DELIMITED DATA, never instruction) ─▶│  
    │          │◀── {class, confidence} ─────────────────────────────────│  
    │          │  below threshold ⇒ held for the operator, not applied       
    │          │─ email_message UPSERT on gmail_id (idempotent) ───────────▶│
    │          │  ┌─ TRANSITION LEGALITY ────────────────────────────────┐  
    │          │  │ may_append() rejects rejected→acknowledged and       │  
    │          │  │ screening→screening; out-of-order arrival yields the │  
    │          │  │ same materialised status as in-order arrival        │  
    │          │  └──────────────────────────────────────────────────────┘  
    │          │─ application_event INSERT (excerpt + confidence) ─────────▶│
    │          │   trigger materialises application.status                  
    │          │─ run_log UPDATE; cursor advances ONLY on success ─────────▶│
    │          │                                                            
 08:15 ───────│──────────────────────────────────────────────────────────▶ §3 of
              │  Bodies are NEVER stored. No reply is ever sent.            digest
```

---

## 9. Deployment view

### 9.1 Compose topology (default)

```
                          Internet / Tailscale
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  VM — 2 vCPU / 4 GB / 40 GB SSD                            Asia/Kolkata  │
│                                                                           │
│  ┌────────────────┐                                                       │
│  │  proxy         │  Caddy — TLS, static assets, /api/* → scout-api:8000  │
│  │  :443          │                                                       │
│  └───────┬────────┘                                                       │
│          │                                                                │
│  ┌───────▼────────────────────────────────────┐                           │
│  │  scout-api                        :8000    │                           │
│  │  uvicorn · FastAPI · all services          │                           │
│  │  APScheduler in-process (Postgres jobstore)│                           │
│  │  jobs: 08:00 discovery · 08:10 + :30 mail  │                           │
│  │        23:30 export · Sun 04:00 prune      │                           │
│  └──┬──────────────┬────────────────┬─────────┘                           │
│     │              │                │                                     │
│  ┌──▼──────────┐ ┌─▼────────────┐ ┌─▼───────────────────────────────────┐ │
│  │ postgres:16 │ │  redis:7     │ │  volumes                            │ │
│  │ pg_trgm     │ │  buckets     │ │   /var/lib/scout/artifacts  .docx   │ │
│  │ + tsvector  │ │  run locks   │ │   /var/lib/scout/exports    .xlsx   │ │
│  │             │ │  robots cache│ │   /var/lib/scout/gmail.token 0600   │ │
│  │  pgdata vol │ │  mail cursor │ │   /var/lib/scout/backups    pg_dump │ │
│  └─────────────┘ └──────────────┘ └─────────────────────────────────────┘ │
│                                                                           │
│  ┌────────────────┐   built once, served by proxy                         │
│  │  scout-web     │   React + Vite static bundle                          │
│  └────────────────┘                                                       │
└──────────────────────────────────────────────────────────────────────────┘
        │ outbound only                        │ outbound only
        ▼                                      ▼
  ATS endpoints · Gmail API            Bedrock / Azure OpenAI
```

**Sizing rationale.** Peak memory is the discovery run: 8 concurrent sources, a
shared HTTP/2 pool, and at most a few thousand `RawPosting` objects in flight —
comfortably under a gigabyte. Peak CPU is HTML-to-text conversion and the
LibreOffice page-count render, both bursty and short. Postgres holds low
single-digit gigabytes at design scale, which is why `raw` and
`description_html` are nulled at 30 days: the raw payload is a debugging aid for
adapters, not a record, and it is the largest single consumer of table size.

**Operational properties.**

| Property | Approach |
|---|---|
| Backup | `pg_dump` nightly to the backups volume, plus an off-host copy. The artifact volume is backed up weekly — a lost `.docx` is regenerable from the plan and the ledger; a lost `application_event` is not |
| Restore | Restore the dump, replay migrations to head, re-authorise Gmail. Discovery reconstructs postings on the next run |
| Secrets | Environment file outside the build context, `0600`, plus the separately-keyed encrypted Gmail token. Neither is ever in an image, a log, or a git-tracked file |
| Access | The UI is not published to the public internet — Tailscale or an IP allow-list. Single-user auth is a session cookie against an environment password, which is adequate only because the network boundary is doing the real work |
| Upgrade | `docker compose pull && up -d`; brief downtime is acceptable. Never during a run window |
| Observability | Structured logs to the host journal; `/api/v1/health` reports per-dependency status and never returns 500 for a degraded dependency, because a degraded LLM provider should not take the UI down |

### 9.2 Environments

| Environment | Purpose | Differences |
|---|---|---|
| **Local** | Development | Compose with the same images; adapters run against recorded fixtures; `RENDER_VERIFY_PAGES` may be off; a stub LLM provider returns fixture objects; Gmail is never authorised |
| **Production** | The operator's actual system | The topology above. `RENDER_VERIFY_PAGES` is on and is never off in the deployed image |

There is no staging environment, and the honest reason is that there is one user
and a bad deploy costs one morning's digest. The compensating control is that
every schema change is a reversible Alembic revision and every behavioural change
on the generation path ships behind a flag that defaults off.

### 9.3 ECS Fargate alternative

For the case where the operator stops wanting to own a VM.

```
Route 53 ─▶ ALB ─▶ ECS Fargate service "scout-api" (1 task, 0.5 vCPU / 1 GB)
                        │
                        ├─▶ RDS PostgreSQL 16, ap-south-1, private, encrypted
                        ├─▶ ElastiCache Redis (or an in-task Redis sidecar)
                        ├─▶ S3 — artifacts and exports (replaces the volume)
                        ├─▶ Secrets Manager — DB URL, LLM keys, Gmail token key
                        └─▶ CloudWatch Logs — structlog JSON

Frontend: S3 + CloudFront (replaces scout-web + proxy)
Scheduler: unchanged — APScheduler stays in the task, because EventBridge would
           split the schedule across two systems for no benefit at one task
```

| Concern | Compose | Fargate |
|---|---|---|
| Monthly cost | One small VM | Roughly 3–5× — RDS and NAT dominate |
| Artifact storage | Volume | S3; `artifact.path` becomes an S3 key, which is why it is stored as opaque text |
| Secrets | Env file + encrypted token file | Secrets Manager + KMS |
| Backup | `pg_dump` cron | RDS automated snapshots |
| Failure recovery | Restart the VM | Task replacement |
| Egress | Direct | NAT gateway — a real line item for a system that makes a few thousand outbound requests a day |

**Recommendation stands with Compose.** Fargate buys managed backups and task
replacement, and costs several times as much for a workload that is idle 23 hours
a day. The switch is worth making only if the operator's constraint changes from
money to time. Nothing in the application code differs between the two — the only
code-visible difference is that `artifact.path` addresses S3 rather than a
filesystem, and that indirection already exists.

---

## 10. Cross-cutting architecture

### 10.1 Configuration

One `Settings` object built by `pydantic-settings` from environment variables,
constructed once at startup and injected. **No magic constants in modules**, and
no module reads `os.environ` directly.

| Family | Examples | Note |
|---|---|---|
| Infrastructure | `DATABASE_URL`, `REDIS_URL`, `SCOUT_BASE_URL` | |
| Sources | `SOURCE_CONCURRENCY` (8), `SOURCE_USER_AGENT`, `SOURCE_PROBE_TIMEOUT_S` (10), `RATE_LIMIT_WAIT_S` (20), `SOURCE_RETRY_BUDGET_S` (90) | The UA carries a contact address and is configuration, not a code constant, so it is not committed |
| Scoring | `SCORING_BLEND_HARD`, `SCORING_TIER_WEIGHTS`, `SCORING_RECENCY_*`, `SCORING_HARD_GATE_BANDS`, `SKILL_ADJACENCY_MIN`, `GENERATION_MIN_COMPOSITE` | Changing any of these bumps `SCORING_PROMPT_VERSION` in the same commit |
| Generation | `GENERATION_DAILY_CAP` (10), `COVER_LETTER_*`, `RESUME_MAX_PAGES` (1), `RENDER_VERIFY_PAGES`, `SIMILARITY_WARN`/`_BLOCK` | |
| Ledger | `LEDGER_EXPIRED_CLAIM_POLICY` (`fail`), `LEDGER_RESTRICTED_DISCLOSURE_COMPANIES`, `GENERATION_VALIDATION_RETRIES` (1) | |
| LLM | `LLM_PROVIDER`, `LLM_MODEL_FAST`/`_STRONG`, `LLM_DAILY_BUDGET_INR` (80), `LLM_PRICE_*`, `LLM_INR_PER_USD` | Model IDs pinned; never a `-latest` alias |
| Mail | `MAIL_OPERATOR_ADDRESS`, `MAIL_ALERT_ADDRESS`, `MAIL_TOKEN_PATH`, `MAIL_TOKEN_KEY` | |
| Tracking | `GHOST_AFTER_DAYS` (30), `MIN_N_FOR_RATE` (15), `MIN_N_FOR_COMPARISON` (30), `FOLLOWUP_*`, `RETENTION_*`, `*_CRON` | |
| Flags | `FF_TAILORING_REPHRASE`, `FF_GAP_IN_OPENING`, `GENERATION_ENABLED`, `COVER_LETTER_ENABLED` | §10.6 |

**What is deliberately *not* configuration:** `NEVER_FETCH_HOSTS`, the coverage
levels and their credit semantics, the tier filter thresholds, the generation
ordering, the one-page resume rule, and the existence of the two human gates.
These are semantics. A setting that could change them would be a setting that
could break an invariant, and a test asserts the deny-list constant is not
reachable from `Settings`.

### 10.2 Logging

Structured, via `structlog`, one line per pipeline stage per run, correlated by
`run_id`.

| Always logged | Never logged, at any level |
|---|---|
| `run_id`, stage, duration, counts in and out | Credentials, API keys, OAuth tokens — not the value, not a prefix, not a length, not a hash |
| `{source_id, adapter, url_template, status, duration_ms, item_count}` | Interpolated URLs (board tokens would leak), response bodies |
| Model call: alias, model ID, prompt version, token usage, cost | Prompt or completion content, full JD text |
| Classification: class and confidence | Email bodies, subjects beyond a truncated excerpt, sender personal data |
| Validation: assertion count, resolved count, failure count | Full resume content, claim statements |

`url_template` rather than the URL is the specific decision that keeps a
Greenhouse board token or a Workday tenant out of a log aggregator.

### 10.3 Error handling

**Fail closed on anything touching data integrity or the compliance boundary;
fail open on enrichment.**

| Class | Posture | Examples |
|---|---|---|
| Compliance | Fail closed, no retry, no override | Denied host, robots disallow, outbound recipient mismatch, failed ledger validation |
| Data integrity | Fail closed | Run lock unobtainable, partial source output (discarded, never ingested), transition legality violation, out-of-band artifact attachment |
| Enrichment | Fail open, degraded and flagged | LLM unavailable at generation (empty plan, base variant still legitimate), page-count verification failure (offered unverified, labelled) |
| Upstream | Isolate, record, continue | Any adapter failure |

Domain services raise domain exceptions; `api/` maps them to the status codes and
stable machine codes in `API.md` §1. A domain service never raises
`HTTPException`.

### 10.4 Idempotency

| Operation | Key | Behaviour on repeat |
|---|---|---|
| Discovery run | Redis lock per `run_type` + `Idempotency-Key` header | 409 while in flight; a repeated key inside 24 h returns the original response |
| Posting ingestion | `(source_id, external_id)` | Upsert. Same hash ⇒ `last_seen_at` bump only |
| Extraction | `content_hash` cache | Unchanged posting costs zero tokens |
| Scoring | `UNIQUE (posting_id, variant_id, prompt_version)` | A new prompt version *inserts* rather than conflicting — which is exactly what makes A/B comparison possible with no shadow table |
| Mail processing | `UNIQUE (email_message.gmail_id)` | Re-processing after a crashed run is a no-op — which is what replaces the `gmail.modify` label the system does not hold |
| Event append | `(application_id, status, email_message_id)` | The same event twice creates one row |
| Approval | `review_item.status` terminal check | Approving an already-decided item returns 409 `review.already_decided` |
| Claim usage | `UNIQUE (claim_id, artifact_id, location)` | Provenance cannot be double-written |

### 10.5 Time

Store and compute in UTC; display and schedule in `Asia/Kolkata`. Every timestamp
column is `TIMESTAMPTZ` and named `*_at`. The 08:00 run fires at 08:00 IST
regardless of host timezone. `posted_at` is never fabricated as `now()` when
upstream gives a fuzzy string it cannot resolve — it is `None`, and recency
ranking falls back to `first_seen_at`, because a fabricated date silently inverts
the decay curve.

### 10.6 Feature flags

Boolean settings on the same `Settings` object, defaulting **off** for anything
new on the generation path. New behaviour ships flagged and is proven on a small
reversible slice before becoming default — `volume`-tier companies only, for two
weeks, with every output reviewed, before it reaches `dream` and `strong`.
Reverting is a settings change, not a deploy: the plan schema tolerates the
absence of an operation and `apply_plan` ignores an op type it is not configured
for.

The highest-risk flag is `FF_TAILORING_REPHRASE`, because `rephrase` is the only
operation that produces text the model *composed* rather than *selected*. That is
precisely why it is the one most worth gating, and why it is evaluated on observed
response rate rather than on how good the letters feel to read.

### 10.7 Testing posture

| Layer | Approach |
|---|---|
| Adapters | Recorded fixtures, no database, no Redis, no network. Every mapping caveat in `SOURCE_ADAPTERS.md` has a test |
| Invariants | Each of the eight has a test that proves it, including static checks: no second call site of Gmail `send`; no code path constructs a request to a denied host; `NEVER_FETCH_HOSTS` unreachable from `Settings`; `ghosted` absent from the enum and assigned nowhere |
| Scoring | A forty-JD golden set with hand-labelled requirements, kinds, tokens and coverage levels; grounding violations gate at **zero**, not a threshold |
| Ledger | Fabrication cases must fail; the DB trigger must refuse a `failed` artifact even when application code tries |
| Pipeline | End-to-end against fixtures: fixture ATS → review item, asserting zero outbound requests from `api/`, `review/` and `tracking/` |

---

## 11. Security architecture summary

`SECURITY_ARCHITECTURE.md` is canonical. The shape, for the architecture reader:

| Boundary | Threat | Control |
|---|---|---|
| ATS response → parser | Malicious or malformed payload | Size caps, no HTML execution, no JS, `extra="forbid"` on every DTO, response bodies never logged |
| JD text → LLM | Prompt injection aimed at inflating a match or exfiltrating context | Nonce-delimited data block; system prompt states the block cannot instruct; a document containing the delimiter form is refused; **the output schema is the containment** — a successful injection can still only emit values that fit `RequirementExtraction` |
| Email body → LLM | Same, with a motivated sender | Identical treatment, plus: no URL in an email body is ever fetched, and never one on the deny list |
| Generated text → document | Fabrication | The ledger, the DB trigger, the absent override endpoint |
| Config → outbound URL | SSRF | Per-field regexes in adapter config models, an allow-list, and the never-scrape gate after redirect resolution |
| Secrets | Leakage | Environment or secret store only; encrypted token file at `0600` with a separately-held key; nothing about a token logged |
| Mailbox | Over-privilege | `gmail.readonly` + `gmail.send` only. `gmail.modify` is deliberately not requested — a write scope on the operator's primary correspondence turns an off-by-one into mutated real mail |
| Outbound mail | Sending to a third party | `send()` takes no recipient argument; a pre-send assertion checks every `To`/`Cc`/`Bcc` against the operator address; a static import-graph test fails the build if a second call site appears |
| Network | Exposure | Not published to the public internet; single-user cookie auth is adequate only because the network boundary carries the real weight — stated plainly rather than dressed as sufficient |

---

## 12. Architecture decision record index

ADR-style entries for every significant decision. Context, decision,
consequence. Status is `accepted` throughout — this is a pre-implementation
design, and a rejected option is recorded as a consequence of the decision that
displaced it.

### ADR-001 · Human approves; the system never submits

**Context.** Automated submission is the feature every competing tool leads with
and the one most requested of a system like this.
**Decision.** No submit endpoint at any API version. No browser automation for
submission. `approve` means "I am going to submit this myself".
**Consequence.** Throughput is bounded by human attention — the mechanism that
keeps output at five to ten applications a week. `submitted` becomes an
unverifiable operator assertion, corrected by PATCH when wrong. The absence is
tested, not merely documented.

### ADR-002 · No automated outbound mail to any third party

**Context.** Automated follow-up to recruiters is the obvious next feature.
**Decision.** Exactly one outbound message class — the digest, to the operator's
own address. Enforced in four places: a no-recipient function signature, a
pre-send assertion, an absent API parameter, and a static import-graph test.
**Consequence.** The operator writes every message to a human. The system
surfaces follow-up candidates instead. The operator's sending reputation and the
individuality of their contact — the one asset a low-volume applicant has — are
preserved.

### ADR-003 · Ledger-only citation, enforced in the database

**Context.** A generated document under the operator's name can contain a
fabricated number, and that failure is not recoverable.
**Decision.** Every factual or numeric assertion must resolve to a `claim` row.
Failed artifacts are unattachable by trigger; no override endpoint exists at any
version. Validation fails closed even when the assertion extractor itself is
unavailable.
**Consequence.** The ledger must be maintained, and a true fact not in it cannot
be used. That cost feeds back into scoring — an uncited quantified bullet cannot
prove a requirement — so ledger neglect visibly lowers the operator's own scores.
Rejected: prompt instructions plus human review, whose residual fabrication rate
is exactly what matters here.

### ADR-004 · LinkedIn and peers are never fetched; alerts arrive as email

**Context.** LinkedIn has the highest coverage of any single source.
**Decision.** `NEVER_FETCH_HOSTS` as a frozen code constant, checked inside the
HTTP client after redirect resolution, unreachable from `Settings`. LinkedIn
roles enter only through job-alert mail the operator already receives.
**Consequence.** Mail-derived postings carry `fidelity_rank` 20 and are labelled
"alert only — open to read"; a directly-fetched duplicate always wins the
collapse. The operator's LinkedIn account is never the credential and therefore
never the thing that gets banned. Rejected: authenticated scraping, headless
browsers, and browser-string user agents.

### ADR-005 · Adapter failure is isolated; a run always completes

**Context.** 320 sources across eleven vendors; something is always broken.
**Decision.** Per-source isolation, two-layer circuit breaking, auto-disable at
five consecutive daily failures, and a per-source result recorded in `run_log`.
**Consequence.** A run never aborts. Re-enabling is a human act, because five
consecutive daily failures nearly always means the board moved rather than that
the network was unlucky. Our own back-pressure (`rate_limited`, `circuit_open`)
does not count toward auto-disable, so a busy run cannot disable healthy boards.

### ADR-006 · APScheduler in-process, not Celery

**Context.** Scheduled daily batch work.
**Decision.** APScheduler with a Postgres job store inside the API container,
plus a Redis run lock.
**Consequence.** One fewer service, one fewer broker, one fewer failure mode. A
long run occupies the API process — invisible at a 15-minute budget on an
otherwise idle machine. The scheduler is a separate module, so splitting it into
its own container later is a compose-file change. Rejected: Celery + RabbitMQ,
whose only needed property (schedule survives restart) the job store already
gives.

### ADR-007 · Postgres full-text and trigram, not a vector database

**Context.** Matching requirements to resume content, and deduplicating company
names.
**Decision.** `tsvector`, `pg_trgm`, and a versioned YAML skill vocabulary with a
three-stage resolver whose LLM fallback is constrained to the closed vocabulary
and cannot mint a token.
**Consequence.** Every match is explainable and reproducible; a vocabulary change
is a diff in a pull request that forces an explicit rescore. The alias list needs
maintenance, handled by the `skill_proposal` queue. `pgvector` remains available
inside the database already present if semantic matching is ever genuinely
needed. Rejected: a managed vector store, whose distances are unexplainable and
whose embeddings drift silently on a model upgrade.

### ADR-008 · Docker Compose on one VM, not Kubernetes

**Context.** One user, one container set, a workload idle 23 hours a day.
**Decision.** Compose on a small VM, with ECS Fargate documented as the
alternative (§9.3).
**Consequence.** Near-zero operating cost and a deploy the operator fully
understands. No zero-downtime deploy, no automatic failover, no staging
environment — all acceptable at this scale, and the compensating control is
reversible migrations plus flags that default off. Rejected: Kubernetes (a
control plane with nothing to control) and a serverless decomposition (a poor fit
for a stateful 15-minute pipeline holding a connection pool and rate-limit
state).

### ADR-009 · No selection probability, ever

**Context.** The operator will want a "chance of selection" number.
**Decision.** Report coverage, named gaps, and observed funnel rates with
sample-size guards. No predicted probability, no column for one.
**Consequence.** `GET /metrics/funnel` carries
`meta.note: "Observed rates from your own history. Not a prediction."` as part of
the contract. Outcome data evaluates the scorer and never trains it, and touches
ranking in exactly one place — a tie-break gated at twenty submissions per
variant. Rejected: a supervised model (positive class in single digits after a
year) and a borrowed base rate (arithmetic theatre).

### ADR-010 · Deterministic filter before any token is spent

**Context.** ~150 new postings a day, ₹80 daily budget.
**Decision.** Stage ④ is pure boolean logic — location, seniority, deny-list,
company status — and removes ~80% before extraction.
**Consequence.** The filter saves roughly ₹88/day, more than the entire budget;
without it, spend is about 1.93× budget before generation grows too. The filter
is deliberately model-free: a filter that called a model to decide what to filter
would spend most of what it saved. Recall is traded for cost, knowingly, and the
designed response to growth is a stricter filter rather than a bigger budget.

### ADR-011 · A reviewable diff, not a regenerated document

**Context.** Stage ⑧ could emit a finished resume.
**Decision.** It emits a `tailoring_plan` — bullet swaps, block reordering,
skills-line edits — against a base variant. The `.docx` is rendered on demand.
**Consequence.** The change is reviewable at a glance rather than requiring a
full re-read; the plan, not the file, is what the operator edits, and their
correction is captured rather than worked around. Rendering is not wasted on the
~40% of items that get skipped. Page count is verified by actually rendering,
never assumed.

### ADR-012 · Prompt and vocabulary versions are part of the row key

**Context.** Prompts and the skill vocabulary will change, and a change silently
re-ranks everything.
**Decision.** `UNIQUE (posting_id, variant_id, prompt_version)`;
`requirement.prompt_version` embeds the vocabulary date; model IDs are pinned,
never `-latest`.
**Consequence.** A new version *inserts* rather than conflicting, so two score
families coexist and A/B comparison needs no shadow table. A partial unique index
keeps exactly one `is_recommended` per posting per version. Promotion is a
settings change and rollback is flipping it back, with no data loss.

### ADR-013 · `gmail.modify` is not requested

**Context.** A `Scout/Processed` label would be the convenient work queue.
**Decision.** `gmail.readonly` + `gmail.send` only. Processing state is a
`historyId` cursor plus `UNIQUE (email_message.gmail_id)`.
**Consequence.** A loop bug cannot mutate real mail; the mailbox stays the
operator's own signal. The system must maintain a cursor and tolerate re-seeing
messages — re-processing is a no-op — and it cannot help triage the inbox. Both
costs accepted. The OAuth app is published "In production" without Google
verification, accepting the one-time unverified-app interstitial in exchange for
refresh tokens that do not expire weekly.

### ADR-014 · The event log is the truth; status is materialised

**Context.** Application status changes arrive out of order, from two sources
(mail inference and manual entry), with varying confidence.
**Decision.** `application_event` is append-only; `application.status` is
maintained by trigger; transition legality is enforced on append; `ghosted` is a
view.
**Consequence.** Out-of-order arrival yields the same materialised status as
in-order arrival; a duplicate event creates one row; an event on day 45 removes
an application from `v_ghosted` with no compensating write. Automated and manual
transitions stay distinguishable via `is_manual`, so a funnel can be read with or
without operator-entered events.

### ADR-015 · Email bodies are never stored

**Context.** Classification needs the body; the audit trail needs justification.
**Decision.** Classify in memory; persist only the class, the confidence and a
short excerpt on the event row.
**Consequence.** A database leak exposes metadata and fragments, not
correspondence. A misclassification cannot be re-diagnosed from stored text — the
accepted cost — and the operator still has the original in Gmail.

### ADR-016 · Single-user, single-session authentication

**Context.** One operator, on a private network.
**Decision.** A long-lived local session cookie against a password from the
environment. No registration, no reset, no user table.
**Consequence.** Every row is implicitly owned by the one operator, which removes
tenancy from every query and every index. The network boundary carries the real
security weight, and that is stated rather than glossed. §14 sets out what
changing this would cost.

---

## 13. Technical debt, registered up front

Debt taken knowingly, with the trigger that would make it worth paying down.

### 13.1 Meta and Apple adapters deferred

Both employers run bespoke career platforms with no public JSON endpoint of the
shape the adapter protocol expects. Writing either means either browser
automation or reverse-engineering an internal endpoint, and both sit close to the
line ADR-004 draws.

*Impact:* two significant employers are reachable only through job-alert mail and
manual import.
*Interest:* low. `POST /postings/import` covers the case at a cost of two minutes
per role.
*Trigger to pay:* a documented public endpoint appearing, or the operator
targeting either employer seriously enough that the alert-only path becomes the
bottleneck. **Not** a trigger: coverage envy.

### 13.2 No multi-user model

Not a partial implementation — a deliberate absence. There is no `user` table, no
ownership column, no tenancy predicate, and no per-row authorisation anywhere.

*Impact:* the system cannot be shared, demonstrated with someone else's data, or
run for a second person without the change described in §14.
*Interest:* zero while the assumption holds; a full retrofit the moment it does
not. That asymmetry is the debt.
*Trigger to pay:* a second real user. Never "in case".

### 13.3 Single-node Postgres, no replica

One instance, one volume, nightly `pg_dump`.

*Impact:* a disk failure loses up to a day of `application_event` rows, which are
the only irreplaceable data in the system — postings re-discover, artifacts
regenerate, claims live in the operator's head and their sources.
*Interest:* low but non-zero and it accrues with every application submitted.
*Trigger to pay:* the first time a restore is actually needed, or the application
count passing a few hundred. The cheapest real mitigation is not a replica but
more frequent off-host dumps, and that should be done before anything cleverer.

### 13.4 No vector search

By design (ADR-007), but it *is* a limitation and worth naming as such: a
requirement phrased in a way the vocabulary has never seen resolves to no token
and scores `missing` with a note.

*Impact:* the long tail of novel phrasings under-scores until the vocabulary
catches up.
*Interest:* low, and self-limiting — `skill_proposal` collects exactly the
phrases that failed, so the tail is measured rather than guessed at.
*Trigger to pay:* a persistently large `skill_proposal` queue that the operator
cannot keep up with, at which point `pgvector` inside the existing database is
the first step, not a new datastore.

### 13.5 Other debts, briefly

| Debt | Impact | Trigger to pay |
|---|---|---|
| No staging environment | A bad deploy costs one morning's digest | A second user, or an incident that costs more than a morning |
| Scheduler shares the API process | A long run competes with the UI | Run wall clock approaching the 15-minute budget habitually |
| `submitted` is unverifiable | Funnel rates skew optimistic if the operator approves without submitting | A measurable gap between approvals and observed acknowledgements |
| No structured skip-reason taxonomy beyond four canned options | Tier-2 scorer evaluation is coarser than it could be | The canned reasons stopping being sufficient to explain inversions |
| Artifact storage on a local volume | Backup is a second, separate concern from the database dump | Migration to Fargate, where S3 makes it free |
| No rate-limit coordination across a restart mid-run | A restart mid-run could briefly exceed a bucket | Never, realistically — buckets are in Redis precisely to survive this |

---

## 14. Evolution path

### 14.1 What would have to change for multi-user

Stated concretely so the cost is visible rather than assumed to be small. This is
not a plan; it is a price.

| Area | Change |
|---|---|
| **Identity** | A `user` table, real password hashing, session management, and either registration or admin provisioning. Every one of these is currently absent, not simplified |
| **Data model** | An owner column on `company`, `source`, `job_posting` (or a shared-postings model with per-user visibility), `resume_variant`, `claim`, `review_item`, `application`, `artifact`, `email_message`, `run_log`. Every unique constraint that is currently global becomes per-user — `company.slug`, `claim.key`, `resume_variant.key`, `source`'s config uniqueness |
| **Authorisation** | A scoping layer on every read and every write. The current design has none because it needs none, so this is new code in every service, not a filter added in one place |
| **The ledger** | The hardest part. Claims are personal facts with confidentiality tiers and a disclosure allow-list. Cross-user leakage of a `restricted` claim into another user's document is the worst failure the system could have, and it would need per-user isolation enforced at the same strength as the current citation rule — trigger-level, not application-level |
| **Mail** | One OAuth grant per user, one token per user, one cursor per user, and a digest per user. The outbound assertion becomes "recipient equals *this* user's address", which is materially weaker than the current constant and needs its own test |
| **Scheduling and cost** | Per-user run locks, per-user rate-limit accounting against shared ATS buckets (users would contend), per-user token budgets, and a fairness policy when the 15-minute window is shared |
| **Sources** | Shared. Two users tracking Stripe should not poll Greenhouse twice — which turns `source` from a per-user row into a shared resource with per-user subscriptions, and turns posting dedup into a global concern |
| **Deployment** | Compose on one VM stops being defensible at the point where an outage affects someone other than the operator. §9.3 becomes the default |
| **Operations** | Backup/restore per user, data export, deletion on request, and a support path — none of which exist |

**Rough shape of the work:** the schema and scoping changes are mechanical but
touch nearly every table and every service. The ledger isolation and the mail
per-user grant are the genuinely hard parts. The honest estimate is that this is
a rewrite of the persistence and authorisation layers with the pipeline reused,
not an incremental feature.

### 14.2 What would *not* change

Worth stating, because it is the test of whether the architecture is sound.

- The eleven-stage pipeline, unchanged.
- Every adapter, unchanged — they never touch the database, so they never learn
  about users.
- The scoring formula, the vocabulary and the composite arithmetic, unchanged.
- The claims-ledger *rule* — only its isolation boundary moves.
- All four invariants. They are properties of what the system does, not of how
  many people it does it for. Multi-user would make ADR-001 and ADR-002 more
  important, not less.

### 14.3 Nearer-term evolution, in likely order

1. **More adapters** for whichever ATS platforms the operator's target list
   actually uses. Additive; the protocol does not change.
2. **Scoring prompt v3** via the A/B mechanism already in the schema — flip rate
   reviewed posting by posting, promoted only on operator agreement over a
   twenty-item sample.
3. **`FF_TAILORING_REPHRASE` promoted to default**, if two weeks on volume-tier
   companies shows no validation regressions and no response-rate harm.
4. **Tier-3 evaluation** once roughly forty applications exist: does the composite
   separate responders from non-responders, is the relationship monotonic, does
   the hard gate earn its place. Read with the sample size in front of you; the
   correct response to an early result is to note it and wait.
5. **Follow-up prompt tuning** against the observed reply-time distribution rather
   than the seeded defaults.
6. **`pgvector` for company deduplication only**, if trigram matching proves
   insufficient on the long tail of name variants — and not for requirement
   matching, which stays explainable.

Each is additive, reversible, and shippable behind a flag. None requires the
multi-user rewrite, and none touches an invariant. That is the property the
architecture was built to have.

---

## 15. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | **Canonical.** Invariants, module boundaries, pipeline, technology decisions, scale envelope |
| `HLD.md` | The same system component by component, for a reader new to it |
| `DATA_MODEL.md` | **Canonical for schema.** Tables, columns, indexes, constraints, views, migrations |
| `API.md` | **Canonical for endpoints.** Contracts, envelope, error codes, deliberate absences |
| `SOURCE_ADAPTERS.md` | Adapter protocol, per-vendor contracts, rate limits, robots, circuit breaking |
| `COMPANY_REGISTRY.md` | Detection flow, the URL pattern table, tiering, dedup, bulk import |
| `MATCH_SCORING.md` | Extraction, vocabulary, coverage, the composite formula, gaps, evaluation |
| `CLAIMS_LEDGER.md` | The grounding rule, confidentiality, validation, enforcement, provenance |
| `DOCUMENT_GENERATION.md` | Tailoring plans, bullet selection, letters, rendering, anti-templating |
| `AI_ARCHITECTURE.md` | Provider abstraction, routing, prompt families, injection defence, cost |
| `EMAIL_INGESTION.md` | Gmail integration, alert parsing, classification, linkage, digest |
| `APPLICATION_PIPELINE.md` | State machine, event-log semantics, funnel metrics, export, retention |
| `SECURITY_ARCHITECTURE.md` | Threat model, controls, secret handling |
| `DATA_SOURCES_AND_COMPLIANCE.md` | Legal basis per source, the deny list, documented rate limits |
| `INFRASTRUCTURE.md` | Runtime topology detail, sizing, backup procedure |
| `SDD.md` | Module-level design detail |
