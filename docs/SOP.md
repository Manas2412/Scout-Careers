# SOP — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for engineering process — the change pipeline, the
gates, the review checklist and the merge rules. `ARCHITECTURE.md` wins on
system design and the invariants; `DEVELOPMENT.md` wins on the mechanical detail
of coding standards, tooling configuration and local setup; `DATA_MODEL.md` wins
on schema; `API.md` wins on endpoint contracts. Where this file summarises one of
those, the summary carries authority only until the source document is read.

---

## 1. Purpose and scope

This is the always-on ruleset for every change made to this repository, by a
human or by an agent. It is loaded before proposing, planning or writing code,
and it is not optional for "small" changes — the definition of small is in §3.7
and it is narrower than instinct suggests.

**Scope.** Everything in the repository: `backend/`, `frontend/`, `ci/`,
`docs/`, `infra/`, seed data, prompts and the migration set.

**Why a process document exists for a single-user tool.** Scout Careers has one
operator and one running instance. Nothing here is about coordination overhead.
It exists because the system's failure modes are asymmetric: an outage costs the
operator a day, while a fabricated claim in a document that reaches an employer
costs them their credibility with that employer permanently, and cannot be
retracted. A process that would be over-engineering for a hobby CRUD app is
correctly sized for a system that writes assertions about a real person's career
and then hands them to strangers who will check.

The second reason is that most of the code here will be written by agents. An
agent will follow a rule it can read and will cheerfully violate a rule that
lives only in someone's head. Every rule in this document is therefore stated so
that it can be checked mechanically, and most of them are.

---

## 2. Golden rules

These are not guidelines. A change that violates one of them is rejected on
sight, regardless of how well it works.

1. **Never bypass the pre-merge gate.** No `--no-verify`, no `# noqa` without a
   cited reason, no `pytest.mark.skip` on a failing invariant test, no
   `--force-with-lease` onto a protected branch, no merging with a red
   `ci/run-checks.sh`. If the gate is wrong, fix the gate in its own merge
   request and say so.
2. **Never hardcode a secret.** No API key, OAuth client secret, refresh token,
   database password, AWS credential or Gmail token appears in any file that is
   tracked by git — including tests, fixtures, seed scripts, docker-compose files
   and documentation examples. Configuration comes from the environment through
   the single `Settings` object (`ARCHITECTURE.md` §8). Secrets never appear in a
   log line at any level (invariant 6).
3. **Never add a submission path or an outbound-contact path.** No code that
   POSTs an application to an employer, drives a browser to submit a form,
   solves a CAPTCHA, or sends mail to any address other than
   `MAIL_OPERATOR_ADDRESS`. This includes helpers, scripts, "temporary" debug
   utilities and anything under `scripts/`. Invariants 1 and 2 are enforced by
   absence: the correct implementation of a submit endpoint is that it does not
   exist (`API.md` §8).
4. **Never let generated text cite outside the claims ledger.** Every factual or
   numeric assertion in a generated resume bullet or cover letter resolves to a
   `claim` row or the artifact fails validation and cannot be attached
   (invariant 3, `CLAIMS_LEDGER.md` §5–§6). There is no override endpoint, no
   settings key and no admin flag that lets a failed artifact through. A change
   that adds one is the most serious change this repository can reject.
5. **Never fetch a host on the never-scrape list.** `NEVER_FETCH_HOSTS` in
   `sources/policy.py` is a frozen code constant, not configuration
   (invariant 4). It is checked inside the HTTP client after redirect
   resolution, so a 302 into LinkedIn is refused mid-flight. No configuration
   value reaches this check.
6. **"Done" means the executable acceptance criteria pass.** Not "it runs", not
   "looks right", not "the diff is clean". A task is done when the criteria
   written in its spec are green in `ci/run-checks.sh`, and — for anything on the
   extraction, scoring or generation path — when the eval harness holds
   (`TEST_PLAN.md` §6). A change with no executable criterion is not finishable
   and should not have been started; go back to §3.1.

