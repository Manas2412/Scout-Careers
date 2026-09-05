# HIGH-LEVEL DESIGN — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** derived. `ARCHITECTURE.md` is canonical for the invariants, module
boundaries and the scale envelope; `DATA_MODEL.md` for schema; `API.md` for
endpoint contracts. This document explains the system at design altitude for a
reader who has not seen the codebase, and adds nothing that contradicts those
three. Where it appears to, this document is wrong.

---

## 1. Purpose and problem statement

### 1.1 The problem

Applying for jobs is two tasks wearing one name, and they have opposite
economics.

**Discovery** is high-volume and low-signal. A role that fits appears somewhere
among a few thousand postings spread across a few hundred employer career sites,
each on a different applicant tracking system (ATS), each with a different wire
format, none of them aggregated anywhere trustworthy. Finding it is repetitive
work with almost no judgement in it, and it has to be redone every day because
the interesting postings are the new ones.

**Preparation** is low-volume and high-signal. Reading a job description
properly, deciding which of several resume framings fits it, deciding which
achievements to lead with, naming the gaps honestly, and writing a letter that a
human will read — that is a couple of hours of genuine thought per application,
and it is the part that determines whether the application converts.

The available tools automate the wrong half. Mass-apply services and browser
extensions automate *submission*, spraying a generic resume at two hundred
postings. This fails in three ways at once: it converts poorly, because generic
applications are screened out; it burns the applicant's standing on shared ATS
platforms, where the same identity is visible to every employer on that vendor;
and it consumes the applicant's attention on the outcome of the spray rather than
on the small number of roles that were worth real effort.

### 1.2 What Scout Careers does

Scout Careers automates discovery, matching and drafting. It leaves submission
and every outbound human contact to the operator.

Once a day it polls the ATS endpoints of every tracked employer, normalises and
deduplicates what it finds, discards the large majority with cheap deterministic
rules, extracts structured requirements from the survivors, scores each against a
library of resume variants, drafts a tailoring plan and a cover letter for the
best-scoring handful, checks every factual assertion in those drafts against a
ledger of verified claims, and puts the result in a review queue with a one-line
digest email. The operator reads the queue, approves or skips, downloads the
documents, and submits on the employer's own site. The system then reads the
reply mail and tracks the outcome.

### 1.3 The intended operating cost

**Ten minutes a day**, producing **five to ten well-targeted applications a
week** instead of two hundred poor ones. Every design decision in this document
is downstream of that sentence. It is why the daily output is a digest rather
than a dashboard, why the review queue shows a gap list before it shows a
document, why generation is capped at ten drafts a day, and why the system has no
feature that would make it possible to submit more applications faster.

### 1.4 Lineage

The architecture is a direct adaptation of the Scout bid-discovery platform (EY
internal): ingest from public portals, normalise, classify, score against a
weighted model, surface a ranked queue for human review, with every scoring
decision explainable. A bid/no-bid decision has to survive being questioned by a
partner; a job-application decision has the same property, and the same shape of
system answers it. The domain changed; the shape did not.

---

## 2. Scope and non-goals

### 2.1 In scope

| Capability | Summary |
|---|---|
| Company and source registry | 300 tracked employers, ~320 ATS endpoints, ATS type auto-detected from a pasted careers URL |
| Scheduled discovery | One daily pass at 08:00 IST across every enabled source, with per-source failure isolation |
| Normalisation and deduplication | Vendor payloads to a canonical posting; identity by `(source_id, external_id)`; cross-source collapse |
| Deterministic filtering | Location, seniority, keyword deny-list, company status — the stage that bounds token cost |
| Requirement extraction | Job description prose to typed, weighted, evidence-grounded `requirement` rows |
| Coverage scoring | Requirement-by-requirement match against each resume variant, with linked evidence and a named gap list |
| Document drafting | A reviewable resume tailoring plan and a cover letter draft, both citing only ledger claims |
| Citation validation | Every numeric and superlative assertion resolved against the claims ledger, or the draft is rejected |
| Review queue | One screen per candidate application: posting, score, gaps, plan, artifacts, approve/skip |
| Application tracking | Lifecycle state machine driven by an append-only event log |
| Mail ingestion | Job-alert parsing into postings; reply classification into status events |
| Daily digest | One email a day to the operator's own address |
| Funnel metrics and export | Observed conversion by variant, tier, week and channel; nightly `.xlsx` |

### 2.2 Explicit non-goals

These are not unbuilt features. They are decisions, and several are enforced as
absences in the API surface (`API.md` §8).

| Non-goal | Why |
|---|---|
| **Automated submission** | Invariant 1. The system never POSTs an application, never drives a browser to submit a form, never completes a CAPTCHA. Automating submission is what converts a careful applicant into a spammer, and it is the single change that would invalidate the product's thesis. |
| **Automated outbound mail to people** | Invariant 2. Machine-generated outreach at volume is spam regardless of tone, it permanently damages the operator's own deliverability, and it destroys the one asset a low-volume applicant has — evident individuality. |
| **LinkedIn (and peer aggregator) fetching** | Invariant 4. `NEVER_FETCH_HOSTS` is a frozen code constant, not a setting. LinkedIn reaches the system only as job-alert *email the operator already receives*. |
| **Multi-user support** | One operator, one session, one password from the environment. No registration, no password reset, no tenancy. §13 of `SOLUTION_ARCHITECTURE.md` states what it would take to change. |
| **A predicted selection probability** | The quantity is not computable from what the system can see, and a displayed percentage becomes the thing the operator optimises. `MATCH_SCORING.md` §7 is the full argument. The system reports *observed* funnel rates from the operator's own history instead. |
| **Interview preparation, salary negotiation, career coaching** | Out of the pipeline's scope entirely. The system stops at "the documents are ready". |
| **Recruiter CRM / contact management** | A direct consequence of invariant 2. Recruiter names and personal contact details found in vendor metadata are stripped before persistence; the system has no use for them. |
| **A job board or anything shared with other people** | Single-operator tool. Artifacts contain the operator's resume content and, under the `internal` confidentiality tier, figures they would not publish. |
| **Real-time or near-real-time discovery** | Batch, once a day, with a 15-minute wall-clock budget. A role that appears at 14:00 is seen at 08:00 the next morning, and that is fine — the bottleneck is the operator's evening, not the ingestion latency. |
| **Horizontal scaling** | One container set, one Postgres, one worker. §12. |

