# DOCUMENT GENERATION — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the tailoring model, the `tailoring_plan` schema,
the `.docx` builder contract and cover-letter drafting. `ARCHITECTURE.md` wins on
system-level concerns and invariants; `DATA_MODEL.md` wins on columns and types;
`CLAIMS_LEDGER.md` wins on citation resolution semantics; `AI_ARCHITECTURE.md`
wins on prompts, model routing and token budgets.

---

## 1. Scope and position in the pipeline

This document covers pipeline stages ⑧ GENERATE and ⑨ VALIDATE from
`ARCHITECTURE.md` §6, and the artifact rendering that happens after a human
approves a review item.

```
⑦ RANK ──▶ ⑧ GENERATE ──▶ ⑨ VALIDATE ──▶ ⑩ ENQUEUE ──▶ [HUMAN] ──▶ RENDER
             │                  │                                      │
             │                  └─ POST /claims/validate                │
             │                                                          │
             ├─ tailoring plan  (review_item.tailoring_plan JSONB)      │
             └─ cover letter    (draft text, held until validated)      │
                                                                         ▼
                                                          artifact rows (.docx)
```

Two things are generated per qualifying posting:

| Output | Stored as | Generated when |
|---|---|---|
| Resume tailoring plan | `review_item.tailoring_plan` (JSONB) | Always, for every item that reaches stage ⑧ |
| Cover letter draft | `artifact` of kind `cover_letter` | Only when `company.cover_letter_worth = TRUE` |

Note what is **not** generated at stage ⑧: a `.docx` file. Stage ⑧ produces a
*plan* and a *letter draft*. The resume `.docx` is rendered on demand — when the
operator opens the review item to preview it, or when they approve it. Rendering
before a human has looked at the plan wastes work on the ~40% of queue items that
get skipped, and it makes the plan, not the file, the thing under review. That is
the point of the whole design.

The cover letter is an exception: it is drafted as text at stage ⑧ because its
text is what must pass the ledger validation gate, and because the letter is the
part the operator most needs to read before deciding. It is rendered to `.docx`
at the same time as the resume.

---

## 2. The tailoring model

### 2.1 Six base variants plus a tailoring layer

The system does not write a resume. It owns six curated, human-authored resume
variants (`resume_variant`, seeded per `DATA_MODEL.md` §5.1):

| Key | Aimed at |
|---|---|
| `ai_product` | AI/ML product and applied-AI roles |
| `ai_enterprise` | Enterprise AI delivery, solution-architecture roles |
| `ai_platform` | AI platform / infrastructure engineering |
| `backend` | Backend and distributed-systems engineering |
| `combined` | Generalist; used when a role straddles two variants |
| `consulting` | Consulting, analyst, transformation and finance-adjacent roles |

Each variant is a fixed, verified document. Its content — summary, labelled skill
lines, experience blocks, project blocks, education, achievements — is stored as
structured JSON in `resume_variant.content` and is the same structure the `.docx`
builder consumes (§5.1).

For a specific posting, the system produces a **tailoring layer**: a bounded set
of proposed edits against the winning variant. Reordering. Bullet swaps. Skills
line adjustments. Never a new document.

### 2.2 Why a reviewable diff beats a regenerated document

This is the central design decision in this document, and it is not a matter of
taste.

**A regenerated document cannot be reviewed at the speed the workflow requires.**
The operating budget is ten minutes per day across five to ten items
(`ARCHITECTURE.md` §1.1). Reading a full one-page resume carefully enough to
catch a subtly wrong claim takes two to three minutes. Reading a seven-line diff
takes fifteen seconds. At the intended volume, a regenerated document means the
operator either spends an hour a day or — far more likely — stops reading and
starts trusting. Trusting a generative model with the factual content of a resume
sent to an employer is the failure mode this entire system exists to prevent.

**A diff has a stable base to validate against.** The six variants are verified
once, by a human, and every claim in them is already resolved to the ledger. A
diff only has to justify what it changes. A regenerated document has to justify
every sentence on every generation, which multiplies the validation surface by
roughly forty and multiplies the chance of a false claim slipping through by the
same factor.

**A diff is reversible and inspectable after the fact.** `review_item.tailoring_plan`
is a small JSON object. Six months later, when the funnel view (`DATA_MODEL.md`
§10) says the `consulting` variant converts at 2× the `combined` variant, the
plans are still there and still legible, and the question "what did we actually
change for the roles that converted?" is answerable by querying JSONB. A corpus
of forty regenerated `.docx` files answers nothing.

**A diff bounds the blast radius of a prompt-injection attempt.** A job
description is attacker-controllable text (`ARCHITECTURE.md` §2). A model asked
to *write a resume* from that text can be pushed a long way. A model asked to
*choose which of these 34 existing bullets to promote, and to return only their
IDs* has almost nowhere to go — the output schema itself is the containment. See
`AI_ARCHITECTURE.md` §7.

**A diff keeps voice consistent.** Six variants written by one person read like
one person. Forty generated resumes read like forty slightly different people,
and a recruiter who sees two of them from the same candidate notices.

**A diff makes editing cheap.** The operator edits the plan
(`PATCH /api/v1/review/{id}/plan`), not the document. An edit to a plan is a
one-field change that the system can learn from (§11). An edit to a `.docx` is
invisible to the system entirely.

### 2.3 What the tailoring layer may and may not change

Bounded on purpose. The permitted operation set is closed; the model cannot
propose an operation type that is not in this list, because the output schema has
no field for it.

| Permitted | Forbidden |
|---|---|
| Reorder experience/project blocks | Invent a new experience block |
| Promote an existing bullet from the variant's bullet bank into a visible block | Write a bullet whose factual content is not backed by claim IDs |
| Swap one visible bullet for another from the same block's bank | Change dates, employers, titles or degrees |
| Rewrite a bullet's *phrasing* while preserving its claim set | Add a number, percentage, currency figure or superlative not in the ledger |
| Add or remove a skill token on a labelled skills line, where that token is already in `resume_variant.skill_set` | Add a skill the operator does not have |
| Set the summary to an alternative pre-approved summary, or a rephrasing of it | Change the contact block, or add links |
| Drop the optional projects section for space | Add a section type the builder does not support |

The rule behind the table: **the tailoring layer changes emphasis and ordering,
never facts.** Every fact in the output was already a fact in the base variant or
in the claims ledger.