Rules 3, 4 and 5 exist because the naive version of this product is both
counterproductive and legally exposed. The reasoning is in
`DATA_SOURCES_AND_COMPLIANCE.md`; it is not re-litigated per merge request.

---

## 3. The change pipeline

Every non-trivial change moves through six stages. The stages exist so that the
expensive disagreements — what are we building, and how will we know it works —
happen before code, when they cost minutes rather than after, when they cost a
rewrite.

```
Specify ──▶ Plan ──▶ Tasks ──▶ Implement ──▶ Verify ──▶ Merge Request ──▶ [HUMAN merges]
   │          │                                  │            │
   ▼          ▼                                  ▼            ▼
[HUMAN     [HUMAN                          run-checks.sh   [HUMAN
 approves   signs off                       + eval          reviews]
 intent]    design]
```

Specs live in `specs/<NNN>-<slug>/`, one directory per change, seeded from
`specs/templates/`.

### 3.1 Specify — `requirements.md`

States the problem, the requirements and — the part that actually matters — the
**acceptance criteria as executable statements**. A criterion that cannot be
turned into a test is not a criterion; it is a hope.

Requirements are written in EARS form so that the trigger and the response are
separable:

```
WHEN a source returns HTTP 500 on page 2 of a paginated fetch,
THE SYSTEM SHALL record source_results[i].status = "error", yield no partial
postings from that source, and complete the run.

WHILE a discovery run holds the Redis run lock,
THE SYSTEM SHALL reject POST /api/v1/runs/discovery with 409 run.already_running.

IF a generated artifact fails ledger validation twice,
THEN THE SYSTEM SHALL set review_item.status = 'needs_manual_review', retain both
failed artifacts, and increment run_log.stats.validation_failures.
```

The spec also names the **invariants it touches**. If the answer is "none",
write "none" explicitly — that sentence is what makes a reviewer notice when it
is wrong.

**Human gate.** The operator approves intent before design starts. An agent does
not self-approve a spec.

### 3.2 Plan — `design.md`

Architecture for this change: module boundaries, the data flow, new or changed
contracts, the migration, the configuration keys, the rollback story.

Two sections are mandatory and are the ones reviewers read first:

- **Criterion-by-criterion coverage.** A table mapping every acceptance
  criterion from `requirements.md` to the specific test that will prove it. An
  uncovered criterion is a design defect, not a testing detail.
- **Invariant impact.** For each invariant in `ARCHITECTURE.md` §3, state
  unaffected / affected-and-preserved-by-X. "Affected" without a named
  enforcement mechanism does not pass.

**Human gate.** The operator signs off the design. Changes to the schema, the
adapter protocol, the prompt registry, the validation pass or any invariant
require a re-read of the affected canonical document before sign-off.

### 3.3 Tasks — `tasks.md`

Atomic, independently shippable units. Each has:

- a one-line statement of what it changes,
- the files it is expected to touch,
- its acceptance check, written as the command that proves it,
- its dependencies on other tasks.

```markdown
### T-004 — Workday config model rejects non-Workday hosts

Touches: backend/src/scout_careers/sources/workday.py,
         backend/tests/sources/test_workday_config.py

Check:  pytest backend/tests/sources/test_workday_config.py -q
Proves: requirements.md AC-3 (a pasted host outside *.myworkdayjobs.com is
        rejected at config parse time, not at fetch time)
Depends on: T-002
```

A task that cannot be described in one line is two tasks.

### 3.4 Implement

One task per branch. Code **and** its tests in the same commit — a commit that
adds behaviour without the test that proves it is incomplete work, not a
checkpoint.

Rules that apply while implementing:

- Write the test first where the behaviour is a rule (invariants, validation,
  transitions, policy refusals). Write it alongside where the behaviour is a
  mapping (adapter field mapping, normalisation).
- Do not widen scope. A defect found outside the task's scope is written down
  and filed, not fixed opportunistically in a branch a reviewer is reading for
  something else.
