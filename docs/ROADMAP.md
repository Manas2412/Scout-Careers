# ROADMAP — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for delivery sequence, phase boundaries and exit gates.
`ARCHITECTURE.md` wins on system-level concerns, `DATA_MODEL.md` on schema,
`API.md` on contracts and `BRD_USER_STORIES_ACCEPTANCE.md` on what a requirement
means. This file decides only *when* each of those is built and *what proves it
is finished*.

---

## 1. Guiding principles

### 1.1 Ship the smallest useful slice

Every phase ends in something the operator can use on a Monday morning without
the next phase existing. A phase whose output is only meaningful once a later
phase lands is not a phase; it is a partial build with a status report attached.
This is enforced by the exit gates in §4 — each one is stated as an operating
outcome, not as a set of merged pull requests.

### 1.2 Discovery and scoring is 80% of the value for 30% of the work

The expensive, slow, judgement-heavy half of job hunting is tailoring. The
tedious, mechanical half is finding the roles worth tailoring for. Only the
second half automates cleanly, and it is the half that carries most of the
benefit:

| Capability | Share of value | Share of build effort |
|---|---|---|
| Ingestion, registry, dedup, filtering, match scoring, digest | ~80% | ~30% |
| Claims ledger, tailoring, letter generation, review UI | ~15% | ~50% |
| Bespoke adapters, mail status ingestion, funnel, export | ~5% | ~20% |

A ranked daily list of thirty roles with named gaps and a recommended variant
already changes how the operator spends their week. It tells them *which* roles
deserve the two hours they were going to spend anyway. The generation layer
saves those two hours; it does not create the decision. That is why Phase 2 is
the stop-able point (§4.2) and why everything past it is optional in a way the
first two phases are not.

### 1.3 Anything on the generation path ships flagged and is proven on a slice

New behaviour that produces text a human sends under their own name ships
`FF_*` off, is enabled for one named slice — `volume`-tier companies, or a fixed
set of source IDs, never a percentage — runs for two weeks, and is promoted only
if the eval holds and the slice metrics did not regress
(`AI_ARCHITECTURE.md` §12, `DOCUMENT_GENERATION.md` §14). The full procedure is
§7 of this document.

### 1.4 Corollaries that decide arguments

- **Build the boring half first.** Ingestion has no interesting decisions in it,
  which is exactly why it must be finished before the interesting decisions get
  attention.
- **No UI before there is something to look at.** Phase 1 ships with no
  frontend. A UI built against an empty database encodes guesses about what the
  data looks like.
- **The invariants are built in Phase 1, not retrofitted.** The never-fetch gate
  and the no-outbound-mail structure exist before the first adapter, because a
  compliance boundary added later is a boundary with holes in it.
- **An unfinished tool is the failure mode**, not a slow one. Scope is the risk
  (§6.4). Every phase is sized so it can be abandoned at its end without waste.

---

## 2. Phase map

| Phase | Objective | Ships | Stop-able? |
|---|---|---|---|
| **1** | Roles land in Postgres, reliably, from four sources | Backend only, no UI | No — nothing is usable yet |
| **2** | The operator gets a ranked daily digest they act on | Registry, scoring, 08:00 run, 08:15 digest, minimal UI | **Yes — this is the stop point** |
| **3** | Coverage becomes defensible and resume advice becomes concrete | Workday, claims ledger, resume recommendation | Yes |
| **4** | Drafts appear, reviewable and validated | Letters, tailoring layer, review queue UI | Yes |
| **5** | The loop closes and measures itself | Bespoke adapters, Gmail status, funnel, export | Terminal |

Phases are sequential. Phase 3 depends on Phase 2's scoring; Phase 4 depends on
Phase 3's ledger; Phase 5 depends on Phase 4's applications existing. There is
no useful parallelism at one developer.

---

## 3. Phase-by-phase delivery

### 3.1 Phase 1 — Ingestion

**Objective.** Postings from Greenhouse, Lever and Ashby, plus roles parsed out
of job-alert mail, land in Postgres, deduplicated and normalised, on a schedule,
with per-source failure isolation. No user interface at all.

**Scope.**

