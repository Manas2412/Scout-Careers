# API — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for endpoint paths, envelopes and error codes.

FastAPI, OpenAPI 3.1 generated at `/api/v1/openapi.json`. The React client is
generated from that schema — hand-written request types in the frontend are a
review failure.

---

## 1. Conventions

**Base path:** every route is under `/api/v1/`. The version is present from day
one so a breaking change never has to be argued about.

**Envelope.** Every response, success or failure, is:

```jsonc
{
  "data": { },          // or [ ], or null on error
  "message": "string",  // human-readable, safe to display
  "meta": { }           // optional: pagination, timing, counts
}
```

**Errors** use the correct HTTP status and a stable machine code:

```jsonc
{
  "data": null,
  "message": "Company already tracked for this board.",
  "meta": { "code": "company.duplicate_source", "field": "careers_url" }
}
```

| Status | Used for |
|---|---|
| 200 | Successful read or update |
| 201 | Resource created |
| 202 | Accepted — long-running job queued (discovery run, generation) |
| 204 | Successful delete |
| 400 | Malformed request |
| 404 | Not found |
| 409 | Conflict — duplicate source, already-decided review item |
| 422 | Validation failure (FastAPI default shape, wrapped in the envelope) |
| 429 | Local rate limit hit |
| 502 | Upstream ATS or LLM provider failed |

**Pagination.** Cursor-based on every list endpoint:
`?limit=50&cursor=<opaque>`. `meta.next_cursor` is null on the last page.
Offset pagination is not offered.

**Auth.** Single-user, single-session. A long-lived local session cookie issued
against a password from the environment. There is no registration, no password
reset and no multi-user model — see `SECURITY_ARCHITECTURE.md` §4 for why that
is the right call for a personal tool and what it would take to change.

**Idempotency.** `POST /runs/discovery` and `POST /review/{id}/generate` accept
an `Idempotency-Key` header. A repeated key inside 24 hours returns the original
response rather than starting a second run.

---

## 2. Companies

```http
GET    /api/v1/companies
POST   /api/v1/companies
GET    /api/v1/companies/{id}
PATCH  /api/v1/companies/{id}
DELETE /api/v1/companies/{id}
POST   /api/v1/companies/detect
POST   /api/v1/companies/{id}/sources
PATCH  /api/v1/sources/{id}
DELETE /api/v1/sources/{id}
POST   /api/v1/sources/{id}/test
```

**`GET /companies`** — filters: `status`, `tier`, `tag`, `adapter`, `q` (trigram
name search), `has_new` (roles first seen in the last 7 days).

```jsonc
{
  "data": [{
    "id": 42, "slug": "adobe", "name": "Adobe",
    "tier": "dream", "status": "tracking",
    "tags": ["big-tech", "product"],
    "cover_letter_worth": false,
    "default_variant": { "id": 3, "key": "ai_platform" },
    "sources": [{ "id": 77, "adapter": "workday", "enabled": true,
                  "last_run_at": "2026-09-05T02:31:00Z",
                  "last_status": "ok", "consecutive_failures": 0 }],
    "stats": { "open_postings": 214, "new_this_week": 11, "applications": 2 }
  }],
  "message": "OK",
  "meta": { "next_cursor": "…", "total": 287 }
}
```

**`POST /companies/detect`** — the paste-a-URL flow. This is the endpoint that
makes maintaining 300 companies practical.

```jsonc
// request
{ "url": "https://adobe.wd5.myworkdayjobs.com/external_experienced" }

// 200
{
  "data": {
    "adapter": "workday",
    "config": { "host": "adobe.wd5.myworkdayjobs.com",
                "tenant": "adobe", "site": "external_experienced" },
    "company_name_guess": "Adobe",
    "probe": { "reachable": true, "sample_count": 25, "latency_ms": 412 },
    "existing_company_id": null
  },
  "message": "Detected Workday board for Adobe."
}
```

Detection is URL-pattern matching plus one live probe. It never guesses without
confirming the endpoint returns postings. If the host is on the never-scrape
list the endpoint returns **403** with code `source.denied_by_policy` — and that
refusal is not overridable through configuration.

**`POST /sources/{id}/test`** — re-probes a source and returns the same `probe`
block. Used by the Settings health table.

---

## 3. Postings

```http
GET  /api/v1/postings
GET  /api/v1/postings/{id}
GET  /api/v1/postings/{id}/requirements
GET  /api/v1/postings/{id}/scores
POST /api/v1/postings/{id}/rescore
POST /api/v1/postings/import
```