- Do not leave a `TODO` without an owner and a spec reference. `# TODO` alone is
  an unfinished thought committed to `main`.

### 3.5 Verify

Two halves, both required.

**Mechanical.** The full gate, locally, before the merge request exists:

```bash
bash ci/run-checks.sh all
```

Plus the eval, for anything touching prompts, the skill vocabulary, the scoring
formula or the generation path:

```bash
cd backend && python -m scout_careers.eval run \
    --family requirement_extraction \
    --prompt-version <new> --baseline <current>
```

**Judgement.** An independent read against the spec, answering one question:
*does this meet the acceptance criteria, or does it meet the tests?* Those are
different, and the second is how a system passes its own suite while being
wrong. The reviewer re-reads `requirements.md` before reading the diff, not
after.

### 3.6 Merge request

Description must contain, in this order:

1. **What changed**, in prose, in five lines or fewer.
2. **Spec link** — `specs/<NNN>-<slug>/`.
3. **Acceptance evidence** — the criterion table with pass/fail, and pasted
   output from `ci/run-checks.sh`. For prompt or scoring changes, the eval delta
   table.
4. **Invariant impact** — repeated from `design.md`, updated if implementation
   changed it.
5. **Migration and rollback** — the Alembic revision ID, whether the migration
   is reversible, and what the rollback procedure is if it is not.
6. **Documents updated** — see §12. "None" is an answer only if the change
   contradicted nothing.

### 3.7 What a human owns, and what is genuinely trivial

| Gate | Owner | Can an agent do it? |
|---|---|---|
| Spec approval (intent) | Operator | No |
| Design sign-off | Operator | No |
| Task decomposition | Agent, operator confirms | Yes, with confirmation |
| Implementation | Agent | Yes |
| Running the gate | Automation | Yes |
| Independent spec review | Operator | No |
| **Merge** | **Operator** | **No, ever** |
| Enabling a feature flag in production | Operator | No |
| Editing an invariant, the never-scrape list, or the claims ledger schema | Operator | No |

**Never merge your own work.** An agent proposes; the operator merges. This is
the single-user analogue of peer review, and it is the only gate that cannot be
automated away without removing the point of the gate.

**Trivial changes** — the ones that may skip Specify and Plan and go straight to
a branch — are exactly these: a typo, a comment, a docstring, a log message
wording change, a dependency pin bump with no behaviour change, and a test that
adds coverage for existing behaviour without changing it. Everything else,
including "just adding a filter to an endpoint", goes through the pipeline. The
gate and the review checklist still apply to trivial changes; only the two
document stages are skipped.

---

## 4. Coding standards

**Authority: `DEVELOPMENT.md`.** This section is the summary that carries in an
agent's working memory. Where the two differ, `DEVELOPMENT.md` is right.

### 4.1 Python

- **Python 3.12.** Type-hint every public signature. `mypy` runs strict-ish and
  its configuration is not loosened to make a change pass; the change is fixed.
- **`ruff` formats and lints.** Not `black`, not `flake8`, not both. One tool,
  one configuration, in `pyproject.toml`.
- **Pydantic v2 at every boundary.** Request bodies, adapter configs, LLM
  structured output, settings. `extra="forbid"` on anything parsed from an
  external shape, so vendor drift is a `schema_error` rather than a silently
  empty result.
- **No magic constants.** Every threshold, interval, cap, weight and model ID is
  a key on `Settings`, documented in `CONFIGURATION.md`. A literal `5` in a
  module is a defect even when it is the right number.
- **Layering is enforced, not encouraged.** Routers are thin, services are
  thick. No business logic in `api/`. No HTTP concerns below `api/`. `sources/`
  never imports from `db/` — it returns DTOs and `ingest/` persists them. An
  import-graph test asserts these edges.
- **Structured logging only** (`structlog`), one line per pipeline stage per
  run, correlated by `run_id`. No `print`. No bare `logging.info` with an
  f-string carrying content.