A phrasing rewrite is permitted but constrained: the rewritten bullet must carry
the same `claim_ids` as the original, and it must pass `POST /claims/validate`.
Rewriting exists so a bullet can be pointed at the requirement it answers ("built
a retrieval pipeline" → "built a retrieval pipeline over 1,615 policy documents,
serving sub-second lookups"), not so it can say something new.

---

## 3. The `tailoring_plan` schema

### 3.1 Full JSON schema

Stored in `review_item.tailoring_plan`. Empty object `{}` means "no tailoring
proposed — render the base variant as-is", which is a legitimate outcome for a
role the variant already answers well.

```jsonc
{
  "schema_version": "1.0",

  // Provenance. Mirrors artifact.model / artifact.prompt_version so a plan can
  // be regenerated identically or diffed against a later prompt version.
  "generated_at":   "2026-09-05T03:12:41Z",
  "model":          "bedrock:anthropic.claude-sonnet-4-20250514-v1:0",
  "prompt_version": "tailoring_plan@2026-09-01.3",
  "variant_id":     6,
  "match_score_id": 8814,

  // ── Operation 1: block ordering ─────────────────────────────────────────
  // Full ordered list of block keys as they should appear. Must be a
  // permutation of the variant's existing block keys — no additions, no
  // omissions. Omit the field entirely to keep the variant's own order.
  "reorder_blocks": ["expenditure_tracker", "khelo_assistant", "pmis", "ey_bid"],
  "reorder_rationale": "Budget-variance and cost-modelling work leads; the role is FP&A-adjacent.",

  // ── Operation 2: bullet operations ──────────────────────────────────────
  // Each op is one of: promote | demote | swap | rephrase
  "bullet_ops": [
    {
      "op": "swap",
      "block": "expenditure_tracker",
      "position": 1,                       // 0-based slot in the visible block
      "from_bullet_id": "exp.b4",          // currently visible; must exist
      "to_bullet_id":   "exp.b9",          // from the block's bullet bank
      "claim_ids": [12, 13],
      "answers_requirement_ids": [40217, 40219],
      "rationale": "Requirement is unit-economics cost modelling; b9 states it directly."
    },
    {
      "op": "promote",
      "block": "expenditure_tracker",
      "position": 0,
      "to_bullet_id": "exp.b2",
      "claim_ids": [9, 10],
      "answers_requirement_ids": [40215],
      "rationale": "Variance analysis against budget is the first hard requirement."
    },
    {
      "op": "rephrase",
      "block": "khelo_assistant",
      "position": 2,
      "bullet_id": "khelo.b3",
      "from_text": "Automated manual reconciliation workflows across the programme.",
      "to_text":   "Automated 3 manual reconciliation workflows, removing ~40 analyst-hours per month.",
      "claim_ids": [21, 22],               // MUST equal the original bullet's claim set
      "answers_requirement_ids": [40222],
      "rationale": "Requirement names reconciliation automation explicitly; quantify it."
    },
    {
      "op": "demote",
      "block": "pmis",
      "position": 2,
      "from_bullet_id": "pmis.b5",
      "rationale": "Frontend delivery detail is irrelevant to this role; frees a line."
    }
  ],

  // ── Operation 3: skills-line edits ──────────────────────────────────────
  // `add` tokens must already exist in resume_variant.skill_set.
  // `remove` tokens must currently be on that line.
  "skills_line_edits": [
    { "line": "Data & Analytics",
      "add":    ["Financial modelling", "Variance analysis"],
      "remove": ["WebSockets"],
      "rationale": "Line is 94 chars after edit; stays on one rendered line." }
  ],

  // ── Operation 4: summary ────────────────────────────────────────────────
  // Either select a pre-approved alternative summary by id, or supply text.
  // Supplied text is validated exactly like a bullet.
  "summary": {
    "op": "rephrase",
    "from_summary_id": "consulting.s1",
    "to_text": "Product and delivery lead who builds the financial instrumentation…",
    "claim_ids": [3, 12],
    "rationale": "Leads with cost/variance framing rather than AI-platform framing."
  },

  // ── Operation 5: section toggles ────────────────────────────────────────
  "sections": {
    "projects": "drop",        // keep | drop
    "achievements": "keep"
  },

  // ── Rendering directives ────────────────────────────────────────────────
  "render": {
    "tight": false,            // set true by the fit loop, not by the model
    "trim_level": 0            // 0..3, set by the fit loop; see §5.4
  },

  // ── Gap surfacing (feeds the cover letter, not the resume) ──────────────
  "surfaced_gaps": [
    { "requirement_id": 40230, "text": "Advanced Excel model building",
      "level": "missing", "nearest_evidence_claim_ids": [12],
      "note": "Adjacent, not equivalent: modelling was done in Python/SQL." }
  ],

  // ── Human edit trail ────────────────────────────────────────────────────
  // Appended by PATCH /review/{id}/plan. Never written by the model.
  "operator_edits": [
    { "at": "2026-09-05T09:41:02Z", "path": "bullet_ops[1]",
      "action": "reject", "note": "b2 is weaker than the bullet it replaces." }
  ]
}
```

### 3.2 Pydantic models

The plan is a Pydantic v2 model shared by the LLM structured-output call, the
API response, and the renderer. There is one definition, not three.

```python
# generate/plan_models.py
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator


class BulletOp(BaseModel):
    op: Literal["promote", "demote", "swap", "rephrase"]
    block: str
    position: Annotated[int, Field(ge=0, le=11)]
    bullet_id: str | None = None        # rephrase
    from_bullet_id: str | None = None   # swap | demote
    to_bullet_id: str | None = None     # swap | promote
    from_text: str | None = None
    to_text: Annotated[str | None, Field(max_length=320)] = None
    claim_ids: list[int] = Field(default_factory=list)
    answers_requirement_ids: list[int] = Field(default_factory=list)
    rationale: Annotated[str, Field(max_length=240)]

    @model_validator(mode="after")
    def _shape_matches_op(self) -> Self:
        required = {
            "promote":  ("to_bullet_id",),
            "demote":   ("from_bullet_id",),
            "swap":     ("from_bullet_id", "to_bullet_id"),
            "rephrase": ("bullet_id", "to_text"),
        }[self.op]
        missing = [f for f in required if getattr(self, f) is None]
        if missing:
            raise ValueError(f"op={self.op} requires {missing}")
        # A bullet that asserts anything must say what backs it.
        if self.op in ("promote", "swap", "rephrase") and not self.claim_ids:
            raise ValueError(f"op={self.op} must carry at least one claim_id")
        return self


class SkillsLineEdit(BaseModel):
    line: str
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    rationale: Annotated[str, Field(max_length=240)]


class SummaryOp(BaseModel):
    op: Literal["keep", "select", "rephrase"]
    from_summary_id: str | None = None
    to_summary_id: str | None = None
    to_text: Annotated[str | None, Field(max_length=520)] = None
    claim_ids: list[int] = Field(default_factory=list)
    rationale: Annotated[str, Field(max_length=240)] = ""


class SurfacedGap(BaseModel):
    requirement_id: int
    text: str
    level: Literal["partial", "missing"]
    nearest_evidence_claim_ids: list[int] = Field(default_factory=list)
    note: Annotated[str, Field(max_length=280)] = ""


class RenderDirectives(BaseModel):
    tight: bool = False
    trim_level: Annotated[int, Field(ge=0, le=3)] = 0


class TailoringPlan(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    model: str
    prompt_version: str
    variant_id: int
    match_score_id: int

    reorder_blocks: list[str] | None = None
    reorder_rationale: str = ""
    bullet_ops: Annotated[list[BulletOp], Field(max_length=12)] = Field(default_factory=list)
    skills_line_edits: Annotated[list[SkillsLineEdit], Field(max_length=4)] = Field(default_factory=list)
    summary: SummaryOp | None = None
    sections: dict[str, Literal["keep", "drop"]] = Field(default_factory=dict)
    render: RenderDirectives = Field(default_factory=RenderDirectives)
    surfaced_gaps: list[SurfacedGap] = Field(default_factory=list)
    operator_edits: list[dict] = Field(default_factory=list)
```

Three further checks run **outside** Pydantic, against the database, in
`generate/plan_validator.py`, because they need the variant and the ledger:

1. `reorder_blocks`, if present, is exactly a permutation of the variant's block
   keys. Anything else is rejected — this is how "invent a new experience block"
   is made structurally impossible.
2. Every `to_bullet_id` / `from_bullet_id` exists in that block's bullet bank in
   `resume_variant.content`. A hallucinated bullet ID fails the plan.
3. For `op = "rephrase"`, `set(claim_ids)` equals the claim set of the original
   bullet. A rephrase that changes the claim set is a rewrite in disguise and is
   rejected.

A plan that fails any of these is not repaired silently. It goes to one
repair-retry (`AI_ARCHITECTURE.md` §6); if it fails again the review item is set
to `needs_manual_review` with the validator's messages in `decision_note`.

### 3.3 Worked example

Posting: *Analyst II, Financial Modeling & AI* — Seagate Technology, Pune.
Winning variant: `consulting` (id 6). `match_score`: coverage 47.5%, hard 4/7,
nice 5/6. This is the item shown in `API.md` §5.

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-09-05T03:12:41Z",
  "model": "bedrock:anthropic.claude-sonnet-4-20250514-v1:0",
  "prompt_version": "tailoring_plan@2026-09-01.3",
  "variant_id": 6,
  "match_score_id": 8814,
  "reorder_blocks": ["expenditure_tracker", "khelo_assistant", "pmis", "ey_bid"],
  "reorder_rationale": "Budget variance and cost modelling are hard requirements 1 and 3; lead with the expenditure work.",
  "bullet_ops": [
    {
      "op": "promote",
      "block": "expenditure_tracker",
      "position": 0,
      "to_bullet_id": "exp.b2",
      "claim_ids": [9, 10],
      "answers_requirement_ids": [40215],
      "rationale": "Hard requirement: budget vs actual variance analysis at scale."
    },
    {
      "op": "swap",
      "block": "expenditure_tracker",
      "position": 1,
      "from_bullet_id": "exp.b4",
      "to_bullet_id": "exp.b9",
      "claim_ids": [12, 13],
      "answers_requirement_ids": [40217, 40219],
      "rationale": "Hard requirement: unit-economics / cost-per-unit modelling."
    },
    {
      "op": "rephrase",
      "block": "khelo_assistant",
      "position": 2,
      "bullet_id": "khelo.b3",
      "from_text": "Automated manual reconciliation workflows across the programme.",
      "to_text": "Automated 3 manual reconciliation workflows, cutting monthly close effort by ~40 analyst-hours.",
      "claim_ids": [21, 22],
      "answers_requirement_ids": [40222],
      "rationale": "Requirement names 'automating reports and reconciliations'; quantify with existing claims."
    },
    {
      "op": "demote",
      "block": "pmis",
      "position": 2,
      "from_bullet_id": "pmis.b5",
      "rationale": "React/UI delivery detail is noise for an analyst role and frees a line for the trim budget."
    }
  ],
  "skills_line_edits": [
    {
      "line": "Data & Analytics",
      "add": ["Financial modelling", "Variance analysis", "Unit economics"],
      "remove": ["WebSockets", "Redis"],
      "rationale": "Line renders at 91 characters after the edit; all three tokens are in skill_set."
    }
  ],
  "summary": {
    "op": "rephrase",
    "from_summary_id": "consulting.s1",
    "to_text": "Delivery and product lead who builds the financial instrumentation behind large public programmes — budget variance analytics across ₹924 Cr and 14 regional centres, unit-economics cost models that cut platform run-rate ~60%, and the AI systems that sit on top of them.",
    "claim_ids": [3, 9, 12],
    "rationale": "Leads on the finance framing the JD asks for, without dropping the AI half the title also asks for."
  },
  "sections": { "projects": "drop", "achievements": "keep" },
  "render": { "tight": false, "trim_level": 0 },
  "surfaced_gaps": [
    { "requirement_id": 40230, "text": "Advanced Excel model building",
      "level": "missing", "nearest_evidence_claim_ids": [12],
      "note": "Modelling was done in Python and SQL, not Excel. Adjacent, not equivalent." },
    { "requirement_id": 40231, "text": "Power BI or similar BI tooling",
      "level": "missing", "nearest_evidence_claim_ids": [11],
      "note": "Dashboards were built in-app; no Power BI or Tableau experience." },
    { "requirement_id": 40233, "text": "SAP / Anaplan / Hyperion",
      "level": "missing", "nearest_evidence_claim_ids": [],
      "note": "No exposure to enterprise FP&A suites." }
  ],
  "operator_edits": []
}
```

Four bullet operations, one skills line, one summary. That is the entire change
set, and it fits on one screen — which is the whole argument of §2.2 made
concrete.

---

## 4. Bullet selection

### 4.1 The bullet bank

Each experience and project block in `resume_variant.content` holds two lists:
`visible` (what renders by default, typically three to five bullets) and `bank`
(additional verified bullets for the same work, typically another four to eight).
Both are authored by hand. The bank exists precisely so tailoring has something
true to reach for.

```jsonc
{
  "key": "expenditure_tracker",
  "title": "Expenditure Tracker — Ministry of Youth Affairs & Sports",
  "role": "Product & Delivery Lead",
  "period": "2025 – present",
  "sub_projects": [
    { "title": "Budget variance engine", "bullet_ids": ["exp.b2", "exp.b3"] }
  ],
  "visible": ["exp.b1", "exp.b4", "exp.b6"],
  "bank":    ["exp.b2", "exp.b3", "exp.b5", "exp.b7", "exp.b9"],
  "bullets": {
    "exp.b2": { "text": "Built budget-versus-actual variance analytics across ₹924 Cr of programme spend spanning 14 regional centres.",
                "claim_ids": [9, 10], "skills": ["variance_analysis", "sql", "postgres"] },
    "exp.b9": { "text": "Modelled unit economics for the platform and cut monthly run-rate ~60% (₹9.4L → ₹3.5L).",
                "claim_ids": [12, 13], "skills": ["unit_economics", "cost_modelling"] }
  }
}
```

### 4.2 The selection algorithm

Selection is a ranked assignment problem, not a free-form generation task, and it
is executed mostly in deterministic code. The model's job is narrow: it proposes
the final ordering and any rephrasings, given a pre-scored shortlist.

```python
# generate/bullet_selection.py
def shortlist(block, requirements, evidence, ledger) -> list[Candidate]:
    """Deterministic pre-ranking. No model call."""
    out: list[Candidate] = []
    for bid in block["visible"] + block["bank"]:
        bullet = block["bullets"][bid]
        answered = [
            r for r in requirements
            if r.normalised_skill and r.normalised_skill in bullet["skills"]
        ]
        out.append(Candidate(
            bullet_id=bid,
            currently_visible=bid in block["visible"],
            # Hard requirements are worth 3x a nice-to-have.
            requirement_weight=sum(3.0 if r.kind == "hard" else 1.0 for r in answered),
            answers_requirement_ids=[r.id for r in answered],
            # A bullet answering a requirement no other bullet answers is
            # disproportionately valuable: it converts a gap into coverage.
            uniqueness=uniqueness_score(bid, answered, block),
            quantified=bool(ledger.metrics_for(bullet["claim_ids"])),
            claim_ids=bullet["claim_ids"],
        ))
    return sorted(out, key=rank_key, reverse=True)
