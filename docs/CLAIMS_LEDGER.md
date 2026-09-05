# CLAIMS LEDGER — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the grounding rule, claim anatomy, confidentiality
tiers, the validation pass and provenance. `ARCHITECTURE.md` wins on system-level
concerns and `DATA_MODEL.md` on schema; this file wins on what may be asserted in
a generated document and how that is enforced.

---

## 1. Purpose and the rule

### 1.1 The rule

> **No factual or numeric claim may appear in a generated document unless it
> resolves to a `claim` row.**

This is invariant 3 from `ARCHITECTURE.md` §3, stated in full. It applies to
every resume bullet, every summary line, every skills-line entry and every
sentence of every cover letter the system produces. It applies to numbers,
currency amounts, percentages, counts, durations, ratings, dates, superlatives
and exclusivity words. Free-form model invention is a build failure, not a
warning.

The enforcement is not a lint pass that someone can waive. An `artifact` with
`validation_status = 'failed'` cannot be attached to a `review_item` or an
`application` — the database refuses it (§6) — and no endpoint exists at any API
version to override a failed validation (`API.md` §8).

### 1.2 Why this is the most important control in the system

Everything else in Scout Careers is recoverable. A bad adapter fetches nothing
and gets reported. A bad score ranks a posting wrongly and the operator skips it.
A wrong classification of a rejection email produces a wrong funnel number that a
manual event corrects.

A fabricated number in a submitted resume is not recoverable. It is discovered by
a hiring manager, in an interview, in front of the person the operator is trying
to work for. The cost is not a wasted application; it is the operator's
credibility, permanently, at that company and anywhere its people move next. A
system that writes documents on someone's behalf and can invent a number is worse
than no system, because it manufactures that risk at a rate of ten drafts a day.

The ledger exists so that the generation stage is structurally incapable of it.
The model does not decide what is true. It decides how to phrase what a verified
row already says.

### 1.3 The PQ-Bot parallel

The operator's Parliamentary Question Bot carries the same rule in its project
constitution, as golden rule 4: never put untrusted web content into a tabled
answer's facts — the web may help with understanding and formatting, but "the
Ministry corpus grounds the *facts*."

Scout Careers is the same principle with a different corpus.

| | PQ-Bot | Scout Careers |
|---|---|---|
| The grounding corpus | 702 curated Ministry documents | The `claim` table |
| What the model may do | Understand the question, structure and format the answer | Understand the job description, structure and phrase the application |
| What the model may **not** do | Introduce a fact the corpus does not support | Introduce a fact the ledger does not support |
| Untrusted input | Web results, the PQ text itself | Job description text, employer email |
| Failure mode being prevented | A wrong figure tabled in Parliament | A wrong figure in a submitted resume |
| Enforcement point | Retrieval grounding + answer validation | The validation pass + a database trigger |

Both systems are built on the same conviction: an LLM is excellent at language
and unreliable at truth, so the architecture must supply the truth and let the
model supply only the language. The claims ledger is that supply.

### 1.4 What the ledger is not

It is not a resume. It is not a portfolio. It is not a store of narrative. It is
a table of **atomic, independently verifiable assertions**, each of which the
operator can defend with evidence if challenged in an interview. The resume
variants (`resume_variant.content`) hold the prose; the ledger holds the facts
the prose is allowed to lean on. A bullet cites claims; a claim never cites a
bullet.

---

## 2. Claim anatomy

The schema is `DATA_MODEL.md` §5.2. This section defines the conventions that
make the columns usable.

### 2.1 `key` — the naming convention

```
<project_slug>.<fact_slug>[_<unit_hint>]
```

- `project_slug` comes from a fixed set, one per body of work:
  `khelo`, `pqbot`, `tasktracker`, `expenditure`, `pmis`, `scout`, `allatone`,
  `eightbyte`, `betterstack`, `personal`, `education`.
- `fact_slug` is lowercase snake_case, a noun phrase, and describes the fact —
  not the resume bullet it happens to serve.
- `unit_hint` is appended **only where two claims about the same fact differ by
  unit**, which is common and is the single most useful naming discipline in the
  table: `khelo.cost_reduction_pct` (60%) and
  `khelo.cost_reduction_run_rate_inr` (₹9.4L → ₹3.5L) are the same reduction
  expressed two ways, at two different confidentiality tiers. §3.3 explains why
  that pairing matters.

Keys are stable and referenced from `resume_variant.content` bullets, from
`match_score.evidence`, from generation prompts and from this document. Renaming
a key is a migration, not an edit.

### 2.2 `statement` — the canonical phrasing

One sentence, the operator's own words, phrased so it can be quoted directly.
Present tense for facts that are still true; past tense for completed work. No
first person — the statement is a fact, not a bullet, and the generator adapts
voice. No adjectives that are not themselves verifiable ("successfully",
"significantly", "seamlessly" are all banned; "sole" and "first" are permitted
only when they are the fact being claimed and the evidence supports them).

The statement is also load-bearing for validation: a **subordinate figure inside
the statement is resolvable by any text that cites the claim.** `pmis.scope_document`
carries `metric_value = 12000`, `metric_unit = words`, and a statement naming 14
sections and 24 tables. A draft citing that claim may assert "14 sections" and
"24 tables" without three separate rows. The primary metric goes in the columns;
genuinely subordinate figures live in the statement. A figure that could be
asserted on its own — the 60% and the ₹9.4L → ₹3.5L — gets its own row.

### 2.3 `metric_value` and `metric_unit`

Stored separately and both as `TEXT`, so that:

- the validator can normalise a span ("₹9.4L", "9.4 lakh", "INR 940,000") to a
  `(value, unit)` pair and compare on the pair, not the string;
- a range is representable — `metric_value = '9.4→3.5'` with
  `metric_unit = 'lakh_inr_per_month'` is one claim about one reduction, not two;
- a non-numeric claim leaves both `NULL` and resolves only by explicit citation
  (§5.4).

The unit vocabulary is closed and lives in `ledger/units.py`:

```
percent · lakh_inr_per_month · crore_inr · inr_per_query · loc · documents ·
chunks · questions · languages · tests · modules · cases · runs ·
score_out_of_4 · rest_endpoints · postgres_tables · alembic_migrations ·
config_settings · prisma_models · server_actions · roles · regional_centres ·
person_months · words · pages · sections · tables · adrs · use_cases ·
features · building_blocks · benchmark_programmes · phases · decisions ·
workflows · permission_checks · hours_per_day · minutes · events_per_second ·
milliseconds · endpoints · jobs_per_minute · concurrent_workers ·
daily_users · chats_per_hour · rating · percentile · placements ·
critical_findings · findings
```

A claim proposing a unit outside this list is rejected at `POST /claims`. Units
grow by commit, not by typing.

### 2.4 `project`, `evidence_ref`, `tags`