- **Errors fail closed on integrity and compliance, open on enrichment.** A
  scoring failure downgrades an item to `needs_manual_review`; it never ships an
  unscored draft. An adapter failure degrades that source only; the run
  completes.
- **Async all the way down** for I/O. No sync database calls inside a request or
  a pipeline stage.

### 4.2 TypeScript

- **Strict mode on.** No `any` without a comment naming why and what would
  remove it.
- **The API client is generated from the OpenAPI schema.** A hand-written
  request or response type in `frontend/src/api/` is a review failure
  (`API.md` §preamble). Regenerate; do not retype.
- **No `console.log` in committed code.** Use the app logger.
- **No hardcoded colours, spacings or strings** that belong in the token set or
  the copy layer.

### 4.3 Both

- Names say what the thing is, not what pattern it implements. `SourceAdapter`,
  not `AbstractSourceAdapterFactory`.
- Comments explain *why*, never *what*. A comment restating the line above it is
  deleted on review.
- Dead code is deleted, not commented out. Git remembers.

---

## 5. API design rules

**Authority: `API.md`.**

1. **Every route is under `/api/v1/`.** The version is present from day one so
   that a breaking change never has to be argued about.
2. **Every response uses the envelope**, success or failure:

   ```jsonc
   {
     "data": { },          // or [ ], or null on error
     "message": "string",  // human-readable, safe to display
     "meta": { }           // optional: pagination, timing, counts, error code
   }
   ```

   A route returning a bare object, a bare list, or FastAPI's default validation
   shape unwrapped, is a review failure. The envelope is applied by a response
   model and an exception handler, not by each route remembering.

3. **Status codes are correct, not convenient.** 201 for created, 202 for a
   queued long-running job, 204 for delete, 409 for conflict, 422 for validation,
   429 for the local rate limit, 502 for an upstream ATS or LLM failure. A 200
   carrying `{"error": ...}` is not an error response.
4. **Errors carry a stable machine code** in `meta.code`, in the form
   `<domain>.<condition>` — `company.duplicate_source`,
   `source.denied_by_policy`, `review.already_decided`, `run.already_running`.
   The code is part of the contract; the message is not.
5. **Cursor pagination on every list endpoint.** `?limit=&cursor=`,
   `meta.next_cursor` null on the last page. Offset pagination is not offered
   and is not added later "just for this one".
6. **OpenAPI is current before merge.** `/api/v1/openapi.json` regenerates in the
   gate and the frontend client regenerates from it. A merge request whose
   generated schema differs from the committed one fails the gate.
7. **`Idempotency-Key` on the two expensive POSTs** — `/runs/discovery` and
   `/review/{id}/generate`. A repeated key inside 24 hours returns the original
   response rather than starting a second run.
8. **Absence is a feature.** No endpoint submits an application, sends mail to a
   non-operator address, fetches a denied host, or overrides a failed ledger
   validation. Adding one of these is not a feature request; it is a violation of
   §2 rules 3, 4 and 5.

---

## 6. Database rules

**Authority: `DATA_MODEL.md`.**

1. **Parameterised statements only.** SQLAlchemy constructs or bound parameters.
   No f-string, `%`-format or `+` concatenation reaches a cursor, ever — job
   descriptions and email bodies are attacker-controllable text
   (`ARCHITECTURE.md` §2). A dynamic identifier, if one is ever genuinely
   needed, goes through a server-side quoting helper and an allow-list, never
   through interpolation.
2. **One Alembic revision per schema change, ID ≤ 32 characters.** The revision
   ships in the same merge request as the model change. A model change without a
   migration fails the gate.
3. **Enum additions get their own revision.** `ALTER TYPE ... ADD VALUE` cannot
   run in a transaction block alongside other DDL.
4. **Migrations are forward-only in operation.** Downgrades are written and
   exercised in development, never relied on in production; the rollback story
   for a destructive migration is a restore, and the merge request says so.