---

## 3. Stakeholder and usage model

### 3.1 The single operator

There is exactly one stakeholder, and they hold every role simultaneously: they
are the user, the administrator, the data owner, the person who decides what a
"dream" employer is, the person who verifies every claim in the ledger, and the
person legally answerable for every document the system helps produce.

This concentration is what licenses several otherwise-indefensible simplifications
— a single session cookie against an environment password, no audit of who
changed a company tier, no approval workflow on a ledger edit. It is also what
makes the invariants non-negotiable rather than configurable: there is no second
party to argue for an exception, and there is no support burden to relieve by
adding an override.

### 3.2 The daily loop

```
08:00  discovery run fires (scheduler)
08:10  mail poll — overnight replies classified
08:15  digest lands in the operator's inbox
       ─────────────────────────────────────────────────────────────
       [ operator, ~10 minutes, usually on a phone ]
       read digest → open 2–4 queue items → read the gap list first
       → skip most → approve one or two
       ─────────────────────────────────────────────────────────────
       [ operator, later, at a desk ]
       download .docx → open the employer's site → submit → done
09:00–22:00  mail polls every 30 minutes; status changes accrue
23:30  spreadsheet export
```

Weekly, the operator spends an hour with the exported workbook: reviewing skip
patterns, checking which variant is actually converting, adding companies,
clearing the `skill_proposal` list, and re-verifying any claim approaching
expiry.

### 3.3 What the operator is *not* asked to do

- Not asked to check the system's arithmetic. Every score is reconstructible by
  hand from rows they can inspect, but the point of that property is that it is
  available when they doubt a result, not that they exercise it daily.
- Not asked to triage failures. A failed adapter appears in the digest with its
  error and its consecutive-failure count; a source that has failed five times
  running is auto-disabled and *named*, never silently dropped.
- Not asked to trust a generated document. The queue shows the tailoring plan as
  a diff against the base variant, so the change is reviewable at a glance rather
  than requiring a full re-read of a regenerated resume.

---

## 4. The pipeline, conceptually

A discovery run is a linear pipeline of eleven stages with an explicit checkpoint
after each. Stages are individually re-runnable against stored intermediate
state, so a failure in scoring never forces a re-fetch, and re-running a
completed stage is a no-op.

```
                         ┌──────────────────────────────────────────┐
                         │  08:00 IST · APScheduler · Redis run lock │
                         └────────────────────┬─────────────────────┘
                                              ▼
 ①  DISCOVER      per source, ≤8 concurrent: adapter.fetch() → RawPosting[]
                  robots + never-scrape gate on every request, after redirects
                  one broken source degrades that source ONLY
                  ~320 sources · 2,000–5,000 raw postings
        │
        ▼
 ②  NORMALISE     RawPosting → JobPosting: canonical fields, HTML stripped,
                  location parsed, timestamps to UTC, content_hash = sha256(text)
        │
        ▼
 ③  DEDUPE        identity  (source_id, external_id)
                  change    content_hash differs ⇒ update + invalidate scores
                  cross-src (company_id, normalised_title, location_city),
                            higher adapter fidelity_rank wins
                  ~150 genuinely new postings survive
        │
        ▼
 ④  FILTER        cheap deterministic gates ONLY — no model, by design:
                  location · seniority · keyword deny-list · company.status
                  kills ~80% BEFORE a single token is spent  →  ~30 survive
        │
        ▼──────────────────────────────── first token spent below this line
 ⑤  EXTRACT       LLM structured output → Requirement[]
                  kinds: hard | nice | responsibility | tool
                  every evidence_span must appear verbatim in the JD, or the
                  whole extraction is discarded and retried once at temp 0
                  skill normalisation is a versioned vocabulary lookup, not a call
        │
        ▼
 ⑥  SCORE         for each active ResumeVariant, requirement by requirement:
                  met (1.0) | partial (0.5) | missing (0.0), weighted
                  every `met` must cite a variant bullet and its claim IDs
                  → MatchScore + gaps[] + evidence[]
        │
        ▼
 ⑦  RANK          composite = 100 · base · tier · recency · hard-gate
                  exactly one variant per posting is is_recommended
        │
        ▼
 ⑧  GENERATE      top N only, ≤10/run, dream tier drafted first:
                  ├─ resume tailoring plan  (a reviewable DIFF, not a document)
                  └─ cover letter draft     (skipped where cover_letter_worth=false)
        │
        ▼
 ⑨  VALIDATE      every numeric / superlative assertion resolved against the
                  claims ledger. Unresolved ⇒ artifact = failed, one regeneration,
                  then needs_manual_review. FAIL CLOSED. No override exists.
        │
        ▼
 ⑩  ENQUEUE       review_item rows, status = pending_review
        │
        ▼
 ⑪  DIGEST        08:15 IST, one email, to the operator's own address only:
                  new queue · status changes · decisions needed · alert-only
                  roles · source failures · run statistics
        │
        ▼
        ╔═══════════════════════════════════════════════════════════════╗
        ║  HUMAN GATE 1 — approve / skip. Approve means "I will submit". ║
        ╚═══════════════════════════════════════════════════════════════╝
        │
        ▼
     RENDER       .docx produced on demand, page count verified by rendering
        │
        ▼
        ╔═══════════════════════════════════════════════════════════════╗
        ║  HUMAN GATE 2 — the operator submits, on the employer's site.  ║
        ║  There is no endpoint that submits. There never will be.      ║
        ╚═══════════════════════════════════════════════════════════════╝
        │
        ▼
     TRACK        application row → mail poller classifies replies →
                  application_event appended → status materialised →
                  funnel recomputed → nightly .xlsx export
```

**Stages ⑤ and ⑧ are the only ones that cost tokens.** Stage ④ exists
specifically to keep that cost bounded: it removes roughly 120 postings a day
that would otherwise cost ₹0.73 each to extract and judge — ₹88 a day, more than
the entire daily budget. A filter that called a model to decide what to filter
would spend most of what it saved, which is why stage ④ is pure boolean logic.

### 4.1 Why the ordering is what it is

The pipeline is arranged so that every stage is cheaper than the stage after it
and removes more work than it costs. Deduplication precedes filtering because
deduplicating is cheaper than filtering the same posting twice. Filtering
precedes extraction because a location mismatch is a string comparison and an
extraction is a model call. Scoring precedes generation because scoring is
deterministic set arithmetic over rows already in the database — six variants ×
thirty postings a day is 180 scorings at zero marginal token cost — while
generation is the most expensive call in the system. Validation follows
generation rather than being folded into it because the generator must not be
the judge of its own output.