**`project`** is the human-readable body of work — "Khelo India Assistant",
"MYAS/SAI Parliamentary Question Bot". It is what the generator uses to decide
which claims are even candidates for a given resume block, and it is the
grouping in `GET /claims?project=…`.

**`evidence_ref`** is where the fact was verified from, specific enough to be
re-checked a year later. Not "the repo" — `repo:pq-panel@a41f9c2 · alembic
history · 33 revisions` or `run-log:khelo-eval-2026-08-28 · 213 cases · mean 3.9`
or `aws:cost-explorer 2026-07 vs 2026-02, ap-south-1`. This is the field that
makes re-verification (§9.2) a five-minute job instead of an afternoon.

**`tags`** drives selection, not truth. Conventional tags:

- capability — `ai`, `rag`, `llm-eval`, `backend`, `infra`, `devops`, `data`,
  `security`, `accessibility`, `product`, `consulting`, `frontend`
- character — `scale`, `cost`, `reliability`, `governance`, `delivery`,
  `ownership`, `competitive`
- audience — `govtech`, `enterprise`, `startup`

The generator filters candidate claims by the requirement's skill family and by
the target company's tags before it writes anything, which keeps a firmware role
from being offered a Bhashini language count.

### 2.5 `verified_at`, `expires_at`, `deleted_at`

`verified_at` is when a human last confirmed the fact against `evidence_ref`. It
is never set by an automated process. `expires_at` is covered in §4.
`deleted_at` is a soft delete — a deprecated claim stays in the table forever,
because `claim_usage` rows referencing it must keep resolving (§7).

### 2.6 Three exemplars in full

```yaml
# seeds/claims.yaml — excerpt
- key: khelo.cost_reduction_pct
  statement: >-
    The Khelo India Assistant's monthly run-rate was reduced by about 60 percent
    after a serving-path and model-routing rebuild.
  metric_value: "60"
  metric_unit: percent
  project: Khelo India Assistant
  evidence_ref: "aws:cost-explorer ap-south-1, 2026-02 baseline vs 2026-07 actual"
  confidentiality: public
  tags: [ai, cost, infra, govtech]
  verified_at: 2026-08-31
  expires_at: null

- key: khelo.cost_reduction_run_rate_inr
  statement: >-
    Monthly run-rate for the Khelo India Assistant fell from ₹9.4 lakh to
    ₹3.5 lakh.
  metric_value: "9.4→3.5"
  metric_unit: lakh_inr_per_month
  project: Khelo India Assistant
  evidence_ref: "aws:cost-explorer ap-south-1, 2026-02 baseline vs 2026-07 actual"
  confidentiality: restricted        # absolute figures, allow-list gated
  tags: [ai, cost, infra, govtech]
  verified_at: 2026-08-31
  expires_at: 2027-03-04

- key: pqbot.sole_engineer
  statement: >-
    Sole engineer on the MYAS/SAI Parliamentary Question Bot, from architecture
    through delivery.
  metric_value: null
  metric_unit: null
  project: MYAS/SAI Parliamentary Question Bot
  evidence_ref: "openforge:pq-panel · git shortlog -sn · single author across 33 migrations"
  confidentiality: public
  tags: [ownership, backend, ai, govtech]
  verified_at: 2026-09-01
  expires_at: null
```

The third is a **superlative claim**. "Sole" is exactly the class of word §5.2
detects and refuses unless it resolves. It resolves here because a row asserts it
and an evidence reference supports it.

---

## 3. The confidentiality model

`claim_confidentiality` is `public | internal | restricted` (`DATA_MODEL.md` §2).

### 3.1 The three tiers

| Tier | May appear in | Reasoning |
|---|---|---|
| `public` | Anything the system generates, including a portfolio export or a shared link | The operator would say this on a public profile |
| `internal` | Documents addressed to a specific employer as part of an application | Fine to state in an application; not something to publish. Implementation detail, internal counts, budget figures under the operator's stewardship |
| `restricted` | Only when the target employer is on an explicit disclosure allow-list, or the operator grants disclosure on that one review item | Commercially or contractually sensitive absolute figures |

The tiers gate **emission**, never storage. Every claim is always present in the
ledger, always visible in the Claims UI, always available to the operator. What
changes is whether the generator is permitted to put it into an outgoing
document.

### 3.2 Per-application gating, not per-file stripping

The naive approach — maintain a "safe" resume file with the sensitive numbers
removed — fails in the way it always fails: two files drift, the wrong one gets
attached, and the operator loses the ability to use a strong number where it is
perfectly appropriate.

Instead the gate is evaluated **at generation time, per artifact, against the
target company**:

```python
# ledger/disclosure.py
def permitted_tiers(company, review_item) -> set[str]:
    tiers = {"public", "internal"}
    if company.slug in settings.ledger_restricted_disclosure_companies:
        tiers.add("restricted")
    if review_item and review_item.tailoring_plan.get("disclose_restricted") is True:
        tiers.add("restricted")
    return tiers


def candidate_claims(session, company, review_item, **filters):
    return select_claims(
        session,
        confidentiality_in=permitted_tiers(company, review_item),
        expired=False,
        **filters,
    )
```

Two independent grants:

1. **A standing allow-list.** `LEDGER_RESTRICTED_DISCLOSURE_COMPANIES` is a list
   of company slugs on the `Settings` object, edited through
   `PATCH /api/v1/settings`. It holds employers where the operator has decided
   absolute figures are appropriate — a consulting firm where cost reduction is
   the pitch, a company already under an NDA conversation.
2. **A per-item grant.** `PATCH /api/v1/review/{id}/plan` accepts
   `disclose_restricted: true` in the tailoring plan (`API.md` §5). This is a
   deliberate, recorded human decision for one application, and the rationale
   goes in `review_item.decision_note`.

Neither grant is a bypass of the ledger. A restricted claim is still a verified
claim; disclosure controls **audience**, validation controls **truth**. The two
mechanisms are orthogonal and a document must pass both.

### 3.3 Paired claims — the mechanism that makes gating painless

Claims 12 and 13 are the same fact at two tiers:

| id | key | Tier | What a bullet can say |
|---|---|---|---|
| 12 | `khelo.cost_reduction_pct` | `public` | "cut monthly run-rate by about 60%" |
| 13 | `khelo.cost_reduction_run_rate_inr` | `restricted` | "cut monthly run-rate from ₹9.4L to ₹3.5L" |

When `restricted` is not permitted, the generator does not delete the
achievement — it resolves the claim's **public sibling** through
`ledger/pairs.yaml` and phrases the bullet with the percentage instead. The
achievement survives; only the absolute figure is withheld.

```yaml
# ledger/pairs.yaml
- restricted: khelo.cost_reduction_run_rate_inr
  public_fallback: khelo.cost_reduction_pct
- restricted: khelo.cost_per_query_inr
  public_fallback: khelo.cost_reduction_pct
```

