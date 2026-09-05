# RELEASE NOTES — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the versioning scheme, the release process and the
changelog. `ROADMAP.md` is canonical for what a phase contains; the planned
entries in §7 mirror it and are corrected from it, never the other way round.
`SOP.md` is canonical for the merge gates a release depends on.

**Nothing has been implemented yet.** At the time of writing, the repository
contains its documentation set and no executable code. Every entry below `0.1.0`
is a **plan**, marked as such, and describes what a release is intended to
contain rather than what it contains. No entry in this file asserts a shipped
feature until the code for it is merged.

---

## 1. Versioning

Semantic versioning, `MAJOR.MINOR.PATCH`. The standard definition assumes a
public API with external consumers, which this system does not have — one
operator, one instance, one client generated from the server's own schema. So the
scheme is reinterpreted around what actually breaks here: the operator's data,
the operator's routine, and the operator's trust in the output.

### 1.1 What each number means

**MAJOR** — the operator has to do something, or something they relied on is
gone.

- An invariant changed (`ARCHITECTURE.md` §3). This is the strongest possible
  reason for a major bump and is treated as one regardless of how small the code
  change is.
- A migration that is not reversible, or that requires a manual step before or
  after `alembic upgrade head`.
- The claims ledger schema changed in a way that requires re-verification or
  re-entry of existing claims.
- Configuration keys removed or renamed such that an existing `.env` no longer
  starts the application.
- Generated documents change in a way that makes previously generated artifacts
  non-reproducible — a change to what `prompt_version` or `model` provenance
  means.

**MINOR** — new capability, backwards compatible. The operator upgrades and their
existing data, configuration and routine keep working.

- A new adapter, a new endpoint, a new UI screen, a new metric.
- A prompt version bump that passed its eval.
- A feature flag added (off), or a flag promoted to default after its slice
  proved out (`SOP.md` §10).
- A reversible migration that adds tables, columns or indexes.

**PATCH** — a defect repaired, or an internal change with no behavioural
difference the operator would notice.

- A bug fix, an adapter repaired after vendor drift, a dependency bump, a
  performance improvement, a documentation correction.
- A tightened validation rule that rejects something that should always have been
  rejected. This is a patch, not a minor: the correct behaviour was always the
  documented one.

### 1.2 The rule that overrides the numbering

**A change to an invariant, to the claims ledger schema, or to the never-scrape
list is called out prominently in the release entry regardless of the version
bump it earns.** See §5. The version number tells the operator how carefully to
upgrade; the callout tells them what to go and read. Those are different jobs,
and a change that is technically a patch can still be the most important thing in
a release.

### 1.3 Pre-1.0

While the major version is `0`, the system is in build-out and the phases in
`ROADMAP.md` map to minor versions (§7). `1.0.0` is reached when all five phases
have shipped, every invariant test is green against real sources, and the system
has run unattended for thirty consecutive days producing a daily digest the
operator acts on. Not before, and not because a date arrived.

---

## 2. The release process

A release is a tag on `main` plus an entry in this file. There is no separate
release branch; the operator is the only consumer and `main` is always the
candidate.

```bash
# 1. main is green and every merge in the release is already reviewed.
git checkout main && git pull
bash ci/run-checks.sh all

# 2. The eval, if any prompt, vocabulary, formula or model ID moved since the
#    last tag. Attach the delta table to the release entry.
cd backend && python -m scout_careers.eval run --family all \
    --baseline "$(git describe --tags --abbrev=0)"

# 3. Migrations, forward and back, against a copy of production data.
alembic upgrade head && alembic downgrade -1 && alembic upgrade head

# 4. The manual checklist (TEST_PLAN.md §10). Results recorded in the entry.

# 5. Write the entry: move [Unreleased] to the new version, date it,
#    add a fresh empty [Unreleased] above it.

# 6. Tag and deploy.
git tag -a v0.2.0 -m "0.2.0 — discovery"
git push origin main --tags
docker compose up -d --build

# 7. Verify in production, then record the verification in the entry.
curl -fsS localhost:8000/api/v1/health | jq .
```

**Before the tag:**

