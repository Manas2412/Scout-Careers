# DOCUMENTATION INDEX — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the documentation map and the authority order.
Nothing here overrides a subject document; this file says which document to open.

28 documents, ~31,000 lines. No executable code exists yet — the documentation
set is deliverable 0.1.0.

---

## 1. Authority order

When two documents disagree, resolve in this order and correct the loser in the
same change:

```
ARCHITECTURE.md  >  DATA_MODEL.md  >  API.md  >  subsystem documents
```

A subsystem document is canonical for its own formula or algorithm only —
`MATCH_SCORING.md` owns the composite score formula, `CLAIMS_LEDGER.md` owns the
validation pass — but never for schema, endpoints or system invariants.

`SOP.md` §13 requires that any change contradicting a document updates that
document in the same merge request. A stale document is a defect, not debt.

---

## 2. Start here

| If you are… | Read, in order |
|---|---|
| New to the project | `README.md` → `HLD.md` → `ARCHITECTURE.md` |
| Implementing a feature | `ARCHITECTURE.md` → `SDD.md` → the subsystem doc → `SOP.md` |
| Setting it up | `INSTALLATION_GUIDE.md` → `CONFIGURATION.md` |
| Operating it | `OPERATIONS_MAINTENANCE.md` → `DEPLOYMENT_ENV_RUNBOOK.md` |
| Reviewing the legal position | `DATA_SOURCES_AND_COMPLIANCE.md` → `SECURITY_ARCHITECTURE.md` |
| Deciding what to build next | `ROADMAP.md` → `BRD_USER_STORIES_ACCEPTANCE.md` |

---

## 3. The four invariants

Stated in `ARCHITECTURE.md` §3, enforced in code, proven by the tests in
`TEST_PLAN.md` §4. Every document is written to hold them.

1. **No automated submission.** The system prepares; the human submits. No
   endpoint submits — the absence is the enforcement.
2. **No automated outbound mail to people.** One message class only: the daily
   digest, to the operator's own address.
3. **Generation cites only the claims ledger.** An artifact failing validation
   cannot attach to an application — enforced by a database trigger.
4. **The never-scrape list is absolute.** LinkedIn first. A code constant, not
   configuration.

`DATA_SOURCES_AND_COMPLIANCE.md` §7–§8 carries the reasoning. Any future request
touching submission, outbound contact or a deny-listed source is rejected at
design time rather than evaluated.

---

## 4. Canonical documents

| Document | Canonical for | Lines |
|---|---|---|
| `ARCHITECTURE.md` | System invariants, module boundaries, technology decisions, pipeline stages, scale envelope | ~330 |
| `DATA_MODEL.md` | Tables, columns, types, constraints, indexes, views, functions, triggers, migration policy | ~640 |
| `API.md` | Endpoint paths, request and response envelopes, status codes, machine error codes | ~380 |

---

## 5. Subsystem documents

| Document | Covers | Canonical for |
|---|---|---|
| `SOURCE_ADAPTERS.md` | The `SourceAdapter` protocol and eleven implementations; endpoint shapes, retry, rate limits, robots, circuit breaking, normalisation, fidelity ranking | Adapter contract and per-source endpoint behaviour |
| `COMPANY_REGISTRY.md` | ATS auto-detection from a pasted URL, tiering, status, tags, the Companies page, deduplication, the 50-company seed set, CSV import | Detection patterns and registry semantics |
| `MATCH_SCORING.md` | Requirement extraction, skill normalisation, coverage scoring, the composite formula, ranking, gap analysis, the two worked Seagate examples | The composite score formula and coverage rules |
| `CLAIMS_LEDGER.md` | The grounding rule, claim anatomy, confidentiality tiers, expiry, the validation pass, database-level enforcement, provenance, the 63-row seed ledger | Citation resolution and validation |
| `DOCUMENT_GENERATION.md` | The tailoring-diff model, bullet selection, the `.docx` contract, cover letters and the computed honest-gap paragraph, anti-templating | Tailoring plan schema and generation rules |
| `AI_ARCHITECTURE.md` | Provider abstraction, model routing, the four prompt families, structured-output enforcement, injection defence, cost model, evaluation | Prompt contracts and model routing |
| `EMAIL_INGESTION.md` | Gmail OAuth and scopes, job-alert parsing, status classification, message-to-application linkage, the daily digest, privacy posture | Mail classification and digest content |
| `APPLICATION_PIPELINE.md` | The state machine, the append-only event log, manual events, funnel metrics, statistical honesty, spreadsheet export, retention | Lifecycle transitions and funnel definitions |

---

## 6. Design and product