5. **Provenance is never orphaned.** Any migration touching `claim` or
   `artifact` preserves `claim_usage` integrity. A migration that would drop a
   `claim_usage` row without dropping its artifact is rejected.
6. **Rules that must survive a refactor live in the database.** The artifact
   validation guards are triggers (`CLAIMS_LEDGER.md` §6.1) precisely because the
   service layer is the layer most likely to be rewritten. New rules of that
   class — where a bad row must be unreachable rather than merely unwritten — get
   the same treatment.
7. **Seed data is not a migration.** The six resume variants, the initial claims
   ledger and the company seed ship as idempotent seed scripts.
8. **Types are deliberate.** `TIMESTAMPTZ` stored UTC and named `*_at`;
   `NUMERIC` for money and percentages, never float; JSONB only for genuinely
   schemaless payloads and never to avoid designing a table.

---

## 7. Security invariants

**Authority: `SECURITY_ARCHITECTURE.md`.** Summary:

| Invariant | Mechanism |
|---|---|
| Secrets never in code, git or logs | Environment or secret store; a `structlog` processor redacts by key name and by pattern (`AKIA`-prefixed, `Bearer`, long base64) as a second line |
| Untrusted text is never instruction | JD text, email bodies and alert HTML are nonce-fenced and declared as data in every prompt (`AI_ARCHITECTURE.md` §7). The output schema is the containment; the ledger validation is the backstop |
| Untrusted text never constructs SQL | Parameterised statements only (§6.1) |
| Untrusted text is never executed | No `eval`, no `exec`, no shelling out with interpolated content, no HTML rendering of a JD without sanitisation |
| Outbound hosts are allow-listed and deny-listed | `assert_fetch_allowed` on every request after redirect resolution; robots.txt honoured per host per run |
| Minimum OAuth scope | Exactly `{gmail.readonly, gmail.send}`. `gmail.modify` and `gmail.compose` are asserted absent by test |
| Email bodies are not retained | Classification runs in memory; only class, confidence and a short excerpt persist (`DATA_MODEL.md` §8.2) |
| Single-user auth is a decision, not an omission | Local session cookie against an environment password; no registration, no reset, no multi-user model — reasoning in `SECURITY_ARCHITECTURE.md` §4 |
| Dependencies are pinned and audited | Lockfiles committed; `pip-audit` and `npm audit` run in the gate |

Any change that touches authentication, the outbound HTTP client, prompt
construction, OAuth scopes or logging redaction is a security-relevant change and
says so in its merge request, whether or not it looks like one.

---

## 8. Performance budgets

| Budget | Target | Measured by |
|---|---|---|
| Discovery run, end to end | **< 15 minutes** wall clock, ~320 sources | `run_log.finished_at - started_at`, asserted in the nightly smoke |
| API read endpoint | **< 500 ms** p95, warm cache, realistic row counts | Timing middleware into `meta.duration_ms`; a load fixture in the integration suite |
| UI first load | **< 1.5 s** to interactive on the operator's machine | Lighthouse run in the frontend gate against the built bundle |
| LLM cost | **< ₹80/day**, ≤ ₹0.80 per posting | `run_log.stats.llm_cost_inr`, circuit-broken at the budget |
| Mail poll | < 2 minutes | `run_log` for `run_type = 'mail'` |

Write endpoints that queue work (202 responses) are exempt from the 500 ms
budget on the queued work, not on the enqueue: `POST /runs/discovery` must
return in under 500 ms even though the run takes minutes.

**The exception rule.** Code that misses a budget does not merge on the promise
of a later fix. It merges only with a **documented exception** in the merge
request stating: the measured number, why the budget cannot be met now, the
user-visible consequence, the conditions under which it must be revisited, and
the operator's explicit approval. The exception is recorded in the spec
directory, not only in the merge request thread, so that it is findable when the
consequence eventually shows up. An undocumented miss is a defect.

---

## 9. Branches and merge requests

**Branches.** Short-lived, one task each, named:

