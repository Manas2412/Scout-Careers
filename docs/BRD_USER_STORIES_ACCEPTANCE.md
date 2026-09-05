# BUSINESS REQUIREMENTS, USER STORIES AND ACCEPTANCE CRITERIA — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for *what the system must do and how that is proven*.
`ARCHITECTURE.md` wins on system-level concerns, `DATA_MODEL.md` on schema and
`API.md` on endpoint contracts. Where this file states a requirement that
contradicts any of those three, this file is wrong and must be corrected.

This document is the contract between the intent and the build. Every
requirement here is numbered, every user story traces to at least one
requirement, and every acceptance criterion is written so that it can be
executed rather than debated.

---

## 1. Background and the problem

### 1.1 The conversion arithmetic

Job applications convert at rates that differ by an order of magnitude depending
on how they were prepared.

| Application type | Observed conversion to first response |
|---|---|
| Generic resume, mass-applied | **1–2%** |
| Tailored resume plus a role-specific cover letter | **10–15%** |
| Tailored plus an internal referral | Materially higher again |

The arithmetic that follows is the entire reason this system exists. Two hundred
generic applications yield two to four responses and cost roughly forty hours.
Twenty tailored applications yield two to three responses and cost roughly the
same forty hours, because tailoring by hand is the expensive part. The two
strategies therefore look equivalent to anyone measuring effort against
outcomes — and that equivalence is what drives applicants toward volume, which
is the losing side of the trade because it degrades standing with shared ATS
platforms and produces no compounding signal.

The asymmetry Scout Careers exploits is that the two halves of the work have
opposite economics:

- **Discovery** is high-volume, low-signal, repetitive, and almost entirely
  mechanical. It is the half that automates well and the half no existing tool
  bothers to do properly.
- **Tailoring** is low-volume, high-signal, expensive, and requires judgement
  about what is true. It is the half that automates badly, and the half every
  existing tool automates anyway.

Existing tools automate the wrong half. They mass-submit generic applications,
which converts at the 1–2% rate while consuming the applicant's reputation.

### 1.2 The design goal

**Five to ten excellent applications per week, at a cost of ten minutes per
day.** Not two hundred poor ones.

Ten minutes per day is the binding constraint, not an aspiration. It sizes the
digest (readable on a phone in under a minute), the review screen (one item
decidable in ninety seconds), the queue depth (≤ 10 drafts per run) and the
scale envelope (`ARCHITECTURE.md` §9). Any feature that cannot be operated
inside that budget is out of scope regardless of its merit.

### 1.3 Who operates it

One person. There is no second user, no team, no tenancy model and no
permission system. The operator is also the administrator, the reviewer and the
only recipient of any mail the system sends. `API.md` §1 and
`SECURITY_ARCHITECTURE.md` §4 explain why single-user is the correct call for a
personal tool and what changing it would cost.

---

## 2. Objectives and measurable success criteria

Objectives are stated as numbers with a measurement method, because a business
requirement that cannot be measured is a preference.

| # | Objective | Measure | Target | Measured by |
|---|---|---|---|---|
| O1 | Reviewing a queued item is fast | Median wall-clock from opening a `review_item` to recording a decision | **≤ 90 seconds** | Client-side timing between `GET /review/{id}` and the approve/skip call, logged per decision |
| O2 | Daily operating cost of attention is bounded | Total operator time per day: digest + queue decisions | **≤ 10 minutes** | Sum of O1 across decisions in a day, plus digest read time |
| O3 | Output volume is in the useful band | Applications reaching `submitted` per calendar week | **5–10** | `count(application) GROUP BY week` |
| O4 | Applications are tailored, not generic | Share of submitted applications with a `review_item` whose `tailoring_plan` is non-empty and validated | **100%** | `application JOIN review_item` |
| O5 | The funnel improves on the operator's own baseline | Response rate (`v_funnel.responded ÷ (submitted − withdrawn)`) after 40 applications, versus the operator's recorded pre-Scout baseline | **≥ 2× baseline**, and reported with a confidence interval | `GET /metrics/funnel`, `APPLICATION_PIPELINE.md` §8 |
| O6 | Token spend is bounded | Daily LLM spend, computed from response `usage`, not estimates | **< ₹80/day** | `run_log.stats.llm_cost_inr`, budget breaker at `LLM_DAILY_BUDGET_INR` |
| O7 | The daily run finishes before the operator reads the digest | `run_log.finished_at − started_at` for `run_type='discovery'` | **< 15 minutes** | `run_log`; 08:00 start, 08:15 digest |
| O8 | The queue is worth the attention it consumes | Precision@10 — of the ten highest-composite items surfaced in a week, how many were approved | **≥ 0.5**; investigate below 0.3 | `MATCH_SCORING.md` §12.2 |
| O9 | Nothing generated is fabricated | Artifacts attached to an application with `validation_status <> 'passed'` | **0, permanently** | Database constraint, `CLAIMS_LEDGER.md` §6.1 |
| O10 | Source coverage is honest about itself | Sources reporting `error` or auto-disabled that are not named in the digest | **0** | Digest section 6 against `run_log.source_results` |

O5 requires the operator to record a pre-Scout baseline before the first
Scout-prepared application is submitted. Without that number, O5 is
unmeasurable and must be reported as such rather than assumed.

**On O5 and statistical honesty.** At forty applications, a response-rate
difference has a confidence interval wide enough to swallow most of the effect
being claimed. The system reports the interval alongside the rate and suppresses
rates entirely below `MIN_N_FOR_RATE` (15). O5 is a target to steer by, not a
result to declare early (`APPLICATION_PIPELINE.md` §9).

---

## 3. Non-goals

### 3.1 The three the architecture forbids outright

These are not deferred features. They are absences enforced in code, each with a
test, and each corresponding to an invariant in `ARCHITECTURE.md` §3.

| Non-goal | Invariant | Why it is forbidden |
|---|---|---|
| **Automated submission** — the system never POSTs an application to an employer system, never drives a browser to submit a form, never completes a CAPTCHA | 1 | Auto-submission is the mechanism that produces the 1–2% strategy. It also breaks the terms of every major ATS, and a shared ATS remembers the applicant across every employer on it. The cost of being flagged is not one rejected application; it is a degraded standing with every employer using that platform. A human gate also forces a final read of what is about to be sent under the operator's name — the one review no automated validator can replace |
| **Automated outbound mail to third parties** — exactly one class of email is sent, the daily digest, to the operator's own address | 2 | Automated cold outreach to recruiters and hiring managers is spam with a personal signature on it. It converts poorly, it is a reputational liability that cannot be recalled, and under Indian and EU rules unsolicited automated commercial contact carries real exposure. Referral and outreach conversations are high-value human work and are precisely the work that must not be delegated to a generator |
| **LinkedIn scraping** — LinkedIn and every host on the never-scrape list are never fetched programmatically under any configuration | 4 | Prohibited by LinkedIn's terms; enforced technically and legally with more energy than any other platform. Account termination costs the operator their professional network, which is worth more than every posting the scrape would have found. LinkedIn content enters the system by exactly one supported route — a LinkedIn job alert delivered to the operator's own mailbox and parsed there (`SOURCE_ADAPTERS.md` §7) — which is mail the operator is entitled to read |

The deny list is a frozen code constant, not a setting. There is no environment
variable, admin toggle or request parameter that relaxes it, and a test asserts
that the constant is not reachable from `Settings` (§10.3).

### 3.2 Out of scope for v1, for ordinary reasons

| Non-goal | Reason |
|---|---|
| Multi-user, teams, tenancy, roles | One operator. Auth, isolation and a permission model are weight with no beneficiary (`API.md` §1) |
| Self-service registration or password reset | No second user to register |
| A predicted "selection probability" per role | Not computable from available data, and a fabricated probability is worse than no number. The system reports observed rates from the operator's own history instead (`MATCH_SCORING.md` §7) |
| Browser autofill of employer application forms | Adjacent to auto-submission; the boundary is easier to hold at "no automation touches the employer's form at all". Deferred, not forbidden (`ROADMAP.md` §5) |
| Salary data enrichment | Available sources are unreliable enough that the number would be decorative |
| Interview preparation material | A different product with a different loop |
| Mobile-native app | The digest is the mobile surface. A responsive review screen is a Phase 5 nicety |
| Offline mode | The system is a scheduled batch plus a local UI; there is nothing to be offline from |
| A vector database | Matching is requirement-by-requirement against six fixed variants (`ARCHITECTURE.md` §4.1) |
| Training any model on outcome data | Forty applications is not a training set. Outcomes evaluate the scorer; they never train it (`MATCH_SCORING.md` §12.3) |

---

## 4. Actors and system boundary

| Actor | Nature | Interaction |
|---|---|---|
| **Operator** | Human, single, trusted | Reviews the queue, approves, submits on the employer's site, records manual events, maintains the registry and the ledger |
| **ATS endpoints** | External, untrusted | Public JSON APIs polled by adapters |
| **Job-alert mail** | External, untrusted | Parsed into low-fidelity postings |
| **Reply mail** | External, untrusted | Classified into application status events |
| **LLM provider** | External, semi-trusted | Structured-output calls only; never given untrusted text as instruction |
| **Employer application form** | External | **Outside the boundary.** The operator submits. No system component ever touches it |