- [ ] `bash ci/run-checks.sh all` green on `main`.
- [ ] Eval run and delta table attached, if the generation path moved.
- [ ] Migrations exercised forward and back against a production data copy.
- [ ] Manual checklist complete (`TEST_PLAN.md` §10), results in the entry.
- [ ] `CONFIGURATION.md` lists every new or changed key with its default.
- [ ] Every document contradicted by the release has been updated (`SOP.md` §12).
- [ ] A database backup taken and its restore verified.

**After the deploy:**

- [ ] `GET /api/v1/health` reports every dependency healthy.
- [ ] One discovery run executed and inspected via `GET /runs/{id}`.
- [ ] The next morning's digest arrived and reads correctly.
- [ ] Rollback rehearsed at least in description: the previous image tag is
      known, and any irreversible migration is named in the entry with its
      restore procedure.

**Rollback.** Re-deploy the previous image tag. If the release contained a
reversible migration, `alembic downgrade` to the previous revision. If it
contained an irreversible one, the release entry names the restore procedure —
that is the entire reason irreversible migrations force a major bump.

**Hotfixes** get a patch tag and skip nothing. There is no "urgent enough to
bypass the gate"; a single-user tool being broken for two extra hours costs less
than a hotfix that ships an unreviewed change to the generation path.

---

## 3. Changelog format

Reverse chronological. Newest at the top, immediately under `[Unreleased]`.

```markdown
## [X.Y.Z] — YYYY-MM-DD

One or two sentences on what this release is for. Written for the operator six
months from now who is trying to work out when a behaviour changed.

### Added
### Changed
### Fixed
### Security
### Deprecated
### Removed
```

Empty categories are omitted, not written as "none".

### 3.1 The categories

| Category | Contains |
|---|---|
| **Added** | New capability. New adapters, endpoints, screens, metrics, flags (state the default), settings keys |
| **Changed** | Existing behaviour that now behaves differently. Prompt version bumps, scoring formula changes, flag promotions, thresholds, envelope or schema changes |
| **Fixed** | Defects repaired. Name the symptom the operator would have seen, not only the cause — "postings from failed sources were closed after two runs", not "corrected the close predicate" |
| **Security** | Anything touching secrets, auth, OAuth scopes, redaction, prompt injection defence, outbound policy or dependency vulnerabilities. Present even when the change is a hardening with no known exploit |
| **Deprecated** | Still working, scheduled for removal. Must state the removal version and the replacement |
| **Removed** | Gone. Must state what to use instead, or that there is no replacement and why |

### 3.2 Writing rules

- Each entry is one line, past tense, describing the change from the operator's
  point of view.
- Reference the spec directory and the merge request: `(specs/012-…, !47)`.
- Name the affected `Settings` key inline where one exists.
- A prompt or scoring change states its version string and the eval verdict:
  `extract.v4 (eval: hard-recall 0.94, +0.02 vs extract.v3)`.
- No marketing language. "Improved matching" is not an entry; "raised
  `SCORING_BLEND_HARD` from 0.75 to 0.80, which demotes roles whose nice-to-have
  coverage was carrying the score" is.

---

## 4. Prominent callouts

Certain changes get a callout block at the top of the entry, above the
categories, regardless of the version bump.

**The three that always qualify:**

1. **An invariant changed** (`ARCHITECTURE.md` §3). Any addition, removal or
   weakening.
2. **The claims ledger schema changed** — the `claim` or `claim_usage` tables,
   the confidentiality model, the expiry semantics, the detection families, or
   the resolution chain. These determine what the system is permitted to assert
   about the operator, and a silent change to them is the one change that could
   let a false statement through without anybody noticing.
3. **The never-scrape list changed** — `NEVER_FETCH_HOSTS`. Adding a host is
   routine and still gets the callout, because the list is the record of a
   deliberate compliance position and the release notes are where that record is
   kept in public view.

The callout format:

```markdown
> **INVARIANT CHANGE — read before upgrading.**
> Invariant 5 (adapter failure isolation) now also covers the mail-alert
> adapter, which previously aborted the run on a parse failure.
> Rationale: specs/031-alert-isolation/requirements.md §2.
> Action required: none. Existing runs behave the same or better.
```