| Area | Work |
|---|---|
| Foundation | Repo layout per `ARCHITECTURE.md` §5, `Settings` via `pydantic-settings`, `structlog`, Docker Compose (Postgres 16 + Redis 7 + app), Alembic |
| Schema | `company`, `source`, `job_posting`, `run_log`; enums `ats_type`, `company_tier`, `company_status`, `run_status`; `pg_trgm` |
| Adapter protocol | `SourceAdapter`, `RawPosting`, `ProbeResult`, shared `SourceHttpClient` with retry, backoff, jitter, rate buckets and timeouts |
| **Compliance gate** | `NEVER_FETCH_HOSTS` as a frozen constant with `assert_fetch_allowed` inside the HTTP client, called after redirect resolution; robots.txt fetch, cache and evaluation |
| Adapters | `greenhouse`, `lever`, `ashby` |
| Mail alerts | Gmail OAuth desktop flow, encrypted token store, `mail_alerts` parser producing low-fidelity postings |
| Ingest | Run orchestration, normalisation (HTML→text, location, seniority, employment type, `posted_at`, title), dedup by `(source_id, external_id)` then `content_hash` then cross-source collapse, two-run close rule |
| Scheduler | APScheduler with a Postgres job store; discovery at 08:00 IST |
| CLI | `scout-careers run discovery`, `scout-careers auth gmail`, `scout-careers company add` |

**Deliverables.** A running container set; a seeded registry of ~40 companies
across the three ATS types; a `run_log` history; the four invariant test
scaffolds with AT-INV-03 (deny list) already passing in full.

**Exit gate.**

| # | Condition |
|---|---|
| 1.1 | Three consecutive nightly runs complete with `status` ∈ {`completed`, `completed_with_errors`}, none ending `running` or crashing the process. |
| 1.2 | ≥ 40 companies across `greenhouse`, `lever` and `ashby` are polled; ≥ 1,000 postings exist in `job_posting`. |
| 1.3 | A deliberately broken source fails alone: the run continues, `run_log.source_results` names it, `consecutive_failures` increments, and the fifth failure auto-disables without deleting the row. |
| 1.4 | Re-running discovery twice in a row creates zero duplicate postings and bumps `last_seen_at` without re-normalising unchanged rows. |
| 1.5 | The two-run close rule is demonstrated: a posting removed from a board is closed after the second miss, not the first. |
| 1.6 | AT-INV-03 passes in full — every URL surface refuses a deny-listed host, zero requests are issued, the constant is unreachable from `Settings`, and a mid-flight redirect aborts. |
| 1.7 | A LinkedIn job alert delivered to the alerts mailbox produces postings, with zero requests to any LinkedIn host during the run. |
| 1.8 | No credential, token or mail body appears in any log line; asserted by test. |

**Explicitly deferred out of Phase 1.** Any frontend. Workday, SmartRecruiters,
Workable, Recruitee, Google, Amazon, Microsoft. Any LLM call. Scoring. The
claims ledger. Generation. The digest. Applications and tracking. Paste-to-detect
(the registry is CLI and CSV in this phase).

---

### 3.2 Phase 2 — Registry, scoring and the daily digest

**Objective.** The operator opens one email at 08:15 and knows which roles are
worth their attention today, with a recommended resume variant and named gaps
for each. **This is the point at which the system becomes useful, and the point
at which stopping is a legitimate outcome.**

**Scope.**

| Area | Work |
|---|---|
| Registry | `POST /companies/detect` — URL pattern table, single live probe, candidate list on ambiguity, 422 `source.undetectable` with the redirect chain, 403 `source.denied_by_policy` before any request. Company CRUD, tiers, tags, status, `location_filter`, `cover_letter_worth`, per-company poll interval, CSV bulk import, deduplication |
| Schema | `resume_variant`, `requirement`, `match_score`; enums `requirement_kind`, `coverage_level` |
| Variants | Six variants seeded (`ai_product`, `ai_enterprise`, `ai_platform`, `backend`, `combined`, `consulting`) with structured `content` and flattened `skill_set` |
| LLM layer | Provider abstraction (Bedrock default, Azure OpenAI alternate), structured-output loop with repair, model routing by alias, prompt registry and versioning, cost accounting from response `usage`, daily budget breaker |
| Filter | Stage ④ — location, seniority, keyword deny-list, blacklisted company. Pure boolean, zero model calls |
| Extraction | JD → `Requirement[]` with `kind`, `weight`, `ordinal`, `evidence_span`; the three-stage skill resolver and the controlled vocabulary |
| Scoring | Coverage per variant, weighted coverage, composite score, ranking, `is_recommended`, gap analysis |
| Digest | Composition and send at 08:15 IST — headline, review queue, alert stream, source failures, run statistics; multipart with a complete plaintext part |
| API + minimal UI | `/postings`, `/companies`, `/variants`, `/runs`, `/health`, `/settings`; React pages for Companies, Jobs and Settings — enough to add a company and inspect a score |
| Eval | The 40-JD golden set and `scripts/eval_scoring.py` in CI |

**Deliverables.** A ranked daily digest. A Companies page that maintains 300
employers by pasting URLs. A Jobs page with the gap list per posting. A scoring
eval that gates every prompt, vocabulary and formula change.

**Exit gate.**