Everything crossing into the system from outside is data, never instruction.
Job descriptions and email bodies are attacker-controllable text; they are
delimited, never executed, never concatenated into SQL, and never treated as
instructions to a model (`AI_ARCHITECTURE.md` §7).

---

## 5. Requirements — EARS notation

Requirements use EARS: `WHEN <trigger> THE SYSTEM SHALL <response>` for
event-driven behaviour, `WHERE <state> THE SYSTEM SHALL <response>` for
state-conditional behaviour, `IF <condition> THEN THE SYSTEM SHALL <response>`
for exception handling, and `THE SYSTEM SHALL <response>` for ubiquitous
requirements.

### 5.1 Discovery and ingestion (R1–R12)

| ID | Requirement |
|---|---|
| **R1** | WHEN the scheduler reaches 08:00 Asia/Kolkata THE SYSTEM SHALL start a discovery run, write a `run_log` row with `run_type='discovery'` and `status='running'`, and poll every `source` where `enabled = TRUE` and the poll interval has elapsed. |
| **R2** | WHEN an adapter fetch raises, times out or returns a non-2xx status THE SYSTEM SHALL record that outcome in `run_log.source_results` for that source, increment `source.consecutive_failures`, and continue the run with the remaining sources. |
| **R3** | THE SYSTEM SHALL always complete a discovery run and always report per-source outcomes, terminating with `status` of `completed`, `completed_with_errors` or `failed` and never leaving a run in `running`. |
| **R4** | WHERE `source.consecutive_failures >= 5` THE SYSTEM SHALL set `source.enabled = FALSE`, name the source explicitly in the next digest, and never delete the source row. |
| **R5** | WHEN a fetched posting is normalised THE SYSTEM SHALL identify it by `(source_id, external_id)` and compute `content_hash` as the SHA-256 of `description_text`. |
| **R6** | IF a re-fetch yields an existing `(source_id, external_id)` with a different `content_hash` THEN THE SYSTEM SHALL update the posting row and invalidate its `match_score` rows so the posting is rescored. |
| **R7** | WHEN the same role is discovered through more than one source THE SYSTEM SHALL collapse the duplicates on `(company_id, normalised_title, location_city)`, retaining the record whose source has the higher fidelity rank. |
| **R8** | WHERE a posting has not been seen in two consecutive runs of its source THE SYSTEM SHALL set `closed_at`, and SHALL NOT set `closed_at` on the strength of a single missed run. |
| **R9** | WHEN normalisation completes THE SYSTEM SHALL apply the deterministic filter — location, seniority, keyword deny-list, and `company.status <> 'blacklisted'` — setting `filtered_out` and `filter_reason` without issuing any model call. |
| **R10** | WHEN a job-alert email is ingested THE SYSTEM SHALL parse it into postings attributed to the `mail_alert` adapter with the fidelity of that adapter, and SHALL NOT fetch the alert's originating platform to enrich them. |
| **R11** | WHEN a posting discovered via `mail_alert` matches a directly-fetched posting THE SYSTEM SHALL keep the directly-fetched record and discard the mail-derived duplicate. |
| **R12** | IF a discovery run is requested while another is in flight THEN THE SYSTEM SHALL reject the request with HTTP 409 on the basis of a Redis lock, not an advisory convention. |

### 5.2 Company registry (R13–R22)

| ID | Requirement |
|---|---|
| **R13** | WHEN the operator posts a careers URL to `POST /api/v1/companies/detect` THE SYSTEM SHALL match it against the URL-pattern table, issue exactly one live probe against the candidate endpoint, and return the detected adapter, its config, a company-name guess and the probe result. |
| **R14** | IF detection produces no pattern match THEN THE SYSTEM SHALL return HTTP 422 with code `source.undetectable` and report what it did — the final URL, the redirect chain, and whether HTML was scanned — so that "nothing there" is distinguishable from "did not try". |
| **R15** | IF detection produces more than one pattern match THEN THE SYSTEM SHALL return every candidate with its own probe and confidence, ordered by confidence, and SHALL NOT auto-select. |
| **R16** | THE SYSTEM SHALL treat a company with zero sources as a fully valid registry row that retains tier, tags, defaults and cover-letter policy, and SHALL apply those to manually imported and mail-discovered postings for that company. |
| **R17** | WHEN a company is created THE SYSTEM SHALL assign `tier` (`dream`/`strong`/`volume`), `status` (`tracking` by default) and `cover_letter_worth`, and SHALL permit a `default_variant_id`. |
| **R18** | WHERE `company.status = 'blacklisted'` THE SYSTEM SHALL exclude every posting of that company at the deterministic filter stage before any token is spent. |
| **R19** | WHERE `company.location_filter` is non-empty THE SYSTEM SHALL filter that company's postings to the listed locations; WHERE it is empty THE SYSTEM SHALL accept all locations for that company. |
| **R20** | WHEN a company legitimately operates several boards THE SYSTEM SHALL permit one `source` row per board under `UNIQUE (company_id, adapter, config)` and SHALL report the health of each independently. |
| **R21** | WHEN the operator requests `POST /api/v1/sources/{id}/test` THE SYSTEM SHALL re-probe that source and return the same probe block used at detection. |
| **R22** | WHEN a candidate company matches an existing registry row on the deduplication signals THE SYSTEM SHALL surface the collision to the operator rather than silently creating a second company. |

### 5.3 Extraction and scoring (R23–R34)

| ID | Requirement |
|---|---|
| **R23** | WHEN a posting survives the deterministic filter THE SYSTEM SHALL extract its requirements by structured-output model call into `requirement` rows, each with `kind` ∈ {`hard`,`nice`,`responsibility`,`tool`}, `weight`, `ordinal`, `model` and `prompt_version`. |
| **R24** | THE SYSTEM SHALL pass job-description text to the model as delimited data inside an explicit data boundary and SHALL NOT treat any instruction contained in that text as an instruction. |
| **R25** | WHEN a requirement is extracted THE SYSTEM SHALL resolve it to a controlled-vocabulary token in `normalised_skill` via the three-stage resolver, and IF no token resolves THEN THE SYSTEM SHALL score the requirement `missing` with an explicit note and record the phrase as a vocabulary proposal rather than dropping it. |
| **R26** | IF an extracted `evidence_span` is not found verbatim in the source text THEN THE SYSTEM SHALL retry once at temperature 0 and, on a second failure, route the posting to `needs_manual_review` and count the failure in `run_log.stats`. |
| **R27** | WHEN requirements exist for a posting THE SYSTEM SHALL score coverage for each shortlisted `resume_variant`, producing `hard_met`, `hard_total`, `nice_met`, `nice_total`, `coverage_pct`, `composite_score`, `gaps` and `evidence` in a `match_score` row keyed `(posting_id, variant_id, prompt_version)`. |
| **R28** | THE SYSTEM SHALL link every met requirement in `evidence` to the variant bullet and claim IDs that satisfy it, so that a coverage assertion is always inspectable. |
| **R29** | THE SYSTEM SHALL mark exactly one `match_score` row per posting `is_recommended = TRUE`. |
| **R30** | IF extraction yields zero `hard` requirements THEN THE SYSTEM SHALL NOT score the posting as a perfect match, and SHALL route it to `needs_manual_review` with the empty hard bucket logged against the posting ID. |
| **R31** | IF scoring raises for one variant THEN THE SYSTEM SHALL skip and log that variant and still produce a recommendation from the remaining variants. |
| **R32** | THE SYSTEM SHALL NOT compute, store or display a predicted selection probability for any posting. |
| **R33** | WHEN a resume variant, the claims ledger, the skill vocabulary or a scoring setting changes THE SYSTEM SHALL bump `SCORING_PROMPT_VERSION` in the same change and SHALL rescore affected postings rather than reusing scores across formula families. |
| **R34** | WHEN the operator requests `POST /api/v1/postings/{id}/rescore` THE SYSTEM SHALL accept with 202 and re-run extraction and scoring for that posting. |

### 5.4 Generation (R35–R46)

| ID | Requirement |
|---|---|
| **R35** | WHERE `composite_score >= GENERATION_MIN_COMPOSITE` and `coverage_pct >= GENERATION_MIN_COVERAGE_PCT` THE SYSTEM SHALL generate a tailoring plan for the recommended variant, subject to `GENERATION_DAILY_CAP`. |
| **R36** | THE SYSTEM SHALL express tailoring as a reviewable diff against the base variant — bullet swaps, block reordering, skills-line edits — and SHALL NOT emit a wholly regenerated resume. |
| **R37** | THE SYSTEM SHALL attach the claim IDs authorising each proposed bullet to that bullet within the tailoring plan. |
| **R38** | WHERE `company.cover_letter_worth = FALSE` THE SYSTEM SHALL skip cover-letter generation for that company's postings. |
| **R39** | WHEN a cover letter is generated THE SYSTEM SHALL compute its gap paragraph from that posting's `match_score.gaps` and SHALL state the gaps rather than conceal them. |
| **R40** | WHEN any document is generated THE SYSTEM SHALL run the ledger validation pass over it, detecting every numeric and superlative assertion and resolving each against a `claim` row. |
| **R41** | IF any assertion fails to resolve THEN THE SYSTEM SHALL set `artifact.validation_status = 'failed'`, attempt regeneration up to `GENERATION_VALIDATION_RETRIES`, and on continued failure route the item to `needs_manual_review`. |
| **R42** | THE SYSTEM SHALL prevent an artifact whose `validation_status <> 'passed'` from being attached to a `review_item` or an `application`, enforced at the database and not by application code alone. |
| **R43** | IF the assertion-extraction call itself fails THEN THE SYSTEM SHALL return `passed: false` and fail closed, and SHALL NOT treat an unchecked document as checked. |
| **R44** | WHERE a cited claim is expired or soft-deleted THE SYSTEM SHALL treat the citation as unresolved under the default policy and SHALL NOT emit the claim. |
| **R45** | WHERE a cited claim has `confidentiality = 'restricted'` and the employer is not on `LEDGER_RESTRICTED_DISCLOSURE_COMPANIES` THE SYSTEM SHALL refuse the assertion and SHALL retry with the declared public sibling claim where one exists. |
| **R46** | WHEN a letter is generated THE SYSTEM SHALL compare it against every previously generated letter, flag at `SIMILARITY_WARN`, regenerate at `SIMILARITY_BLOCK`, and after two consecutive blocks route the item to `needs_manual_review` rather than looping. |