```markdown
> **LEDGER SCHEMA CHANGE — action required.**
> `claim.expires_at` is now NOT NULL for claims with `metric_unit = 'count'`.
> Twelve existing claims have no expiry and must be given one before the next
> generation run; `POST /api/v1/claims/validate` will fail closed for drafts
> citing them until they are updated. Run `scripts/list_undated_claims.py`.
```

```markdown
> **NEVER-SCRAPE LIST CHANGED.**
> Added `wellfound.com` and `angel.co` following the terms review in
> DATA_SOURCES_AND_COMPLIANCE.md §4. Two sources are auto-disabled by this
> release; roles from those employers now enter via mail alerts only.
```

A callout states **what changed, why, and what the operator must do** — in that
order, and the third part is never omitted, even when the answer is "nothing".

---

## 5. Release entry template

```markdown
## [X.Y.Z] — YYYY-MM-DD

<One or two sentences: what this release is for.>

> **<CALLOUT TYPE>.**            <!-- only if §4 applies; delete otherwise -->
> What changed. Why. What the operator must do.

### Added
- <capability>. Settings: `NEW_KEY` (default `x`). (specs/NNN-slug, !MR)

### Changed
- <behaviour>, previously <old behaviour>. (specs/NNN-slug, !MR)
- Prompt `family.vN` → `family.vN+1`. Eval: <metric> <value> (<delta> vs baseline).

### Fixed
- <symptom the operator would have seen>. (!MR)

### Security
- <control added or tightened>. (!MR)

### Deprecated
- <thing>. Removal in X.Y+1.0. Use <replacement>.

### Removed
- <thing>. Replaced by <replacement>. / No replacement: <why>.

---

**Migrations:** `<revision_id>` (reversible: yes/no). <Manual steps, if any.>
**Flags:** `FF_X` added, default off. `FF_Y` promoted to default after <slice>,
<duration>, <metrics observed>.
**Eval:** run `<id>`, <verdict>. / Not required: no generation-path change.
**Manual checklist:** completed <date>. <Anything noted.>
**Performance:** run <mm:ss>, API p95 <ms>, first load <s>. / Exception: <link>.
**Verified in production:** <date>, <what was checked>.
```

---

## 6. Changelog

### [Unreleased]

Nothing merged yet beyond the documentation set below.

---

### [0.1.0] — Unreleased

**The documentation set.** The first deliverable of this project is its
specification, not its code. Every invariant, contract, schema, threshold and
failure mode was designed and written down before implementation began, because
the system's worst failure mode — a false claim reaching an employer — is a
design failure, not a coding failure, and it is cheapest to prevent on paper.

**No executable code has been written.** There is no backend, no frontend, no
migration and no adapter. The repository contains `docs/` and nothing else that
runs.

#### Added

- `ARCHITECTURE.md` — system context, trust boundaries, **the eight invariants**,
  technology decisions with their rejections, module structure, the eleven-stage
  pipeline, core entities, cross-cutting concerns, the scale envelope.
- `DATA_MODEL.md` — the full PostgreSQL 16 schema: enumerations, registry,
  postings, requirements, resume variants, the claims ledger, `claim_usage`,
  scoring, review, applications and their event log, artifacts, mail, `run_log`,
  the derived views, and the migration policy.
- `API.md` — the `{data, message, meta}` envelope, status codes, cursor
  pagination, idempotency, every endpoint under `/api/v1/`, and §8's record of
  what is deliberately absent from the API surface.
- `SOURCE_ADAPTERS.md` — the `SourceAdapter` protocol, `RawPosting`, the shared
  HTTP client, retry, rate limiting, robots.txt and the never-scrape list,
  circuit breaking, all ten adapter families, fidelity ranking, normalisation,
  failure isolation, and the thirteen-step checklist for adding an adapter.
- `COMPANY_REGISTRY.md` — ATS auto-detection from a pasted URL, the pattern
  table, the live probe, the never-scrape refusal path, tiering, status, tags,
  per-company defaults, deduplication, seeding and CSV bulk import.