A restricted claim with no declared fallback is simply omitted, and the omission
is recorded in `artifact.validation_notes` as an informational entry so the
operator can see what the letter did not get to say.

This is the answer to "how does the operator control whether ₹9.4L → ₹3.5L
appears on a given application": it is one boolean on one review item, or one
slug on one settings list, and the fallback means saying no costs nothing.

---

## 4. Expiry and decay

### 4.1 Facts are true on a date

"532 offline tests across 40 modules" was true on 31 August 2026. It was false a
week later, because the operator wrote more tests. That is the good direction, but
"3.9 out of 4 on a 213-case eval bank" can move the other way when the bank grows,
and a resume that asserts a stale number is asserting a false one.

`expires_at` encodes this. Claims fall into two classes:

| Class | `expires_at` | Examples |
|---|---|---|
| **Volatile** — a count of a living system, a rating, a cost | `verified_at + LEDGER_DEFAULT_TTL_DAYS` (180) | corpus sizes, test counts, LOC, endpoint counts, migration counts, cost figures, Codeforces rating |
| **Frozen** — a completed event or a closed body of work | `NULL` | a 2026 pilot happened; a hackathon placement; a degree; a delivered document's section count; work at a former employer |

The distinction is *whether the underlying thing can still change*, not whether
the fact is old. Work at a company the operator has left is frozen the day they
leave.

### 4.2 What happens when an expired claim is cited

Resolution succeeds, and the assertion is marked stale:

```jsonc
{ "span": "532 offline tests", "resolved": true, "claim_id": 11,
  "claim_key": "khelo.offline_tests", "stale": true,
  "expired_at": "2027-03-04T00:00:00Z",
  "note": "Claim expired 12 days ago. Re-verify or remove." }
```

Behaviour is governed by `LEDGER_EXPIRED_CLAIM_POLICY`:

| Policy | Effect |
|---|---|
| `fail` (**default**) | The artifact fails validation exactly as an unresolved assertion would. The number does not go out |
| `warn` | Validation passes; the stale assertions appear in `artifact.validation_notes` and are shown in the review queue before approval |

The default is `fail`, because the failure mode of `warn` is that the operator
approves quickly and ships a stale number, which is the thing the ledger exists
to prevent.

Expiry also reaches into scoring: a `met` requirement whose only evidence bullet
cites an expired claim degrades to `partial` (`MATCH_SCORING.md` §4.3, §13.2). A
neglected ledger costs the operator score, which is the right incentive.

### 4.3 Staying ahead of it

- `GET /api/v1/claims?expired=true` lists what has lapsed.
- The 08:15 IST digest (`ARCHITECTURE.md` §6, stage ⑪) carries an **"expiring
  within 30 days"** section listing key, current value and `evidence_ref`. Since
  `evidence_ref` names exactly where to look, re-verification is usually a
  command and a glance.
- The nightly export includes an `expired` sheet.

Nothing auto-renews. An automatic extension of `expires_at` would convert the
whole mechanism into decoration.

---

## 5. The validation pass

Pipeline stage ⑨ (`ARCHITECTURE.md` §6). Input: generated draft text plus the
`claim_ids` the generator cited for each block. Output: a pass/fail verdict and a
per-assertion resolution list. Exposed as `POST /api/v1/claims/validate`
(`API.md` §4) and called internally before any artifact is written.

### 5.1 The shape of the problem

Two kinds of assertion have to be caught:

1. **Surface-detectable** — anything with digits, a currency symbol, a percent
   sign, an ordinal or a superlative word. Regex finds these completely and
   cheaply, and completeness is what matters: a missed numeric is a shipped
   fabrication.
2. **Prose assertions** — "built end to end", "the first such deployment in the
   Ministry", "owned the architecture". No digits, still a factual claim. These
   need a model.

The pass runs both and unions the results. Regex alone under-detects; an LLM
alone is not reliably exhaustive on the class that matters most. Neither is
optional.

### 5.2 Detection — regex families

```python
# ledger/detect.py
import re

NUMBER = r"\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?"

PATTERNS: dict[str, re.Pattern] = {
    # 60%, ~60 %, 3.9%
    "percentage": re.compile(rf"[~≈]?\s?(?:{NUMBER})\s?(?:%|per\s?cent|percent)\b", re.I),

    # ₹9.4L, ₹3.5 lakh, ₹924 Cr, Rs. 1.35, INR 940000
    "currency": re.compile(
        rf"(?:₹|Rs\.?\s?|INR\s?)\s?(?:{NUMBER})\s?(?:L|Lk|lakh|lakhs|Cr|crore|crores|k|M|bn)?\b", re.I),

    # 9.4L → 3.5L, 15 min to under 3, 19 use cases to 8
    "range": re.compile(
        rf"((?:₹\s?)?{NUMBER}\s?\w*)\s*(?:→|->|–|—|\bto\b|\bdown to\b)\s*((?:₹\s?)?{NUMBER}\s?\w*)", re.I),

    # 206 documents, 11,611 chunks, 95 REST endpoints, 60,000 lines
    "count": re.compile(
        rf"\b(?:{NUMBER})\s?(?:\+|k)?\s+"
        r"(documents?|chunks?|languages?|tests?|modules?|cases?|runs?|questions?|"
        r"endpoints?|tables?|migrations?|settings?|models?|actions?|roles?|"
        r"centres?|centers?|person-months?|words?|pages?|sections?|ADRs?|"
        r"use cases?|features?|building blocks?|programmes?|phases?|decisions?|"
        r"workflows?|permission checks?|teams?|LOC|lines of code|placements?)\b", re.I),

    # sub-300ms, <200 ms, ~4 hrs/day, under 3 minutes, 300+ events/sec
    "rate_or_duration": re.compile(
        rf"(?:sub-?|under\s|<|~|≈|>)?\s?(?:{NUMBER})\s?\+?\s?"
        r"(?:ms|milliseconds?|s|seconds?|min|minutes?|hrs?|hours?|days?)"
        r"(?:\s?/\s?(?:day|hour|min|sec|query|month))?\b", re.I),

    # 1st Place, 2nd Place, top 15%
    "ordinal": re.compile(r"\b(?:1st|2nd|3rd|\d+th|first|second|third)\b\s*(?:place|prize)?", re.I),

    # sole, only, first, never, zero, every, 100%
    "superlative": re.compile(
        r"\b(?:sole|solely|only|first|fastest|largest|biggest|best|highest|lowest|"
        r"unique|unprecedented|never|always|every|all|entire|zero|none)\b", re.I),

    # 3.9/4, 1615 rating, top ~15% globally
    "rating": re.compile(rf"\b(?:{NUMBER})\s?(?:/|out of)\s?(?:{NUMBER})\b|\brating\s+(?:{NUMBER})\b", re.I),
}
```