### 5.5 Review (R47–R54)

| ID | Requirement |
|---|---|
| **R47** | WHEN generation completes for a posting THE SYSTEM SHALL create exactly one `review_item` per posting with `status = 'pending_review'`. |
| **R48** | WHEN the operator opens a review item THE SYSTEM SHALL return, in one response, the posting, the recommended variant, the coverage numbers, the named gaps, the linked evidence, the tailoring plan and the artifact validation statuses. |
| **R49** | WHEN the operator approves a review item THE SYSTEM SHALL create exactly one `application` row with `status = 'submitted'`, freeze the artifacts, return download links, and issue zero outbound HTTP requests to any employer system. |
| **R50** | THE SYSTEM SHALL expose no endpoint, at any API version, that submits an application to an employer. |
| **R51** | IF the operator approves or skips an already-decided review item THEN THE SYSTEM SHALL return HTTP 409 with code `review.already_decided`. |
| **R52** | WHEN the operator edits a tailoring plan via `PATCH /api/v1/review/{id}/plan` THE SYSTEM SHALL persist the edit, regenerate the affected artifacts from the edited plan, and re-run validation before the artifacts may be attached. |
| **R53** | WHEN the operator skips a review item THE SYSTEM SHALL record `decided_at` and a `decision_note`, offering the four canned skip reasons while permitting free text. |
| **R54** | WHERE a review item has been approved for longer than `SUBMIT_CONFIRM_DAYS` with no subsequent events THE SYSTEM SHALL surface a "possibly not submitted" prompt to the operator. |

### 5.6 Tracking (R55–R64)

| ID | Requirement |
|---|---|
| **R55** | THE SYSTEM SHALL treat `application_event` as append-only and as the sole truth for application status; `application.status` is a materialised projection of the latest legal event. |
| **R56** | WHEN mail is polled THE SYSTEM SHALL classify each message into one `mail_class`, link it to an application where the resolution chain succeeds, and append a status event where the class and confidence justify it. |
| **R57** | THE SYSTEM SHALL store no email bodies, retaining only the classification, confidence and a short excerpt on the event row. |
| **R58** | IF an event would violate the legal-transition rules THEN THE SYSTEM SHALL reject the append rather than record a status regression. |
| **R59** | WHEN the same event — same `(application_id, status, email_message_id)` — is appended twice THE SYSTEM SHALL create exactly one row. |
| **R60** | WHEN events arrive out of chronological order THE SYSTEM SHALL produce the same `application.status` as if they had arrived in order. |
| **R61** | WHEN the operator posts a manual event THE SYSTEM SHALL set `is_manual = TRUE`, leave `confidence` NULL, and keep manual and observed transitions distinguishable in every downstream query. |
| **R62** | THE SYSTEM SHALL compute `ghosted` as a view over `application_event` and SHALL NOT include `ghosted` in the `application_status` enum or assign it from any code path. |
| **R63** | WHEN an event arrives for an application currently appearing in the ghosted view THE SYSTEM SHALL remove it from that view with no compensating write. |
| **R64** | WHERE mail classification confidence falls below the acceptance threshold THE SYSTEM SHALL hold the message for operator decision rather than appending a speculative event. |

### 5.7 Reporting (R65–R71)

| ID | Requirement |
|---|---|
| **R65** | WHEN the operator requests funnel metrics THE SYSTEM SHALL return submitted, responded, advanced, interviewed and offer counts, grouped by variant, tier, week or source channel as requested. |
| **R66** | WHERE the group size is below `MIN_N_FOR_RATE` THE SYSTEM SHALL return the counts and a null rate, and SHALL NOT return a computed percentage. |
| **R67** | THE SYSTEM SHALL return `meta.n`, `meta.ci_low` and `meta.ci_high` on every funnel row. |
| **R68** | THE SYSTEM SHALL segment referral-sourced applications from direct applications in every rate, and SHALL NOT emit a figure combining the two. |
| **R69** | THE SYSTEM SHALL label funnel output as observed rates from the operator's own history and not as a prediction. |
| **R70** | WHEN the scheduler reaches `EXPORT_CRON` THE SYSTEM SHALL write the full-pipeline `.xlsx` export, and SHALL produce a workbook that opens cleanly when there are zero applications, zero companies and zero runs. |
| **R71** | WHEN the digest is composed at 08:15 Asia/Kolkata THE SYSTEM SHALL send it to the operator's address containing the new review queue, status changes, decisions needed, the alert-only stream, source failures and run statistics; and SHALL send it even when there is nothing to report and even when the run failed. |

### 5.8 Operations (R72–R82)

| ID | Requirement |
|---|---|
| **R72** | THE SYSTEM SHALL complete a discovery run within 15 minutes of wall clock at the design scale of ~320 sources. |
| **R73** | THE SYSTEM SHALL compute LLM spend from the `usage` returned on each response, accumulate it per run in `run_log.stats.llm_cost_inr`, and enforce `LLM_DAILY_BUDGET_INR` as a circuit breaker. |
| **R74** | WHERE daily spend exceeds `LLM_BUDGET_WARN_PCT` of the budget THE SYSTEM SHALL restrict generation to the top-ranked items rather than exceeding the budget silently. |
| **R75** | THE SYSTEM SHALL make each pipeline stage individually re-runnable against stored intermediate state, so that a failure in scoring never forces a re-fetch. |
| **R76** | THE SYSTEM SHALL accept an `Idempotency-Key` on `POST /runs/discovery` and `POST /review/{id}/generate`, returning the original response for a repeated key within 24 hours. |
| **R77** | THE SYSTEM SHALL emit one structured log line per pipeline stage per run, correlated by `run_id`. |
| **R78** | THE SYSTEM SHALL NOT log credentials, OAuth tokens, cookies, full email bodies or raw resume content at any level, including as a prefix, length or hash. |
| **R79** | WHEN `GET /api/v1/health` is called THE SYSTEM SHALL return 200 with a per-dependency status map for Postgres, Redis, the LLM provider and the Gmail token, and SHALL NOT return 500 because a dependency is degraded. |
| **R80** | IF the Gmail refresh token is rejected with `invalid_grant` THEN THE SYSTEM SHALL abort the mail run without retry, report `gmail: "unauthenticated"` on health, write the digest to disk instead of sending it, and leave the mail cursor unadvanced. |
| **R81** | THE SYSTEM SHALL ship every new behaviour on the generation path behind a feature flag defaulting off, and SHALL make the off state a complete correct behaviour rather than a degraded one. |
| **R82** | THE SYSTEM SHALL record on every artifact the model, prompt version, variant ID and the exact claim IDs used, such that the artifact is reproducible from its provenance. |

### 5.9 Compliance (R83–R92)

| ID | Requirement |
|---|---|
| **R83** | THE SYSTEM SHALL refuse to issue an HTTP request to any host on `NEVER_FETCH_HOSTS` or any of its subdomains, the check being made inside the shared HTTP client after redirect resolution. |
| **R84** | THE SYSTEM SHALL hold `NEVER_FETCH_HOSTS` as a frozen code constant that is not reachable from `Settings` and not modifiable by any environment variable, request parameter or admin toggle. |
| **R85** | WHEN a deny-listed host is submitted to `POST /companies/detect` THE SYSTEM SHALL return HTTP 403 with code `source.denied_by_policy` **before issuing any request**, so the refusal is not observable to the denied host. |
| **R86** | THE SYSTEM SHALL apply the deny-list check to every URL-accepting surface — detection, manual company creation, `company.careers_url`, `company.website`, source config and `POST /postings/import` — on write. |
| **R87** | THE SYSTEM SHALL permit a deny-listed URL to be **stored** as a human-clickable reference while never fetching it, storing and following a link being separate acts of which only the second is forbidden. |
| **R88** | IF a source redirects mid-flight to a deny-listed host THEN THE SYSTEM SHALL abort that request and record the refusal against the source. |
| **R89** | THE SYSTEM SHALL fetch and honour `robots.txt` for every host not on the deny list, lowering the host's rate for a `Crawl-delay` directive and disabling a source whose endpoint is `Disallow`ed. |
| **R90** | IF `robots.txt` is unreachable for a host that is not a documented public JSON API THEN THE SYSTEM SHALL fail that source closed for the run. |
| **R91** | THE SYSTEM SHALL send outbound email only to the operator's configured address, and SHALL have no code path that addresses mail to any other recipient. |
| **R92** | THE SYSTEM SHALL request the minimum Gmail scope set and SHALL NOT request `gmail.modify`. |