| # | Condition |
|---|---|
| 2.1 | Ten consecutive digests arrive at 08:15 IST, including on days with nothing to report and on a day the run failed. |
| 2.2 | The registry holds ≥ 150 companies, ≥ 100 of them added through paste-to-detect rather than CSV or CLI. |
| 2.3 | A discovery run at ≥ 150 sources completes in under 15 minutes. |
| 2.4 | The Tier 1 golden set passes every gate: hard recall ≥ 0.90, hard precision ≥ 0.85, `kind` accuracy ≥ 0.88, normalisation ≥ 0.95, coverage κ ≥ 0.70, ranking ρ ≥ 0.75, grounding violations **0**. |
| 2.5 | Measured daily LLM spend, computed from `usage`, is under ₹80 across ten consecutive runs. |
| 2.6 | The deterministic filter eliminates ≥ 70% of new postings before any model call, and issues zero model calls itself. |
| 2.7 | **The operator, over two weeks, used the digest to choose what to apply to on ≥ 8 of 10 working days.** |
| 2.8 | Exactly one `match_score` row per posting carries `is_recommended`, and every gap in the digest names an actual requirement rather than a category. |
| 2.9 | A deny-listed URL pasted into detection returns 403 with zero requests issued, and the message names the two supported routes. |

Gate 2.7 is the one that matters and the only one that cannot be faked. The
others are preconditions for it.

**Explicitly deferred out of Phase 2.** The claims ledger. Any document
generation, resume or letter. The review queue and its state machine.
Applications, events, mail status ingestion, the funnel and the export. Workday
and every bespoke adapter. A tailoring layer of any kind.

**Why stopping here is legitimate.** At the end of Phase 2 the operator has a
ranked daily list of roles across 150–300 employers, with per-role coverage
against six resume variants and a named gap list. They still write their own
resume edits and their own letters — which they were going to do anyway, and
which is the part they are good at. What they no longer do is spend an hour a
day trawling boards, and what they gain is a defensible answer to "which five
roles this week". If the project ends here it has paid for itself, and every
subsequent phase should be justified against that baseline rather than against
zero.

---

### 3.3 Phase 3 — Workday, the ledger and resume recommendation

**Objective.** Coverage becomes trustworthy enough to act on, and the resume
advice becomes concrete: *use this variant, here is what it covers, here is what
it does not.*

**Scope.**

| Area | Work |
|---|---|
| **Workday adapter** | Tenant/site/host config, the POST-based search endpoint, pagination, detail fetch, per-tenant rate buckets, in-run circuit breaker |
| Coverage widening | `smartrecruiters`, `workable`, `recruitee` — cheap once the Workday detail-fetch pattern exists |
| Schema | `claim`, `claim_usage`, `artifact`; enums `claim_confidentiality`, `artifact_kind`, `artifact_validation` |
| Ledger | Seeded starter ledger across the operator's real projects; `key` convention, canonical `statement`, metric value/unit, `evidence_ref`, confidentiality tiers, paired public/restricted claims, `expires_at` and decay |
| Validation pass | Regex assertion families plus the LLM assertion pass, union with longest-span wins; resolution against the ledger; `POST /claims/validate` |
| **Enforcement** | The database-level constraint preventing a non-`passed` artifact from attaching to a `review_item` or `application`; AT-INV-04 passing in full |
| Resume recommendation | Per-posting coverage table rendered in the UI and the digest: requirement, kind, level, evidence bullet, claim IDs; recommended variant with the runner-up and the margin |
| Rendering | `.docx` render of an unmodified variant via the existing builder, page-count verified by rendering; `POST /variants/{id}/render` |
| UI | Claims page (CRUD, usage, expiry warnings), Variants page, coverage table on the posting detail |

**Why Workday first.** It is the single biggest coverage win available. A large
share of the employers the operator cares about — enterprise, Indian IT
services, most large product companies outside the Greenhouse/Lever/Ashby
startup band — run Workday and nothing else. It is also the most expensive
adapter to write (POST search, tenant-specific hosts, a required detail fetch,
aggressive rate behaviour), which is why it waits until the pipeline around it
is proven rather than being debugged simultaneously with the scorer.

**Exit gate.**

| # | Condition |
|---|---|
| 3.1 | ≥ 30 Workday tenants poll successfully on three consecutive nights, with detail fetches inside the run's wall-clock budget. |
| 3.2 | Registry coverage reaches ≥ 250 companies with ≥ 300 sources, and the run still completes in under 15 minutes. |
| 3.3 | The ledger holds every claim appearing in the six variants; a report of variant text against the ledger shows zero unbacked numeric or superlative assertions. |
| 3.4 | AT-INV-04 passes in full, including the raw-SQL bypass case and the absence of any override endpoint. |
| 3.5 | `POST /claims/validate` correctly resolves a known-good paragraph and correctly rejects a paragraph containing one fabricated figure. |
| 3.6 | An expired claim blocks a document under the default `fail` policy, and a `restricted` claim is refused for a non-allow-listed employer with the public sibling attempted. |
| 3.7 | A rendered variant `.docx` is verified to `RESUME_MAX_PAGES` by actual rendering, not estimation. |
| 3.8 | **The operator can look at a coverage table and say which variant to send without opening the JD.** |