Spans are collected across all families and de-overlapped, longest span winning,
so "₹9.4L → ₹3.5L" is resolved once as a `range` rather than three times as two
currencies and a range.

### 5.3 Detection — the LLM assertion pass

```python
# ledger/assertions.py
class Assertion(BaseModel):
    span: str = Field(min_length=2, max_length=300)
    kind: Literal["numeric", "currency", "percentage", "count", "duration",
                  "rating", "ordinal", "superlative", "exclusivity",
                  "credential", "prose_fact"]
    subject: str = Field(description="What the assertion is about, in three to eight words.")
    normalised_value: str | None = None
    normalised_unit: str | None = None


class AssertionExtraction(BaseModel):
    assertions: list[Assertion] = Field(max_length=80)
```

The prompt (`validate.v2`) delimits the draft the same way extraction delimits a
job description (`MATCH_SCORING.md` §2.3) — nonce-fenced, declared as data — and
instructs the model to find every statement a reader could challenge with "prove
it", including ones with no digits. It is explicitly told **not** to judge
whether an assertion is true; its only job is to find the assertions.

A post-parse validator requires every `span` to be a verbatim substring of the
draft. A hallucinated span means the extraction is unusable, and validation fails
closed rather than passing a document nobody actually checked.

### 5.4 Resolution

```python
# ledger/validate.py
def resolve(assertion, cited_claims, session) -> Resolution:
    # 1. Non-numeric assertions resolve ONLY by explicit citation.
    if assertion.normalised_value is None:
        for c in cited_claims:
            if statement_supports(c.statement, assertion.span, assertion.subject):
                return Resolution(resolved=True, claim=c, stale=c.is_expired)
        return Resolution(resolved=False,
                          note="Prose assertion is not supported by any cited claim.")

    value, unit = normalise_measure(assertion)   # "₹9.4L" -> ("9.4", "lakh_inr")

    # 2. Exact match on (value, unit) against a cited claim.
    for c in cited_claims:
        if measures_match(c, value, unit, tolerance=Decimal("0")):
            return Resolution(resolved=True, claim=c, stale=c.is_expired)

    # 3. Approximation. "~60%", "about 60%", "roughly 4 hrs" tolerate ±5%.
    if assertion_is_approximate(assertion.span):
        for c in cited_claims:
            if measures_match(c, value, unit, tolerance=Decimal("0.05")):
                return Resolution(resolved=True, claim=c, stale=c.is_expired,
                                  note="Matched within approximation tolerance.")

    # 4. Range spans resolve against a single claim encoding the range.
    if (pair := as_range(assertion)) is not None:
        for c in cited_claims:
            if claim_encodes_range(c, *pair):
                return Resolution(resolved=True, claim=c, stale=c.is_expired)

    # 5. Subordinate figure inside a cited claim's canonical statement (§2.2).
    for c in cited_claims:
        if figure_appears_in_statement(c.statement, value, unit):
            return Resolution(resolved=True, claim=c, stale=c.is_expired,
                              note="Subordinate figure from the claim statement.")

    # 6. Last resort: the whole permitted ledger, not just cited claims.
    #    A hit here means the generator failed to cite; it resolves, and the
    #    missing citation is repaired so claim_usage stays complete.
    if (c := search_ledger(session, value, unit, permitted_tiers)) is not None:
        return Resolution(resolved=True, claim=c, stale=c.is_expired,
                          note="Resolved against the ledger; citation was missing and has been added.")

    return Resolution(resolved=False, note=f"No ledger claim for {value} {unit or ''}".strip() + ".")
```

Rules worth calling out:

- **Superlatives and prose facts never resolve by search.** Step 1 requires an
  explicit citation whose `statement` contains the assertion. "Sole engineer"
  passes only because `pqbot.sole_engineer` was cited; there is no fuzzy path to
  a superlative, because a fuzzy path is how "first" and "only" get attached to
  things that were merely early or merely rare.
- **Exact spans must match exactly.** "60%" against a claim of 60 passes; "62%"
  fails. Tolerance is granted only to spans the operator's own prose marked as
  approximate — `~`, `about`, `roughly`, `approximately`, `over`, `nearly`.
- **Step 6 repairs rather than punishes.** If the number is true and in the
  ledger but the generator forgot to cite it, the document is not rejected; the
  citation is written so provenance (§7) stays complete, and the omission is
  logged so a pattern of them shows up.
- **Confidentiality is checked here too.** A resolved claim outside
  `permitted_tiers` (§3) fails the assertion with
  `note: "Claim is restricted and this employer is not on the disclosure
  allow-list."`

### 5.5 The response shape

Exactly as `API.md` §4 specifies:

```jsonc
// POST /api/v1/claims/validate
// request
{ "text": "cut run-rate ~60% (₹9.4L → ₹3.5L per month) across 14 centres",
  "claim_ids": [12, 13],
  "company_slug": "seagate" }

// 200
{
  "data": {
    "passed": false,
    "assertions": [
      { "span": "~60%", "resolved": true, "claim_id": 12,
        "claim_key": "khelo.cost_reduction_pct" },
      { "span": "₹9.4L → ₹3.5L", "resolved": true, "claim_id": 13 },
      { "span": "14 centres", "resolved": false, "claim_id": null,
        "note": "No ledger claim for centre count." }
    ]
  },
  "message": "1 assertion could not be resolved."
}
```

That example is instructive, and its failure is correct in an interesting way.
Claim 39 (`expenditure.regional_centres`, 14 regional centres) *does* exist — but
it belongs to the Expenditure Tracker, not to the Khelo India Assistant. The
sentence has spliced a true number from one project onto a true achievement from
another. Both halves are verified; the composition is false. Resolution scoped to
cited claims catches exactly this class of error, which is the most likely way an
LLM produces a falsehood out of entirely true inputs.

**`passed: false` blocks artifact attachment. It does not merely warn.**

### 5.6 Failure behaviour

```
validate(draft) → passed
    └─ artifact written, validation_status = 'passed', claim_usage rows inserted

validate(draft) → failed, attempt 1
    └─ artifact written, validation_status = 'failed', validation_notes = assertions
    └─ regenerate once, with the unresolved spans quoted back verbatim and the
       instruction: remove these assertions or replace them with a cited claim.
       The prompt is NOT told to "try harder"; it is given the ledger subset it
       may draw on.

validate(draft) → failed, attempt 2
    └─ second failed artifact retained for diagnosis
    └─ review_item.status = 'needs_manual_review'
    └─ counted in run_log.stats.validation_failures and surfaced in the digest
```

`GENERATION_VALIDATION_RETRIES` defaults to 1. Failed artifacts are kept, never
deleted — a failed artifact plus its `validation_notes` is the primary evidence
for whether a generation prompt is drifting.