```

`rank_key` orders on, in descending precedence:

1. **Hard-requirement coverage.** A bullet that answers an unmet hard requirement
   outranks everything.
2. **Uniqueness.** Among bullets answering the same requirement, prefer the one
   that is the *only* answer to something.
3. **Quantification.** A bullet whose claims carry a `metric_value` outranks a
   qualitative one. Numbers survive a six-second scan; adjectives do not.
4. **Recency of the work.**
5. **Incumbency.** All else equal, keep the bullet that is already visible.
   Churn for its own sake is a cost, not a feature.

Rule 5 is why most plans are small. Most of the time the base variant is already
close to right, and the correct plan is three operations, not twelve.

The shortlist — every candidate bullet with its score, its claim IDs and the
requirement IDs it answers — is what goes to the model. The model returns
`bullet_ops`. It never sees the raw ledger and never invents text from scratch:
for `promote`, `swap` and `demote` it returns only IDs.

### 4.3 Surfacing gap-relevant evidence

The interesting case is a requirement with **no** exact skill match. Two
sub-cases, handled differently:

**Adjacent evidence exists.** The requirement is "Power BI dashboards"; the
operator built dashboards, in-app, in React over a Postgres aggregate layer. This
is genuinely adjacent. Adjacency is computed against a hand-maintained adjacency
map in `scoring/vocabulary.py` (`power_bi ↔ dashboarding ↔ data_visualisation`),
not by the model, so it is auditable and stable across runs. Where adjacency
scores above the threshold, the candidate bullet is offered to the model with an
explicit `adjacency` marker and the coverage level `partial`. A `partial` bullet
may be promoted to the resume — the work is real — but the *claim* of the tool is
never made. The bullet says "built the operational dashboards the programme ran
on", not "Power BI".

**No adjacent evidence exists.** The requirement is "RTOS scheduling" and there
is nothing. Nothing is promoted. The requirement lands in `surfaced_gaps` and
becomes cover-letter material (§6.2), or nothing at all. The system does not
manufacture a bullet for a gap. This is the difference between the resume and the
letter: the resume asserts, so it only carries what is true and demonstrable; the
letter argues, so it can address what is missing directly.

### 4.4 Every proposed bullet carries claim IDs

Non-negotiable, and enforced in three places:

1. **Schema.** `BulletOp` rejects a `promote`, `swap` or `rephrase` with an empty
   `claim_ids` (§3.2).
2. **Plan validator.** Every `claim_id` must resolve to a live `claim` row —
   `deleted_at IS NULL`, and `expires_at` either null or in the future. An expired
   claim is a stale fact and is treated as absent.
3. **Validation gate.** The rendered text passes `POST /claims/validate`, which
   independently re-derives every assertion from the text and checks it resolves
   (§8). The bullet's declared `claim_ids` are not trusted as proof; they are a
   hypothesis the gate tests.

On approval, one `claim_usage` row is written per (claim, artifact, location) —
`location` being a path such as `experience.0.bullet.1` or `summary`. That is the
provenance trail from `DATA_MODEL.md` §5.3: given any submitted document, every
number in it traces back to the ledger row that authorised it.

`claim.confidentiality` is enforced here, not at render time. A `restricted`
claim — internal cost figures, for instance — is filtered out of the candidate
set before the model ever sees it, unless the target company is on that claim's
allow-list. Filtering at the source means a restricted fact cannot leak through a
rephrasing.

---

## 5. Rendering — the `.docx` builder contract

### 5.1 The builder input

The builder already exists; it produced the six variants. It takes one structured
object and emits one `.docx`. This contract does not change for Scout Careers —
the tailoring layer's job is to produce a *valid input to the existing builder*,
not to introduce a new document model.

```python
# generate/render_models.py
from pydantic import BaseModel, Field