- `MATCH_SCORING.md` — requirement extraction and its structured-output
  contract, skill normalisation, coverage scoring, the composite formula with
  worked arithmetic, ranking, gap analysis, two full worked examples, rescoring
  and prompt versioning, the three tiers of scorer evaluation, and §7's argument
  for why no selection probability is computable.
- `CLAIMS_LEDGER.md` — claim anatomy, the confidentiality model, expiry and
  decay, the validation pass (regex families plus the LLM assertion pass),
  enforcement at the database, provenance, the seeded starter ledger, and
  maintenance discipline.
- `DOCUMENT_GENERATION.md` — the tailoring model, the `tailoring_plan` schema,
  bullet selection, the `.docx` builder contract and the one-page fit loop, cover
  letter generation with the honest-gap paragraph, the validation gate,
  anti-templating, prompt versioning and rollout discipline.
- `EMAIL_INGESTION.md` — the outbound invariant and its three enforcement layers,
  Gmail OAuth with the minimum scope set, job-alert parsing, status tracking,
  message-to-application linkage, classification, the privacy posture, prompt
  injection defence, the daily digest, and quota arithmetic.
- `APPLICATION_PIPELINE.md` — the full state machine, legal transitions, why
  `ghosted` is a view, the event log as truth, manual events, the two human
  gates, funnel metrics, statistical honesty at small sample sizes, the
  spreadsheet export, follow-up prompts and retention.
- `AI_ARCHITECTURE.md` — the governing principle, provider abstraction, model
  routing, the four prompt families with versioning, structured-output
  enforcement, prompt injection defence, the cost model, caching, the evaluation
  harness, observability, feature flags and rollback.
- `SOP.md` — the golden rules, the Specify → Plan → Tasks → Implement → Verify →
  Merge Request pipeline and which gates a human owns, coding standards, API and
  database rules, security invariants, performance budgets and the documented
  exception, branch and merge rules, "prove before flipping", the review
  checklist, and the document-maintenance rule.
- `TEST_PLAN.md` — the testing philosophy, the pyramid as applied here, unit
  tests by module, **the invariant tests**, adapter contract tests with a
  scheduled drift smoke, the LLM evaluation harness with its gates, the claims
  ledger test table, integration tests against a fixture Postgres, frontend
  tests, the manual checklist, the deterministic LLM stub, coverage targets, and
  the composition of `ci/run-checks.sh`.
- `RELEASE_NOTES.md` — this file.

#### Notes

**Migrations:** none — no schema exists yet.
**Flags:** none.
**Eval:** not applicable — no prompts have been written.
**Manual checklist:** not applicable — nothing renders yet.
**Performance:** budgets are stated in `SOP.md` §8 and have not been measured.
**Verified in production:** nothing is deployed.

---

## 7. Planned releases

These map one-to-one onto the five phases in `ROADMAP.md`, which is canonical for
their contents. They are **plans**, not commitments to dates, and the list below
is corrected from `ROADMAP.md` whenever the two disagree.

The ordering follows the pipeline: nothing downstream is built before the stage
that feeds it exists, because a scorer with no postings to score is untestable
and a generator with no ledger to cite from is dangerous.

### [0.2.0] — Phase 1 · Foundation and discovery

Everything up to and including pipeline stage ④. The system finds roles and shows
them; it does not yet understand them.

- Project skeleton, `Settings`, structured logging, the `ci/run-checks.sh` gate.
- The full schema and its Alembic baseline; the seed script for the six resume
  variants and the starter ledger.
- `SourceAdapter` protocol, the shared HTTP client with retry, rate limiting,
  robots.txt handling and **`NEVER_FETCH_HOSTS` enforced from the first commit**.
- The Greenhouse, Lever and Ashby adapters, with their fixture sets.
- The company registry, ATS auto-detection from a pasted URL, CSV bulk import.
- Discovery run orchestration: normalise, dedupe, deterministic filters,
  `run_log` with per-source results, failure isolation.
- APScheduler with the 08:00 IST daily run.
- The Companies and Jobs screens.