---

## 5. Component inventory

Nine backend modules plus the frontend. The layering rule is absolute: **routers
are thin, services are thick**. No business logic in `api/`, no HTTP concerns
below `api/`, and `sources/` never touches the database.

| Component | Responsibility | Consumes | Produces / exposes |
|---|---|---|---|
| `common/` | Configuration (`Settings` via `pydantic-settings`), structured logging, ULID generation, hashing, UTC/IST time helpers, the encrypted token store | Environment | The `Settings` singleton every other module reads |
| `db/` | SQLAlchemy 2.0 async models, session management, Alembic environment | — | Typed model classes, `AsyncSession` |
| `sources/` | The `SourceAdapter` protocol and eleven implementations; the shared HTTP client with rate limiting, retry/backoff, circuit breaking, robots enforcement and the never-scrape gate | `source.config` (validated per-adapter Pydantic model) | `RawPosting` DTOs — frozen, `extra="forbid"`, no DB access |
| `registry/` | Company and source CRUD; ATS auto-detection from a pasted URL (policy gate → pattern match → live probe → dedup check); tiering, tags, per-company defaults; CSV bulk import | Pasted URLs, operator edits | `company` and `source` rows; the detection result consumed by `POST /companies/detect` |
| `ingest/` | Run orchestration, per-source failure isolation, normalisation into `job_posting`, identity and change detection, cross-source collapse, the deterministic filter | `RawPosting[]` | `job_posting` rows, `run_log.source_results` |
| `extract/` | JD prose → typed requirements via constrained structured output; evidence-span grounding; the versioned skill vocabulary and its three-stage resolver | `job_posting.description_text` | `requirement` rows with `normalised_skill` |
| `scoring/` | Coverage levels with evidence linkage, weighted bucket arithmetic, the composite formula, tie-breaking, gap assembly | `requirement` + `resume_variant` | `match_score` rows with `gaps` and `evidence` |
| `ledger/` | The claims store, assertion detection, citation resolution, confidentiality gating, expiry, the validation pass and `claim_usage` provenance | Draft text, `claim` rows | Pass/fail verdicts, `claim_usage` rows, `artifact.validation_status` |
| `generate/` | Bullet selection, the `tailoring_plan` diff, cover-letter drafting, the anti-templating similarity check, `.docx` rendering with verified page count | `match_score`, `resume_variant`, ledger | `review_item.tailoring_plan`, `artifact` rows and files |
| `review/` | The queue, the approval state machine, plan editing, artifact freezing on approval | `review_item` | Application creation on approve |
| `mail/` | Gmail read (alert parsing and reply classification), message-to-application linkage, digest composition and send | Gmail API, `application` | `job_posting` rows from alerts, `email_message` rows, `application_event` rows, one digest a day |
| `tracking/` | Lifecycle transitions, event-log append discipline, funnel metrics with sample-size guards, follow-up prompts, spreadsheet export, retention pruning | `application_event` | `application.status`, `v_funnel`, the `.xlsx` workbook |
| `scheduler/` | APScheduler job definitions with a Postgres job store; the Redis run lock | — | Timed invocation of discovery, mail, export, prune |
| `llm/` | Provider-agnostic client (Bedrock default, Azure OpenAI alternate), the prompt registry and versioning, structured-output enforcement with repair, cost metering and the budget circuit breaker | Prompts + data | Validated Pydantic objects, per-call usage records |
| `api/` | FastAPI routers, dependency wiring, the `{data, message, meta}` envelope, cursor pagination, error codes | Services | OpenAPI 3.1 at `/api/v1/openapi.json` |
| `frontend/` | React 18 + Vite + TypeScript, TanStack Query, a client generated from the OpenAPI schema | The API | Dashboard · Queue · Jobs · Companies · Applications · Claims · Variants · Settings |

### 5.1 The interfaces that matter

Three boundaries carry the system's structure. Everything else is ordinary
function calls.

**`sources/` → `ingest/` — the `RawPosting` DTO.** Frozen and `extra="forbid"`.
Freezing means `ingest/` cannot mutate an adapter's output in place, which keeps
fixture-replay tests meaningful. Forbidding extras turns "the vendor added a
field and someone quietly started depending on it" into a test failure. The DTO
deliberately omits `content_hash`, `company_id` and `source_id`: the hash
algorithm lives in exactly one place so an adapter cannot change change-detection
semantics, and the runner already knows which source it invoked, so an adapter
asserting its own company is only an opportunity for the two to disagree.

**`generate/` → `ledger/` — the validation call.** Generation cannot write an
attachable artifact. It produces text; `ledger/` decides whether that text may
become an artifact a `review_item` can reference. The separation is what makes
invariant 3 enforceable, and it is backed by a database trigger, not by
convention.

**`llm/` → everything — the structured-output contract.** No caller of `llm/`
ever sees free text. Every call is constrained to a Pydantic schema, and a
response that does not validate is repaired once and then fails the item. This is
the containment boundary for prompt injection: even a JD that successfully
persuades the model to misbehave can only emit values that fit the schema.

---

## 6. External integrations

Seven, grouped by trust posture and failure behaviour. Postgres and Redis are
runtime dependencies of the application, not integrations, and are covered in
§12 and in `SOLUTION_ARCHITECTURE.md` §9.