---

## 6. User stories

Stories are grouped by epic. Every story carries the requirement IDs it
realises. Acceptance criteria are Given/When/Then and are written to be
executed.

### Epic A — The 08:00 run and the 08:15 digest

#### US-A1 — The digest tells me what today needs
*As the operator, I want one email at 08:15 that tells me everything the system
did overnight, so that on a day I never open the app I still know where I stand.*
**Realises:** R1, R3, R71, R72

| # | Acceptance criterion |
|---|---|
| A1.1 | **Given** a completed discovery run that queued 6 new review items and detected 2 status changes, **when** the digest composes at 08:15 IST, **then** the subject line reads `Scout · 6 to review · 2 status change(s) · <date>` and sections 2 and 3 contain exactly those items. |
| A1.2 | **Given** a run in which nothing new was queued and nothing changed, **when** 08:15 IST is reached, **then** a digest is still sent, with sections 2–5 omitted and section 6 reading `All N sources healthy.` |
| A1.3 | **Given** a discovery run that ended `failed`, **when** the digest composes, **then** a digest is still sent and states the failure and the run ID. |
| A1.4 | **Given** any digest, **when** it is rendered, **then** the `text/plain` part contains every item present in the HTML part, and the message contains no image, remote asset or tracking pixel. |
| A1.5 | **Given** a review-queue entry in section 2, **when** it is rendered, **then** it shows title, company, tier, location, coverage percentage, hard met/total, recommended variant, up to three **named** gaps, artifact validation status and a deep link. |
| A1.6 | **Given** the run started at 08:00, **when** `run_log.finished_at` is written, **then** the elapsed time is under 15 minutes at the design scale. |

#### US-A2 — A broken source does not break my morning
*As the operator, I want one failing employer board to cost me that board only.*
**Realises:** R2, R3, R4, R10, R79

| # | Acceptance criterion |
|---|---|
| A2.1 | **Given** 320 enabled sources of which one returns HTTP 403, **when** the run executes, **then** the run reaches `completed_with_errors`, the other 319 sources are polled, and `source_results` carries one entry with `status: "error"` and the HTTP status. |
| A2.2 | **Given** a source that has now failed on five consecutive runs, **when** the fifth failure is recorded, **then** `source.enabled` becomes `FALSE`, the source row still exists, and the next digest names it explicitly as auto-disabled. |
| A2.3 | **Given** an adapter that raises an unhandled exception, **when** the runner catches it, **then** the exception is recorded against that source and no other source's result is affected. |
| A2.4 | **Given** a degraded LLM provider, **when** `GET /health` is called, **then** it returns 200 with `llm` reported as degraded, and the UI remains usable. |

#### US-A3 — I can re-run without breaking anything
*As the operator, I want to trigger a run by hand and be sure I have not started a
second one or double-counted a posting.*
**Realises:** R5, R6, R8, R12, R75, R76

| # | Acceptance criterion |
|---|---|
| A3.1 | **Given** a discovery run in flight, **when** `POST /runs/discovery` is called again, **then** the response is 409 and no second run row is created. |
| A3.2 | **Given** a completed run, **when** the same `Idempotency-Key` is replayed within 24 hours, **then** the original response is returned and no new run starts. |
| A3.3 | **Given** an unchanged posting, **when** it is re-fetched, **then** `last_seen_at` is bumped, `content_hash` is unchanged and no re-extraction occurs. |
| A3.4 | **Given** a posting whose description changed, **when** it is re-fetched, **then** the row is updated and its `match_score` rows are invalidated for rescoring. |
| A3.5 | **Given** a posting missing from one run, **when** the next run also does not see it, **then** `closed_at` is set; **given** it reappears on the second run, **then** `closed_at` remains null. |

### Epic B — Reviewing a queued item

#### US-B1 — I can decide one item in ninety seconds
*As the operator, I want everything I need for the decision on one screen.*
**Realises:** R28, R47, R48, R53; **Objective:** O1

| # | Acceptance criterion |
|---|---|
| B1.1 | **Given** a `pending_review` item, **when** `GET /review/{id}` is called, **then** one response returns the posting, company and tier, the recommended variant, `coverage_pct`, hard and nice met/total, the gap list with `kind` and `level` per gap, the evidence list with claim IDs and the bullet text, the tailoring plan, and the validation status of each artifact. |
| B1.2 | **Given** a gap in that list, **when** it is displayed, **then** it names the missing requirement in the employer's own words, not a category label. |
| B1.3 | **Given** an evidence entry, **when** it is displayed, **then** it links to the specific variant bullet and the claim IDs that satisfy the requirement. |
| B1.4 | **Given** the item is opened and a decision recorded, **when** the elapsed time is measured across a sample of decisions, **then** the median is at or below 90 seconds. |
| B1.5 | **Given** a skip decision, **when** it is recorded, **then** `decided_at` and `decision_note` are persisted and one of the four canned reasons is offered without preventing free text. |

#### US-B2 — Approve means I am submitting it myself
*As the operator, I want approval to be unambiguous about who submits.*
**Realises:** R49, R50, R51; **Invariant:** 1

| # | Acceptance criterion |
|---|---|
| B2.1 | **Given** a `pending_review` item with passed artifacts, **when** `POST /review/{id}/approve` is called, **then** the response is 201, exactly one `application` row exists with `status='submitted'`, the artifacts are frozen, and download links are returned. |
| B2.2 | **Given** that same call, **when** outbound HTTP is observed for its duration, **then** zero requests are made to any employer host. |
| B2.3 | **Given** an already-approved item, **when** approve or skip is called again, **then** the response is 409 with code `review.already_decided` and no second application row is created. |
| B2.4 | **Given** the generated OpenAPI schema, **when** every path and operation is enumerated, **then** none submits an application to an external system. |
| B2.5 | **Given** an approved item with no subsequent events after `SUBMIT_CONFIRM_DAYS`, **when** the digest composes, **then** the item appears under "needs your decision" as possibly not submitted. |

#### US-B3 — Nothing reaches me that failed validation
*As the operator, I want to be structurally unable to send a document containing a
number the ledger cannot back.*
**Realises:** R40, R41, R42, R43, R44, R45; **Invariant:** 3

| # | Acceptance criterion |
|---|---|
| B3.1 | **Given** a draft asserting "14 centres" with no matching ledger claim, **when** validation runs, **then** the response is `passed: false` with that span marked unresolved and named. |
| B3.2 | **Given** an artifact with `validation_status = 'failed'`, **when** an attempt is made to set it as `review_item.resume_artifact_id`, **then** the database rejects the write. |
| B3.3 | **Given** the same artifact, **when** an attempt is made to attach it to an `application`, **then** the database rejects the write. |
| B3.4 | **Given** a failed validation, **when** regeneration is attempted up to `GENERATION_VALIDATION_RETRIES` and still fails, **then** the review item is routed to `needs_manual_review` and no artifact is attached. |
| B3.5 | **Given** the assertion-extraction model call itself fails, **when** validation returns, **then** it returns `passed: false` with a note that extraction was unavailable — never `passed: true`. |
| B3.6 | **Given** a cited claim whose `expires_at` has passed, **when** validation runs under the default policy, **then** the citation is unresolved and the document is blocked. |
| B3.7 | **Given** a `restricted` claim and an employer not on the disclosure allow-list, **when** generation runs, **then** the assertion fails on disclosure grounds and the declared public sibling is attempted. |

### Epic C — Maintaining the registry

#### US-C1 — I add a company by pasting a URL
*As the operator, I want to add an employer in one paste, because maintaining 300
companies by hand is the thing that would kill the tool.*
**Realises:** R13, R14, R15, R16, R17, R20, R21, R22

| # | Acceptance criterion |
|---|---|
| C1.1 | **Given** `https://adobe.wd5.myworkdayjobs.com/external_experienced`, **when** posted to `/companies/detect`, **then** the response is 200 with `adapter: "workday"`, config containing host, tenant and site, a company-name guess, and a probe reporting reachability, sample count and latency. |
| C1.2 | **Given** a URL that matches no pattern, **when** detection runs, **then** the response is 422 `source.undetectable` and includes the final URL, the redirect chain and whether HTML was scanned. |
| C1.3 | **Given** a URL matching two patterns, **when** detection runs, **then** both candidates are returned with independent probes and confidences, ordered by confidence, with none auto-selected. |
| C1.4 | **Given** a detection result, **when** the operator confirms it, **then** a `company` and one `source` row are created; **when** they do not confirm, **then** nothing is persisted. |
| C1.5 | **Given** an employer with a Workday experienced site and a Workday campus site, **when** both URLs are pasted, **then** two `source` rows exist on the same company and the Companies page shows independent health for each. |
| C1.6 | **Given** a company created with no source, **when** a posting for it arrives by manual import or mail alert, **then** the company's tier, tags, default variant and cover-letter policy apply to it. |
| C1.7 | **Given** a pasted URL for an employer already in the registry, **when** deduplication signals match, **then** the collision is surfaced and no second company row is created. |

