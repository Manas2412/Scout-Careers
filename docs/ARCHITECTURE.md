# ARCHITECTURE — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Owner:** Manas Sisodia

This is the canonical architecture document. Where any other document in
`docs/` disagrees with this file on entity names, module boundaries, endpoint
paths, configuration keys or the system invariants, **this file wins** and the
other document is wrong and must be corrected.

---

## 1. What the system is

Scout Careers is a single-user job discovery and application-preparation
platform. It runs a scheduled ingestion pass across employer applicant tracking
systems (ATS), scores every discovered role against a library of resume
variants, drafts a tailored resume plan and cover letter for the roles that
score well, and presents them in a review queue. A human approves, downloads and
submits. The system then tracks the outcome by reading the reply mail.

It is deliberately **not** an auto-applier. See §3.

### 1.1 The problem it solves

Job discovery is high-volume, low-signal, and repetitive. Tailoring an
application is low-volume, high-signal, and expensive. Existing tools automate
the wrong half — they mass-submit generic applications, which converts poorly
and damages the applicant's standing with shared ATS platforms.

Scout Careers automates discovery, matching and drafting, and leaves submission
and outbound contact to the human. The intended operating cost is **ten minutes
per day**, producing five to ten well-targeted applications per week instead of
two hundred poor ones.

### 1.2 Lineage

The architecture is a direct adaptation of the Scout bid-discovery platform
(EY internal): ingest from public portals → normalise → classify → score against
a weighted model → surface a ranked queue for human review, with every scoring
decision explainable. The domain changed; the shape did not.

---

## 2. System context

```
┌──────────────────────────────────────────────────────────────────────┐
│                          EXTERNAL SOURCES                             │
│                                                                       │
│  Greenhouse   Lever   Ashby   Workday   SmartRecruiters   Workable    │
│  Google Careers   Amazon Jobs   Microsoft Careers                     │
│  Job-alert email (LinkedIn / Naukri / Indeed → dedicated mailbox)     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  HTTPS (public JSON endpoints)
                                │  IMAP/Gmail API (alert mail)
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        SCOUT CAREERS (FastAPI)                        │
│                                                                       │
│   sources ──▶ ingest ──▶ extract ──▶ scoring ──▶ generate ──▶ review  │
│                                          ▲            ▲               │
│                                          │            │               │
│                                      registry      ledger             │
│                                                                       │
│   mail ──▶ tracking ──▶ export            scheduler (08:00 IST)       │
└───────────┬──────────────────────────────────────┬───────────────────┘
            │                                       │
            ▼                                       ▼
┌───────────────────────┐              ┌────────────────────────────────┐
│ Postgres 16 + Redis   │              │  React SPA (Vite + TS)         │
│ local filesystem /    │              │  Dashboard · Queue · Jobs ·    │
│ object store for      │              │  Companies · Applications ·    │
│ generated artifacts   │              │  Claims · Settings             │
└───────────────────────┘              └────────────────────────────────┘
            │
            ▼
┌───────────────────────┐              ┌────────────────────────────────┐
│  Gmail API            │              │  LLM provider (Bedrock default,│
│  (read + digest send) │              │  Azure OpenAI alternate)       │
└───────────────────────┘              └────────────────────────────────┘
```

**Trust boundaries.** Everything above the FastAPI box is untrusted input.
Job descriptions and email bodies are attacker-controllable text and are never
executed, never used to construct SQL, and never treated as instructions to the
LLM — they are always passed as delimited data. See `SECURITY_ARCHITECTURE.md`.

---

## 3. Invariants

These hold at every stage of the system's life. They are enforced in code, not
by convention, and each has a test that proves it.

1. **No automated submission.** The system never POSTs an application to an
   employer system, never drives a browser to submit a form, and never
   completes a CAPTCHA. It prepares; the human submits.