| # | Integration | What it is | Direction | Auth | Failure behaviour |
|---|---|---|---|---|---|
| 1 | **Multi-tenant ATS APIs** — Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee | Public JSON job-board endpoints; one adapter serves N employers, config carries the tenant key | Outbound read | **None** | Per-source isolation. Retry with full jitter, per-bucket circuit breaker, auto-disable at 5 consecutive daily failures |
| 2 | **Single-employer career APIs** — Google, Amazon, Microsoft | Public search endpoints for boards too large to enumerate; the query is the config | Outbound read | **None** | Identical to (1) |
| 3 | **Employer careers hosts** — detection probe, one-hop redirect resolution, `robots.txt` | Reached only during `POST /companies/detect` and `POST /sources/{id}/test`, plus a per-host robots fetch cached 24 h | Outbound read | None | Detection returns `reachable: false` rather than erroring; an unreachable `robots.txt` fails the source closed except on six documented public API hosts |
| 4 | **Gmail API — read** | Alert-stream parsing and reply classification | Inbound read | OAuth 2.0, `gmail.readonly` | `invalid_grant` aborts the run without retry, reports `gmail: "unauthenticated"` on `/health`, banners the dashboard, does not advance the cursor |
| 5 | **Gmail API — send** | The daily digest, and nothing else | Outbound send | OAuth 2.0, `gmail.send` | On failure the digest is written to `exports/digest-YYYY-MM-DD.html` and shown in-app; no day's output is lost |
| 6 | **AWS Bedrock** (default LLM) | Extraction, coverage judgement, mail classification (`fast`); tailoring plan and cover letter (`strong`) | Outbound | IAM role / environment credentials | Postings stay unscored and retry next run; generation degrades to an un-tailored base variant; never a partially-scored row |
| 7 | **Azure OpenAI** (alternate LLM) | The same four prompt families behind the same interface | Outbound | API key from environment | As (6). Provider selection is one configuration key; both are provisioned, so neither is a lock-in |

**The never-scrape list applies to every one of the outbound integrations.**
`assert_fetch_allowed` runs inside the shared HTTP client, on every request,
*after* redirects resolve — so a source that 302s to LinkedIn is refused
mid-flight. There is no setting, environment variable or admin toggle that
disables it, and a test asserts the constant is not reachable from `Settings`.

**No integration is authenticated to an employer.** That is a selection
criterion, not an accident: an adapter that would need a scraped session cookie,
a reverse-engineered token or a headless browser login does not get written. The
outbound user-agent is honest, carries a contact address, and never varies per
source. A 403 to that user-agent is treated as a policy signal — the source is
disabled — not as a prompt to impersonate a browser.

---

## 7. Data flow — from ATS endpoint to submitted application

Traced end to end for one posting, naming the row that exists at each step.

**1 · Fetch.** The runner acquires a Redis rate-limit lease on the adapter's
bucket key — which is the rate-limiting *domain*, not the source ID, so several
Adobe Workday sites sharing one tenant host share one bucket. It calls
`adapter.fetch()`. The Workday adapter posts to the CXS list endpoint, pages at
the server-enforced limit of 20, and fans out one detail request per stub because
the list response carries no description. Every request passes the never-scrape
gate and the robots check. The adapter yields a frozen `RawPosting` with
`external_id = jobPostingInfo.id`, HTML already stripped, location parsed,
`posted_at` in UTC or `None` — never fabricated as `now()`.

**2 · Persist.** `ingest/` computes `content_hash = sha256(description_text)` and
upserts on `(source_id, external_id)`. A new identity inserts a `job_posting`
with a ULID primary key. A known identity with a changed hash updates the row and
invalidates its `match_score` rows so it will be rescored. A known identity with
the same hash bumps `last_seen_at` only. A posting absent for two consecutive
runs gets `closed_at` set — which is why a source cancelled by the 180-second
ceiling has its partial output *discarded* rather than ingested: a half-fetched
board looks like "everything else closed" and would close live postings.

**3 · Collapse.** The same role reaching the system through both a company board
and a LinkedIn alert email is collapsed on `(company_id, normalised_title,
location_city)`, keeping the record whose adapter has the higher `fidelity_rank`
— a directly-fetched Workday posting at 85 beats a mail-derived one at 20, so the
canonical row carries the full description rather than the alert's snippet.

**4 · Filter.** Location against `company.location_filter`, seniority against the
operator's band, a keyword deny-list, and `company.status ≠ blacklisted`. Around
80% of new postings stop here with `filtered_out = true` and a recorded
`filter_reason`. They remain queryable; nothing is silently dropped.

**5 · Extract.** The JD is passed to the `fast` model as data, inside a
nonce-delimited block, with an explicit instruction that the block is a third-party
advertisement that cannot issue instructions. Output is constrained to a Pydantic
schema. Every `evidence_span` is then checked to appear verbatim in
`description_text`; a span that does not appear means the model invented a
requirement, and the whole extraction is discarded and retried once at temperature
0. Weights are clamped into the band implied by the emphasis the model assigned —
the model proposes, a deterministic post-pass decides. `requirement` rows land
with `normalised_skill` resolved through a versioned vocabulary: exact alias
lookup first, trigram fuzzy match second, and a closed-vocabulary model call only
on the long tail, which cannot mint a new token.

**6 · Score.** For each active variant, each `hard`/`nice`/`tool` requirement
resolves to `met`, `partial` or `missing`. `met` requires the skill token to be
in the variant's `skill_set` **and** a body bullet to demonstrate it **and**,
where that bullet is quantified, for it to cite claim IDs — a quantified bullet
with no ledger backing degrades to `partial`, so letting the ledger rot lowers
the operator's own scores. Weighted coverage of each bucket produces `H` and `N`;
the composite multiplies the blend by company tier, a recency decay with a floor,
and a hard-requirement gate that drops the score sharply when the must-haves are
absent. `gaps` and `evidence` are written as inspectable JSONB. Exactly one row
per posting, per prompt version, carries `is_recommended`.

**7 · Rank and gate.** Postings whose winning composite clears
`GENERATION_MIN_COMPOSITE` enter generation, capped per run and ordered
dream-tier first so a budget that runs out runs out on volume-tier roles. A
posting below the floor still keeps its scores and gap list and stays visible
under `GET /postings`; the operator can override by importing it manually if they
know something the system cannot see — a referral, most often.

**8 · Generate.** Bullets are shortlisted and pre-ranked in code, then the
`strong` model produces a `tailoring_plan`: a diff against the base variant —
bullet swaps, block reordering, skills-line edits — never a regenerated document.
Where `company.cover_letter_worth` is true, a letter is drafted, including an
honest-gap paragraph built from the top one or two hard gaps and the nearest true
experience. No `.docx` is produced yet: the plan, not the file, is the thing
under review, and rendering before a human has looked wastes work on the roughly
40% of items that get skipped.

**9 · Validate.** Every numeric, currency, percentage, count, duration, rating,
date, superlative and exclusivity word in the drafts is detected — by regex
families and an LLM assertion pass, unioned, longest span winning — and resolved
against the ledger. An unresolved assertion writes the artifact as `failed` with
its notes, triggers one regeneration in which the unresolved spans are quoted
back verbatim alongside the ledger subset the model may draw on, and on a second
failure routes the item to `needs_manual_review`. Failed artifacts are retained
for diagnosis and can never be attached — a database trigger refuses it, and no
endpoint exists at any API version to override the verdict.