#### US-C2 — A deny-listed host is refused everywhere
*As the operator, I want the never-scrape rule to be something I cannot
accidentally defeat.*
**Realises:** R83, R84, R85, R86, R87, R88; **Invariant:** 4

| # | Acceptance criterion |
|---|---|
| C2.1 | **Given** `https://www.linkedin.com/company/acme/jobs/`, **when** posted to `/companies/detect`, **then** the response is 403 `source.denied_by_policy`, and network capture shows **zero** requests issued to that host. |
| C2.2 | **Given** the refusal message, **when** it is read, **then** it names the two supported routes — a job alert into the alerts mailbox, or manual import. |
| C2.3 | **Given** a deny-listed host, **when** it is submitted to manual company creation, source config, `careers_url`, `website` or `POST /postings/import`, **then** each entry point refuses the fetch on the same gate. |
| C2.4 | **Given** a LinkedIn company URL stored in `company.website`, **when** the row is saved, **then** the write succeeds and no fetch is ever issued to it. |
| C2.5 | **Given** an allowed host that 302s to a deny-listed host, **when** the client follows the redirect, **then** the request is aborted and the refusal is recorded against the source. |
| C2.6 | **Given** the codebase, **when** `NEVER_FETCH_HOSTS` is inspected, **then** it is a frozen constant with no read path from `Settings` and no environment variable that mutates it. |

### Epic D — Importing a role found outside the pipeline

#### US-D1 — I import a role someone referred me to
*As the operator, I want a role a friend sent me to go through the same
extraction, scoring and drafting as everything else.*
**Realises:** R23, R27, R35, R47, R86

| # | Acceptance criterion |
|---|---|
| D1.1 | **Given** an employer careers URL not on the deny list, **when** posted to `POST /postings/import`, **then** extraction, scoring and generation run synchronously and the resulting `review_item` is returned. |
| D1.2 | **Given** a URL that cannot be fetched, **when** `description_text` is supplied instead, **then** the import proceeds on the pasted text. |
| D1.3 | **Given** a `company_hint` naming an employer already in the registry, **when** the import runs, **then** the posting attaches to that company and inherits its tier, default variant and cover-letter policy. |
| D1.4 | **Given** a `company_hint` naming an unknown employer, **when** the import runs, **then** a company row is created with default tier and the posting attaches to it. |
| D1.5 | **Given** an import URL on the deny list, **when** the import is attempted, **then** it is refused with `source.denied_by_policy` and nothing is fetched. |
| D1.6 | **Given** an imported posting whose composite falls below `GENERATION_MIN_COMPOSITE`, **when** the import completes, **then** the posting, its scores and its gaps are returned and no draft is generated. |

#### US-D2 — A referral is recorded as a referral
*As the operator, I want referral applications segmented, because otherwise they
will silently inflate every rate I read.*
**Realises:** R61, R68

| # | Acceptance criterion |
|---|---|
| D2.1 | **Given** an approval of a referred role, **when** the application is created with `source_channel = 'referral'` and a `referral_contact`, **then** both persist. |
| D2.2 | **Given** a mixed history of direct and referral applications, **when** `GET /metrics/funnel` is grouped by variant, **then** it defaults to `source_channel=direct` and no returned figure combines the two channels. |
| D2.3 | **Given** the spreadsheet export, **when** the Funnel sheet is inspected, **then** no cell combines direct and referral applications. |

### Epic E — Correcting the machine

#### US-E1 — I correct a tailoring plan rather than fight the output
*As the operator, I want to edit the proposed diff, so my correction is captured
instead of worked around in the .docx afterwards.*
**Realises:** R36, R37, R52, R81, R82

| # | Acceptance criterion |
|---|---|
| E1.1 | **Given** a review item with a tailoring plan, **when** the plan is rendered, **then** it appears as a diff against the base variant — bullet swaps, block reordering, skills-line edits — not as a whole new resume. |
| E1.2 | **Given** a proposed bullet, **when** it is displayed, **then** the claim IDs authorising it are shown with it. |
| E1.3 | **Given** an edited plan, **when** `PATCH /review/{id}/plan` is called, **then** the edit persists, the affected artifacts regenerate from the edited plan and validation re-runs before attachment. |
| E1.4 | **Given** a regenerated artifact, **when** it is stored, **then** it records the model, the prompt version, the variant ID and the exact claim IDs used. |
| E1.5 | **Given** a feature flag governing a generation behaviour is off, **when** a plan containing that operation is applied, **then** the operation is ignored and the remaining operations apply cleanly. |

#### US-E2 — I keep the ledger honest
*As the operator, I want to add, re-verify and retire facts, because the ledger is
what makes every generated number defensible.*
**Realises:** R40, R44, R45, R82

| # | Acceptance criterion |
|---|---|
| E2.1 | **Given** a new verified fact, **when** it is posted to `/claims`, **then** it persists with `key`, `statement`, metric value and unit, project, `evidence_ref`, confidentiality, tags and `verified_at`. |
| E2.2 | **Given** a claim, **when** `GET /claims/{id}/usage` is called, **then** every artifact citing it is listed with the location within the document. |
| E2.3 | **Given** a claim within `LEDGER_EXPIRY_WARNING_DAYS` of expiry, **when** the digest composes, **then** the impending expiry is reported. |
| E2.4 | **Given** a soft-deleted claim, **when** a new document cites it, **then** the citation is unresolved and the document is blocked. |
| E2.5 | **Given** a change to the ledger, **when** affected postings are rescored, **then** coverage that depended on a removed or expired claim degrades rather than silently persisting. |

### Epic F — Tracking what happened

#### US-F1 — Replies move the application without me typing
*As the operator, I want the mail I already receive to maintain the pipeline.*
**Realises:** R55, R56, R57, R58, R59, R60, R64, R92

| # | Acceptance criterion |
|---|---|
| F1.1 | **Given** an acknowledgement email linked to an application, **when** the poll classifies it, **then** an `application_event` with `status='acknowledged'`, the excerpt and a confidence is appended and `application.status` projects to `acknowledged`. |
| F1.2 | **Given** the same message polled twice, **when** classification repeats, **then** exactly one event row exists. |
| F1.3 | **Given** a rejection arriving before a late acknowledgement, **when** both are appended, **then** the resulting `application.status` matches the in-order result and `rejected → acknowledged` is refused. |
| F1.4 | **Given** any classified message, **when** persistence completes, **then** no email body is stored — only class, confidence and the event excerpt. |
| F1.5 | **Given** a classification below the acceptance threshold, **when** the poll completes, **then** the message is held for the operator in the digest's decisions section and no event is appended. |
| F1.6 | **Given** the OAuth scope request, **when** it is inspected, **then** `gmail.modify` is absent. |

#### US-F2 — I record what happened off-email
*As the operator, I want to log a phone screen without pretending an email
proved it.*
**Realises:** R61, R58, R55

| # | Acceptance criterion |
|---|---|
| F2.1 | **Given** an application, **when** `POST /applications/{id}/events` records a `screening` event, **then** the row has `is_manual = TRUE`, `confidence` NULL and no `email_message_id`. |
| F2.2 | **Given** a manual event that would violate the transition rules, **when** it is submitted, **then** it is rejected. |
| F2.3 | **Given** a mix of manual and observed events, **when** the funnel is computed, **then** the two remain distinguishable and the manual ones are not silently attributed to mail classification. |
| F2.4 | **Given** a `force` parameter supplied by a non-manual caller, **when** the append is attempted, **then** it is rejected. |

#### US-F3 — Silence is reported as silence
*As the operator, I want ghosting computed, not asserted.*
**Realises:** R62, R63

| # | Acceptance criterion |
|---|---|
| F3.1 | **Given** the `application_status` enum, **when** it is enumerated, **then** `ghosted` is absent, and a grep-level test asserts no code path assigns it. |
| F3.2 | **Given** an application submitted 31 days ago with no events, **when** `v_ghosted` is queried, **then** it appears. |
| F3.3 | **Given** an event arriving on day 45, **when** it is appended, **then** the application leaves `v_ghosted` with no compensating write. |

### Epic G — Reading the funnel

#### US-G1 — I learn which variant actually converts
*As the operator, I want the funnel to tell me the truth about my own history and
to refuse to tell me anything it cannot support.*
**Realises:** R65, R66, R67, R68, R69, R70; **Objective:** O5

| # | Acceptance criterion |
|---|---|
| G1.1 | **Given** applications grouped by variant, **when** the funnel is requested, **then** each row returns submitted, responded, advanced, interviewed and offers with `meta.n`, `meta.ci_low` and `meta.ci_high`. |
| G1.2 | **Given** a group with `n < MIN_N_FOR_RATE`, **when** the rate is requested, **then** the rate is null and the count is present — never a computed percentage. |
| G1.3 | **Given** two variants with fewer than `MIN_N_FOR_COMPARISON` applications each, **when** a comparison is requested, **then** the comparison is not displayed. |
| G1.4 | **Given** any funnel response, **when** `meta.note` is read, **then** it states that these are observed rates from the operator's own history and not a prediction. |
| G1.5 | **Given** an empty system, **when** the nightly export runs, **then** the workbook is written and opens cleanly with zero applications, zero companies and zero runs. |
| G1.6 | **Given** a recorded pre-Scout baseline response rate, **when** the funnel is read after 40 applications, **then** the current rate is presented alongside that baseline with both intervals. |