Nothing about this path ever produces a document that goes out. The worst case is
an empty queue slot and a line in the digest.

---

## 6. Enforcement

### 6.1 At the database, not only in application code

`DATA_MODEL.md` §8.1 states that a failed artifact can never be attached to a
`review_item` or an `application`, enforced by a trigger rather than by
application code alone. That trigger:

```sql
CREATE OR REPLACE FUNCTION assert_artifact_validated() RETURNS trigger AS $$
DECLARE offending TEXT;
BEGIN
  SELECT string_agg(a.id || ' (' || a.kind || ')', ', ')
    INTO offending
    FROM artifact a
   WHERE a.id IN (NEW.resume_artifact_id, NEW.cover_letter_artifact_id)
     AND a.validation_status = 'failed';

  IF offending IS NOT NULL THEN
    RAISE EXCEPTION
      'artifact % failed ledger validation and cannot be attached', offending
      USING ERRCODE = '23514', HINT = 'Regenerate. There is no override.';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER review_item_artifact_guard
  BEFORE INSERT OR UPDATE OF resume_artifact_id, cover_letter_artifact_id
  ON review_item
  FOR EACH ROW EXECUTE FUNCTION assert_artifact_validated();

CREATE TRIGGER application_artifact_guard
  BEFORE INSERT OR UPDATE OF resume_artifact_id, cover_letter_artifact_id
  ON application
  FOR EACH ROW EXECUTE FUNCTION assert_artifact_validated();
```

A second trigger closes the back door of flipping a status after attachment:

```sql
CREATE OR REPLACE FUNCTION assert_attached_artifact_not_failed() RETURNS trigger AS $$
BEGIN
  IF NEW.validation_status = 'failed' AND OLD.validation_status <> 'failed' THEN
    IF EXISTS (SELECT 1 FROM review_item r
                WHERE NEW.id IN (r.resume_artifact_id, r.cover_letter_artifact_id))
    OR EXISTS (SELECT 1 FROM application a
                WHERE NEW.id IN (a.resume_artifact_id, a.cover_letter_artifact_id)) THEN
      RAISE EXCEPTION 'artifact % is attached; detach before marking it failed', NEW.id
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER artifact_status_guard
  BEFORE UPDATE OF validation_status ON artifact
  FOR EACH ROW EXECUTE FUNCTION assert_attached_artifact_not_failed();
```

The reason this lives in the database is that it must survive the code. A refactor
that forgets a service-layer check, a script run against production, a
half-finished migration, an agent writing a helper that inserts a row directly —
all of them hit the trigger. A rule that only exists in the layer most likely to
be rewritten is not enforced; it is documented.

### 6.2 There is no bypass endpoint

`API.md` §8 lists overriding a failed ledger validation among the things for which
no endpoint exists at any version. That is the second half of the enforcement: the
trigger makes the bad state unreachable from the database, and the absent endpoint
makes it unreachable from the API. Neither is sufficient alone.

### 6.3 The `bypassed` status

`artifact_validation` has a third value, `bypassed` (`DATA_MODEL.md` §2), and it
must not be mistaken for an override.

It marks an artifact that **was never machine-generated**: a document the operator
wrote by hand and rendered through the builder, or a historical file imported when
the system was first seeded. Such a document has no generation-time claim
citations, so validation is not applicable rather than passed.

- `bypassed` is settable only by the seed and import paths, never by any API
  route, and never as a response to a failed validation.
- A `bypassed` artifact may attach — the trigger blocks `failed` only — because
  the operator authored it and is answerable for it.
- The review queue and the Applications page label it explicitly: *hand-authored,
  not ledger-validated.*
- `POST /review/{id}/generate` can never produce `bypassed`. A generated document
  is either `passed` or `failed`.

The distinction that matters: `bypassed` means *a human takes responsibility for
this text*; it never means *the model's text was let through*.

---

## 7. Provenance

### 7.1 `claim_usage`

Every resolved assertion writes a row (`DATA_MODEL.md` §5.3):

```sql
INSERT INTO claim_usage (claim_id, artifact_id, location)
VALUES (12, '01JC7…', 'summary'),
       (21, '01JC7…', 'experience.1.bullet.0'),
       (38, '01JC7…', 'experience.0.bullet.2'),
       (39, '01JC7…', 'experience.0.bullet.2')
ON CONFLICT (claim_id, artifact_id, location) DO NOTHING;
```

`location` is the structural path into the document — `summary`,
`experience.2.bullet.1`, `cover_letter.paragraph.3` — so provenance is per
sentence, not per file.

Rows are written inside the same transaction that sets
`artifact.validation_status = 'passed'`. An artifact cannot exist in a passed
state without its usage rows, and `DATA_MODEL.md` §11 forbids any migration that
orphans them.

### 7.2 Answering "where did this number come from"

```sql
-- Every claim behind one generated document, with its evidence.
SELECT u.location,
       c.id, c.key, c.statement,
       c.metric_value, c.metric_unit,
       c.confidentiality,
       c.evidence_ref,
       c.verified_at,
       c.expires_at,
       (c.expires_at IS NOT NULL AND c.expires_at < a.generated_at) AS was_stale_at_generation
FROM claim_usage u
JOIN claim    c ON c.id = u.claim_id
JOIN artifact a ON a.id = u.artifact_id
WHERE u.artifact_id = :artifact_id
ORDER BY u.location;
```

Exposed as `GET /api/v1/review/{id}` (the evidence block) and, from the other
direction, `GET /api/v1/claims/{id}/usage`:

```sql
-- Every document that has ever asserted this claim, and what became of it.
SELECT a.id AS artifact_id, a.kind, a.generated_at,
       p.title, co.name AS company, u.location,
       app.status AS application_status, app.submitted_at
FROM claim_usage u
JOIN artifact a       ON a.id = u.artifact_id
LEFT JOIN job_posting p  ON p.id = a.posting_id
LEFT JOIN company co     ON co.id = p.company_id
LEFT JOIN application app ON app.id IN (
       SELECT id FROM application
        WHERE resume_artifact_id = a.id OR cover_letter_artifact_id = a.id)
WHERE u.claim_id = :claim_id
ORDER BY a.generated_at DESC;
```

This is the query that runs when a number turns out to be wrong. It answers, in
one shot: which applications went out asserting it, to which employers, and
whether any of them are still live. Without it, discovering an error means
guessing which of ninety submitted documents contained it.

It is also the interview-preparation query. Before a call, the operator pulls the
claims behind the exact document that employer received — including the
`evidence_ref` for each — and is prepared to defend precisely what was sent, not
what a different variant said.

### 7.3 Reproducibility