class SkillLine(BaseModel):
    label: str                       # "Data & Analytics"
    items: list[str]


class SubProject(BaseModel):
    title: str                       # rendered as an italic run-in heading
    bullets: list[str]


class ExperienceBlock(BaseModel):
    title: str                       # "Expenditure Tracker — MYAS"
    role: str
    location: str | None = None
    period: str                      # "2025 – present"
    bullets: list[str] = Field(default_factory=list)
    sub_projects: list[SubProject] = Field(default_factory=list)


class ProjectEntry(BaseModel):
    title: str
    stack: str | None = None
    bullets: list[str]


class EducationEntry(BaseModel):
    institution: str
    qualification: str
    period: str
    detail: str | None = None


class ResumeDocument(BaseModel):
    """The complete builder input. Nothing else is rendered."""
    name: str
    contact_line: str                # email · phone · city
    summary: str
    skill_lines: list[SkillLine]
    experience: list[ExperienceBlock]
    projects: list[ProjectEntry] = Field(default_factory=list)   # optional section
    education: list[EducationEntry]
    achievements: list[str] = Field(default_factory=list)


def build_docx(doc: ResumeDocument, *, tight: bool = False) -> bytes:
    """Render to a single-page .docx. See §5.3 for `tight`."""
```

Applying a plan is a pure function, which makes it trivially testable:

```python
# generate/apply_plan.py
def apply_plan(variant_content: dict, plan: TailoringPlan) -> ResumeDocument:
    """Deterministic. Same variant + same plan == byte-identical output."""
```

`apply_plan` has no model call in it and no I/O. Every property that matters —
"a plan never adds a block", "a rephrase preserves the claim set", "dropping the
projects section removes exactly one section" — is a unit test over this function.

### 5.2 The one-page constraint

One page. Not "usually one page". The variants are one page, the operator's
experience fits on one page, and a two-page resume from a candidate at this level
reads as an inability to prioritise. More practically: a tailoring layer that
silently pushes a document to 1.1 pages produces a second page containing three
lines and a lot of white space, which is worse than any content decision it was
trying to protect.

The constraint is held by exactly two levers, applied in a fixed order.

### 5.3 Lever one — `tight` mode

`tight=True` compresses the document's furniture, not its content. It is a
presentation change only and never removes a word.

| Property | Normal | Tight |
|---|---|---|
| Page margins (all four) | 0.55 in | 0.40 in |
| Body font size | 10.5 pt | 10.0 pt |
| Section-heading size | 11.5 pt | 11.0 pt |
| Line spacing | 1.02 | 0.94 |
| Space after a bullet | 3 pt | 1 pt |
| Space after a block | 8 pt | 5 pt |
| Space before a section heading | 10 pt | 6 pt |
| Bullet hanging indent | 0.22 in | 0.18 in |

```python
# generate/docx_builder.py
from docx.shared import Inches, Pt

TIGHT = {
    "margin_in":        (0.55, 0.40),
    "body_pt":          (10.5, 10.0),
    "heading_pt":       (11.5, 11.0),
    "line_spacing":     (1.02, 0.94),
    "bullet_after_pt":  (3, 1),
    "block_after_pt":   (8, 5),
    "heading_before_pt": (10, 6),
}

def metrics(tight: bool) -> dict[str, float]:
    return {k: v[1 if tight else 0] for k, v in TIGHT.items()}
```

Tight mode buys roughly six to eight body lines on a typical variant. Below 10 pt
body / 0.40 in margins the document starts to look like it is hiding something,
and ATS text extraction gets less reliable, so those are hard floors and are not
configurable.

### 5.4 Lever two — content trimming

If tight mode is not enough, content comes out, in a fixed priority order so the
outcome is deterministic and reviewable.

| `trim_level` | Action |
|---|---|
| 0 | Nothing removed |
| 1 | Drop the optional `projects` section (if the plan has not already dropped it) |
| 2 | Drop the lowest-ranked bullet from the lowest-ranked experience block, repeatedly, until it fits — never dropping a block below two bullets |
| 3 | Truncate `achievements` to the top two entries |

Ranking for level 2 reuses `rank_key` from §4.2, so trimming removes the bullet
that answers the fewest requirements — the same judgement that chose what to
promote, run in reverse. A block is never emptied and a block is never removed
entirely: an experience block with one bullet reads as an apology, so the floor
is two.

Trimming never touches the summary, the skills lines, education, or the contact
block. Those are structural.

### 5.5 Page count is verified by rendering, never assumed

**This is the part that gets skipped and then bites.** `python-docx` writes a
document; it does not lay one out. There is no page count in the OOXML a builder
emits, because pagination is the renderer's job, and Word, LibreOffice and
Google Docs do not agree to the line. Any heuristic — counting characters,
counting lines, estimating from a per-line height — will be wrong for the exact
documents that matter: the ones sitting on the boundary.

So the fit loop renders and measures.

```python
# generate/fit.py
import subprocess, tempfile
from pathlib import Path
from pypdf import PdfReader

MAX_PAGES = 1
LADDER = [                       # (tight, trim_level)
    (False, 0), (True, 0), (True, 1), (True, 2), (True, 3),
]