### Epic H — Operating within budget

#### US-H1 — The system cannot quietly become expensive
*As the operator, I want a spend ceiling that is enforced rather than hoped for.*
**Realises:** R9, R18, R38, R73, R74; **Objective:** O6

| # | Acceptance criterion |
|---|---|
| H1.1 | **Given** a completed run, **when** `run_log.stats.llm_cost_inr` is read, **then** it is computed from response `usage`, not from estimates, and is under ₹80 at design scale. |
| H1.2 | **Given** cumulative daily spend crossing `LLM_BUDGET_WARN_PCT`, **when** generation is scheduled, **then** it is capped to the top-ranked items. |
| H1.3 | **Given** cumulative daily spend reaching `LLM_DAILY_BUDGET_INR`, **when** a further model call is attempted, **then** the breaker opens and the run reports the truncation. |
| H1.4 | **Given** ~150 new postings after dedup, **when** the deterministic filter runs, **then** roughly 120 are eliminated before any model call, and the filter itself issues zero model calls. |
| H1.5 | **Given** a company with `cover_letter_worth = FALSE`, **when** generation runs for its postings, **then** no letter is generated and no letter tokens are spent. |
| H1.6 | **Given** any run, **when** generation completes, **then** no more than `GENERATION_DAILY_CAP` drafts exist for that run. |

#### US-H2 — I can see why a source is failing without reading logs
*As the operator, I want source health on a page.*
**Realises:** R2, R4, R21, R77, R79

| # | Acceptance criterion |
|---|---|
| H2.1 | **Given** a completed run, **when** `GET /runs/{id}` is called, **then** per-source results are returned with adapter, status, fetched, new, error and duration. |
| H2.2 | **Given** a source suspected of breaking, **when** `POST /sources/{id}/test` is called, **then** a fresh probe result is returned in the same shape as detection. |
| H2.3 | **Given** any run, **when** logs are inspected, **then** one structured line exists per pipeline stage, correlated by `run_id`, containing no credential, token or body content. |

#### US-H3 — Losing Gmail authorisation does not lose me a day
*As the operator, I want a broken token to be loud, recoverable and non-destructive.*
**Realises:** R80, R71

| # | Acceptance criterion |
|---|---|
| H3.1 | **Given** a refresh returning `invalid_grant`, **when** the mail run starts, **then** it aborts immediately without retry and writes a `run_log` row with `status='failed'` and `error='gmail_auth_invalid_grant'`. |
| H3.2 | **Given** that state, **when** `GET /health` is called, **then** it returns 200 with `gmail: "unauthenticated"`. |
| H3.3 | **Given** that state, **when** 08:15 arrives, **then** the digest is written to `exports/digest-YYYY-MM-DD.html` and shown in-app, and no day's content is lost. |
| H3.4 | **Given** that state, **when** the mail cursor is inspected, **then** it has not advanced, and after re-authorisation the unread window is processed in full. |

---

## 7. Traceability matrix

Requirement → user story → acceptance test → specifying document. Test IDs map
to files under `backend/tests/`; the acceptance-criterion IDs in §6 are the
human-readable form of the same checks.