**10 · Enqueue and digest.** A `review_item` is created with the plan, the
recommended variant, the match score and any validated artifacts. At 08:15 the
digest reports it: title, company and tier, coverage, hard met/total, the
recommended variant, up to three *named* gaps, validation status, and a deep
link.

**11 · Human gate 1.** The operator opens the item. The gap list renders above
the artifacts, deliberately: three hard gaps at `missing` and `critical` emphasis
is a skip, and a skip costs one click and no tokens because `skipped` is terminal
and never generates. On approve, the `.docx` files are rendered — page count
verified by actually rendering, never assumed — the artifacts are frozen, and an
`application` row is created with status `submitted` in the same transaction.

**12 · Human gate 2.** The operator opens the employer's site and submits.
Nothing in the system observes this. `submitted` is the only state that is an
unverifiable assertion, and the honest cost of not automating submission is that
if the operator approves and never submits, the row is wrong until they correct
it with `PATCH /applications/{id}`.

**13 · Track.** The mail poller reads replies, links each message to an
application by thread ID then by sender domain, classifies it with the `fast`
model, and appends an `application_event` carrying an evidence excerpt and a
confidence. `application.status` is materialised from the event log by trigger;
the log is the truth. `ghosted` is never written — it is a view over the absence
of events, because absence of evidence is not evidence and storing it would make
a non-decision look like one.

**14 · Learn.** `v_funnel` reports submitted → responded → advanced →
interviewed → offers, sliced by variant, tier, week and channel, with rates
suppressed below a minimum sample size and referrals segmented out. This is the
only "selection percentage" the system will ever produce: observed, from the
operator's own history, not predicted.

---

## 8. The human-in-the-loop gates

Two gates, both non-configurable, both visible in the pipeline diagram as the
double rules.

### 8.1 Gate 1 — approve or skip

**What it is.** A single screen carrying everything needed for the decision: the
posting, the coverage numbers, the ranked gap list, the tailoring plan as a diff,
and the validation status of each artifact. Three outcomes: approve, skip with an
optional canned reason, or edit the plan and regenerate.

**Why it exists.** Not as a safety net for a model that might be wrong — though
it is that too — but because the decision it gates is genuinely the operator's.
The system can tell you that a role requires advanced Excel modelling and you
cannot evidence it. It cannot tell you that you are willing to spend the first
month climbing that curve, that you know someone on the team, or that you have
decided to stop applying to that industry. Those are the variables that dominate
the outcome and none of them are in a job description.

**What the gate costs and what it buys.** It costs the operator perhaps ninety
seconds per item. It buys the property that the system's throughput is bounded by
human attention rather than by compute — which is the mechanism, and the only
reliable mechanism, that keeps the system at five to ten applications a week.
Every design that removes this gate ends at two hundred.

**The name is load-bearing.** "Approve" means *I am going to submit this myself*.
It does not mean *the system may now proceed*. There is no next automated step.

### 8.2 Gate 2 — the operator submits

**What it is.** The operator downloads the `.docx` files and completes the
employer's own application form.

**Why it exists.** Three reasons, in order of force.

*It is the compliance boundary.* Automated submission means driving forms on
systems whose terms prohibit it, and at any volume it means defeating bot
detection. The system's honest user-agent and its refusal to run browser
automation for discovery are the same decision applied consistently: a tool that
would spoof a browser to submit is a tool that would spoof a browser to scrape.

*It is the quality boundary.* An ATS form asks questions a resume does not
answer — notice period, visa status, salary expectation, "why this company". A
system that filled those would be inventing answers on the operator's behalf, in
a field a human will read, with no ledger to constrain it. That is precisely the
failure mode the claims ledger exists to prevent, reintroduced at the last step.

*It is the honesty boundary.* Someone must be answerable for the contents of a
submitted application, and it has to be a person. The ledger makes the *facts*
defensible; the human gate makes the *act* attributable.

### 8.3 A third, smaller gate

The ledger validation pass is not a human gate, but it is where a human is
summoned. A draft that fails twice does not degrade quietly into a weaker
document — it becomes `needs_manual_review` and appears in the digest. The worst
case the system permits is an empty queue slot and a line in an email. The worst
case it forbids is a fabricated number in a document a hiring manager reads.

---

## 9. Key design decisions

Each decision states what was chosen, what was considered, and what it costs.

### 9.1 APScheduler in-process, not Celery + a broker

**Chosen.** APScheduler with a Postgres job store, running inside the API
container, with a Redis lock preventing a manual `POST /runs/discovery` from
overlapping the scheduled one.

**Rejected: Celery with RabbitMQ or Redis as broker.** A distributed task queue
buys horizontal worker scaling, task routing, retries with per-task policies, and
result backends. This system has one user, one daily batch of about fifteen
minutes, and no fan-out that a bounded `asyncio` semaphore does not already
handle. The only durability property actually needed — the schedule survives a
restart — is satisfied by a Postgres job store.

**Cost accepted.** A long run occupies the API process. At a 15-minute budget on
a machine that is otherwise idle at 08:00, that is invisible. If run duration
ever became a problem the answer is a second container running the scheduler
against the same database, not a broker.

### 9.2 Postgres full-text and trigram, not a vector database

**Chosen.** `tsvector` for posting search, `pg_trgm` for fuzzy company-name
matching and skill-alias resolution, and a versioned YAML vocabulary for skill
normalisation.

**Rejected: a vector database (Pinecone, Weaviate, Qdrant) with embedded
requirements and embedded resume bullets, matched by cosine similarity.** It is
the obvious modern answer and it is wrong here for four reasons. Matching is
requirement-by-requirement against six fixed variants — the corpus is tiny and
the comparison is structured, not semantic search over an open corpus. An
embedding distance is not explainable: "why did this score 47.5" must terminate
in a row the operator can read, and "the vectors were 0.71 apart" does not.
Embeddings drift with a model upgrade, which would silently re-rank every posting
without a deploy having happened. And a second datastore is a second thing to
back up, secure and operate for one user.

**Cost accepted.** The alias vocabulary needs maintenance. That is handled by the
`skill_proposal` queue: a phrase the resolver cannot place is recorded, never
silently dropped, and enters the vocabulary through a reviewed commit. If
semantic matching is ever genuinely needed, `pgvector` covers it inside the
database already present.