2. **No automated outbound mail to people.** The system sends exactly one class
   of email — the daily digest, to the operator's own address. It never emails a
   recruiter, hiring manager or any third party.
3. **Generation cites only the ledger.** Every factual or numeric claim in a
   generated resume bullet or cover letter must resolve to a `claim` row. A
   validation pass rejects any uncited numeric or superlative. Free-form model
   invention is a build failure, not a warning.
4. **The never-scrape list is absolute.** Sources on the deny list — LinkedIn
   first among them — are never fetched programmatically under any
   configuration. The list is a code constant, not a config value.
5. **Adapter failure is isolated.** One broken source degrades that source only.
   A discovery run always completes and always reports which sources failed.
6. **No secrets in code or logs.** Credentials come from environment or a secret
   store. Tokens, cookies and OAuth material are never logged at any level.
7. **Every artifact is reproducible.** Each generated resume or cover letter
   records the model, prompt version, variant ID and the exact claim IDs used.
8. **Robots and rate limits are respected.** Every adapter declares its polling
   interval and honours `robots.txt` and any documented rate limit.

Invariants 1, 2 and 4 exist because the naive version of this product — mass
auto-apply and cold outreach — is both counterproductive and legally exposed.
See `DATA_SOURCES_AND_COMPLIANCE.md` for the reasoning.

---

## 4. Technology decisions

| Layer | Choice | Rationale |
|---|---|---|
| Language | **Python 3.12** | Strongest ecosystem for parsing, document generation and LLM tooling; matches the operator's deepest production experience |
| API | **FastAPI 0.115** | Async, Pydantic-native, OpenAPI for free |
| ORM | **SQLAlchemy 2.0 (async)** + **Alembic** | Typed, async, migration discipline |
| Validation | **Pydantic v2** | Shared models between API, LLM structured output and config |
| Database | **PostgreSQL 16** | JSONB for adapter config and raw payloads; full-text for job search; `pg_trgm` for fuzzy company matching |
| Cache / queue | **Redis 7** | Dedup keys, rate-limit buckets, run locks |
| Scheduler | **APScheduler** (in-process, Postgres job store) | Single-user scale; Celery is unjustified operational weight here |
| LLM | **AWS Bedrock** default, **Azure OpenAI** alternate, behind one interface | Both already provisioned; no lock-in |
| Frontend | **React 18 + Vite 5 + TypeScript 5.6 + TanStack Query** | Matches existing frontend experience |
| Browser automation | **Playwright** (only where no API exists) | Already used in Scout |
| Document output | **python-docx** | Same toolchain that produced the resume set |
| Deployment | **Docker Compose** on one VM (default); ECS Fargate documented as an alternative | Personal-scale; Compose keeps operating cost near zero |

### 4.1 Explicitly rejected

- **Celery / RabbitMQ** — a distributed task queue for one user's daily batch is
  operational overhead with no benefit. APScheduler with a Postgres job store
  survives restarts, which is the only durability property needed.
- **A vector database** — matching is requirement-by-requirement against a small
  fixed set of resume variants. Embeddings assist company deduplication only,
  and `pgvector` covers that if it is ever needed.
- **Kubernetes** — one container set, one user.
- **LinkedIn scraping** — see invariant 4.

---

## 5. Module structure

```
backend/src/scout_careers/
├── common/        config, logging, types, time helpers, hashing, ID generation
├── db/            SQLAlchemy models, async session, Alembic env
├── sources/       SourceAdapter protocol + one module per ATS
│   ├── base.py        protocol, shared HTTP client, retry/backoff
│   ├── greenhouse.py  lever.py  ashby.py  workday.py
│   ├── smartrecruiters.py  workable.py  recruitee.py
│   ├── google.py  amazon.py  microsoft.py
│   └── mail_alerts.py  (parses job-alert email into postings)
├── registry/      company CRUD, ATS auto-detection from a pasted URL
├── ingest/        run orchestration, normalisation, dedup, change detection
├── extract/       JD → structured Requirement records (LLM, structured output)
├── scoring/       variant coverage scoring, gap analysis, ranking
├── ledger/        claims store, citation resolution, validation pass
├── generate/      resume tailoring plans, cover letter drafting, .docx render
├── review/        queue, approval state machine
├── mail/          Gmail read + classify, digest composition and send
├── tracking/      application pipeline, funnel metrics, spreadsheet export
├── scheduler/     APScheduler job definitions
├── llm/           provider-agnostic client, prompt registry, versioning
└── api/           routers (thin), dependency wiring
```