Invariant 7 (`ARCHITECTURE.md` §3) requires every artifact to record model, prompt
version, variant ID and the exact claim IDs used. `artifact` carries the first
three; `claim_usage` carries the fourth. Together they mean a document generated
in March can be explained in November, even after the ledger has moved on —
because deprecated claims are soft-deleted, never removed (§9.3).

---

## 8. The seeded starter ledger

Shipped as `seeds/claims.yaml` and loaded by an idempotent seed script, not a
migration (`DATA_MODEL.md` §11). Insert order is fixed by the file, so IDs are
stable across a rebuild and the IDs referenced from `resume_variant.content`,
`API.md` and `MATCH_SCORING.md` remain correct.

**63 rows.** The principle governing the row count: **one claim per independently
assertable number.** A figure that a resume bullet could state on its own gets a
row; a figure that only ever appears as an attribute of another gets a mention in
the parent's `statement` (§2.2).

Legend: **†** = volatile, `expires_at = verified_at + 180 days`. Unmarked rows
are frozen (`expires_at = NULL`). Tier is `confidentiality`.

### 8.1 Khelo India Assistant

`project = "Khelo India Assistant"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 1 | `khelo.production_live` | The Khelo India Assistant is live in production on kheloindia.gov.in. | — | — | public | ai, rag, govtech, delivery |
| 2 | `khelo.corpus_documents` † | The retrieval corpus holds 206 curated documents. | 206 | documents | public | rag, data |
| 3 | `khelo.corpus_chunks` † | The corpus is indexed as 11,611 retrieval chunks. | 11611 | chunks | public | rag, data |
| 4 | `khelo.languages_supported` | The assistant answers in 12 languages. | 12 | languages | public | ai, accessibility, govtech |
| 5 | `khelo.languages_voice` | Nine of the 12 supported languages are voice-enabled. | 9 | languages | public | ai, accessibility |
| 6 | `khelo.speech_stack` | Speech and translation run on Bhashini, with Sarvam as fallback. | — | — | public | ai, infra, govtech |
| 7 | `khelo.hypothetical_questions` † | 86,000 hypothetical questions were generated to widen retrieval recall. | 86000 | questions | public | rag, ai, data |
| 8 | `khelo.eval_bank_cases` † | The evaluation bank holds 213 cases. | 213 | cases | public | llm-eval |
| 9 | `khelo.eval_runs_archived` † | 28 evaluation runs are archived for comparison. | 28 | runs | public | llm-eval, governance |
| 10 | `khelo.eval_correctness` † | Judged correctness averages 3.9 out of 4 across the archived runs. | 3.9 | score_out_of_4 | public | llm-eval, ai |
| 11 | `khelo.offline_tests` † | 532 offline tests run across 40 modules. | 532 | tests | public | reliability, backend |
| 12 | `khelo.cost_reduction_pct` | Monthly run-rate was reduced by about 60 percent. | 60 | percent | public | cost, infra, ai |
| 13 | `khelo.cost_reduction_run_rate_inr` † | Monthly run-rate fell from ₹9.4 lakh to ₹3.5 lakh. | 9.4→3.5 | lakh_inr_per_month | **restricted** | cost, infra |
| 14 | `khelo.cost_per_query_inr` † | Serving cost settled at about ₹1.35 per query. | 1.35 | inr_per_query | **restricted** | cost, scale |
| 15 | `khelo.capacity_daily_users` † | Capacity is modelled at 10,000 daily users. | 10000 | daily_users | internal | scale, infra |
| 16 | `khelo.capacity_chats_per_hour` † | Capacity is modelled at 20,000 chats per hour. | 20000 | chats_per_hour | internal | scale, infra |

### 8.2 Allatone

`project = "Allatone"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 17 | `allatone.deploy_time_pct` | CI/CD deploy time was cut by 80 percent. | 80 | percent | public | devops, delivery |
| 18 | `allatone.deploy_time_absolute` | Deploys went from 15 minutes to under 3. | 15→3 | minutes | public | devops, delivery |
| 19 | `allatone.rbac_consolidation` | More than 15 scattered permission checks were consolidated into a single JWT RBAC middleware. | 15 | permission_checks | public | backend, security |
| 20 | `allatone.ops_hours_eliminated` | Roughly 4 hours per day of operations overhead was eliminated. | 4 | hours_per_day | public | delivery, data |
| 21 | `allatone.reconciliation_automation` | Three manual reconciliation workflows were automated. | 3 | workflows | public | backend, data, delivery |

### 8.3 MYAS/SAI Parliamentary Question Bot

`project = "MYAS/SAI Parliamentary Question Bot"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 22 | `pqbot.production_pilot` | The PQ-Bot was piloted during the 2026 Parliament monsoon session. | — | — | public | govtech, delivery, ai |
| 23 | `pqbot.corpus_documents` † | The grounding corpus holds 702 Ministry documents. | 702 | documents | public | rag, data, govtech |
| 24 | `pqbot.loc` † | The codebase is approximately 60,000 lines. | 60000 | loc | internal | backend, ownership |
| 25 | `pqbot.endpoints` † | The backend exposes 95 REST endpoints. | 95 | rest_endpoints | public | backend |
| 26 | `pqbot.tables` † | The schema holds 34 Postgres tables. | 34 | postgres_tables | public | backend, data |
| 27 | `pqbot.migrations` † | The schema is managed through 33 Alembic migrations. | 33 | alembic_migrations | internal | backend, governance |
| 28 | `pqbot.config_settings` † | Runtime behaviour is governed by 144 configuration settings, none hardcoded. | 144 | config_settings | internal | backend, governance |
| 29 | `pqbot.backend_tests` † | 312 backend tests run green. | 312 | tests | public | reliability, backend |
| 30 | `pqbot.sast_zero_critical` † | Static analysis was closed at zero critical findings. | 0 | critical_findings | internal | security, governance |
| 31 | `pqbot.sole_engineer` | Sole engineer on the PQ-Bot, from architecture through delivery. | — | — | public | ownership, backend, ai |
| 32 | `pqbot.accessibility_remediation` † | 15 of 23 GIGW/WCAG 2.2 AA pre-audit findings were remediated. | 15/23 | findings | internal | accessibility, governance |

### 8.4 MYAS Task Tracker

`project = "MYAS Task Tracker"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 33 | `tasktracker.replatform_fargate` | The platform was moved from EC2 with PM2 to ECS Fargate on ARM64, RDS Postgres 16, and an ALB fronted by WAF. | — | — | public | infra, devops, govtech |
| 34 | `tasktracker.oidc_cicd` | CI/CD authenticates through GitHub OIDC with no stored AWS keys. | — | — | public | devops, security |
| 35 | `tasktracker.prisma_models` † | The data model spans 26 Prisma models. | 26 | prisma_models | internal | backend, data |
| 36 | `tasktracker.server_actions` † | The application implements 94 server actions. | 94 | server_actions | internal | backend, frontend |
| 37 | `tasktracker.rbac_roles` † | Access control resolves a 9-role hierarchy. | 9 | roles | public | security, product |