**`GET /postings`** — filters: `company_id`, `tier`, `location`, `is_remote`,
`min_coverage`, `variant_id`, `seen_since`, `status` (`open|closed`), `q`
(full-text over `search_tsv`). Default sort is `composite_score DESC`, falling
back to `first_seen_at DESC` for unscored rows.

**`POST /postings/import`** — manual entry for a role found outside the pipeline
(a referral, a role someone sent you). Accepts a URL or a pasted description,
runs extract → score → generate synchronously, and returns the resulting
`review_item`. This is the path the two Seagate applications would have taken.

```jsonc
// request
{ "url": "https://seagatecareers.com/job/…/14869-en_US",
  "description_text": "…optional, if the URL cannot be fetched…",
  "company_hint": "Seagate Technology" }
```

**`POST /postings/{id}/rescore`** — 202. Forces re-extraction and re-scoring,
for example after a resume variant or the claims ledger changes.

---

## 4. Variants and the claims ledger

```http
GET    /api/v1/variants
GET    /api/v1/variants/{id}
PATCH  /api/v1/variants/{id}
POST   /api/v1/variants/{id}/render

GET    /api/v1/claims
POST   /api/v1/claims
GET    /api/v1/claims/{id}
PATCH  /api/v1/claims/{id}
DELETE /api/v1/claims/{id}
GET    /api/v1/claims/{id}/usage
POST   /api/v1/claims/validate
```

**`POST /variants/{id}/render`** — renders a variant to `.docx` using the
existing builder and returns an `artifact`. Optional `tailoring_plan` in the body
applies a review item's proposed edits before rendering.

**`GET /claims`** — filters: `project`, `tag`, `confidentiality`, `expired`.

**`POST /claims/validate`** — the enforcement endpoint. Given draft text, returns
every numeric or superlative assertion and whether it resolves to a ledger claim.

```jsonc
// request
{ "text": "cut run-rate ~60% (₹9.4L → ₹3.5L per month) across 14 centres" }

// 200
{
  "data": {
    "passed": false,
    "assertions": [
      { "span": "~60%",        "resolved": true,  "claim_id": 12,
        "claim_key": "khelo.cost_reduction_pct" },
      { "span": "₹9.4L → ₹3.5L", "resolved": true, "claim_id": 13 },
      { "span": "14 centres",  "resolved": false, "claim_id": null,
        "note": "No ledger claim for centre count." }
    ]
  },
  "message": "1 assertion could not be resolved."
}
```

A `passed: false` result blocks artifact attachment. It does not merely warn.

---

## 5. Review queue

```http
GET   /api/v1/review
GET   /api/v1/review/{id}
POST  /api/v1/review/{id}/generate
POST  /api/v1/review/{id}/approve
POST  /api/v1/review/{id}/skip
PATCH /api/v1/review/{id}/plan
GET   /api/v1/review/{id}/artifacts/{artifact_id}/download
```

**`GET /review/{id}`** — everything needed to make the decision on one screen:

```jsonc
{
  "data": {
    "id": "01JB…",
    "status": "pending_review",
    "posting": { "title": "Analyst II, Financial Modeling & AI",
                 "company": { "name": "Seagate Technology", "tier": "strong" },
                 "location_city": "Pune", "url": "https://…",
                 "posted_at": "2026-08-13T00:00:00Z" },
    "recommended_variant": { "id": 6, "key": "consulting" },
    "score": {
      "coverage_pct": 47.5,
      "hard_met": 4, "hard_total": 7,
      "nice_met": 5, "nice_total": 6,
      "gaps": [
        { "text": "Advanced Excel model building", "level": "missing",
          "kind": "hard", "note": "No ledger evidence of Excel modelling." },
        { "text": "Power BI or similar", "level": "missing", "kind": "hard" },
        { "text": "SAP / Anaplan / Hyperion", "level": "missing", "kind": "nice" }
      ],
      "evidence": [
        { "requirement": "Automating reports, reconciliations",
          "level": "met", "claim_ids": [21],
          "bullet": "Automated 3 manual reconciliation workflows…" }
      ]
    },
    "tailoring_plan": {
      "reorder_blocks": ["expenditure_tracker", "khelo_assistant", "pmis"],
      "bullet_swaps": [
        { "block": "summary", "from": "…", "to": "…", "claim_ids": [12, 31] }
      ],
      "skills_line_edits": [ { "line": "Data & Tooling", "add": ["Excel"] } ]
    },
    "artifacts": {
      "resume":       { "id": "01JC…", "validation_status": "passed" },
      "cover_letter": { "id": "01JD…", "validation_status": "passed" }
    }
  },
  "message": "OK"
}
```