**Layering rule:** routers are thin, services are thick. No business logic in
`api/`. No HTTP concerns below `api/`. `sources/` never touches the database —
it returns normalised DTOs and `ingest/` persists them.

```
frontend/src/
├── routes/        Dashboard · Queue · Jobs · Companies · Applications ·
│                  Claims · Variants · Settings
├── components/    shared UI
├── api/           typed client generated from the OpenAPI schema
└── lib/           formatting, hooks, state
```

---

## 6. The pipeline

A discovery run is a linear pipeline with an explicit checkpoint after each
stage. Stages are individually re-runnable against stored intermediate state,
so a failure in scoring never forces a re-fetch.

```
 ①  DISCOVER      per source: adapter.fetch() → RawPosting[]
                  failures isolated, recorded in run_log
        │
 ②  NORMALISE     RawPosting → JobPosting (canonical fields, cleaned HTML)
        │
 ③  DEDUPE        by (company_id, external_id) then content_hash;
                  cross-source duplicates collapsed to one posting
        │
 ④  FILTER        cheap deterministic gates — location, seniority,
                  keyword deny-list, company status ≠ blacklisted.
                  Kills ~80% before any token is spent.
        │
 ⑤  EXTRACT       LLM structured output → Requirement[]
                  (hard | nice | responsibility | tool | condition)
        │
 ⑥  SCORE         for each active ResumeVariant: requirement-by-requirement
                  coverage with linked evidence → MatchScore + gap list
        │
 ⑦  RANK          composite of coverage, company tier, posting recency
        │
 ⑧  GENERATE      top N only: resume tailoring plan + cover letter draft
                  (cover letter skipped where company.cover_letter_worth = false)
        │
 ⑨  VALIDATE      citation check against the claims ledger;
                  uncited numeric or superlative ⇒ draft rejected, logged
        │
 ⑩  ENQUEUE       ReviewItem rows, status = pending_review
        │
 ⑪  DIGEST        08:15 IST email: new queue, status changes, source failures
```

Stages ⑤ and ⑧ are the only ones that cost tokens. Stage ④ exists specifically
to keep that cost bounded — see `AI_ARCHITECTURE.md` for the cost model.

### 6.1 What happens after the queue

```
review queue → [HUMAN approves] → artifacts rendered to .docx
             → [HUMAN submits on the employer's site]
             → application row created, status = submitted
             → mail poller classifies replies → status transitions
             → funnel metrics recomputed → spreadsheet export
```

The two `[HUMAN]` steps are invariants 1 and 2 made visible. They are not
configurable.

---

## 7. Core entities

Authoritative definitions, column types and indexes live in `DATA_MODEL.md`.
This is the conceptual map only.

| Entity | Purpose |
|---|---|
| `company` | A tracked employer: tier, status, tags, default variant, cover-letter policy |
| `source` | One ATS endpoint belonging to a company: adapter name + config JSONB |
| `job_posting` | A normalised, deduplicated role |
| `requirement` | One extracted requirement belonging to a posting |
| `resume_variant` | One of the resume variants, with its structured content |
| `claim` | The ledger: one verified, citable fact |
| `match_score` | Coverage of one posting by one variant, with gaps and evidence |
| `review_item` | A queued, drafted application awaiting human decision |
| `application` | A submitted application and its lifecycle state |
| `application_event` | An observed status transition with its evidence |
| `artifact` | A generated resume or cover letter file, with provenance |
| `email_message` | An ingested mail, its classification and linked application |
| `run_log` | One pipeline execution, its stats and per-source outcomes |