| Req | Story | Acceptance test | Specified in |
|---|---|---|---|
| R1 | US-A1 | `AT-D-01 test_scheduler_fires_0800_ist` | `ARCHITECTURE.md` §6, §8 |
| R2 | US-A2 | `AT-D-02 test_adapter_failure_isolated` | `SOURCE_ADAPTERS.md` §10 |
| R3 | US-A2 | `AT-D-03 test_run_always_terminates_with_status` | `SOURCE_ADAPTERS.md` §10.4 |
| R4 | US-A2 | `AT-D-04 test_five_failures_auto_disable_and_report` | `DATA_MODEL.md` §3.2 |
| R5 | US-A3 | `AT-D-05 test_posting_identity_and_hash` | `DATA_MODEL.md` §4.1 |
| R6 | US-A3 | `AT-D-06 test_content_change_invalidates_scores` | `DATA_MODEL.md` §4.1 |
| R7 | US-A3 | `AT-D-07 test_cross_source_dedupe_keeps_higher_fidelity` | `SOURCE_ADAPTERS.md` §8 |
| R8 | US-A3 | `AT-D-08 test_two_run_close_rule` | `SOURCE_ADAPTERS.md` §10.5 |
| R9 | US-H1 | `AT-D-09 test_filter_is_deterministic_zero_model_calls` | `ARCHITECTURE.md` §6 |
| R10 | US-A2 | `AT-D-10 test_mail_alert_never_fetches_origin` | `SOURCE_ADAPTERS.md` §7 |
| R11 | US-A3 | `AT-D-11 test_mail_duplicate_yields_to_direct_fetch` | `EMAIL_INGESTION.md` §4.6 |
| R12 | US-A3 | `AT-D-12 test_concurrent_run_rejected_409` | `API.md` §7 |
| R13 | US-C1 | `AT-R-01 test_detect_returns_adapter_config_probe` | `COMPANY_REGISTRY.md` §2 |
| R14 | US-C1 | `AT-R-02 test_undetectable_returns_422_with_evidence` | `COMPANY_REGISTRY.md` §2.5 |
| R15 | US-C1 | `AT-R-03 test_ambiguous_returns_candidates_no_autoselect` | `COMPANY_REGISTRY.md` §2.5 |
| R16 | US-C1 | `AT-R-04 test_sourceless_company_is_valid` | `COMPANY_REGISTRY.md` §2.5 |
| R17 | US-C1 | `AT-R-05 test_company_defaults_persist` | `DATA_MODEL.md` §3.1 |
| R18 | US-H1 | `AT-R-06 test_blacklisted_company_filtered_pre_llm` | `COMPANY_REGISTRY.md` §4 |
| R19 | US-C1 | `AT-R-07 test_location_filter_semantics` | `COMPANY_REGISTRY.md` §5.2 |
| R20 | US-C1 | `AT-R-08 test_multiple_boards_independent_health` | `DATA_MODEL.md` §3.2 |
| R21 | US-H2 | `AT-R-09 test_source_test_returns_probe` | `API.md` §2 |
| R22 | US-C1 | `AT-R-10 test_company_collision_surfaced` | `COMPANY_REGISTRY.md` §8 |
| R23 | US-D1 | `AT-S-01 test_extraction_writes_typed_requirements` | `MATCH_SCORING.md` §2 |
| R24 | US-D1 | `AT-S-02 test_jd_text_is_delimited_data` | `AI_ARCHITECTURE.md` §7 |
| R25 | US-B1 | `AT-S-03 test_unresolved_skill_is_flagged_not_dropped` | `MATCH_SCORING.md` §3 |
| R26 | US-B1 | `AT-S-04 test_ungrounded_span_retries_then_manual` | `MATCH_SCORING.md` §13.2 |
| R27 | US-B1 | `AT-S-05 test_match_score_shape_and_key` | `DATA_MODEL.md` §6.1 |
| R28 | US-B1 | `AT-S-06 test_evidence_links_bullet_and_claims` | `MATCH_SCORING.md` §4.3 |
| R29 | US-B1 | `AT-S-07 test_exactly_one_recommended_per_posting` | `DATA_MODEL.md` §6.1 |
| R30 | US-B1 | `AT-S-08 test_zero_hard_requirements_not_perfect` | `MATCH_SCORING.md` §13.2 |
| R31 | US-B1 | `AT-S-09 test_variant_failure_does_not_kill_scoring` | `MATCH_SCORING.md` §13.2 |
| R32 | US-G1 | `AT-S-10 test_no_selection_probability_anywhere` | `MATCH_SCORING.md` §7 |
| R33 | US-E2 | `AT-S-11 test_formula_change_bumps_version` | `MATCH_SCORING.md` §11 |
| R34 | US-D1 | `AT-S-12 test_rescore_accepts_202` | `API.md` §3 |
| R35 | US-D1 | `AT-G-01 test_generation_thresholds_and_cap` | `DOCUMENT_GENERATION.md` §13 |
| R36 | US-E1 | `AT-G-02 test_plan_is_diff_not_document` | `DOCUMENT_GENERATION.md` §2.2 |
| R37 | US-E1 | `AT-G-03 test_every_bullet_carries_claim_ids` | `DOCUMENT_GENERATION.md` §4.4 |
| R38 | US-H1 | `AT-G-04 test_cover_letter_gate_respected` | `DOCUMENT_GENERATION.md` §7 |
| R39 | US-B1 | `AT-G-05 test_gap_paragraph_from_this_role_gaps` | `DOCUMENT_GENERATION.md` §6.2 |
| R40 | US-B3 | `AT-G-06 test_validation_runs_on_every_artifact` | `CLAIMS_LEDGER.md` §5 |
| R41 | US-B3 | `AT-G-07 test_failed_validation_retries_then_manual` | `CLAIMS_LEDGER.md` §10.2 |
| R42 | US-B3 | `AT-INV-04 test_failed_artifact_cannot_attach` | `CLAIMS_LEDGER.md` §6.1 |
| R43 | US-B3 | `AT-G-08 test_extraction_failure_fails_closed` | `CLAIMS_LEDGER.md` §10.2 |
| R44 | US-E2 | `AT-G-09 test_expired_or_deleted_claim_blocks` | `CLAIMS_LEDGER.md` §4.2 |
| R45 | US-B3 | `AT-G-10 test_restricted_claim_gated_by_employer` | `CLAIMS_LEDGER.md` §3 |
| R46 | US-E1 | `AT-G-11 test_similarity_warn_block_and_manual` | `DOCUMENT_GENERATION.md` §9.3 |
| R47 | US-B1 | `AT-V-01 test_one_review_item_per_posting` | `DATA_MODEL.md` §7.1 |
| R48 | US-B1 | `AT-V-02 test_review_detail_is_one_call` | `API.md` §5 |
| R49 | US-B2 | `AT-INV-02 test_approve_creates_one_app_zero_http` | `APPLICATION_PIPELINE.md` §15.2 |
| R50 | US-B2 | `AT-INV-01 test_no_submit_endpoint_in_openapi` | `API.md` §8 |
| R51 | US-B2 | `AT-V-03 test_already_decided_409` | `APPLICATION_PIPELINE.md` §15.3 |
| R52 | US-E1 | `AT-V-04 test_plan_edit_regenerates_and_revalidates` | `DOCUMENT_GENERATION.md` §11 |
| R53 | US-B1 | `AT-V-05 test_skip_records_reason` | `MATCH_SCORING.md` §12.2 |
| R54 | US-B2 | `AT-V-06 test_submit_confirm_prompt` | `APPLICATION_PIPELINE.md` §12 |
| R55 | US-F1 | `AT-T-01 test_event_log_is_truth` | `APPLICATION_PIPELINE.md` §5 |
| R56 | US-F1 | `AT-T-02 test_classification_appends_event` | `EMAIL_INGESTION.md` §8 |
| R57 | US-F1 | `AT-T-03 test_no_body_persisted` | `EMAIL_INGESTION.md` §9.1 |
| R58 | US-F1 | `AT-T-04 test_may_append_rejects_regressions` | `APPLICATION_PIPELINE.md` §15.4 |
| R59 | US-F1 | `AT-T-05 test_duplicate_event_idempotent` | `APPLICATION_PIPELINE.md` §15.5 |
| R60 | US-F1 | `AT-T-06 test_out_of_order_events_converge` | `APPLICATION_PIPELINE.md` §15.6 |
| R61 | US-F2 | `AT-T-07 test_manual_event_flags_and_nulls` | `APPLICATION_PIPELINE.md` §15.9 |
| R62 | US-F3 | `AT-T-08 test_ghosted_is_view_only` | `APPLICATION_PIPELINE.md` §15.7 |
| R63 | US-F3 | `AT-T-09 test_late_event_exits_ghosted_view` | `APPLICATION_PIPELINE.md` §15.8 |
| R64 | US-F1 | `AT-T-10 test_low_confidence_held_for_review` | `EMAIL_INGESTION.md` §7.3 |
| R65 | US-G1 | `AT-M-01 test_funnel_counters` | `APPLICATION_PIPELINE.md` §8.2 |
| R66 | US-G1 | `AT-M-02 test_rate_suppressed_below_min_n` | `APPLICATION_PIPELINE.md` §15.11 |
| R67 | US-G1 | `AT-M-03 test_every_row_carries_ci` | `APPLICATION_PIPELINE.md` §15.10 |
| R68 | US-D2 | `AT-M-04 test_referral_never_merged_with_direct` | `APPLICATION_PIPELINE.md` §9.4 |
| R69 | US-G1 | `AT-M-05 test_observed_not_predicted_note` | `API.md` §6 |
| R70 | US-G1 | `AT-M-06 test_export_opens_when_empty` | `APPLICATION_PIPELINE.md` §15.14 |
| R71 | US-A1 | `AT-M-07 test_digest_always_sends` | `EMAIL_INGESTION.md` §11 |
| R72 | US-A1 | `AT-O-01 test_run_under_fifteen_minutes` | `ARCHITECTURE.md` §9 |
| R73 | US-H1 | `AT-O-02 test_cost_from_usage_not_estimate` | `AI_ARCHITECTURE.md` §8 |
| R74 | US-H1 | `AT-O-03 test_budget_warn_caps_generation` | `AI_ARCHITECTURE.md` §13 |
| R75 | US-A3 | `AT-O-04 test_stage_rerun_is_noop` | `ARCHITECTURE.md` §8 |
| R76 | US-A3 | `AT-O-05 test_idempotency_key_replay` | `API.md` §1 |
| R77 | US-H2 | `AT-O-06 test_one_log_line_per_stage` | `ARCHITECTURE.md` §8 |
| R78 | US-H2 | `AT-O-07 test_no_secrets_in_logs` | `EMAIL_INGESTION.md` §2.5 |
| R79 | US-A2 | `AT-O-08 test_health_degrades_without_500` | `API.md` §7 |
| R80 | US-H3 | `AT-O-09 test_invalid_grant_recovery_path` | `EMAIL_INGESTION.md` §2.5 |
| R81 | US-E1 | `AT-O-10 test_flag_off_is_complete_behaviour` | `AI_ARCHITECTURE.md` §12 |
| R82 | US-E1 | `AT-O-11 test_artifact_provenance_complete` | `DATA_MODEL.md` §8.1 |
| R83 | US-C2 | `AT-INV-03 test_deny_list_refused_at_every_entry` | `SOURCE_ADAPTERS.md` §4.7 |
| R84 | US-C2 | `AT-INV-03b test_constant_unreachable_from_settings` | `SOURCE_ADAPTERS.md` §4.7 |
| R85 | US-C2 | `AT-INV-03c test_detect_403_before_any_request` | `COMPANY_REGISTRY.md` §2.4 |
| R86 | US-C2 | `AT-INV-03d test_all_url_surfaces_gated` | `COMPANY_REGISTRY.md` §2.4 |
| R87 | US-C2 | `AT-C-01 test_denied_url_storable_not_fetchable` | `COMPANY_REGISTRY.md` §2.4 |
| R88 | US-C2 | `AT-C-02 test_redirect_to_denied_host_aborts` | `SOURCE_ADAPTERS.md` §4.7 |
| R89 | US-A2 | `AT-C-03 test_robots_honoured_and_crawl_delay` | `SOURCE_ADAPTERS.md` §4.7 |
| R90 | US-A2 | `AT-C-04 test_unreachable_robots_fails_closed` | `SOURCE_ADAPTERS.md` §4.7 |
| R91 | US-A1 | `AT-INV-02b test_no_outbound_mail_to_third_party` | `EMAIL_INGESTION.md` §1 |
| R92 | US-F1 | `AT-C-05 test_gmail_modify_not_requested` | `EMAIL_INGESTION.md` §2.4 |

---

## 8. Invariant acceptance tests

The four invariants that define the product are proven by four tests. These are
not ordinary unit tests: they fail the build, they may not be marked `xfail`,
and they may not be deleted without a change to `ARCHITECTURE.md` §3.

### 8.1 AT-INV-01 — No submit endpoint exists in the OpenAPI schema

*Proves invariant 1 (no automated submission). Realises R50, and supports R49.*

```python
# backend/tests/invariants/test_no_submission.py
FORBIDDEN_PATH_TOKENS = {"submit", "apply", "application/send", "autofill"}

def test_no_submit_endpoint_in_openapi(app):
    schema = app.openapi()
    for path, ops in schema["paths"].items():
        low = path.lower()
        assert not any(t in low for t in FORBIDDEN_PATH_TOKENS), path
        for method, op in ops.items():
            summary = (op.get("summary", "") + op.get("description", "")).lower()
            assert "submit to" not in summary, (path, method)

def test_no_module_posts_to_employer_host(monkeypatch):
    """Static + runtime: nothing under api/, review/, tracking/ issues an
    outbound POST to a host that is not the LLM provider, Gmail, or an
    allow-listed ATS read endpoint."""
    calls = capture_outbound(["scout_careers.api",
                             "scout_careers.review",
                             "scout_careers.tracking"])
    exercise_full_review_and_approve_flow()
    assert [c for c in calls if c.method == "POST"] == []
```

Passing condition: every generated path and operation is enumerated; none
submits an application; and a full approve flow issues zero outbound POSTs from
the API, review and tracking layers.

### 8.2 AT-INV-02 — No outbound mail to a non-operator address

*Proves invariant 2 (no automated outbound mail to people). Realises R91, and
supports R49.*

```python
# backend/tests/invariants/test_no_outbound_mail.py
def test_only_recipient_is_the_operator(settings, sent_messages):
    run_full_day(discovery=True, mail_poll=True, digest=True)
    assert sent_messages, "the digest must actually send"
    for msg in sent_messages:
        assert msg.to == [settings.OPERATOR_EMAIL]
        assert msg.cc == [] and msg.bcc == []

def test_no_send_path_accepts_a_variable_recipient():
    """The send function's recipient is bound to configuration, not passed in."""
    sig = inspect.signature(scout_careers.mail.send.send_digest)
    assert "to" not in sig.parameters and "recipient" not in sig.parameters

def test_no_recruiter_address_reachable(sent_messages, seeded_mail):
    """Recruiter addresses exist in the ingested corpus. None is ever a
    destination."""
    ingested = {m.from_address for m in seeded_mail}
    run_full_day()
    assert not ({m.to[0] for m in sent_messages} & ingested)
```

Passing condition: the only recipient of the only class of outbound mail is the
configured operator address, the recipient cannot be varied by a caller, and no
address observed in ingested mail is ever a destination.