**`POST /review/{id}/approve`** — 201. Creates the `application` row with status
`submitted`, freezes the artifacts, and returns download links.

> The name is deliberate. **Approve means "I am going to submit this myself."**
> The system does not submit. There is no endpoint that submits. See
> `ARCHITECTURE.md` §3, invariant 1.

**`PATCH /review/{id}/plan`** — edit the tailoring plan before generation, so a
human correction is captured rather than worked around.

---

## 6. Applications and tracking

```http
GET   /api/v1/applications
GET   /api/v1/applications/{id}
PATCH /api/v1/applications/{id}
POST  /api/v1/applications/{id}/events
GET   /api/v1/applications/{id}/events
GET   /api/v1/metrics/funnel
GET   /api/v1/metrics/sources
POST  /api/v1/exports/spreadsheet
```

**`POST /applications/{id}/events`** — manual status entry, for anything that
happened off-email (a phone screen, a LinkedIn message). Sets `is_manual = true`
so automated and observed transitions stay distinguishable.

**`GET /metrics/funnel`** — group by `variant`, `tier`, `week`, `source_channel`.

```jsonc
{
  "data": [{
    "group": { "variant_key": "backend", "tier": "strong" },
    "submitted": 18, "withdrawn": 1, "responded": 6,
    "advanced": 3, "interviewed": 2, "offers": 0,
    "response_rate": 0.353, "interview_rate": 0.118
  }],
  "message": "OK",
  "meta": { "note": "Observed rates from your own history. Not a prediction." }
}
```

Counters come straight from `v_funnel` (`DATA_MODEL.md` §10). `responded`
excludes withdrawn applications and `withdrawn` is its own column, so every rate
is over the net denominator `submitted - withdrawn` — a withdrawal says nothing
about the employer's interest and belongs in neither numerator nor denominator
(`APPLICATION_PIPELINE.md` §8.2).

That `meta.note` is intentional and should not be removed. It is the system
stating plainly that these are measured outcomes, not a selection probability.

**`POST /exports/spreadsheet`** — 202. Writes an `.xlsx` of the full pipeline and
returns an artifact reference. Runs nightly on a schedule as well.

---

## 7. Runs, health and settings

```http
POST /api/v1/runs/discovery
GET  /api/v1/runs
GET  /api/v1/runs/{id}
POST /api/v1/runs/mail
GET  /api/v1/health
GET  /api/v1/settings
PATCH /api/v1/settings
```

**`POST /runs/discovery`** — 202, returns a `run_id`. Optional
`{ "source_ids": [...] }` to re-run a subset. Rejected with **409** if a
discovery run is already in flight — a Redis lock, not an advisory convention.

**`GET /runs/{id}`** — per-source results, so a failing adapter is visible
without reading logs:

```jsonc
{
  "data": {
    "id": "01JE…", "run_type": "discovery", "status": "completed_with_errors",
    "started_at": "2026-09-05T02:30:00Z", "finished_at": "2026-09-05T02:41:12Z",
    "stats": { "fetched": 4192, "new": 147, "filtered": 118,
               "extracted": 29, "scored": 29, "generated": 8,
               "validation_failures": 1, "llm_cost_inr": 63.40 },
    "source_results": [
      { "source_id": 77, "adapter": "workday", "status": "ok",
        "fetched": 214, "new": 6, "duration_ms": 2104 },
      { "source_id": 91, "adapter": "meta", "status": "error",
        "error": "HTTP 403", "duration_ms": 890 }
    ]
  },
  "message": "Completed with 1 source failure."
}
```

**`GET /health`** — liveness plus dependency checks (Postgres, Redis, LLM
provider reachability, Gmail token validity). Returns 200 with a per-dependency
status map; never returns 500 for a degraded dependency, since a degraded LLM
provider should not take the UI down.

---

## 8. What is deliberately absent

No endpoint exists, at any version, for:

- submitting an application to an employer,
- sending mail to any address other than the operator's own,
- fetching from a host on the never-scrape list,
- overriding a failed ledger validation.

These are not unimplemented features. They are the invariants from
`ARCHITECTURE.md` §3, expressed as an absence in the API surface — the most
reliable way to enforce a rule is to give it nowhere to be called from.