def page_count(docx_bytes: bytes) -> int:
    """Render via headless LibreOffice and count the pages in the PDF."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "resume.docx")
        src.write_bytes(docx_bytes)
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf",
             "--outdir", d, str(src)],
            check=True, timeout=60, capture_output=True,
        )
        return len(PdfReader(Path(d, "resume.pdf")).pages)


def render_to_one_page(doc: ResumeDocument, plan: TailoringPlan) -> FitResult:
    for tight, trim in LADDER:
        candidate = apply_trim(doc, trim)
        blob = build_docx(candidate, tight=tight)
        pages = page_count(blob)
        if pages <= MAX_PAGES:
            return FitResult(blob=blob, tight=tight, trim_level=trim, pages=pages)
    # Exhausted the ladder. Do not ship a two-page document silently.
    raise DoesNotFit(
        f"Still {pages} pages at tight+trim_level=3 for variant {plan.variant_id}"
    )
```

Notes on this loop:

- **LibreOffice headless is the reference renderer**, pinned by version in the
  container image (`INFRASTRUCTURE.md`). It is not identical to Word, but it is
  deterministic, it is what the CI runs, and it is conservative: a document that
  is one page in LibreOffice at these metrics has never been two pages in Word in
  testing. The renderer version is recorded alongside the artifact.
- **The ladder is tried in order and stops at the first fit.** The chosen `tight`
  and `trim_level` are written back into `plan.render`, so the review UI can show
  "rendered tight, projects section dropped" and the operator can override.
- **`DoesNotFit` is a hard failure**, not a fallback to two pages. The review item
  goes to `needs_manual_review`. A plan that cannot fit is a plan that promoted
  too much, and that is a defect worth seeing.
- Conversion is ~1.5 s per attempt and at most five attempts run, so the worst
  case is ~8 s per document. At ten documents a day this is irrelevant, and it is
  well inside the 15-minute run budget (`ARCHITECTURE.md` §9). Rendering happens
  on preview/approve, not in the nightly run, so it does not sit on the critical
  path at all.
- There is a unit test asserting that each of the six base variants renders to
  exactly one page at `(tight=False, trim_level=0)`. If a variant is edited into
  a second page, CI fails before any tailoring is involved.

### 5.6 Cover letter rendering

Same builder, a much smaller model: `CoverLetterDocument(name, contact_line,
date, recipient_block, salutation, paragraphs, sign_off)`. Same one-page
constraint, same `page_count` verification, same tight ladder — but no trimming
ladder, because the length target (§6.1) already holds it under a page. If a
letter does not fit at `tight=True`, it is too long and the draft is regenerated
with a lower token budget rather than squeezed.

### 5.7 File naming and storage

```
{artifact_id}/{Manas_Sisodia}_{Company}_{RoleSlug}_{resume|cover}.docx
```

Human-readable, because the operator uploads these by hand into an employer's
form and needs to pick the right one under time pressure. `artifact.path` stores
the key; `artifact.checksum` is the SHA-256 of the bytes, so a file that changed
on disk is detectable.

---

## 6. Cover letter generation

### 6.1 Structure and length

Target: **~400 words, one page, four paragraphs.** Not a hard cap — a 360-word
letter and a 430-word letter are both fine — but the token budget and the prompt
both aim at 400, because that is the length at which a letter is read rather than
skimmed.

| ¶ | Purpose | Words | Source |
|---|---|---|---|
| 1 | The specific hook: why *this* role at *this* company, and the single strongest piece of evidence. May carry the gap (§6.2). | 70–110 | Top-ranked met requirement + company context |
| 2 | The strongest matched requirement, told as a compact narrative with a number. | 90–130 | `match_score.evidence`, claim-backed |
| 3 | **The honest-gap paragraph** — or, where the gap led in ¶1, the second strongest evidence. | 80–120 | `match_score.gaps` |
| 4 | What the operator would do in the first ninety days, and a plain close. | 60–90 | Requirements marked `responsibility` |

Fixed rules, enforced by a post-generation lint before validation:

- No "I am writing to apply for". The first six words must contain either the
  company name or a concrete noun from the role.
- No adjective stacking ("passionate", "results-driven", "proven track record").
  A deny-list of 40 phrases; a hit is a lint failure and forces regeneration.
- Every number traces to a claim (§8).
- No claim about the company's mission that is not in `company.notes` or the
  posting text. The system does not know what the company values and will not
  pretend to.
- One page, verified by render (§5.5).

### 6.2 The honest-gap paragraph

**This is the design point that makes the letters worth generating at all.**

The paragraph is computed per role from `match_score.gaps` and
`plan.surfaced_gaps`. It is never templated, and there is no fallback template if
generation fails — if it cannot be computed, no letter is produced.

**Why naming the disqualifier converts better than concealing it.**

The reader of a cover letter for a role the applicant does not fully match is
performing exactly one task: finding the reason to reject. The gap is not hidden
from them — it is on the resume, in the absence of the keyword they are scanning
for. Concealment does not remove the gap; it removes the applicant's only chance
to frame it.

Three concrete mechanisms:

1. **You control the framing exactly once.** If the letter says nothing about
   RTOS, the reader supplies their own framing when they notice: "no embedded
   experience, next." If the letter says "I have not shipped on an RTOS", the
   reader's next sentence is the one the applicant wrote — the adjacent evidence,
   the ramp plan, the reason it is bridgeable. That is the entire value being
   bought, and it is only available once.
2. **Naming it is a costly signal.** Stating a weakness against interest is
   evidence of accurate self-assessment, which is a trait the reader is screening
   for anyway and cannot otherwise observe from a document optimised to flatter.
   It also raises the credibility of the strengths in the same letter: a letter
   that admits one thing is believed more on everything else.
3. **It routes the application correctly.** If the gap is genuinely
   disqualifying, an early rejection costs both sides nothing and preserves the
   operator's standing with that employer for the next role — which matters, given
   300 tracked companies and a multi-year horizon. If it is not disqualifying, the
   reader has been told so explicitly, in the applicant's words, and has no reason
   to reject on it.

**Why a templated gap paragraph is worse than no letter.**

A template — "While I don't have direct experience with {gap}, I'm a fast learner
and confident I could ramp quickly" — inverts every one of the three mechanisms.
It is instantly recognisable as boilerplate, which converts the costly signal into
a cheap one, and a cheap signal about a weakness reads as an excuse. It is not
specific, so it supplies no framing the reader can use. And it appears on every
application the applicant sends, so a recruiter who has seen two of them now
knows the whole letter is machinery. At that point the letter is not neutral — it
is actively negative evidence, and the applicant would have been better off
attaching nothing. Hence: **if the gap paragraph cannot be computed from real
`match_score.gaps` data and real ledger evidence, the letter is not generated.**

**How it is computed.**

```python
# generate/gap_paragraph.py
def select_gap(gaps: list[Gap], plan: TailoringPlan) -> GapFraming | None:
    """Pick at most two gaps to name, and decide how to place them."""
    hard_missing = [g for g in gaps if g.kind == "hard" and g.level == "missing"]
    if not hard_missing:
        return None                      # nothing worth naming; ¶3 becomes evidence

    ranked = sorted(hard_missing, key=lambda g: (-g.weight, g.ordinal))
    named  = ranked[:2]                  # never more than two; three reads as a list of failures

    # Where a *cluster* of related hard requirements is missing, the gap is the
    # spine of the application and belongs in the opening, not buried in ¶3.
    cluster = same_domain(hard_missing)
    placement = "opening" if len(cluster) >= 3 else "third_paragraph"

    return GapFraming(
        gaps=named,
        placement=placement,
        adjacent_claim_ids=[
            cid for g in named for cid in g.nearest_evidence_claim_ids
        ],
        # If nothing adjacent exists, the letter says so and pivots to the
        # transferable capability. It does not claim adjacency it does not have.
        has_adjacent=any(g.nearest_evidence_claim_ids for g in named),
    )
```

Two rules fall out of this:

- **At most two gaps are named.** Naming three or more turns the letter into a
  list of reasons to reject.
- **Placement depends on severity.** A single missing hard requirement goes in ¶3,
  after strength has been established. A *cluster* of missing hard requirements —
  where the gap is the story of the application — goes in the opening, because a
  reader who discovers it in paragraph three feels the first two paragraphs were
  a sales pitch. Discovering it in the first sentence, from the applicant, reads
  as candour. Same fact, opposite effect.

### 6.3 Worked example A — firmware engineer, gap in the opening

**The role.** Firmware Engineer — storage controller team. Hard requirements
extracted: embedded C, RTOS (FreeRTOS/ThreadX), Linux device drivers, RAID
controller firmware, SCSI/SAS/NVMe protocol stacks, hardware bring-up and JTAG
debugging.

**The score.** Variant `backend`. `hard_met` 1 of 6 (C, from the systems work),
`nice_met` 2 of 5, `coverage_pct` 21.3. Three of the four missing hard
requirements — RTOS, Linux device drivers, RAID controller firmware — are in the
same domain cluster. `same_domain()` returns a cluster of three, so
`placement = "opening"`.

**What the system does before writing anything.** At 21.3% coverage this posting
would normally not reach stage ⑧ at all; the generation threshold is 45%. It
reaches it here because the operator imported it manually via
`POST /api/v1/postings/import` — a referral, or a role someone forwarded. Manual
imports bypass the ranking threshold but not the gap logic, and the resulting
letter is honest about the arithmetic. This is the correct behaviour: the system
does not refuse to help with a long-shot application, and it does not pretend a
long shot is a strong match.

**The generated gap framing (¶1, verbatim from a draft):**

> I'll be direct about the mismatch first, because it's the thing you'd find on
> page one anyway: I have not written RTOS firmware, I have not shipped a Linux
> device driver, and I have never worked on RAID controller code. If those are
> hard gates — and for a controller firmware team they reasonably might be — this
> is the wrong application and I'd rather not waste your screening time. What I
> do bring is eight years of systems work in C and Python on the layer directly
> above yours: the telemetry, tooling and data pipelines that firmware teams
> depend on and usually have to build themselves.

Then ¶2 is claim-backed evidence of the adjacent capability, ¶3 is the specific
thing that makes the pivot plausible rather than aspirational, and ¶4 closes
without restating the gap.

Note what the paragraph does not do. It does not say "fast learner". It does not
say "confident I can ramp quickly". It does not claim RTOS adjacency it does not
have — `has_adjacent` was false for RTOS and device drivers, so the letter pivots
to a genuinely different capability rather than stretching. And it names the
possibility that this is a wasted read, which is the sentence that makes the rest
credible.

**The counterfactual.** The same application with a concealing letter — four
paragraphs of C experience and enthusiasm for storage — gets rejected at the
resume, on the same missing keywords, having taught the reader nothing except
that the applicant either did not read the requirements or hoped nobody would
check. The honest version has a small chance of a "we have a tools/firmware
infrastructure opening, actually" reply. Small, but strictly larger than zero,
which is what the concealing version has.

### 6.4 Worked example B — Analyst II, Financial Modeling & AI, gap in ¶3

**The role.** Analyst II, Financial Modeling & AI — Seagate Technology, Pune.
This is the posting from §3.3 and `API.md` §5.

**The score.** Variant `consulting`. Coverage 47.5%, hard 4/7, nice 5/6. Missing
hard: advanced Excel model building, Power BI. Missing nice: SAP/Anaplan/Hyperion.
Two missing hard requirements, not in one cluster (`same_domain` returns 2), so
`placement = "third_paragraph"`.

Present, and claim-backed:

| Evidence | Claims |
|---|---|
| Budget-versus-actual variance analysis across ₹924 Cr of programme spend, 14 regional centres | `khelo.budget_total`, `khelo.regional_centres` |
| Unit-economics cost modelling: platform run-rate cut ~60%, ₹9.4L → ₹3.5L per month | `khelo.cost_reduction_pct`, `khelo.run_rate_before_after` |
| Reconciliation automation: 3 manual workflows automated | `khelo.reconciliation_count` |

**¶1 — the hook** (no gap here; the gap is not the story of this application):

> Your posting asks for someone who can model the economics of a business and
> then build the AI that acts on the model. I have spent the last two years doing
> exactly that on the other side of the ledger: running variance analysis against
> ₹924 Cr of programme budget across 14 regional centres, and building the
> systems that produced the numbers rather than just consuming them.

**¶2 — the strongest matched requirement:**

> The clearest example is unit economics. The platform's monthly run-rate was
> ₹9.4 lakh when I took ownership of the cost model. I instrumented per-workload
> cost, found that inference and storage were mispriced against actual usage, and
> restructured both — run-rate now sits at ₹3.5 lakh, roughly a 60% reduction,
> with no loss of capability. That was a modelling exercise before it was an
> engineering one.

**¶3 — the honest-gap paragraph:**

> Two things you asked for that I do not have. I have not built financial models
> in advanced Excel — mine were built in Python and SQL against the live
> transaction data, which is a different discipline even where the arithmetic is
> the same, and I would be starting from the beginning on the Excel conventions
> your team works in. I have also never used Power BI; the dashboards I built
> were in-product, against a Postgres aggregate layer. I mention both because
> they are real, and because I would rather you weigh them now than find them in
> week three. What transfers is the part underneath: knowing which number matters,
> where it comes from, and whether it can be trusted.

**¶4 — first ninety days and close:**

> If this went forward, the first thing I would want is your existing model and
> the last four quarters it produced, to understand what the team actually
> forecasts against before proposing anything. I am in Pune and available to talk.

**Why this letter works.** ¶3 concedes two specific tools, distinguishes tool from
capability without hiding behind the distinction, names the ramp cost explicitly
("starting from the beginning on the Excel conventions"), and closes on the thing
that is genuinely transferable. It is 118 words and contains no adjective the
reader has seen on another application this week. Total: 412 words, one page.

**And a real validation failure from this letter.** The first generated draft of
¶1 said "across 14 regional centres" while the ledger held only
`khelo.budget_total`. `POST /claims/validate` returned exactly the response shown
in `API.md` §4:

```jsonc
{ "span": "14 centres", "resolved": false, "claim_id": null,
  "note": "No ledger claim for centre count." }
```

`passed: false`, so the artifact could not attach. The correct fix was not to
weaken the sentence — the fact is true — but to add `khelo.regional_centres` to
the ledger with its evidence reference, and regenerate. That is the gate working:
it did not block a lie, it blocked an *unverified* statement, and it forced the
verification to be recorded once, permanently, for every future document.

### 6.5 What the letter never contains

- The recruiter's name, unless it is in the posting. The system does not look
  people up and never fetches LinkedIn (`ARCHITECTURE.md` §3, invariant 4).
- Salary expectations, notice period, or visa status. Those are form fields, and
  they are the operator's to state.
- Any claim about the company's culture, mission or recent news that is not in
  the posting text or `company.notes`.
- Anything derived from an instruction found inside the job description. See
  `AI_ARCHITECTURE.md` §7.

---

## 7. When not to generate a cover letter

`company.cover_letter_worth` (`DATA_MODEL.md` §3.1) gates stage ⑧'s letter
branch. When false, no letter is drafted, no tokens are spent, and the review item
shows "no cover letter — this employer's ATS has no field for one."

**The reason.** Most large-tech ATS configurations have no cover-letter upload at
all, or have one marked optional that is dropped before a human sees the
application. A letter written for a form that discards it is pure cost. At ~₹1.9
per letter and a design target of ten drafts a day, this is not a large sum, but
it is a large fraction of a ₹80 budget spent on nothing.

The flag is a **default with an override**, set per company, not per posting,
because the ATS configuration is a property of the employer.

| Employer category | Default | Reasoning |
|---|---|---|
| Large tech with a self-serve ATS (Google, Amazon, Microsoft, Meta, Adobe) | `false` | No field, or a field that is not routed to a human. Resume and the answers to the screening questions are the whole application. |
| Mid-size product companies on Greenhouse / Lever / Ashby | `true` | The field exists, is usually read, and these are the roles where a 47% match can be argued upward. Highest return on a letter. |
| Startups under ~200 people | `true` | Frequently read by the hiring manager or a founder. The highest-conversion category for a letter, by a wide margin. |
| Consultancies and professional services | `true` | The letter *is* the work sample — written argument is the job. |
| Staffing agencies and recruitment firms | `false` | The recruiter reformats the resume anyway and does not forward a letter. |
| Job-board aggregator listings with no identified employer | `false` | No addressee, no company context. There is nothing to write. |
| Public sector and research institutions | `true` | Often mandatory, and often scored formally against the criteria. |
| Any employer where a specific gap needs framing | `true` — **override** | If the gap is the application, the letter is the application. This overrides the category default. |

That last row is the important one. The categories are heuristics for where a
letter is *usually* read. The override exists because a role where the operator
is a 21% match with a cluster of missing hard requirements (§6.3) has exactly one
mechanism available to it, and it is the letter. `cover_letter_worth = false` on
such a posting is a per-company default being applied to a case it was not
designed for, and the operator can flip it on the review item.

Where `cover_letter_worth` is false, the operator may still request a letter from
the review screen — `POST /api/v1/review/{id}/generate` with
`{ "force_cover_letter": true }`. It runs the identical pipeline, including the
validation gate. Nothing about the flag weakens any invariant.

---

## 8. The validation gate

Nothing generated attaches to a `review_item` or an `application` until it passes
the claims-ledger citation check. This is invariant 3 from `ARCHITECTURE.md` §3,
and it is enforced by a database trigger in addition to application code
(`DATA_MODEL.md` §8.1), because an invariant that only one layer enforces is a
convention.

### 8.1 The flow

```
draft text (resume bullets, or letter paragraphs)
        │
        ▼
POST /api/v1/claims/validate      ← the enforcement endpoint, API.md §4
        │
        ├─ passed: true  ──▶ artifact row written, validation_status = 'passed'
        │                    claim_usage rows written, one per (claim, location)
        │                    artifact attachable to review_item
        │
        └─ passed: false ──▶ artifact row written, validation_status = 'failed'
                             artifact retained for diagnosis
                             CANNOT attach — DB trigger refuses the FK write
                             one regeneration attempt with the failures fed back
                             still failing ⇒ review_item.status = needs_manual_review
```

### 8.2 What it checks

The endpoint extracts every **numeric assertion** (a number, percentage, currency
amount, duration, count, ratio) and every **superlative or comparative** ("first",
"largest", "only", "reduced by", "fastest") from the submitted text, and resolves
each against the `claim` table. Resolution semantics — how a span maps to a claim
key, how `metric_value` and `metric_unit` are compared, how tolerance on rounded
figures is handled — are specified in `CLAIMS_LEDGER.md`, which is canonical for
that logic. This document only states the consequence: **an unresolved assertion
is a hard block.**

Three properties worth stating explicitly:

- **It runs on the text, not on the plan's metadata.** The `claim_ids` a bullet
  op declares are a hypothesis. The gate independently re-derives what the text
  actually asserts. A rephrasing that quietly introduces "across 20 centres" when
  the claim says 14 is caught here even though the declared claim set is correct.
- **It is the final backstop against prompt injection.** If a job description
  contains an instruction that succeeds in steering generation, the output still
  has to make only claims that resolve to the ledger. An injected instruction can
  at most reorder or omit true things; it cannot manufacture a false one into a
  document. See `AI_ARCHITECTURE.md` §7.
- **There is no override.** `API.md` §8 lists "overriding a failed ledger
  validation" among the endpoints that deliberately do not exist. The remedy for
  a false negative is to add the missing claim to the ledger with its evidence
  reference — which is the correct outcome, because the fact then exists once,
  verified, for every future document.

### 8.3 `bypassed`

`artifact_validation` has a third value, `bypassed`. It applies to exactly one
case: an artifact rendered directly from an unmodified base variant with an empty
tailoring plan, where the variant's content was validated at seed time and has
not changed since (checked by hash). Nothing generated is ever `bypassed`. The
value exists so the base-variant render path does not have to re-validate 40
already-verified bullets on every download, and it is scoped narrowly enough that
it cannot become a loophole.

---

## 9. Anti-templating

At forty applications the letters are read one at a time. At four hundred, some
subset lands in front of the same recruiter, the same agency, or the same
shared-ATS reviewer. Detectable formula at that point is worse than no letter —
it converts every application into evidence that none of them were written.

Four mechanisms, in increasing order of strength.

### 9.1 Per-role computed content

Structural, not cosmetic. The gap paragraph is computed from *this role's*
`match_score.gaps` (§6.2). The evidence paragraph is selected from *this role's*
top-ranked met requirement. The ninety-days paragraph is built from *this role's*
`responsibility` requirements. Two letters are similar only to the extent that
two roles are similar — which is the correct amount of similarity.

### 9.2 Varied structure

The paragraph *order* is not fixed. `placement` (§6.2) already moves the gap
between ¶1 and ¶3 based on severity. Beyond that, the prompt is given a
structural directive selected per role from a small set — evidence-first,
gap-first, problem-first (opening on the problem the role exists to solve),
question-first — chosen deterministically from `hash(posting_id) % 4`, biased by
gap placement. Deterministic because a letter must be reproducible from its
`prompt_version` and posting (invariant 7); varied because a fixed order is the
most detectable thing about a generated letter.

Opening sentence forms are similarly constrained rather than templated: the lint
in §6.1 bans the recognisable openers, and the prompt is shown the openers of the
three most recent letters with the instruction not to reuse their shape.

### 9.3 Similarity check against previous letters

Every generated letter is compared against every previously generated letter for
the same operator.

```python
# generate/similarity.py
from datasketch import MinHash, MinHashLSH

SHINGLE = 5            # word-level 5-grams
WARN = 0.55            # flag for review
BLOCK = 0.72           # regenerate; do not ship

def shingles(text: str) -> set[str]:
    w = normalise(text).split()
    return {" ".join(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}

def check(new_text: str, lsh: MinHashLSH) -> SimilarityResult:
    mh = MinHash(num_perm=128)
    for s in shingles(new_text):
        mh.update(s.encode())
    neighbours = [(k, jaccard(mh, stored[k])) for k in lsh.query(mh)]
    worst = max((s for _, s in neighbours), default=0.0)
    return SimilarityResult(
        max_similarity=worst,
        nearest=[k for k, s in neighbours if s >= WARN],
        verdict="block" if worst >= BLOCK else "warn" if worst >= WARN else "ok",
    )
```

- Two-tier thresholds. Above 0.55, the review UI shows "78% similar to your
  Zerodha letter from 12 August" with a diff, and the operator decides. Above
  0.72, the letter is regenerated with the near-duplicate passages supplied to
  the prompt as text to avoid. Two consecutive blocks send the item to
  `needs_manual_review` rather than looping.
- MinHash/LSH over word 5-grams, not embeddings. The question is "is this
  textually near-duplicate", which is a lexical question. Embedding similarity
  would flag two letters about the same *kind* of role, which is expected and
  fine, and would miss a shared boilerplate paragraph inside two otherwise
  different letters, which is exactly the thing being hunted.
- The index is per-paragraph as well as per-letter. A letter can be 0.3 similar
  overall while sharing an identical ¶4 with eleven others. Paragraph-level
  thresholds are stricter: warn at 0.45, block at 0.60.
- The index is rebuilt from stored artifact text on startup and is cheap at this
  scale — a few hundred documents.

### 9.4 Corpus-level drift monitoring

A weekly job computes mean pairwise similarity across the last thirty letters and
the type-token ratio of the corpus. Both rising is the signature of a prompt that
has collapsed onto a favourite structure — a failure mode that no single-letter
check catches, because each letter is individually fine and the *set* is
formulaic. A rise past threshold is reported in the digest and is treated as a
prompt defect requiring a version bump.

---

## 10. Prompt versioning and forced regeneration

Every generated thing records the prompt that produced it:
`artifact.prompt_version`, `requirement.prompt_version`,
`match_score.prompt_version`, and `tailoring_plan.prompt_version`. Version
strings are `{family}@{date}.{n}` — `cover_letter@2026-09-01.2`. The registry and
its loading are specified in `AI_ARCHITECTURE.md` §5.

**A prompt change invalidates every artifact produced by the old version.** Not
silently — the invalidation is explicit and staged:

1. A new prompt version is added to the registry. Old versions stay; they are
   never deleted, because invariant 7 requires that an artifact from six months
   ago can be explained.
2. The eval suite runs against the new version (`AI_ARCHITECTURE.md` §10). It
   ships only if the eval holds.
3. On deploy, a migration marks `review_item` rows in `pending_review` whose
   plan or artifacts were produced by a superseded version as **stale**. Stale
   items render with a banner and cannot be approved until regenerated.
4. Already-`approved` items and `application` rows are **never** regenerated. The
   document that was submitted is the document that was submitted; rewriting
   history to match a newer prompt would destroy the only honest record of what
   was sent.
5. `POST /api/v1/postings/{id}/rescore` (202) re-runs extract → score → generate
   for a posting under the current versions.

The same staleness rule applies when the underlying inputs change: a
`resume_variant` edit invalidates plans referencing it, and a `claim` edit or
soft-delete invalidates any artifact with a `claim_usage` row pointing at it.
The latter is the important one — if a fact turns out to be wrong, every document
that used it must be findable, and `claim_usage` is how.

---

## 11. Human in the loop

### 11.1 The operator edits the plan, not the output

`PATCH /api/v1/review/{id}/plan` is the editing surface. The review screen shows
the plan as a list of proposed operations, each with its rationale and its claim
IDs, each individually accept/reject/modify. Rendering happens after.

This is deliberate and it is the corollary of §2.2. If the operator edits the
`.docx`, the correction lives in a file, the system never learns it, and the same
wrong swap is proposed again next week. If the operator rejects a bullet op, the
system has a labelled example.

Editing an approved item is not possible. Approval freezes the artifacts
(`API.md` §5) because they are what gets submitted, and a mutable record of a
submitted application is not a record.

### 11.2 Capturing the edits

Every accept, reject and modification is appended to `plan.operator_edits` with a
timestamp, the JSON path of the operation, and an optional note. Nothing is
overwritten — the model's original proposal stays in the plan alongside the human
verdict, because "what was proposed and rejected" is the signal, and a plan that
only contains the accepted operations has thrown it away.

### 11.3 What the system does with them

Three uses, in increasing ambition. Only the first two are in scope now.

**Acceptance-rate reporting (in scope).** A weekly figure per operation type and
per bullet ID: what fraction of proposed swaps involving `exp.b9` were accepted.
A bullet that is proposed twelve times and accepted twice is a bad bullet or a
bad rule, and either is worth knowing. This is a SQL query over
`review_item.tailoring_plan`, not a model.

**Prompt few-shots (in scope).** The highest-signal rejections — where the
operator supplied a note — are candidates for the tailoring-plan prompt's few-shot
block on its next version. Promotion into the prompt is a human decision reviewed
against the eval suite, never automatic. A prompt that edits itself from
production data is a prompt nobody can reason about.

**Learned ranking (out of scope, deliberately).** Adjusting `rank_key` weights
from acceptance data is the obvious next step and it is wrong at this scale. A few
hundred decisions from one person is not enough to fit anything that will not
mostly encode noise, and the failure mode — silently drifting selection with no
one able to say why a bullet stopped being proposed — is exactly what the
explainable-scoring lineage in `ARCHITECTURE.md` §1.2 exists to avoid. Revisit at
low thousands of decisions, behind a flag, with the deterministic ranker as the
control arm.

---

## 12. Failure modes and error policy

Generation is on the enrichment side of the fail-open/fail-closed line
(`ARCHITECTURE.md` §8) — with one exception, marked below.

| Failure | Handling |
|---|---|
| LLM provider unavailable at stage ⑧ | Item enqueues with `tailoring_plan = {}` and no letter. The base variant is still a legitimate application. Digest reports the degradation. |
| Plan fails Pydantic or the DB-backed validator | One repair-retry with the errors fed back. Second failure ⇒ `needs_manual_review`. |
| Plan references a bullet ID that does not exist | Treated as a validator failure. Never partially applied — a plan applies wholly or not at all. |
| Letter fails the ledger gate | Artifact stored as `failed`, one regeneration with the unresolved spans fed back, then `needs_manual_review`. **Fail closed** — this is the invariant-3 boundary. |
| Letter fails the phrase lint | Regenerate once with the offending phrases named. Then `needs_manual_review`. |
| Letter blocks on similarity | §9.3. |
| `DoesNotFit` after the full ladder | `needs_manual_review` with the page count and the ladder trace. Never ships two pages. |
| LibreOffice conversion fails or times out | Render is retried once; on second failure the `.docx` is offered for download **unverified**, flagged in the UI as "page count not verified". This is the one place a soft degradation is allowed, because a resume the operator can inspect themselves beats no resume. |
| A `claim` referenced by the plan expired between scoring and generation | The bullet op is dropped from the plan before rendering, with a note. Expired facts are absent facts. |

---

## 13. Configuration

Keys on the `Settings` object (`CONFIGURATION.md` is canonical for the full list).

| Key | Default | Effect |
|---|---|---|
| `GENERATION_ENABLED` | `true` | Master switch for stage ⑧ |
| `GENERATION_MIN_COVERAGE_PCT` | `45.0` | Coverage floor for automatic generation; manual imports bypass it |
| `GENERATION_DAILY_CAP` | `10` | Hard cap on drafts per day; one discovery run per day, so equivalently the per-run cap. Matches the scale envelope |
| `COVER_LETTER_ENABLED` | `true` | Master switch for the letter branch |
| `COVER_LETTER_TARGET_WORDS` | `400` | Prompt target |
| `RESUME_MAX_PAGES` | `1` | Not raised. Present so the fit loop has no magic constant. |
| `RENDER_VERIFY_PAGES` | `true` | Off only in local development, never in the deployed image |
| `SIMILARITY_WARN` / `SIMILARITY_BLOCK` | `0.55` / `0.72` | §9.3 |
| `FF_TAILORING_REPHRASE` | `false` | Flags the `rephrase` op; ships off, proven on a slice (§14) |
| `FF_GAP_IN_OPENING` | `false` | Flags gap-first placement; ships off |

---

## 14. Rollout discipline

New generation behaviour ships flagged off (`ARCHITECTURE.md` §8) and is proven
on a small reversible slice before becoming default. Concretely, for the two
flags above:

- `FF_TAILORING_REPHRASE` enables the `rephrase` operation, which is the only
  operation that produces text the model composed rather than selected. It is
  therefore the highest-risk operation in the system and the one most worth
  gating. Proven on `volume`-tier companies only, for two weeks, with every
  rephrase reviewed and its validation result recorded, before it is enabled for
  `dream` and `strong` tiers.
- `FF_GAP_IN_OPENING` enables §6.2's opening placement. Proven the same way —
  and evaluated on response rate from `v_funnel`, not on how good the letters
  feel to read.

Reverting either is a settings change, not a deploy. The plan schema tolerates the
absence of the operations, and `apply_plan` ignores an op type it is not
configured for.

---

## 15. Related documents

| Document | Covers |
|---|---|
| `ARCHITECTURE.md` | Invariants, pipeline stages, scale envelope |
| `DATA_MODEL.md` | `review_item`, `artifact`, `claim`, `claim_usage`, `resume_variant` |
| `API.md` | `/review/*`, `/claims/validate`, `/variants/{id}/render` |
| `CLAIMS_LEDGER.md` | Claim resolution semantics, the validation algorithm, confidentiality gating |
| `MATCH_SCORING.md` | Requirement extraction, coverage, `gaps` and `evidence` structures |
| `AI_ARCHITECTURE.md` | Prompt families, model routing, structured output, injection defence, cost |
| `SECURITY_ARCHITECTURE.md` | Untrusted-input handling, secret handling |
| `INFRASTRUCTURE.md` | LibreOffice pinning, artifact storage, backups |