### 8.5 Expenditure Tracker

`project = "MYAS Expenditure Tracker"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 38 | `expenditure.budget_tracked_inr` | The tracker covers ₹924 crore of scheme budget. | 924 | crore_inr | internal | data, govtech, scale |
| 39 | `expenditure.regional_centres` | Budget is tracked across 14 regional centres. | 14 | regional_centres | public | data, govtech |
| 40 | `expenditure.variance_analysis` | Variance and utilisation analysis was performed across the tracked scheme budget, in Python and SQL. | — | — | public | data, consulting, govtech |

### 8.6 PMIS AI Scope

`project = "PMIS AI Scope"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 41 | `pmis.scope_document` | Authored a 12,000-word AI Scope Document across 14 sections and 24 tables. | 12000 | words | public | consulting, product, ai |
| 42 | `pmis.feasibility_assessment` | Produced a 14-page feasibility assessment. | 14 | pages | public | consulting, product |
| 43 | `pmis.use_case_consolidation` | Consolidated 19 candidate use cases into 8 features on 6 building blocks. | 19→8 | use_cases | public | consulting, product |
| 44 | `pmis.effort_estimate` | Reconciled a 144 person-month estimate as 134 plus 10. | 144 | person_months | internal | consulting, delivery |
| 45 | `pmis.benchmark_programmes` | Benchmarked 13 international programmes. | 13 | benchmark_programmes | public | consulting, product |
| 46 | `pmis.phase_gate_model` | Structured delivery as five phases behind five data gates. | 5 | phases | public | consulting, governance |
| 47 | `pmis.ministry_escalations` | Escalated ten open decisions to the Ministry rather than assuming them. | 10 | decisions | internal | consulting, governance |

### 8.7 Scout (EY)

`project = "Scout — bid discovery (EY)"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 48 | `scout.tests` | The platform carries 190 tests. | 190 | tests | internal | reliability, backend |
| 49 | `scout.adrs` | 31 architecture decision records were written. | 31 | adrs | internal | governance, backend |
| 50 | `scout.scoring_model` | Built a five-parameter weighted scoring model over the GeM and CPPP procurement portals. | 5 | features | public | data, product, enterprise |

### 8.8 8Byte

`project = "8Byte"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 51 | `eightbyte.event_throughput` | The pipeline sustains more than 300 events per second. | 300 | events_per_second | public | backend, scale |
| 52 | `eightbyte.api_call_reduction_pct` | External API calls were reduced by about 60 percent. | 60 | percent | public | backend, cost |
| 53 | `eightbyte.ws_latency_ms` | WebSocket latency stays under 300 ms. | 300 | milliseconds | public | backend, reliability |

### 8.9 BetterStack

`project = "BetterStack"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 54 | `betterstack.endpoints_monitored` | More than 100 endpoints are monitored. | 100 | endpoints | public | backend, reliability |
| 55 | `betterstack.jobs_per_minute` | The scheduler runs about 500 jobs per minute. | 500 | jobs_per_minute | public | backend, scale |
| 56 | `betterstack.concurrent_workers` | More than 20 concurrent BullMQ workers process the queue. | 20 | concurrent_workers | public | backend, scale |
| 57 | `betterstack.check_latency_ms` | Check latency stays under 200 ms. | 200 | milliseconds | public | backend, reliability |

### 8.10 Personal and education

`project = "Personal"` / `"Education"`

| id | key | statement | value | unit | tier | tags |
|---|---|---|---|---|---|---|
| 58 | `personal.codeforces_rating` † | Codeforces rating of 1615, competing in C++. | 1615 | rating | public | competitive, backend |
| 59 | `personal.codeforces_percentile` † | That rating sits in roughly the top 15 percent globally. | 15 | percentile | public | competitive |
| 60 | `personal.hackathon_placements` | Placed in seven hackathons. | 7 | placements | public | competitive, delivery |
| 61 | `personal.uhi_spectrum_slam` | First place at the UHI Hackathon Spectrum Slam, among more than 200 teams. | 1 | placements | public | competitive |
| 62 | `personal.bvp_hex` | Second place at the BVP-HEX Hackathon. | 2 | placements | public | competitive |
| 63 | `education.btech_ece` | B.Tech in Electronics and Communication Engineering, GGSIPU Delhi, 2021–2025. | — | — | public | credential |

### 8.11 Notes on the seed

- **Claims 12 and 13** are the pairing described in §3.3 and are the reason the
  restricted tier exists. 13 and 14 are the only two restricted rows in the seed.
- **Claims 20 and 21** are what `MATCH_SCORING.md` §4.3 cites as evidence for the
  "automating recurring reports and reconciliations" requirement, and 38–40 are
  what carry the ₹924 Cr variance analysis into the Seagate analyst application.
- **Claim 31** ("sole engineer") is the seed's most-scrutinised row: a superlative
  that resolves only by explicit citation (§5.4, step 1) and that the operator
  must be ready to defend.
- **Claim 59** states "roughly the top 15 percent" and its `metric_unit` is
  `percentile`, not `percent`. A draft asserting "top 15%" resolves; a draft
  asserting "15% faster" does not, because the units differ. Unit separation is
  what makes that discrimination possible.
- **Claims 61 and 62** carry `placements` as a unit for an ordinal position. The
  "more than 200 teams" figure lives in claim 61's statement as a subordinate
  figure (§2.2) rather than as its own row.
- Nothing in this table is aspirational, in progress, or rounded up. If a number
  is not defensible in an interview it does not belong in the ledger, and if it is
  not in the ledger it cannot be in a document.

---

## 9. Ledger maintenance

The ledger is only as good as the discipline around it. Four operations, each
with a rule.

### 9.1 Adding a claim

```http
POST /api/v1/claims
```

```jsonc
{ "key": "khelo.eval_bank_cases",
  "statement": "The evaluation bank holds 213 cases.",
  "metric_value": "213", "metric_unit": "cases",
  "project": "Khelo India Assistant",
  "evidence_ref": "repo:khelo-assistant@7f31ac0 · evals/bank.jsonl · wc -l",
  "confidentiality": "public",
  "tags": ["llm-eval"],
  "verified_at": "2026-08-31T00:00:00Z" }