**Invariants live at this release:** 4 (never-scrape), 5 (failure isolation), 6
(no secrets), 8 (robots and rate limits). Their tests ship with the code that
they constrain, not afterwards.

### [0.3.0] — Phase 2 · Understanding and matching

Pipeline stages ⑤ to ⑦, plus the ledger that stages ⑧ and ⑨ will depend on.

- The provider-agnostic LLM client, the prompt registry, `PROMPTS.lock`.
- Requirement extraction with structured output and injection-resistant prompt
  construction.
- Skill normalisation and its controlled vocabulary.
- Coverage scoring, the composite formula, ranking, gap analysis.
- The claims ledger: CRUD, confidentiality, expiry, and
  `POST /claims/validate` with both detection passes.
- The golden set and the eval harness, with its gates enforced.
- The Claims and Variants screens; scores and gaps on the Jobs screen.

**Invariant 3 becomes enforceable here** — the validation pass exists before
anything can generate a document that would need it. That ordering is deliberate.

### [0.4.0] — Phase 3 · Generation and review

Pipeline stages ⑧ to ⑩. The first release that produces something a human would
send.

- Tailoring plan generation, bullet selection, the plan schema.
- The `.docx` builder contract, the one-page fit loop with rendered
  verification, file naming and storage.
- Cover letter generation with the honest-gap paragraph and the anti-templating
  similarity check.
- The validation gate wired into generation, with the database triggers that make
  a failed artifact unattachable.
- The review queue and its state machine; approve, skip, edit-the-plan, download.
- `FF_TAILORING_REPHRASE` and `FF_GAP_IN_OPENING`, both **off**.

**Invariants 1, 3 and 7 are fully live.** The manual checklist
(`TEST_PLAN.md` §10) becomes a release requirement from this version, because
this is the first release whose output leaves the machine.

### [0.5.0] — Phase 4 · Mail, tracking and the digest

Pipeline stage ⑪, and everything after the human submits.

- Gmail OAuth with the minimum scope set; the read poller and the sync cursor.
- Job-alert parsing — the path by which LinkedIn-sourced roles enter without
  LinkedIn ever being fetched.
- Message-to-application linkage, classification, and the transition rules.
- The application lifecycle, the append-only event log, manual events.
- Funnel metrics with confidence intervals and the minimum-`n` rule.
- The daily digest at 08:15 IST, to the operator's address and structurally
  nowhere else.
- The spreadsheet export and follow-up prompts.
- The Applications and Dashboard screens.

**Invariant 2 becomes live and is enforced in three layers from the first
commit** that can send anything at all.

### [0.6.0] — Phase 5 · Breadth and hardening

- The remaining adapters: Workday, SmartRecruiters, Workable, Recruitee, Google,
  Amazon, Microsoft.
- The adapter drift smoke test on its schedule.
- Backups with a verified restore, and the operational runbook.
- Performance work against the budgets in `SOP.md` §8.
- Settings and health screens complete; the fourteen-day cost sparkline.
- Flag promotions for anything that proved out on its slice.

### [1.0.0] — When it has earned it

All five phases shipped. Every invariant test green against real sources. Thirty
consecutive days of unattended operation producing a digest the operator acts on.
Enough applications tracked that the funnel view says something — which,
per `APPLICATION_PIPELINE.md` §9, is around forty, and is a fact about the
operator's job search rather than about the software.

---

## 8. Related documents

| Document | Relationship |
|---|---|
| `ROADMAP.md` | Canonical for phase contents; §7 mirrors it |
| `SOP.md` | The gates a release depends on; the document-maintenance rule |
| `TEST_PLAN.md` | The gate composition, the eval, the manual checklist |
| `ARCHITECTURE.md` §3 | The invariants whose change forces a callout |
| `CLAIMS_LEDGER.md` | The ledger schema whose change forces a callout |
| `SOURCE_ADAPTERS.md` §4.7 | The never-scrape list whose change forces a callout |
| `DATA_MODEL.md` §11 | Migration policy, which drives the major-bump rule |
| `CONFIGURATION.md` | Every settings key a release adds, changes or removes |
| `INFRASTRUCTURE.md` | Deployment, backups and the restore procedure |