### 9.3 Docker Compose on one VM, not Kubernetes

**Chosen.** Four containers — API + scheduler, Postgres 16, Redis 7, and a static
frontend behind a reverse proxy — on a single small VM, with a nightly `pg_dump`
and a volume for artifacts.

**Rejected: Kubernetes, ECS Fargate as the default, or a serverless
decomposition.** Kubernetes for one container set and one user is a control plane
with nothing to control. Serverless fits badly: the daily run is a stateful
15-minute pipeline with a shared HTTP connection pool and per-host rate-limit
state, which is the opposite of what a function invocation is good at.

**Cost accepted.** No zero-downtime deploy and no automatic failover. A restart
during business hours costs the operator nothing, and a failed run reruns.
`SOLUTION_ARCHITECTURE.md` §9 documents the Fargate topology as the alternative
for the case where the operator stops wanting to own a VM.

### 9.4 LinkedIn via forwarded alert email, never fetched

**Chosen.** LinkedIn, Naukri, Indeed and their peers appear in
`NEVER_FETCH_HOSTS`, a frozen code constant. Their roles reach the system only by
parsing job-alert email that the operator already subscribed to and already
receives, in a mailbox they own.

**Rejected: scraping LinkedIn, with or without a session cookie, with or without
a headless browser.** It has the highest coverage of any single source and it is
still not worth it. It requires an authenticated session, which means the
operator's own account is the credential and the account is what gets banned. It
requires impersonating a browser, which is the same act as evading a refusal. It
is explicitly prohibited by terms the operator agreed to. And the failure mode is
catastrophic and personal: losing the LinkedIn account costs far more than the
postings are worth.

**Cost accepted.** Mail-derived postings have low fidelity — a title, a company, a
location, a snippet, and a link the operator must open. That is honestly labelled
in the digest as "alert only — open to read", those postings carry
`fidelity_rank` 20 so a directly-fetched duplicate always wins, and the system
never fetches the link itself.

### 9.5 No auto-submit, at any price

**Chosen.** Invariant 1, expressed as the absence of any endpoint that submits.

**Rejected: Playwright-driven form filling for the subset of ATS platforms whose
forms are stable.** Technically the most feasible of the rejected options —
Greenhouse and Lever forms are regular and Playwright is already in the stack for
the narrow cases where no API exists. It is rejected because the constraint is not
technical. §8.2 gives the three reasons. The decisive one is that the value of
this system is entirely in *not* being a mass applier, and auto-submit is the one
feature whose presence converts it into one — first as an option, then as the
default, then as the point.

**Cost accepted.** The operator spends five minutes per application on forms, and
`submitted` is an unverifiable assertion. Both are small, and both are stated
plainly rather than engineered around.

### 9.6 A claims ledger, not prompt instructions to be truthful

**Chosen.** Every factual or numeric assertion in a generated document must
resolve to a `claim` row, enforced by a validation pass, a database trigger, and
the absence of an override endpoint.

**Rejected: instructing the model not to fabricate, and reviewing the output.**
Instructions reduce fabrication; they do not eliminate it, and the residual rate
is the thing that matters when the output is a document submitted under the
operator's name. Human review is real but unreliable at ten drafts a day — a
plausible number in a familiar bullet is exactly what a tired reader passes over.

**Cost accepted.** The ledger must be maintained, and a claim that is not in it
cannot be used no matter how true it is. That cost is deliberate and is fed back
into scoring: a quantified bullet with no claim IDs cannot prove a requirement
either, so ledger neglect visibly lowers the operator's own scores.

### 9.7 No selection probability

**Chosen.** Report coverage, named gaps, and observed funnel rates. Never a
predicted probability.

**Rejected: a per-posting "chance of selection" figure**, whether from a
supervised model or a base rate. There is no ground truth to learn from — a year
of a single operator's applications yields a positive class in single digits. The
dominant variables are invisible to the system: an internal candidate already
lined up, a referral, a post-publication headcount freeze, the recruiter's actual
keyword filter, the compensation band. Any one of them outweighs requirement
coverage entirely.

**Cost accepted.** The operator does not get the number they will initially want.
They get a gap list instead, which is more actionable, and a funnel that becomes
meaningful after about forty applications. `API.md` attaches
`meta.note: "Observed rates from your own history. Not a prediction."` to the
funnel endpoint, and that note is part of the contract.

---

## 10. Quality attributes

The system's priority ordering is **correctness of claims → cost → latency**, and
the ordering is not a platitude; it decides real trade-offs.

| Attribute | Target | How it is achieved | What it is traded against |
|---|---|---|---|
| **Correctness of claims** | Zero fabricated assertions in any attachable artifact | Ledger resolution, DB trigger, no override endpoint, evidence-span grounding at extraction, fail-closed when the assertion extractor itself is unavailable | Throughput. A failed validation costs a queue slot |
| **Cost** | < ₹80/day LLM spend, ≈ ₹2,000/month | Stage ④ before any token is spent; deterministic normalisation and scoring; `fast`/`strong` routing; shortlisting to two variants before coverage judgement; cover-letter gating; a daily budget circuit breaker | Recall. A stricter filter loses some marginal postings |
| **Latency (batch)** | Run wall clock < 15 min; digest by 08:15 IST | 8 concurrent sources, shared HTTP/2 client, per-source 180 s ceiling, run-level 900 s cancellation | Completeness of a slow source. A cancelled source is reported, its output discarded |
| **Latency (interactive)** | Detection probe feels instant (≤ 10 s); queue and list endpoints sub-second | One probe request, never paging; cursor pagination; partial indexes on every hot predicate | — |
| **Explainability** | Every number reconstructible by hand from stored rows | No learned model, no embedding distance, no probability; `gaps` and `evidence` stored as inspectable JSONB; `claim_usage` provenance | Sophistication. A smarter unexplainable scorer is not wanted |
| **Reproducibility** | Same input + same prompt version ⇒ same rows | Temperature 0, pinned model IDs (never `-latest`), prompt and vocabulary versions in the row key | Automatic model upgrades |
| **Resilience** | A run always completes and always reports what failed | Per-source isolation, two-layer circuit breaking, auto-disable at 5 failures with a named report | Silent recovery. The system never re-enables itself on a timer |
| **Security / least privilege** | No credential in code or logs; no write scope on the mailbox | `gmail.readonly` + `gmail.send` only; encrypted token file at `0600`; untrusted text always delimited data; SSRF allow-list and host regexes on every config that reaches a URL | Convenience. Not holding `gmail.modify` means maintaining a cursor |