| Document | Covers |
|---|---|
| `HLD.md` | High-level design for a reader new to the system: purpose, scope, components, integrations, decisions with rejected alternatives, quality attributes, failure modes |
| `SOLUTION_ARCHITECTURE.md` | Capability map, architecture layers, integration table, entity lifecycle, four sequence diagrams, deployment views, sixteen ADR entries, technical-debt register, multi-user evolution path |
| `SDD.md` | Low-level design: module-by-module interfaces with real signatures, seven key algorithms, concurrency and transaction boundaries, the scheduler, frontend design, exception taxonomy, performance budget, nine open questions |
| `BRD_USER_STORIES_ACCEPTANCE.md` | 92 EARS requirements, 8 epics / 19 user stories with Given–When–Then, the traceability matrix, the four invariant acceptance tests, definition of done |
| `ROADMAP.md` | Five phases with exit gates, the deferred backlog with reopen conditions, risks, the prove-before-flipping procedure |

---

## 7. Operations

| Document | Covers |
|---|---|
| `INFRASTRUCTURE.md` | Compose topology and `docker-compose.yml`, VM sizing, storage growth, TLS, the ECS Fargate alternative and its cost delta, backup and restore, RPO/RTO, capacity at 3,000 companies |
| `DEPLOYMENT_ENV_RUNBOOK.md` | First deploy, routine deploy with migrations-before-traffic, rollback, secret provisioning, the Gmail OAuth walkthrough, smoke tests, go-live checklist, eight troubleshooting runbooks |
| `CONFIGURATION.md` | The complete environment-variable surface, feature flags and defaults, tuning constants, `.env.example`, boot validation, per-environment overrides |
| `OPERATIONS_MAINTENANCE.md` | Daily rhythm, weekly and monthly checklists, source health, cost monitoring, database maintenance, the ledger review discipline, a 24-row metrics table, five incident playbooks |
| `INSTALLATION_GUIDE.md` | Prerequisites, local install, seed data, Gmail OAuth and Bedrock enablement, production install, six verification steps, install troubleshooting |
| `DEVELOPMENT.md` | Repository layout, coding standards, branch strategy, `ci/run-checks.sh`, adding an adapter or migration or prompt or claim, debugging recipes, definition of done |

---

## 8. Governance

| Document | Covers |
|---|---|
| `SECURITY_ARCHITECTURE.md` | Threat model, prompt injection as the primary application-layer threat, SSRF defence on URL detection, the deny list as a constant, single-user auth and the multi-user delta, secrets, OWASP A01–A10, database-level controls, stated limitations |
| `DATA_SOURCES_AND_COMPLIANCE.md` | The never-scrape list with LinkedIn first, the sanctioned-channel principle, the per-source legal-basis table, rate limits, why auto-submission and automated outreach are excluded, DPDP and GDPR position, the release checklist |
| `SOP.md` | Golden rules, the spec-driven change pipeline, API and database rules, performance budgets, branch and merge-request rules, prove-before-flipping, the review checklist |
| `TEST_PLAN.md` | Testing philosophy, the invariant tests (the most important in the repository), adapter contract tests, the LLM evaluation harness, ledger tests, manual checklist, coverage targets |
| `RELEASE_NOTES.md` | Versioning, the release process, the changelog format, the prominent-callout policy, `0.1.0 — Unreleased` and the planned 0.2.0–0.6.0 phase mapping |

---

## 9. Reading paths

**Understanding how a job becomes an application** — follow the pipeline:
`SOURCE_ADAPTERS.md` → `COMPANY_REGISTRY.md` → `MATCH_SCORING.md` →
`CLAIMS_LEDGER.md` → `DOCUMENT_GENERATION.md` → `APPLICATION_PIPELINE.md` →
`EMAIL_INGESTION.md`.

**Understanding why it refuses to do things** — the design constraints:
`ARCHITECTURE.md` §3 → `DATA_SOURCES_AND_COMPLIANCE.md` →
`SECURITY_ARCHITECTURE.md` → `TEST_PLAN.md` §4.

**Understanding the numbers** — cost, capacity and honesty about metrics:
`ARCHITECTURE.md` §9 → `AI_ARCHITECTURE.md` (cost model) →
`INFRASTRUCTURE.md` (sizing) → `MATCH_SCORING.md` §7 (why there is no selection
probability) → `APPLICATION_PIPELINE.md` §8 (observed funnel rates).

---

## 10. Known open items

Carried from the documents that raised them, so they are visible in one place.

| Item | Raised in | Position |
|---|---|---|
| Workday's 1 req/s rate limit plus mandatory per-job detail fetch cannot complete a board of more than ~170 postings inside the 180 s per-source ceiling | `SDD.md` §11.3, §12.1 | Real defect in the current design. Must be resolved before the Workday adapter ships in Phase 3 — options are a longer ceiling for that adapter, incremental pagination across runs, or deferring detail fetch |
| Meta and Apple adapters deferred | `SOURCE_ADAPTERS.md` | Deliberate. GraphQL-backed with aggressive bot protection; expected to break repeatedly |
| Nine design questions | `SDD.md` §12 | Open by design; resolve during implementation |
| Sixteen ADRs recorded, technical debt registered | `SOLUTION_ARCHITECTURE.md` | Accepted for a single-user tool |
| Claims ledger expiry dates need first verification pass | `CLAIMS_LEDGER.md` | Seed ledger carries `verified_at`; re-verification is a monthly task |