**Explicitly deferred out of Phase 3.** Any generated prose — no cover letters,
no rephrasing, no model-composed bullets. The tailoring layer. The review queue
state machine. Applications and tracking. Google, Amazon and Microsoft adapters.

---

### 3.4 Phase 4 — Generation, tailoring and the review queue

**Objective.** The system drafts, and the operator reviews a diff rather than a
document. This is the first phase whose output leaves the machine under the
operator's name, and it ships behind flags accordingly.

**Scope.**

| Area | Work |
|---|---|
| Schema | `review_item`; enum `review_status` |
| Tailoring layer | `tailoring_plan` schema and Pydantic models; bullet bank and selection algorithm; block reordering; skills-line edits; every proposed bullet carrying its claim IDs |
| Application of plans | `apply_plan` over the render model; the one-page fit loop with `tight` mode and content trimming, verified by rendering |
| Cover letters | Structure, length target, the honest-gap paragraph computed from this role's `gaps`, placement by severity, the banned-opener lint |
| Cover-letter gating | `company.cover_letter_worth` respected; no tokens spent on letters no employer reads |
| Anti-templating | Per-role computed content, deterministic structural variation from `hash(posting_id)`, MinHash/LSH similarity at letter and paragraph level with warn/block thresholds, weekly corpus drift monitoring |
| Validation in the loop | Every generated artifact validated before it can attach; regeneration on failure; `needs_manual_review` after retries |
| Review queue | `GET /review`, `GET /review/{id}` returning the whole decision on one call; `POST /generate`, `/approve`, `/skip`; `PATCH /plan`; artifact download |
| Review UI | The queue list and the item screen — posting, coverage, gaps, evidence, plan diff, letter preview, validation badges, approve/skip with canned reasons |
| Flags | `FF_TAILORING_REPHRASE` and `FF_GAP_IN_OPENING` shipped **off** |

**Deliverables.** A review queue the operator clears in ten minutes. Downloadable
`.docx` resume and letter per approved item. A generation eval alongside the
scoring eval in CI.

**Exit gate.**

| # | Condition |
|---|---|
| 4.1 | Twenty consecutive drafts generated with zero uncited assertions reaching an attached artifact. |
| 4.2 | Median time from opening a review item to recording a decision is ≤ 90 seconds across ≥ 30 decisions. |
| 4.3 | Every rendered resume is within `RESUME_MAX_PAGES`, verified by rendering. |
| 4.4 | Across the first 30 letters, maximum pairwise similarity stays below `SIMILARITY_BLOCK` and no paragraph exceeds the stricter paragraph threshold. |
| 4.5 | AT-INV-01 passes in full — no submit path in the schema, and an approve flow issuing zero outbound POSTs to any employer host. |
| 4.6 | `PATCH /review/{id}/plan` regenerates and re-validates, and the operator's edit is visible in the resulting artifact's provenance. |
| 4.7 | Both generation flags remain `false` in the deployed configuration at the end of the phase; enabling either is a §7 exercise, not part of this gate. |
| 4.8 | **The operator submitted ≥ 5 applications in one week using system-prepared documents without editing the `.docx` by hand afterwards.** |

Gate 4.8 is the honest test of the tailoring layer. Hand-editing the output
after download means the plan was wrong and the correction was not captured.

**Explicitly deferred out of Phase 4.** Applications, events, mail status
ingestion, the funnel and the export — an approved item creates an `application`
row in Phase 5, and until then approval simply freezes artifacts and marks the
item decided. Google, Amazon and Microsoft adapters. Follow-up prompts.

---

### 3.5 Phase 5 — Bespoke adapters, tracking and the funnel

**Objective.** The loop closes. Applications are tracked from submission to
outcome using mail the operator already receives, and the system reports what
actually converted.

**Scope.**