```
feature/<NNN>-<slug>     new behaviour, tied to spec NNN
fix/<NNN>-<slug>         defect repair
chore/<slug>             tooling, dependencies, CI
docs/<slug>              documentation only
```

Delete after merge. A branch that has been open longer than a week is either a
task that was not atomic or work that has stalled; split it or close it.

**`main` is protected.** No direct pushes. No force-push. No self-merge. The gate
must be green. The operator merges.

**Commits.** Imperative subject under 72 characters, body explaining why. The
task ID belongs in the branch name and the merge request, not repeated in every
subject line.

```
Refuse redirects into denied hosts mid-flight

assert_fetch_allowed ran only on the initial URL, so a source that 302s
into a denied host was fetched. The check now runs on every response in
the redirect chain. Adds test_redirect_into_denied_host.

Spec: specs/012-redirect-policy/
```

**Merging.** Squash by default; a merge commit only where the intermediate
history is genuinely useful to a future reader, which is rare.

---

## 10. Prove before flipping

Anything on the extraction, scoring or generation path — the path that produces
text a human will send to an employer — ships behind a flag and is proven before
it becomes default. This is not caution theatre; it is the only way to tell a
change that reads well from a change that works, when the sample size is ten
documents a day.

**The discipline, uniformly:**

1. **Ship the flag off.** The code path exists in production and is unreachable.
   Default `false` for anything new on the generation path
   (`ARCHITECTURE.md` §8).
2. **Prove the eval holds.** The golden set must pass its gates, and no metric
   may regress by more than two points against the baseline even while passing
   (`AI_ARCHITECTURE.md` §10.3). A change that trades four points of recall for
   one of precision clears every absolute gate and is still a bad change.
3. **Enable for a named slice.** `volume`-tier companies, or a fixed list of
   source IDs. **Never a percentage** — at ten generations a day a percentage
   rollout is noise, and a named slice is reproducible.
4. **Run for two weeks.** Compare validation pass rate, repair-retry rate, cost
   per item, operator acceptance rate, and — where there is enough data —
   response rate from `v_funnel`.
5. **Promote only if the eval holds and the slice metrics did not regress.**
   Promotion is a settings change made by the operator, recorded in
   `RELEASE_NOTES.md`.

**Rollback is a settings change, not a deploy.** Every flagged path is written so
that the off state is a complete, correct behaviour rather than a degraded one —
`FF_COVERAGE_JUDGEMENT` off means deterministic coverage only, which understates
and is safe; `FF_TAILORING_REPHRASE` off means `apply_plan` ignores `rephrase`
ops, and plans written while it was on still apply their other operations.

**Model IDs are treated as prompt changes.** Eval first, then a pinned bump in
configuration. There is no auto-upgrade path, because an artifact whose model
cannot be named violates invariant 7.

---

## 11. Review checklist

Run top to bottom. Any "no" blocks the merge.

**Correctness against the spec**

- [ ] Every acceptance criterion in `requirements.md` has a test, and it passes.
- [ ] The tests prove the criteria, not merely the implementation's own shape.
- [ ] Behaviour outside the spec's scope is unchanged.

**Invariants**

- [ ] No new path submits, posts to an employer, or drives a browser to a form.
- [ ] No new call site of the Gmail send method; recipient is still structurally
      fixed to the operator address.
- [ ] Every constructed outbound URL passes `assert_fetch_allowed`; the deny list
      remains unreachable from `Settings`.
- [ ] Generation still cannot attach an artifact with
      `validation_status = 'failed'`; no bypass was introduced at any layer.
- [ ] One failing adapter still leaves the run completing and reporting.
- [ ] Every generated artifact still records model, prompt version, variant and
      claim IDs.

**Contracts**

- [ ] Envelope, status codes, `/api/v1/` prefix, `meta.code` on errors.
- [ ] OpenAPI regenerated and committed; frontend client regenerated from it.
- [ ] Alembic revision present, ID ≤ 32 chars, downgrade written.