### 10.1 Why that ordering, for a batch system

**Correctness of claims outranks everything** because it is the only failure in
the system that is not recoverable. A broken adapter fetches nothing and gets
reported. A bad score ranks a posting wrongly and the operator skips it. A
misclassified rejection email produces a wrong funnel number that a manual event
corrects. A fabricated number in a submitted resume is discovered by a hiring
manager, in an interview, in front of the person the operator is trying to work
for — and the cost is the operator's credibility, permanently, at that company
and anywhere its people move next. A system that writes documents on someone's
behalf and *can* invent a number is worse than no system, because it manufactures
that risk at ten drafts a day.

**Cost outranks latency because the system is a batch job with a human
downstream.** The operator reads the digest at 08:15. Whether the run took four
minutes or fourteen is unobservable to them; the only latency requirement is
"finished before the digest goes out", and that is a deadline, not a
minimisation target. Spend, by contrast, is a recurring monthly bill on a
personal tool, and it is the constraint most likely to end the project — a job
search that costs ₹6,000 a month in tokens gets switched off before it converts.
So the design pays latency for cost wherever the two conflict: the deterministic
filter adds a stage rather than letting the model sort it out; scoring runs six
variants in code rather than asking a model to compare them; extraction is cached
on `content_hash` so a re-fetched unchanged posting costs nothing.

**Latency still matters, bounded rather than optimised.** The 15-minute budget
exists so that a sick upstream cannot turn a morning batch into an all-day job
holding a run lock. That is why a source has a hard 180-second ceiling, why the
in-run circuit breaker short-circuits forty Workday sources on a down tenant
instead of letting each burn its retry budget, and why waiting on a crowded rate
bucket has a deadline. Every one of those decisions sacrifices completeness for
predictability — the right trade when the output is a queue the operator reads
once.

The one place this ordering inverts is the interactive path. `POST
/companies/detect` is a human waiting on a form, so it gets a 10-second whole-call
budget and one non-paging probe. Maintaining 300 companies is only practical if
adding one feels instant.

---

## 11. Failure modes and degradation

The uniform posture: **fail closed on anything touching data integrity or the
compliance boundary; fail open on enrichment.** An unscoreable posting is visible
and flagged, never silently scored or silently dropped.

| Failure | Blast radius | Behaviour | Operator sees |
|---|---|---|---|
| One adapter returns 5xx or times out | That source | Retry with full jitter within a 90 s budget, then fail the source; partial output discarded | Digest §6, with the error and the failure count |
| A tenant host is down | Every source on that bucket key | In-run breaker opens after 5 consecutive failures; remaining sources short-circuit to `circuit_open` without a request, and this does **not** count against auto-disable | Digest §6 |
| A source fails 5 days running | That source | Auto-disabled, `last_status = 'auto_disabled'`. Re-enabling is a human act; the system never re-enables on a timer, because five consecutive daily failures nearly always means the board moved | Digest §6 and the Settings health table |
| A board moves ATS vendor | That company | The old source auto-disables; detection on a re-pasted URL produces the new adapter and config. Sources are disabled, never deleted, so history survives | Digest, then a two-minute re-add |
| Robots disallows an endpoint | That source | Source disabled. Never bypassed | Digest |
| A redirect lands on a denied host | That request | `DeniedByPolicy` mid-flight, source failed for the run | Digest |
| Extraction returns zero hard requirements | That posting | **Not** scored as a perfect match; routed to `needs_manual_review` | Queue, flagged |
| Evidence-span grounding fails twice | That posting | `needs_manual_review`, counted in run stats | Queue and digest §7 |
| LLM provider unavailable during scoring | That run's scoring | Postings stay unscored, retried next run; never a partially-scored row | Digest §7 |
| LLM provider unavailable during generation | That item | Enqueued with an empty plan and no letter — the base variant is still a legitimate application | Digest notes the degradation |
| Ledger validation fails twice | That artifact | Artifact retained as `failed`, item to `needs_manual_review`, **never attached** | Digest and the queue |
| Assertion extraction itself unavailable | Every draft that run | **Fail closed** — validation returns not-passed. A document nobody checked is never treated as checked | Empty queue slots, explained |
| Daily token budget hit | Generation | Circuit breaker; at the warn threshold generation caps to the top 5 items | Digest §7 shows cost |
| `.docx` will not fit one page after the full ladder | That artifact | `needs_manual_review` with the ladder trace. Never ships two pages | Queue |
| LibreOffice page-count verification fails twice | That artifact | The `.docx` is offered **unverified**, flagged "page count not verified" — the one permitted soft degradation, because a resume the operator can inspect beats no resume | Queue label |
| Gmail refresh token invalid | Mail and digest | Run aborts without retry, cursor **not** advanced, digest written to disk and shown in-app | Persistent dashboard banner with the re-auth command |
| Gmail history cursor expired | One mail run | Bounded full sweep over the two queries, cursor rebuilt. Expected after a multi-day outage; WARN, not an error | Digest run statistics |
| Redis unavailable | Rate limiting, locks, cursor cache | Fail closed on the run lock — a run that cannot prove it is alone does not start. Cursors rebuild from Postgres | `/health` degraded |
| Postgres unavailable | Everything | The system is down. Nightly `pg_dump` is the recovery path | `/health` |
| A cited claim expires between scoring and generation | That bullet | The bullet operation is dropped from the plan with a note. Expired facts are absent facts | Plan diff, and a 30-day expiry lookahead in the digest |
| A duplicate discovery run is triggered | — | 409, a Redis lock rather than an advisory convention | API error |

**Degradation, stated as a sentence:** the system's worst normal day produces a
digest that says which sources failed, which items need a look, and an empty or
short queue. It does not produce a wrong document, a submitted application, or an
email to a stranger — none of those states is reachable.

---

## 12. Scale envelope

Sized deliberately small. This is a personal tool and over-engineering it is the
main risk to it ever being finished.