| Area | Work |
|---|---|
| Bespoke adapters | `google`, `amazon`, `microsoft` — one employer each, each worth an adapter on volume alone |
| Schema | `application`, `application_event`, `email_message`; enums `application_status`, `mail_class`; views `v_funnel`, `v_ghosted` |
| Approve → application | `POST /review/{id}/approve` creates the `application` row, freezes artifacts, records `source_channel` and `referral_contact` |
| Mail status ingestion | Classification into `mail_class` with confidence thresholds; the message-to-application resolution chain including the shared-ATS-domain case; the hold-for-review path below threshold |
| Event log | Append-only `application_event`, legal-transition rules, idempotent append, order-independent projection, the status trigger |
| Manual events | `POST /applications/{id}/events` with `is_manual = TRUE` and NULL confidence |
| Ghosting | `v_ghosted` as a view; `ghosted` absent from the enum and unassignable |
| Funnel | `GET /metrics/funnel` grouped by variant, tier, week or channel; counts with intervals; rate suppression below `MIN_N_FOR_RATE`; comparison suppression below `MIN_N_FOR_COMPARISON`; direct and referral never merged |
| Follow-up prompts | Quiet periods per status, per-day and per-application caps |
| Export | Nightly `.xlsx` of the full pipeline, conditional highlighting for stale applications, correct on an empty system |
| Retention | Pruning of closed postings and mail metadata, never orphaning provenance |
| UI | Applications page, Dashboard with the funnel |

**Exit gate.**

| # | Condition |
|---|---|
| 5.1 | ≥ 30 applications tracked end to end, with ≥ 80% of status transitions arriving from mail classification rather than manual entry. |
| 5.2 | Zero false status regressions across the tracked set; every rejected transition is inspectable. |
| 5.3 | Replaying the full mail history produces identical event rows — no duplicates, and the same final statuses. |
| 5.4 | The funnel returns counts with intervals for every row, suppresses rates below `MIN_N_FOR_RATE`, and contains no figure merging direct with referral. |
| 5.5 | The nightly export runs for seven consecutive nights and opens cleanly, including against an empty database. |
| 5.6 | AT-INV-02 passes in full — the only recipient of the only outbound mail class is the operator, the recipient cannot be varied by a caller, and no ingested address is ever a destination. |
| 5.7 | Pruning deletes nothing referenced by an application or an event. |
| 5.8 | **After 40 applications the operator can name, from the funnel, which variant and which company tier converted, with the sample size and interval in front of them.** |

**Explicitly deferred out of Phase 5.** Everything in §5.

---

## 4. Phase summary table

| Phase | Adapters live | LLM stages | Human surface | Stop-able |
|---|---|---|---|---|
| 1 | greenhouse, lever, ashby, mail_alert | none | CLI only | No |
| 2 | + none | extract, coverage | Digest + minimal UI | **Yes — the stop point** |
| 3 | + workday, smartrecruiters, workable, recruitee | + validation assertions | + Claims, Variants, coverage tables | Yes |
| 4 | + none | + tailoring plan, cover letter | + Review queue | Yes |
| 5 | + google, amazon, microsoft | + mail classification | + Applications, Dashboard | Terminal |

---

## 5. Deferred backlog

Items that are real, considered, and deliberately not in any of the five phases.
Each carries the reason, because a backlog without reasons is a wish list that
re-argues itself every quarter.

| Item | Reason for deferral | What would change the decision |
|---|---|---|
| **Meta adapter** | `facebook.com` is on `NEVER_FETCH_HOSTS`, and the Meta careers surface is behind that boundary. The adapter cannot be written without weakening invariant 4, which is not a trade that is on the table | A documented public JSON endpoint on a host that is not deny-listed |
| **Apple adapter** | The board is JavaScript-rendered with no public JSON contract, requiring Playwright and per-session tokens. High build cost, high breakage rate, one employer of return | Apple publishing a stable public endpoint |
| **Multi-user support** | One operator. Auth, tenancy, per-user ledgers, per-user variants and a permission model are a rewrite of half the system for a beneficiary that does not exist. `API.md` §1 states the single-user decision and `SECURITY_ARCHITECTURE.md` §4 states what changing it costs | A second real user with their own ledger — not a hypothetical one |
| **Browser autofill extension** | Sits one inch from invariant 1. The boundary "no automation touches the employer's form" is trivially defensible; "automation may fill but not submit" is a boundary that erodes under its own convenience. Also a large surface — per-ATS field mapping, credential handling, extension distribution — for a saving of perhaps three minutes per application | Only after the funnel proves that form-filling time, not decision time, is the binding constraint. It is currently not |
| **Referral-contact tracking** | `application.referral_contact` is a free-text field in Phase 5, which is enough to segment the funnel. A contact CRM — people, companies, conversation history, reminders — is a second product, and the outreach it would exist to support is human work by design (invariant 2) | The operator maintaining ≥ 20 active referral relationships and losing track of them in a spreadsheet |
| **Salary data enrichment** | Available sources are self-reported, stale, or scraped from hosts on the deny list. An unreliable number displayed next to reliable ones borrows their credibility, which is worse than an empty field | A source with a documented public API and stated methodology |
| **Interview preparation from the JD** | A different loop with a different cadence: it runs after an interview is scheduled, not during discovery, and its inputs are the company and the panel rather than the posting. Bolting it onto the discovery pipeline would distort both | Phase 5 complete and interviews actually occurring at a rate that makes preparation the bottleneck |
| **Mobile view of the review queue** | The digest *is* the mobile surface, and it is deliberately complete in plaintext. Reviewing a tailoring diff on a phone is a bad experience regardless of how well it is built | Evidence from O1 timing that decisions are being deferred because the operator is not at a laptop |
| **Additional resume variants beyond six** | Six variants already produce a shortlisting step before coverage judgement, without which the cost model breaks (`AI_ARCHITECTURE.md` §8.3). A seventh must displace an existing one | Funnel data showing one variant never wins and one gap recurs across skipped items |
| **`pgvector` for company deduplication** | Trigram matching on `company.name` plus domain and slug signals is sufficient at 300 companies | Registry beyond ~1,000 companies with observed dedup misses |
| **Celery, Kubernetes, horizontal scaling** | One user, one daily batch. Rejected in `ARCHITECTURE.md` §4.1 and not reopened here | Nothing within this product's definition |