```

Rejected with 422 when: the key does not match
`^[a-z0-9]+(?:_[a-z0-9]+)*\.[a-z0-9]+(?:_[a-z0-9]+)*$`; the project slug is not
in the fixed set; `metric_unit` is outside the closed vocabulary (§2.3);
`evidence_ref` is shorter than 12 characters or matches a banned placeholder
(`n/a`, `tbd`, `see repo`); `verified_at` is in the future; or `metric_value` is
present without `metric_unit`.

The discipline, which no validator can enforce: **write the claim before writing
the bullet.** A number that arrives because a sentence needed it is the number
most likely to be wrong.

### 9.2 Re-verifying

```http
PATCH /api/v1/claims/{id}
```

Two distinct cases, and conflating them is the mistake to avoid.

**Value unchanged.** Update `verified_at`, recompute `expires_at`, optionally
tighten `evidence_ref`. Nothing else changes; `claim_usage` is untouched; every
document that cited it remains accurate.

**Value changed.** The old claim is not edited, because documents already sent
asserted the old number and their provenance must keep resolving. Instead:

```sql
BEGIN;
  -- 1. retire the old row, freeing the canonical key
  UPDATE claim
     SET key        = key || '@' || to_char(verified_at, 'YYYY-MM-DD'),
         deleted_at = now()
   WHERE id = :old_id;

  -- 2. the new fact takes the canonical key
  INSERT INTO claim (key, statement, metric_value, metric_unit, project,
                     evidence_ref, confidentiality, tags, verified_at, expires_at)
  VALUES ('khelo.offline_tests',
          '604 offline tests run across 44 modules.',
          '604', 'tests', 'Khelo India Assistant',
          'repo:khelo-assistant@c19be4d · pytest --collect-only -q | tail -1',
          'public', ARRAY['reliability','backend'],
          now(), now() + interval '180 days');
COMMIT;
```

`claim_usage` references `claim.id`, not `claim.key`, so a resume sent in March
still explains itself against the March row — with its own `verified_at` and its
own evidence reference — while every new document picks up the current one. **A
changed number is a new claim, not an edit.** This is the same reasoning that
makes `application_event` append-only (`DATA_MODEL.md` §7.3): the log is the
truth, the current value is a projection of it.

Affected postings are rescored, because evidence citing a superseded claim
degrades coverage (`MATCH_SCORING.md` §11.1).

### 9.3 Deprecating

```http
DELETE /api/v1/claims/{id}      → 204, sets deleted_at
```

Soft delete only. A deprecated claim:

- disappears from `GET /claims` unless `?include_deleted=true`;
- is excluded from every generation candidate set;
- still resolves in `claim_usage` and in every provenance query;
- triggers a rescore of postings whose `match_score.evidence` cites it.

Deprecate when a fact stops being *true* (a system was decommissioned, a figure
was found to be wrong) or stops being *the operator's* (work reattributed). Do
not deprecate merely because a claim has gone unused; an unused claim costs
nothing and may be exactly right for the next role.

Hard deletion is available only through a maintenance script, refuses to run when
`claim_usage` rows exist, and exists solely for correcting a seed mistake before
anything has been generated.

### 9.4 The review discipline

What keeps the ledger honest is a habit, not a feature:

| Cadence | Action |
|---|---|
| **On every claim added** | State the evidence reference before the statement. If the reference cannot be written, the claim is not ready |
| **Weekly, in the digest** | Clear the "expiring within 30 days" list. Re-verify or let it lapse — both are acceptable; ignoring it is not |
| **On every validation failure** | Read the unresolved span. Either the ledger is missing a true fact — add it — or the generator invented one — which is a prompt problem worth a version bump. Never resolve it by loosening the check |
| **Monthly** | `GET /claims?expired=true`. Anything expired for more than 60 days is deprecated rather than left to rot |
| **Before every interview** | Run the provenance query (§7.2) for the artifact that employer received. Every claim in it must be defensible that day |
| **Quarterly** | Re-read the whole table as if it were someone else's. Any statement that reads as marketing rather than fact gets rewritten or removed |

The last one is the one that matters most. The ledger's failure mode is not
fabrication — the validator catches that. It is **drift toward flattery**: a
statement that was precise when written and has quietly become a claim it cannot
support. That drift is only detectable by a human reading the table with the
evidence references open, which is why it is a scheduled habit and not a
scheduled job.

---

## 10. Configuration and failure modes

### 10.1 Settings

| Key | Default | Effect |
|---|---|---|
| `LEDGER_DEFAULT_TTL_DAYS` | `180` | `expires_at` for volatile claims |
| `LEDGER_EXPIRED_CLAIM_POLICY` | `fail` | `fail` or `warn` on a stale citation (§4.2) |
| `LEDGER_RESTRICTED_DISCLOSURE_COMPANIES` | `[]` | Company slugs where `restricted` may be emitted |
| `LEDGER_APPROXIMATION_TOLERANCE` | `0.05` | ±5% for spans marked approximate |
| `LEDGER_VALIDATION_PROMPT_VERSION` | `validate.v2` | Assertion-extraction prompt |
| `GENERATION_VALIDATION_RETRIES` | `1` | Regeneration attempts after a failure |
| `LEDGER_EXPIRY_WARNING_DAYS` | `30` | Digest lookahead window |

### 10.2 Failure modes

| Condition | Behaviour |
|---|---|
| An assertion does not resolve | Artifact `failed`, one regeneration attempt, then `needs_manual_review`. Never attached |
| The assertion-extraction call fails or returns an ungrounded span | **Fail closed.** Validation returns `passed: false` with `note: "Assertion extraction unavailable."` A document nobody checked is never treated as checked |
| A cited claim is expired | `stale: true`; blocked under the default `fail` policy (§4.2) |
| A cited claim is restricted and the employer is not allow-listed | Assertion fails on disclosure grounds; the generator retries with the public sibling if `ledger/pairs.yaml` declares one (§3.3) |
| A cited claim has been soft-deleted | Treated as unresolved. Deprecated facts cannot re-enter documents |
| A true number is present but uncited | Resolves at step 6, the citation is written, the omission is logged (§5.4) |
| The regex families disagree with the LLM pass | Union, longest span wins. Over-detection costs a resolution lookup; under-detection ships a fabrication |
| A claim resolves against the wrong project | Not automatically detectable, and the reason resolution is scoped to cited claims first (§5.5). The cross-project splice is the residual risk the operator reads for in review |

The uniform posture: **fail closed on anything touching what goes out.** An empty
queue slot costs the operator nothing. A fabricated number costs them the thing
the whole system exists to build.

---

## 11. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | Invariant 3, invariant 7, pipeline stage ⑨ |
| `DATA_MODEL.md` | `claim`, `claim_usage`, `artifact` schema and the enum values |
| `API.md` | `/claims`, `/claims/validate`, `/claims/{id}/usage`, `/settings` |
| `MATCH_SCORING.md` | Evidence linkage, coverage degradation on expired claims |
| `DOCUMENT_GENERATION.md` | Consumes the ledger; produces the text this pass validates |
| `AI_ARCHITECTURE.md` | Prompt registry and versioning for `validate.vN` |
| `SECURITY_ARCHITECTURE.md` | Untrusted-input handling, secret and PII policy |