### 8.3 AT-INV-03 — A deny-listed host is refused at every entry point

*Proves invariant 4 (the never-scrape list is absolute). Realises R83–R88.*

```python
# backend/tests/invariants/test_never_fetch.py
DENIED = "https://www.linkedin.com/company/acme/jobs/"

@pytest.mark.parametrize("entry", [
    lambda c: c.post("/api/v1/companies/detect", json={"url": DENIED}),
    lambda c: c.post("/api/v1/companies",
                     json={"name": "Acme", "careers_url": DENIED}),
    lambda c: c.post("/api/v1/companies/1/sources",
                     json={"adapter": "manual", "config": {"url": DENIED}}),
    lambda c: c.patch("/api/v1/companies/1", json={"careers_url": DENIED}),
    lambda c: c.post("/api/v1/postings/import", json={"url": DENIED}),
])
def test_denied_at_every_entry_point(client, network_capture, entry):
    resp = entry(client)
    assert resp.status_code in (403, 422)
    assert resp.json()["meta"]["code"] == "source.denied_by_policy"
    assert network_capture.requests_to("linkedin.com") == []   # zero, not few

def test_constant_is_frozen_and_unreachable_from_settings():
    from scout_careers.sources.policy import NEVER_FETCH_HOSTS
    assert isinstance(NEVER_FETCH_HOSTS, frozenset)
    fields = set(Settings.model_fields)
    assert not any("never" in f.lower() or "deny" in f.lower() for f in fields)
    with pytest.raises((AttributeError, TypeError)):
        NEVER_FETCH_HOSTS.add("example.com")

def test_redirect_into_denied_host_is_aborted(respx_mock, network_capture):
    respx_mock.get("https://jobs.example.com/board").respond(
        302, headers={"Location": DENIED})
    with pytest.raises(DeniedByPolicy):
        SourceHttpClient().get("https://jobs.example.com/board")
    assert network_capture.requests_to("linkedin.com") == []

def test_denied_url_may_be_stored_but_never_fetched(client, network_capture):
    client.patch("/api/v1/companies/1", json={"website": DENIED})   # allowed
    run_discovery()
    assert network_capture.requests_to("linkedin.com") == []
```

Passing condition: every URL-accepting surface refuses, including manual company
creation; zero requests reach the denied host in any case; the constant is frozen
and not reachable from configuration; a mid-flight redirect is aborted; and a
denied URL may be stored as a reference without ever becoming a fetch.

### 8.4 AT-INV-04 — A failed artifact cannot attach to an application

*Proves invariant 3 (generation cites only the ledger). Realises R42, and
supports R40, R41, R43.*

```python
# backend/tests/invariants/test_ledger_enforcement.py
def test_failed_artifact_cannot_attach_to_review_item(db, failed_artifact, review_item):
    with pytest.raises(IntegrityError):
        db.execute(update(ReviewItem)
                   .where(ReviewItem.id == review_item.id)
                   .values(resume_artifact_id=failed_artifact.id))

def test_failed_artifact_cannot_attach_to_application(db, failed_artifact, application):
    with pytest.raises(IntegrityError):
        db.execute(update(Application)
                   .where(Application.id == application.id)
                   .values(cover_letter_artifact_id=failed_artifact.id))

def test_enforcement_is_in_the_database_not_only_the_service(db, failed_artifact):
    """Bypassing the service layer entirely must still fail."""
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE review_item SET resume_artifact_id = :a "
                        "WHERE id = :r"), {"a": failed_artifact.id, "r": "01J..."})

def test_no_override_endpoint_exists(app):
    schema = app.openapi()
    for path, ops in schema["paths"].items():
        for op in ops.values():
            text_ = (path + op.get("summary", "") + op.get("description", "")).lower()
            assert "bypass" not in text_ and "force_validation" not in text_

def test_uncited_numeric_fails_validation(validator):
    result = validator.check("cut run-rate ~60% across 14 centres")
    assert result.passed is False
    assert any(a.span == "14 centres" and not a.resolved for a in result.assertions)
```

Passing condition: attachment of a non-passing artifact is rejected by the
database even when the service layer is bypassed; no override or bypass endpoint
exists in the schema; and an uncited numeric assertion fails validation rather
than warning.

### 8.5 Where these tests run

All four run in the standard test job on every change, and additionally in a
dedicated `invariants` job that runs alone so that a failure is unambiguous.
Merging a change that skips, xfails or deletes any of them requires a
corresponding edit to `ARCHITECTURE.md` §3 in the same change, which is the
point at which the decision becomes visible instead of incidental.

---

## 9. Definition of done — the project

The project is done when every statement below is true simultaneously and has
been true across a full operating week.

### 9.1 Functional

| # | Criterion |
|---|---|
| DoD-1 | A discovery run at 08:00 IST polls the registry, completes in under 15 minutes, and reports per-source outcomes. |
| DoD-2 | A digest arrives at 08:15 IST every day, including days with nothing to report and days the run failed. |
| DoD-3 | The queue contains between 0 and 10 items per day, each with coverage numbers, named gaps, linked evidence and a reviewable tailoring plan. |
| DoD-4 | The operator can add a company by pasting a careers URL, and by pasting a role URL for a referral. |
| DoD-5 | Approve creates an application, freezes artifacts and returns downloads; the operator submits on the employer's site. |
| DoD-6 | Reply mail moves applications through the lifecycle without manual entry, and manual entry exists for everything off-email. |
| DoD-7 | The funnel reports observed rates with counts and intervals, suppresses rates below `MIN_N_FOR_RATE`, and never merges referral with direct. |
| DoD-8 | The nightly spreadsheet export is written and opens cleanly, including on an empty system. |

### 9.2 Quality gates

| # | Criterion |
|---|---|
| DoD-9 | All four invariant tests (§8) pass, in their own job. |
| DoD-10 | The scoring golden set passes every Tier 1 gate: hard recall ≥ 0.90, hard precision ≥ 0.85, `kind` accuracy ≥ 0.88, normalisation accuracy ≥ 0.95, coverage-level κ ≥ 0.70, variant-ranking ρ ≥ 0.75, grounding violations **0**. |
| DoD-11 | The generation eval passes: zero uncited assertions across the golden set, zero letters above `SIMILARITY_BLOCK` against the corpus, and every rendered resume within `RESUME_MAX_PAGES` verified by rendering rather than estimation. |
| DoD-12 | `ruff`, type checking and the unit suite pass; every schema change has an Alembic revision with an ID ≤ 32 characters. |
| DoD-13 | No secret, token, cookie, email body or raw resume content appears in any log line at any level; asserted by test, not by review. |
| DoD-14 | Every feature flag on the generation path defaults off, and its off state is a complete correct behaviour. |

### 9.3 Operational

| # | Criterion |
|---|---|
| DoD-15 | Measured daily LLM spend is under ₹80 at design scale, computed from response `usage`. |
| DoD-16 | `GET /health` reports Postgres, Redis, the LLM provider and the Gmail token, returning 200 while degraded. |
| DoD-17 | Gmail `invalid_grant` is recoverable by one CLI command and one browser consent, with no lost mail window and no lost digest. |
| DoD-18 | A restore from backup reproduces the registry, the ledger, the variants and the application history. |

### 9.4 Outcome — the only criterion that matters

| # | Criterion |
|---|---|
| DoD-19 | Across one full operating week the operator spent ≤ 10 minutes per day, reviewed the queue every day, and prepared 5–10 applications. |
| DoD-20 | After 40 applications the funnel reports a response rate with its interval against the recorded pre-Scout baseline, and the operator can name which variant and which tier converted. |

DoD-19 is the real test. A system that passes DoD-1 through DoD-18 and is not
opened on day nine has failed, and the failure mode is over-scope, not
under-quality (`ROADMAP.md` §6).

---

## 10. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | Canonical for invariants, module boundaries, pipeline and scale envelope |
| `DATA_MODEL.md` | Canonical for the entities every requirement here refers to |
| `API.md` | Canonical for endpoint paths, envelopes and status codes cited in acceptance criteria |
| `ROADMAP.md` | The delivery sequence in which these requirements are satisfied, and the exit gate per phase |
| `MATCH_SCORING.md` | Specifies R23–R34; owns the Tier 1/2/3 evaluation cited in DoD-10 |
| `CLAIMS_LEDGER.md` | Specifies R40–R45 and the enforcement behind AT-INV-04 |
| `DOCUMENT_GENERATION.md` | Specifies R35–R39, R46, and the rollout discipline behind R81 |
| `APPLICATION_PIPELINE.md` | Specifies R55–R70; its §15 acceptance criteria are a subset of §7 here |
| `EMAIL_INGESTION.md` | Specifies R10, R56–R57, R64, R71, R80, R91–R92 |
| `SOURCE_ADAPTERS.md` | Specifies R1–R11, R83–R90 |
| `COMPANY_REGISTRY.md` | Specifies R13–R22 and R85–R87 |
| `AI_ARCHITECTURE.md` | Specifies R24, R73–R74, R81; owns the cost model behind O6 |
| `DATA_SOURCES_AND_COMPLIANCE.md` | The legal basis behind §3.1 |
| `SECURITY_ARCHITECTURE.md` | Threat model behind R24, R78 and the single-user decision in §1.3 |