---

## 6. Risks and mitigations

### 6.1 ATS endpoints change shape

**Risk.** Greenhouse, Lever, Ashby and Workday endpoints are public but
undocumented as contracts. A field rename or a pagination change silently
breaks an adapter, and the failure is quiet — an adapter that returns zero
postings looks identical to a company with no openings.

**Impact.** Silent coverage loss, which is the worst kind because nothing alerts.

**Mitigation.**
- Per-adapter contract tests against recorded fixtures, so a shape change fails
  in CI rather than in production.
- `consecutive_failures` auto-disables at 5 and the digest names the source
  explicitly — no source ever disappears quietly (R4).
- A **zero-postings-from-a-previously-productive-source** check: a source that
  returned ≥ 10 postings last week and 0 today is reported as suspicious in the
  digest, not treated as an empty board.
- Adapters are thin and isolated by contract (`sources/` never touches the
  database), so a repair is a single-file change.
- Every adapter declares its poll interval and rate bucket, so a fix does not
  require re-reasoning about load.

### 6.2 LLM cost overrun

**Risk.** The design sits at ~₹67/day against a ₹80 budget — 17% headroom, thin
on purpose. Registry growth, a longer JD corpus, a prompt that grows, or a
retry storm each push it over.

**Impact.** The tool becomes something the operator resents paying for, which is
an abandonment path (§6.4).

**Mitigation.**
- Spend is computed from response `usage` on every call, not estimated. The
  meter reports the truth (R73).
- `LLM_DAILY_BUDGET_INR` is a circuit breaker, not a report. At
  `LLM_BUDGET_WARN_PCT` generation is capped to the top items; at 100% the
  breaker opens and the run reports the truncation (R74).
- Stage ④ — the deterministic filter — saves more per day than the entire
  budget (₹87.85/day at design scale). It contains no model call precisely so
  that it cannot itself become the cost.
- Cover-letter gating and two-variant shortlisting are structural economies, not
  optimisations to be revisited under pressure.
- The documented response to growth is **a stricter filter, not a bigger
  budget** (`AI_ARCHITECTURE.md` §8.4).

### 6.3 Generated letters become detectably templated at volume

**Risk.** At forty letters each is read alone. At four hundred, some subset
reaches the same recruiter, agency or shared-ATS reviewer. Detectable formula at
that point is worse than sending no letter, because it retroactively converts
every application into evidence that none were written by hand.

**Impact.** Reputational, cumulative, and invisible until it has already
happened.

**Mitigation.**
- Content is computed per role, not filled per template: the gap paragraph comes
  from *this* role's gaps, the evidence paragraph from *this* role's top met
  requirement, the ninety-days paragraph from *this* role's responsibilities.
- Structure varies deterministically from `hash(posting_id)` — reproducible, as
  invariant 7 requires, but not fixed.
- MinHash/LSH similarity against every prior letter: warn at 0.55, block and
  regenerate at 0.72; stricter at paragraph level (0.45 / 0.60); two consecutive
  blocks route to `needs_manual_review` rather than looping.
- Weekly corpus drift monitoring — mean pairwise similarity and type-token ratio
  across the last thirty letters — catches the failure mode where every letter is
  individually fine and the *set* is formulaic. A rise past threshold is a prompt
  defect requiring a version bump.
- The volume ceiling itself is a mitigation: 5–10 letters a week never reaches
  the density at which the risk bites.

### 6.4 The operator abandons the tool — the most likely failure

**Risk.** This is the highest-probability failure mode by a wide margin, and it
does not look like a failure. It looks like a system that runs correctly every
morning into an inbox nobody opens. Personal tools die of scope: the build
extends, the daily payoff stays theoretical, and by the time Phase 4 lands the
operator has gone back to a spreadsheet.