| Dimension | Design target | Note |
|---|---|---|
| Tracked companies | 300 | 15–30 dream, 80–120 strong, remainder volume |
| Sources polled per run | ~320 | Some companies expose several boards |
| Raw postings per run | 2,000–5,000 | Dominated by Workday tenants |
| New after dedup | ~150 | |
| Surviving the filter | ~30/day | The number that sets token cost |
| LLM extractions/day | ~30 | Plus ~30 coverage judgements and ~25 mail classifications on the `fast` alias |
| Drafts generated/day | ≤ 10 | Hard cap; dream tier first |
| Cover letters/day | ~6 | The rest are `cover_letter_worth = false` |
| Applications submitted | 5–10/week | The output that matters |
| Applications tracked, lifetime | Low thousands | |
| Concurrent users | 1 | |
| Run wall clock | < 15 min | 900 s hard cancellation |
| Daily LLM cost | < ₹80 | ≈ ₹67 at design target — 17% headroom, thin on purpose |
| Gmail quota use | ≈ 8,300 units/day | Roughly 0.0008% of the project daily quota |

Single Postgres instance, single application container, single worker process. No
horizontal scaling is designed for, because none is needed. The growth
sensitivity is known: doubling the company count roughly doubles the postings
surviving the filter and puts spend over budget, and the designed response is a
stricter filter, not a bigger budget.

---

## 13. Assumptions and constraints

### 13.1 Assumptions

| # | Assumption | If it proves false |
|---|---|---|
| 1 | The listed ATS platforms continue to serve unauthenticated public JSON endpoints | Coverage drops to whatever remains plus the mail-alert stream. No adapter is ever rewritten to authenticate or to impersonate a browser |
| 2 | An employer's board is discoverable from a pasted careers URL, possibly after one redirect or an embedded-board scan | The company is added with a `manual` source and postings arrive via import or alert mail |
| 3 | Job descriptions state their requirements in prose a constrained extraction can parse | The posting routes to `needs_manual_review` rather than being scored on a bad extraction |
| 4 | The operator maintains the claims ledger — verifies facts, re-verifies before expiry, retires stale ones | Generation blocks on unresolved assertions and coverage degrades. The incentive is deliberate and self-correcting |
| 5 | The operator honestly records whether they actually submitted after approving | `submitted` counts drift and every funnel rate is optimistic. There is no technical remedy; the correction path is a PATCH |
| 6 | Gmail remains reachable and the OAuth grant stays valid | Alert-sourced postings and automatic status tracking stop; direct fetching, scoring, generation and manual events all continue |
| 7 | A Bedrock or Azure OpenAI account remains provisioned with pinned model IDs available | Stages ⑤, ⑥ and ⑧ stop. Discovery, dedup, filtering and tracking still run; the queue simply does not fill |
| 8 | Roughly thirty postings a day survive the filter | Above it, cost exceeds budget and the filter must tighten. Below it, the operator widens `location_filter` or adds companies |
| 9 | One person's application volume stays in the five-to-ten-a-week range | The design's throughput bound is human attention; a change here is a change of product |

### 13.2 Constraints

| Constraint | Origin | Consequence |
|---|---|---|
| No automated submission | Invariant 1 | No submit endpoint at any version; a test asserts no outbound POST to a non-allowlisted host from `api/`, `review/` or `tracking/` |
| No outbound mail to third parties | Invariant 2 | `send()` takes no recipient argument; a static check over the import graph fails the build if a second call site appears |
| Ledger-only citation | Invariant 3 | A DB trigger blocks attaching a `failed` artifact; no override endpoint exists |
| The never-scrape list is absolute | Invariant 4 | A code constant, unreachable from `Settings`, checked after redirect resolution |
| Adapter failure is isolated | Invariant 5 | A run always completes and always reports per-source outcomes |
| No secrets in code or logs | Invariant 6 | Adapters log `url_template`, not interpolated URLs, so board tokens do not leak; nothing about an OAuth token is logged, not even a length |
| Every artifact is reproducible | Invariant 7 | Model, prompt version, variant ID and claim IDs recorded on every artifact |
| Robots and rate limits respected | Invariant 8 | Per-adapter poll intervals, per-bucket token buckets, `Crawl-delay` may only lower a rate |
| Single-user, single-session | Design | A long-lived local session cookie against an environment password; no registration, no reset |
| All money and percentages are `NUMERIC`/`Decimal` | `DATA_MODEL.md` §1 | Floats never appear in scoring or cost arithmetic |
| UTC internally, IST for display and schedule | `ARCHITECTURE.md` §8 | The 08:00 run fires at 08:00 IST regardless of host timezone |
| One page for a resume | `RESUME_MAX_PAGES = 1` | Verified by rendering, never assumed; a plan that cannot fit fails to `needs_manual_review` |
| Every schema change ships an Alembic revision, ID ≤ 32 chars | `DATA_MODEL.md` §11 | Enum additions are standalone revisions |

---

## 14. Related documents

| Document | Covers |
|---|---|
| `ARCHITECTURE.md` | **Canonical.** Invariants, module boundaries, pipeline stages, scale envelope |
| `SOLUTION_ARCHITECTURE.md` | The same system through a capability, integration, lifecycle and deployment lens; ADR index; technical debt; evolution path |
| `DATA_MODEL.md` | Tables, columns, indexes, constraints, views, migration policy |
| `API.md` | Endpoint contracts, envelope, error codes, and the deliberate absences |
| `SOURCE_ADAPTERS.md` | The adapter protocol, every implementation, rate limits, robots, circuit breaking |
| `COMPANY_REGISTRY.md` | ATS detection, tiering, tags, per-company defaults, dedup, bulk import |
| `MATCH_SCORING.md` | Extraction, skill vocabulary, coverage, the composite formula, gap analysis, evaluation |
| `CLAIMS_LEDGER.md` | The grounding rule, claim anatomy, confidentiality, validation, provenance |
| `DOCUMENT_GENERATION.md` | Tailoring plans, bullet selection, cover letters, rendering, anti-templating |
| `AI_ARCHITECTURE.md` | Provider abstraction, model routing, prompt families, injection defence, cost model |
| `EMAIL_INGESTION.md` | Gmail integration, alert parsing, classification, linkage, the digest |
| `APPLICATION_PIPELINE.md` | Lifecycle state machine, event-log semantics, funnel metrics, export, retention |
| `SECURITY_ARCHITECTURE.md` | Threat model, controls, secret handling |
| `DATA_SOURCES_AND_COMPLIANCE.md` | Legal basis per source, the deny list, documented rate limits |
| `SDD.md` | Design at the lowest altitude — module-level detail |