**Security**

- [ ] No secret in the diff, the fixtures, the compose file or a docstring.
- [ ] No untrusted text reaching SQL, a shell, `eval`, or an unfenced prompt.
- [ ] Nothing new logged from the never-logged list (`AI_ARCHITECTURE.md` §11.2).

**Quality**

- [ ] `bash ci/run-checks.sh all` green, output pasted.
- [ ] Eval run and delta table attached, if prompts, vocabulary, formula or
      generation changed.
- [ ] Performance budgets met, or a documented exception with operator approval.
- [ ] No `TODO` without an owner and a spec reference; no commented-out code; no
      skipped test without a reason and a ticket.

**Documents**

- [ ] Any document this change contradicts is updated **in this merge request**
      (§12).

---

## 12. Document maintenance

**A change that contradicts a document updates that document in the same merge
request.** Not in a follow-up, not in a documentation sprint, not "when it
settles".

The rule is absolute because the alternative has a predictable end state: a
`docs/` directory that describes a system that no longer exists, which is worse
than no documentation, because it is confidently wrong and agents read it as
authoritative. These documents are the specification an agent loads before
writing code. A stale specification produces stale code, and the error
compounds silently.

Practically:

- Changing a table, column, constraint or index → `DATA_MODEL.md`, same MR.
- Changing an endpoint, envelope field, status code or error code → `API.md`,
  same MR.
- Changing the adapter protocol, adding an adapter, or changing rate-limit or
  robots behaviour → `SOURCE_ADAPTERS.md`, same MR.
- Changing a prompt family, a routing decision, a flag or a cost assumption →
  `AI_ARCHITECTURE.md`, same MR.
- Changing detection, resolution, confidentiality or enforcement in the ledger →
  `CLAIMS_LEDGER.md`, same MR.
- Changing a `Settings` key, its default or its effect → `CONFIGURATION.md`, same
  MR, and the table in whichever document owns that subsystem.
- Changing the process, the gates or this checklist → this file, same MR.

**Changing an invariant is not a documentation task.** An invariant changes only
by an operator decision recorded in `ARCHITECTURE.md` §3, with the reasoning
written down and the release called out prominently in `RELEASE_NOTES.md`
regardless of the version bump. An agent may not propose an invariant change as
part of a feature; it may only raise it as its own spec, and the answer will
usually be no.

**Where documents disagree**, the authority order is:
`ARCHITECTURE.md` → the subsystem's canonical document (`DATA_MODEL.md`,
`API.md`, `SOURCE_ADAPTERS.md`, `CLAIMS_LEDGER.md`, …) → this SOP →
`DEVELOPMENT.md` → everything else. Discovering a disagreement obliges you to
fix it, not to route around it.

---

## 13. Related documents

| Document | Covers |
|---|---|
| `ARCHITECTURE.md` | The system, the module map, the pipeline, **the invariants** |
| `DEVELOPMENT.md` | Local setup, tooling configuration, the full coding standard |
| `TEST_PLAN.md` | The test strategy, the invariant tests, the eval harness, the gate |
| `RELEASE_NOTES.md` | Versioning, the release process, the changelog |
| `DATA_MODEL.md` | Schema, constraints, migration policy |
| `API.md` | Endpoint contracts, envelope, errors, deliberate absences |
| `SECURITY_ARCHITECTURE.md` | Threat model, controls, secret handling |
| `CLAIMS_LEDGER.md` | The ledger, validation, enforcement, provenance |
| `AI_ARCHITECTURE.md` | Prompts, structured output, routing, cost, eval, flags |
| `SOURCE_ADAPTERS.md` | The adapter protocol, the deny list, failure isolation |
| `DATA_SOURCES_AND_COMPLIANCE.md` | Legal basis per source, the reasoning behind the deny list |
| `CONFIGURATION.md` | Every `Settings` key, its default and its effect |
| `ROADMAP.md` | Phases and what ships in each |