**Impact.** Total. Every invariant held and the product still failed.

**Mitigation.**
- **Phase 2 is genuinely useful on its own, and stopping there is a legitimate
  outcome, not a defeat.** This is the primary mitigation and the reason the
  phase boundary is drawn where it is. A ranked daily digest with named gaps
  changes the operator's week without any of Phases 3–5 existing.
- Exit gates are stated as operating outcomes, not merge counts. Gate 2.7 —
  "used the digest to choose what to apply to on ≥ 8 of 10 working days" —
  cannot be satisfied by shipping code.
- Ten minutes a day is a hard constraint on every design decision, not an
  aspiration. Features that cannot be operated inside it are out of scope no
  matter how good they are.
- The digest always sends, including with nothing to report. A daily artefact
  that silently stops arriving is indistinguishable from a quiet day, and that
  ambiguity is what breaks the habit.
- The scale envelope is deliberately small (`ARCHITECTURE.md` §9), and
  over-engineering is named there as the main risk to the tool ever being
  finished. Scope creep is the disease; the phase gates are the treatment.
- Every phase is abandonable at its exit gate with the work to date still
  standing on its own.

### 6.5 Gmail OAuth token expiry

**Risk.** A refresh token becomes invalid when the operator revokes access,
changes their Google password, deletes the app, or leaves it unused for six
months. The failure is `400 invalid_grant`, and it takes out mail status
ingestion, job-alert parsing **and** the digest simultaneously — because the
digest sends through the same credential.

**Impact.** The system goes silent on exactly the surface the operator uses to
notice it is alive.

**Mitigation.**
- The mail run aborts immediately without retry. `invalid_grant` is permanent;
  retrying a permanent failure only burns quota.
- `run_log` records `status='failed'`, `error='gmail_auth_invalid_grant'`.
- `GET /health` reports `gmail: "unauthenticated"` at 200, and the dashboard
  shows a persistent banner with the re-authorisation command.
- **The digest is written to `exports/digest-YYYY-MM-DD.html` and shown in-app**
  rather than lost. No day's output disappears because a credential expired.
- The Gmail cursor is **not** advanced, so nothing is skipped; on recovery the
  full unread window is processed.
- Recovery is one CLI command and one browser consent.
- The token file is `0600`, encrypted at rest with a key from a separate source,
  outside the repo and outside the Docker build context, and nothing about it —
  not a value, prefix, length or hash — is ever logged.
- Access-token refresh happens 300 seconds before expiry and is serialised by a
  Redis lock, so a poll and a digest send never invalidate each other's token.

### 6.6 Risk summary

| Risk | Likelihood | Impact | Primary mitigation |
|---|---|---|---|
| Operator abandonment | **High** | Total | Phase 2 useful alone; outcome-stated gates; 10-minute constraint |
| ATS shape change | High | Moderate, silent | Contract tests; zero-posting anomaly detection; auto-disable + digest |
| LLM cost overrun | Medium | Moderate | `usage`-based metering; circuit breaker; stage ④ |
| Gmail token expiry | Medium | High, temporary | Fail-fast, digest to disk, cursor unadvanced, one-command recovery |
| Templated letters at volume | Low at 5–10/week | High, cumulative | Per-role content; deterministic variation; MinHash block; drift monitoring |

---

## 7. Prove before flipping

No change to scoring or generation becomes default because it looked better. The
procedure below is identical for a prompt change, a formula change, a vocabulary
change, a model-ID change or a provider switch.

### 7.1 The golden sets

| Set | Contents | Gates |
|---|---|---|
| **Scoring golden set** — `tests/golden/scoring/` | 40 hand-labelled JDs with expected requirement lists, kinds, normalised tokens and per-variant coverage levels | Hard recall ≥ 0.90 · hard precision ≥ 0.85 · `kind` accuracy ≥ 0.88 · normalisation ≥ 0.95 · coverage κ ≥ 0.70 · ranking ρ ≥ 0.75 · **grounding violations 0** |
| **Generation golden set** — `tests/golden/generation/` | Paired JD + expected tailoring plan shape + a known-good letter per archetype | Zero uncited assertions · zero letters above `SIMILARITY_BLOCK` · every resume within `RESUME_MAX_PAGES` by rendering · banned-opener lint clean |

Grounding violations and uncited assertions gate at **zero**, not at a
threshold. A fabricated evidence span is the same class of failure as an uncited
claim in a generated document, and both are build failures rather than warnings.

### 7.2 The procedure