### 7.1 Application lifecycle

```
discovered → queued → drafted → approved → submitted
                          │                    │
                          ▼                    ├─▶ acknowledged
                       skipped                 ├─▶ screening
                                               ├─▶ interview
                                               ├─▶ offer
                                               ├─▶ rejected
                                               ├─▶ withdrawn
                                               └─▶ ghosted (derived: no event in N days)
```

`ghosted` is computed, never set by the mail classifier — absence of evidence is
not evidence, so it is a view over `application_event`, not a stored decision.

---

## 8. Cross-cutting concerns

**Configuration.** One `Settings` object built by `pydantic-settings` from
environment variables. No magic constants in modules. Keys are documented in
`CONFIGURATION.md`.

**Feature flags.** Boolean settings on the same object, defaulting off for
anything new on the generation path. New behaviour ships flagged and is proven
on a small slice before being made default.

**Logging.** Structured (`structlog`), one line per pipeline stage per run, with
`run_id` correlation. Never logs credentials, OAuth tokens, full email bodies or
raw resume content.

**Error policy.** Fail closed on anything touching data integrity or the
compliance boundary; fail open on enrichment. A scoring failure downgrades the
item to `needs_manual_review`; it never silently ships an unscored draft.

**Idempotency.** Discovery runs are safe to re-execute. Posting identity is
`(source_id, external_id)`; content change is detected by `content_hash`.
Re-running a completed stage is a no-op.

**Time.** Store and compute in UTC. Display and schedule in `Asia/Kolkata`. The
daily run fires at 08:00 IST regardless of host timezone.

---

## 9. Scale envelope

Sized deliberately small. This is a personal tool and over-engineering it is the
main risk to it ever being finished.

| Dimension | Design target |
|---|---|
| Tracked companies | 300 |
| Sources polled per run | ~320 |
| Postings ingested per run | 2,000–5,000 raw, ~150 new after dedup |
| Postings surviving filter | ~30/day |
| LLM extractions per day | ~30 |
| Drafts generated per day | ≤ 10 |
| Applications tracked, lifetime | low thousands |
| Concurrent users | 1 |
| Run wall-clock budget | < 15 minutes |
| Daily LLM cost budget | < ₹80 |

Single Postgres instance, single application container, single worker process.
No horizontal scaling is designed for, because none is needed.

---

## 10. Related documents

| Document | Covers |
|---|---|
| `DATA_MODEL.md` | Tables, columns, indexes, constraints, migrations |
| `API.md` | Endpoint contracts, request/response envelopes, errors |
| `SOURCE_ADAPTERS.md` | The adapter protocol and every implementation |
| `COMPANY_REGISTRY.md` | ATS auto-detection, tiers, the company page |
| `MATCH_SCORING.md` | Requirement extraction, coverage scoring, ranking |
| `CLAIMS_LEDGER.md` | The ledger, citation resolution, validation |
| `DOCUMENT_GENERATION.md` | Resume tailoring and cover letter drafting |
| `EMAIL_INGESTION.md` | Gmail read, classification, digest composition |
| `APPLICATION_PIPELINE.md` | Lifecycle, funnel metrics, spreadsheet export |
| `AI_ARCHITECTURE.md` | Prompts, structured output, model routing, cost |
| `SECURITY_ARCHITECTURE.md` | Threat model, controls, secret handling |
| `DATA_SOURCES_AND_COMPLIANCE.md` | Legal basis per source, deny list, rate limits |
| `INFRASTRUCTURE.md` | Runtime topology, sizing, backups |
| `HLD.md` / `SOLUTION_ARCHITECTURE.md` / `SDD.md` | Design at decreasing altitude |