```
1. VERSION      Bump the version string in the same commit as the change.
                extract.v3 → extract.v4, or score.v2 → score.v3, or
                cover_letter@2026-09-01.2 → @2026-09-14.1.
                A formula change that reuses a version string makes two
                incomparable score families indistinguishable in the
                database and defeats every comparison below.

2. EVAL         Run the relevant golden set in CI. Every gate must pass.
                A regression on any single metric blocks the change,
                including a regression that comes with an improvement
                elsewhere — those are two changes and are argued separately.

3. A/B IN PLACE The UNIQUE (posting_id, variant_id, prompt_version) key on
                match_score means old and new versions coexist per posting.
                Rescore a fixed sample under the new version and diff the
                rankings against the old. Inspect every inversion by hand.

4. FLAG         Ship the behaviour behind FF_*, defaulting off. The code
                path exists in production and is unreachable.

5. SLICE        Enable for one NAMED slice — volume-tier companies, or a
                fixed set of source IDs. Never a percentage: at ten
                generations a day a percentage rollout is noise, and a
                named slice is reproducible.

6. TWO WEEKS    Run. Collect: validation pass rate, repair-retry rate,
                cost per item, operator acceptance rate, similarity
                distribution, and — where n permits — response rate
                from v_funnel.

7. PROMOTE      Default on only if the eval still holds AND no slice
                metric regressed. Otherwise revert to step 4 with what
                was learned.

8. ROLLBACK     A settings change, never a deploy. Every flagged path is
                written so the off state is a COMPLETE correct behaviour,
                not a degraded one.
```

### 7.3 What is compared, and against what

| Change | Tier 1 gate | Tier 2 signal (weeks) | Tier 3 signal (months) |
|---|---|---|---|
| Extraction prompt | Golden-set recall, precision, `kind`, grounding | Skip-reason mix — a rise in `wrong domain` means extraction is misclassifying | — |
| Skill vocabulary | Normalisation accuracy | Unresolved-phrase rate into `skill_proposal` | — |
| Scoring formula | Coverage κ, ranking ρ | Precision@10 ≥ 0.5; inversions inspected by hand | AUC of `composite_score` for `responded`, ≥ 0.62 at n ≈ 40 |
| Tailoring plan prompt | Zero uncited assertions; plan applies cleanly | Operator edit rate on plans | Response rate by variant, `n ≥ MIN_N_FOR_COMPARISON` |
| Cover letter prompt | Similarity, lint, gap paragraph derived from real gaps | Similarity distribution across the last 30 | Response rate, direct channel only |
| Model ID or provider | Both golden sets, unchanged gates | Cost per item, repair-retry rate | — |

### 7.4 The rules that make this honest

- **Tier 1 gates every change. Tier 2 informs. Tier 3 accumulates.** At forty
  applications an AUC of 0.62 has an interval that includes 0.5. The correct
  response to a Tier 3 result is to note it and wait, not to re-tune a formula on
  twelve data points — that is how a scorer overfits to one quarter's job market.
- **Outcomes evaluate the scorer; they never train it.** There is no automatic
  promotion path from observed data to a changed weight.
- **A model-ID change is a prompt change.** Eval first, then a pinned bump in
  configuration, then the same staleness handling for in-flight review items.
  There is no auto-upgrade, because an artifact whose model cannot be named
  violates invariant 7.
- **A prompt change invalidates every artifact produced by the old version.**
  In-flight review items are regenerated rather than shipped with mixed
  provenance.
- **Feature-flag defaults are part of the exit gate.** Phase 4 exits with both
  generation flags still `false`; flipping either is a separate §7 exercise with
  its own two weeks.

---

## 8. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | Canonical for invariants, pipeline stages and the scale envelope every phase is sized against |
| `BRD_USER_STORIES_ACCEPTANCE.md` | What each phase must satisfy: requirements R1–R92, the invariant tests, and the project definition of done |
| `DATA_MODEL.md` | The tables introduced in each phase, and the migration policy governing them |
| `API.md` | The endpoints introduced in each phase; §8 is the API-surface expression of the invariants |
| `SOURCE_ADAPTERS.md` | Adapter roster, fidelity ranking, the deny list and robots policy delivered in Phases 1, 3 and 5 |
| `COMPANY_REGISTRY.md` | Paste-to-detect, tiering and bulk import delivered in Phase 2 |
| `MATCH_SCORING.md` | The scoring delivered in Phase 2 and the three evaluation tiers behind §7 |
| `CLAIMS_LEDGER.md` | The ledger and its enforcement delivered in Phase 3 |
| `DOCUMENT_GENERATION.md` | The generation and anti-templating delivered in Phase 4, and its own rollout discipline |
| `APPLICATION_PIPELINE.md` | The tracking, funnel and export delivered in Phase 5 |
| `EMAIL_INGESTION.md` | Mail-alert parsing (Phase 1), the digest (Phase 2) and status ingestion (Phase 5) |
| `AI_ARCHITECTURE.md` | The cost model behind §6.2 and the feature-flag discipline behind §7 |
