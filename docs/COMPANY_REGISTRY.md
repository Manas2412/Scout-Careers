# COMPANY REGISTRY — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for ATS auto-detection from a careers URL, the tiering
and status models, the tag taxonomy, per-company defaults, company
deduplication, the Companies page, CSV bulk import, and ATS-migration
maintenance. `ARCHITECTURE.md` wins on system-level concerns and the invariants;
`DATA_MODEL.md` wins on the `company` and `source` columns; `API.md` wins on
endpoint paths, envelopes and error codes; `SOURCE_ADAPTERS.md` wins on adapter
behaviour, config models and probe semantics.

Module: `backend/src/scout_careers/registry/`. Frontend: `routes/Companies`.

---

## 1. What the registry is for

The registry is the input to the entire pipeline. Nothing is discovered that a
company row did not authorise, and every downstream decision — what gets polled,
what gets ranked highly, what gets a cover letter, what gets generated first —
reads from it.

It also has to survive being maintained by one person in the margins of a
working week. Three hundred companies (`ARCHITECTURE.md` §9) is a lot of rows to
curate by hand, so the design target for adding a company is **paste a careers
URL, confirm two guesses, save**. Everything in §2 exists to make that true, and
everything in §9 exists so the first forty rows arrive without any pasting at
all.

The registry is deliberately a **curated list, not a crawl**. There is no
company discovery crawler and no "find companies like this one" feature. The
operator decides who is worth applying to; the system's job is to watch that
list well.

---

## 2. ATS auto-detection

### 2.1 The flow

`POST /api/v1/companies/detect` (`API.md` §2) takes one URL and returns an
adapter, a validated config, a company-name guess and a probe result. It is
three steps, in strict order, and the order is the design:

```
    pasted URL
        │
   ①  POLICY GATE ───────────▶ host on NEVER_FETCH_HOSTS?
        │                        yes ⇒ 403 source.denied_by_policy, stop
        │                              (no request is made, ever)
        ▼
   ②  PATTERN MATCH ─────────▶ zero matches   ⇒ 422 source.undetectable
        │                      >1  matches    ⇒ 200 with candidates[] (§2.5)
        │  exactly one match
        ▼
   ③  LIVE PROBE ────────────▶ adapter.probe() — one request, ≤10 s
        │                      unreachable ⇒ 200 with reachable:false
        │                      reachable, 0 postings ⇒ 200 with a warning
        ▼
   ④  DEDUP CHECK ───────────▶ existing_company_id, existing_source_id
        │
        ▼
    detection result
```

Detection **never returns a config it has not probed**. A pattern match alone is
a guess about a URL shape; a probe is evidence that the endpoint exists and
returns postings. Writing an unprobed source is how a registry accumulates 40
quietly-dead rows that nobody notices for a month.

Detection is also **read-only**. It creates nothing. The operator sees what was
detected and confirms with `POST /companies` or
`POST /companies/{id}/sources`. This keeps a mistyped URL from silently
producing a company row.

### 2.2 The URL pattern table

Patterns are tried in order. Each yields `(adapter, config)` with named capture
groups mapped straight onto the adapter's config model
(`SOURCE_ADAPTERS.md` §2.5), which then validates them — a pattern that captures
a value the config model rejects is treated as a non-match, not an error.

| # | Host / path shape | Example | → adapter | → config |
|---|---|---|---|---|
| 1 | `boards.greenhouse.io/{token}` | `boards.greenhouse.io/stripe` | `greenhouse` | `{board_token: "stripe"}` |
| 2 | `job-boards.greenhouse.io/{token}` | `job-boards.greenhouse.io/anthropic` | `greenhouse` | `{board_token: "anthropic"}` |
| 3 | `boards-api.greenhouse.io/v1/boards/{token}/…` | direct API paste | `greenhouse` | `{board_token: …}` |
| 4 | `*.greenhouse.io/embed/job_board?for={token}` | legacy embed | `greenhouse` | `{board_token: …}` |
| 5 | `jobs.lever.co/{site}` | `jobs.lever.co/netflix` | `lever` | `{site: "netflix"}` |
| 6 | `api.lever.co/v0/postings/{site}` | direct API paste | `lever` | `{site: …}` |
| 7 | `jobs.ashbyhq.com/{name}` | `jobs.ashbyhq.com/openai` | `ashby` | `{board_name: "openai"}` |
| 8 | `api.ashbyhq.com/posting-api/job-board/{name}` | direct API paste | `ashby` | `{board_name: …}` |
| 9 | `{tenant}.{wdN}.myworkdayjobs.com/{locale}/{site}` | `adobe.wd5.myworkdayjobs.com/en-US/external_experienced` | `workday` | `{host, tenant, site, locale}` |
| 10 | `{tenant}.{wdN}.myworkdayjobs.com/{site}` | `adobe.wd5.myworkdayjobs.com/external_experienced` | `workday` | `{host, tenant, site}` |
| 11 | `{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | direct CXS paste | `workday` | `{host, tenant, site}` |
| 12 | `jobs.smartrecruiters.com/{id}` | `jobs.smartrecruiters.com/Visa` | `smartrecruiters` | `{company_id: "Visa"}` |
| 13 | `careers.smartrecruiters.com/{id}` | `careers.smartrecruiters.com/BoschGroup` | `smartrecruiters` | `{company_id: …}` |
| 14 | `api.smartrecruiters.com/v1/companies/{id}/postings` | direct API paste | `smartrecruiters` | `{company_id: …}` |
| 15 | `apply.workable.com/{account}` | `apply.workable.com/acme-inc` | `workable` | `{account: "acme-inc"}` |
| 16 | `{account}.workable.com` | legacy subdomain | `workable` | `{account: …}` |
| 17 | `{company}.recruitee.com` | `acme.recruitee.com` | `recruitee` | `{company: "acme"}` |
| 18 | `careers.google.com/*`, `google.com/about/careers/*` | any | `google` | `KeywordSearchConfig` defaults |
| 19 | `amazon.jobs/*`, `www.amazon.jobs/*` | any | `amazon` | `KeywordSearchConfig` defaults |
| 20 | `jobs.careers.microsoft.com/*`, `careers.microsoft.com/*` | any | `microsoft` | `KeywordSearchConfig` defaults |

**Normalisation before matching.** The URL is lower-cased on host, stripped of
query string and fragment, stripped of a trailing slash, and `www.` is removed
only where the pattern does not require it. A path segment that is a two- or
five-character locale (`en`, `en-US`, `en_us`, `global/en`) is removed before
site extraction — this is what lets patterns 9 and 10 collapse into one config.

**Tenant extraction for Workday** takes the leftmost label as `tenant` and the
full hostname as `host`. `tenant` is *usually* the same as the first label but
not always; where the CXS path (pattern 11) is available it is authoritative and
overrides the host-derived guess.

**Redirect following, once.** If the pasted URL matches nothing, detection issues
a single `GET` with `follow_redirects=True`, `max_redirects=3`, and re-runs the
table against the **final** URL. This resolves the common cases: a vanity
careers domain that CNAMEs or 302s to Workday (`careers.example.com` →
`example.wd3.myworkdayjobs.com/External`), a short link, and a marketing page
whose "See all openings" is a server-side redirect. The policy gate (§2.4)
re-runs on every hop; a redirect into a denied host is refused mid-chain.

**Body scanning, as a last resort.** If the final URL still matches nothing and
the response is HTML, detection scans the body for an embedded board — a
Greenhouse or Lever iframe `src`, an Ashby `job-board` script tag, a
`data-board-token` attribute, or an inline `boards.greenhouse.io/embed` URL.
Anything found is fed back through the pattern table. This is the path that
handles the very common "company careers page is a marketing page with an
embedded board" case, and it is why pattern 4 exists.

Body scanning is strictly bounded: one request, 512 KB read cap, HTML parsed
with `selectolax`, **no JavaScript execution**, no asset loading, no second hop.
If the board only exists after client-side rendering, detection fails and says
so. Rendering a page to find a board is browser automation, and this system does
not do browser automation for discovery (`SOURCE_ADAPTERS.md` §11).

### 2.3 The live probe

The matched adapter's `probe()` runs exactly once (`SOURCE_ADAPTERS.md` §2.4):
one request, no pagination, 10-second ceiling, honouring the same rate-limit
bucket, robots rules and user-agent policy as a real run.

```jsonc
// POST /api/v1/companies/detect
{ "url": "https://adobe.wd5.myworkdayjobs.com/external_experienced" }

// 200
{
  "data": {
    "adapter": "workday",
    "config": { "host": "adobe.wd5.myworkdayjobs.com",
                "tenant": "adobe", "site": "external_experienced" },
    "company_name_guess": "Adobe",
    "probe": { "reachable": true, "sample_count": 25, "latency_ms": 412,
               "http_status": 200, "detail": null },
    "existing_company_id": null,
    "existing_source_id": null,
    "warnings": []
  },
  "message": "Detected Workday board for Adobe."
}
```

**Outcomes and what the UI does with each:**

| Probe result | `message` | Save button |
|---|---|---|
| `reachable: true`, `sample_count > 0` | "Detected {adapter} board for {name}." | Enabled, primary |
| `reachable: true`, `sample_count == 0` | "Board reachable but currently lists no roles." | Enabled, with a warning — a genuinely empty small board is normal |
| `reachable: false`, 404 | "No board found at that token." | Disabled |
| `reachable: false`, 403 | "The board refused the request." | Disabled, and §2.4 note shown |
| `reachable: false`, timeout | "The board did not respond in time." | Enabled, with a warning — a slow tenant is worth retrying |
| `robots_denied` | "That board's robots.txt disallows automated access." | Disabled, not overridable |

**`company_name_guess`** comes from, in order: the adapter's own response
(`workday.hiringOrganization.name`, `smartrecruiters.company.name`,
`greenhouse.offices` context), then the HTML `<title>` or `og:site_name` of the
careers page if it was fetched, then a title-cased de-slug of the config token
(`external_experienced` is skipped as a site name; `adobe` becomes `Adobe`). It
is a *guess*, presented in an editable field, never written without confirmation.

A 403 from the probe is **not** treated as a transient failure to retry with a
different user agent. It is a refusal, and it is recorded as one
(`SOURCE_ADAPTERS.md` §4.5). The UI says the board refused the request and
offers manual import (`API.md` §3) instead.

### 2.4 The never-scrape refusal path

If the host — the pasted one, or any host in the redirect chain — is on
`NEVER_FETCH_HOSTS` (`SOURCE_ADAPTERS.md` §4.7), detection returns **403**
before any request is made:

```jsonc
// POST /api/v1/companies/detect
{ "url": "https://www.linkedin.com/company/acme/jobs/" }

// 403
{
  "data": null,
  "message": "linkedin.com is on the never-fetch list. Scout never requests it. Set up a LinkedIn job alert to your alerts mailbox instead, or import the role manually.",
  "meta": { "code": "source.denied_by_policy", "field": "url", "host": "linkedin.com" }
}
```

Properties of this path, all of them deliberate:

- **No request is issued.** The gate runs on the parsed host before the HTTP
  client is touched, so the refusal is not observable to the denied host.
- **It is not overridable.** No setting, environment variable, admin flag or
  request parameter turns it off. `NEVER_FETCH_HOSTS` is a frozen code constant,
  and a test asserts it is not reachable from `Settings`
  (`ARCHITECTURE.md` §3, invariant 4; `API.md` §8).
- **It applies to every URL field on the surface, not just detection.**
  `company.careers_url`, `company.website` and the manual-import URL are all
  validated against the same gate on write. A denied host can be *stored* as a
  human-clickable reference — the operator may well want the LinkedIn company
  page recorded — but is never fetched. Storing a link and following a link are
  separate acts, and only the second is forbidden.
- **The message routes the operator somewhere useful.** A refusal that ends the
  conversation gets worked around; a refusal that names the supported path
  (`mail_alert`, `SOURCE_ADAPTERS.md` §7) gets followed. The two supported
  answers are always: set up a job alert into the alerts mailbox, or paste the
  role into `POST /postings/import`.

### 2.5 Ambiguity

Three distinct ambiguities, three distinct behaviours. None of them guesses.

**(a) Multiple pattern matches.** Rare, but real: a URL containing both a vanity
host and a query parameter naming a board. Detection returns 200 with
`candidates[]` instead of a single result, and the UI renders a choice. Each
candidate carries its own probe, so the operator is choosing between two things
that were both tested:

```jsonc
{
  "data": {
    "candidates": [
      { "adapter": "greenhouse", "config": { "board_token": "acme" },
        "probe": { "reachable": true, "sample_count": 31, "latency_ms": 210 },
        "confidence": "high" },
      { "adapter": "workable", "config": { "account": "acme" },
        "probe": { "reachable": false, "http_status": 404, "latency_ms": 180 },
        "confidence": "low" }
    ],
    "company_name_guess": "Acme"
  },
  "message": "Two possible boards. Greenhouse is reachable; Workable is not."
}
```

`confidence` is `high` when the probe returned postings, `medium` when reachable
with zero postings, `low` when unreachable. Candidates are ordered by confidence
then by pattern order. The UI preselects the first, and it is still a
confirmation, not an auto-save.

**(b) Multiple boards, all correct.** A large employer legitimately runs several
sites — Workday experienced plus Workday campus, or a Greenhouse board plus a
separate Ashby board for a subsidiary. This is not ambiguity; it is the case
`source`'s `UNIQUE (company_id, adapter, config)` was designed for
(`DATA_MODEL.md` §3.2). Detection returns one result per paste; the operator
pastes each board URL and each becomes an additional `source` row on the same
company. The Companies page shows all of a company's sources with independent
health.

**(c) No match at all.** 422 with `source.undetectable`:

```jsonc
{
  "data": { "final_url": "https://careers.example.com/openings",
            "redirect_chain": ["https://example.com/careers"],
            "html_scanned": true, "embedded_board_found": false },
  "message": "No supported ATS found at that URL. If the board loads only after JavaScript runs, Scout cannot detect it automatically.",
  "meta": { "code": "source.undetectable", "field": "url" }
}
```

The response deliberately returns what it *did* — the redirect chain, whether
HTML was scanned — so the operator can tell "you didn't try" from "there is
nothing there". The offered next steps are: paste the inner board URL directly
if they can find it in the page's network tab, create the company with **no
source** and rely on `mail_alert` plus manual import, or add it as a
`manual`-only company.

A company with zero sources is a fully valid registry row. It still holds tier,
tags, defaults and a cover-letter policy, all of which apply to manually
imported and mail-discovered postings. This is how employers with bespoke
portals — a large share of Indian IT services and most government bodies — live
in the registry honestly rather than as a broken source that fails every night.

---

## 3. Tiering

`company.tier` is `dream | strong | volume` (`DATA_MODEL.md` §2). It is the
operator's judgement about how much of a scarce resource — attention, tokens,
and the willingness to write a real cover letter — a given employer deserves.

### 3.1 What each tier means

| Tier | Meaning | Expected count |
|---|---|---|
| **dream** | Would take the job over most alternatives. Worth a bespoke application and worth applying to a role that is a 60% fit. | 15–30 |
| **strong** | A genuinely good outcome. Worth applying when the fit is real. | 80–120 |
| **volume** | Worth knowing about. Apply when the match is strong and the effort is low. | The remainder |

`volume` is the default on creation (`DATA_MODEL.md` §3.1) because the cost of
mis-tiering upward is wasted tokens and a worse cover letter, while the cost of
mis-tiering downward is a slightly later look at a role that is still in the
queue. Defaults should fail cheap.

### 3.2 What tier actually changes

Tier is not decoration. It is read at four points in the pipeline:

| Stage | Effect |
|---|---|
| ④ FILTER | The minimum coverage threshold to survive filtering: `dream` 35%, `strong` 45%, `volume` 55%. A dream-tier role gets past a weaker fit because the operator wants to see it. |
| ⑦ RANK | Composite score multiplier — the tier weight `T` in the composite formula. `MATCH_SCORING.md` §5.1 is canonical for the value; at the time of writing it is `dream` ×1.10, `strong` ×1.00, `volume` ×0.90. Applied to the coverage-and-recency composite, so tier breaks a tie between comparable roles without letting tier outrank fit. |
| ⑧ GENERATE | Generation order within the daily budget. Dream-tier items are drafted first, so if the ≤10-draft budget (`ARCHITECTURE.md` §9) runs out, it runs out on volume-tier roles. |
| ⑧ GENERATE | Cover-letter policy interacts with tier — see §6.2. A `dream`-tier company with `cover_letter_worth = false` still gets a letter, because that combination means "their ATS has no field, but if I find a way to attach one, I want one." |

The multipliers are a setting (`SCORING_TIER_WEIGHTS`), not constants, because
they will need tuning against the funnel view (`DATA_MODEL.md` §10) once there is
history. Do not restate the numbers elsewhere — `MATCH_SCORING.md` §5.1 owns the
composite-score formula and every term in it, including `T`. The thresholds and
the generation ordering are not settings —
those are the semantics of the tier and changing them changes what the word
means.

### 3.3 Tier is not a proxy for company size

Explicit, because it is the obvious wrong inference. A twelve-person GovTech
non-profit doing work the operator cares about is legitimately `dream`; a
50,000-person enterprise with a strong brand and no interesting roles is
legitimately `volume`. Tier answers "would I take this job", not "is this a big
company". Company size lives in `size_band` and is descriptive only.

---

## 4. Status

`company.status` is `tracking | paused | blacklisted` (`DATA_MODEL.md` §2).

| Status | Sources polled | Postings ingested | Existing postings | Appears in queue |
|---|---|---|---|---|
| `tracking` | Yes | Yes | Visible | Yes |
| `paused` | **No** | No | Visible, marked stale | No new items |
| `blacklisted` | **No** | No | Visible, marked blacklisted | **No, ever** |

**`paused`** is temporary and reversible with no loss. Use it for a hiring
freeze, a company being reorganised, or an employer the operator has just
applied to and does not want to see again for a month. Postings already ingested
stay queryable — pausing is not deleting. The Companies page shows a paused
company's last poll time so it is obvious the data is ageing.

**`blacklisted`** is a decision, not a pause. Use it for an employer the operator
will not work for, a recruiter-mill that reposts the same requisition weekly, a
company that has already rejected them and should not resurface for six months,
or one whose ATS is so noisy it drowns the queue.

The gate lives at stage ④ of the pipeline, listed explicitly among the
deterministic filters (`ARCHITECTURE.md` §6: "company status ≠ blacklisted"), and
also at source selection so a blacklisted company's boards are not even fetched.
Two gates, because the second one saves the request and the first one is the
guarantee.

### 4.1 Why a blacklist is worth as much as a tracking list

This is the claim most likely to be dismissed, so it is argued rather than
asserted.

The system's binding constraint is not "how many roles can it find" — it finds
thousands. It is **how many roles a human can meaningfully look at**, which is
about thirty a day (`ARCHITECTURE.md` §9). Every slot in that thirty consumed by
a role the operator was never going to apply to is a slot that a real
opportunity did not get. The tracking list determines what enters the funnel;
the blacklist determines what stops re-entering it. On a daily cadence, over
months, the second compounds harder.

Concretely, the blacklist is what stops:

- **Re-surfacing after rejection.** Without it, the same company's near-identical
  requisition returns in the queue a month after a rejection, and the operator
  re-reads it before remembering. That is a pure attention tax with a negative
  expected value.
- **Requisition churn.** Some employers close and repost the same role on a
  cycle. Each repost is a new `external_id`, so it is legitimately a new posting
  by every rule the system has. The registry is the only place that knowledge can
  live.
- **Deliberate exclusions.** Employers the operator will not work for, for
  whatever reason. Encoding that once, in one place, means it never has to be
  re-decided at 8 a.m. in a digest.
- **Silent regression.** Because `blacklisted` is a stored status with a
  `notes` field rather than an unwritten habit of skipping, the reason survives
  the operator forgetting it, and a review six months later is a query, not an
  act of memory.

A blacklisted company is never deleted. Deleting it loses the reason and lets a
future paste or CSV import silently re-add it. §7.3 covers the collision.

---

## 5. Tags and location filters

### 5.1 Tag taxonomy

`company.tags` is `TEXT[]` with a GIN index (`DATA_MODEL.md` §3.1) — free-form at
the database level, disciplined by convention above it. The convention is
**`axis:value`**, lower-case, hyphenated, ASCII:

| Axis | Purpose | Values |
|---|---|---|
| `sector:` | What the company does | `fintech`, `ecommerce`, `saas`, `devtools`, `ai-research`, `enterprise-software`, `semiconductor`, `consulting`, `govtech`, `edtech`, `healthtech`, `gaming`, `media`, `logistics` |
| `stage:` | Maturity | `seed`, `series-a`, `series-b`, `series-c-plus`, `public`, `private-large`, `non-profit`, `government` |
| `geo:` | Where the operator would work | `india`, `bengaluru`, `delhi-ncr`, `hyderabad`, `pune`, `mumbai`, `remote-india`, `remote-global`, `emea`, `us` |
| `role:` | What the operator wants there | `ai-product`, `ai-platform`, `backend`, `data`, `solutions`, `consulting`, `product-management` |
| `origin:` | How it entered the registry | `seed`, `csv-import`, `mail-alert`, `manual` |
| `watch:` | Operator workflow | `applied-before`, `referral-available`, `revisit-q1`, `interviewed` |

Rules:

- **The axis prefix is enforced on write.** A tag without a `:` is rejected with
  422 `company.tag_invalid`. Untyped tags become a folksonomy within a month, and
  a folksonomy cannot be filtered on reliably.
- **The value list is *not* enforced.** New values are allowed on any known axis;
  new *axes* are not. This is the right place to be strict — axes are structure,
  values are vocabulary.
- **`role:` tags feed the default variant suggestion** (§6.1) and nothing else
  automatic. They are a hint, not a scoring input; scoring reads requirements
  (`MATCH_SCORING.md`).
- **`origin:` is written by the system**, not the operator, so "where did these
  180 rows come from" is a query.
- A settings page lists every tag in use with its company count, which is the
  cheap way to notice `sector:fin-tech` sitting next to `sector:fintech`.

### 5.2 Location filters

`company.location_filter` is `TEXT[]`; empty means "accept all locations for this
company" (`DATA_MODEL.md` §3.1).

It is applied at stage ④ against the normalised `location_city`,
`location_country` and `is_remote` from `SOURCE_ADAPTERS.md` §9.2. Accepted
token forms:

| Token | Matches |
|---|---|
| `Bengaluru` | `location_city == 'Bengaluru'` after alias normalisation |
| `IN` | `location_country == 'IN'` |
| `remote` | `is_remote == true`, any country |
| `remote:IN` | `is_remote == true AND location_country == 'IN'` |
| `!Chennai` | Negation — excludes that city even if another token matches |

Semantics: **positive tokens OR together; negative tokens veto.** A posting with
an unresolvable location (`location_city IS NULL`, §9.2 of
`SOURCE_ADAPTERS.md`) **passes** any non-empty filter rather than failing it.
That is a deliberate choice of false positives over false negatives: a role
listed as "Multiple Locations" that the operator never sees is invisible, while
one they see and dismiss costs two seconds.

There is also a global default, `settings.default_location_filter`, applied to
companies with an empty `location_filter`. Per-company values override it
entirely rather than intersecting — a company-level filter is a statement about
that company, and intersecting two filters produces surprises nobody can debug.

---

## 6. Per-company defaults

Four fields on `company` that change downstream behaviour without any per-posting
decision.

### 6.1 `default_variant_id`

The resume variant used when scoring cannot separate the field —
`match_score` rows within `settings.variant_tie_epsilon` (default 3 percentage
points) of each other. Without it, a near-tie resolves by row order, which is
arbitrary and unstable across runs.

It is a **tiebreak, not an override.** A variant that clearly scores higher wins
regardless. Setting `default_variant_id = consulting` on Seagate does not stop
`backend` from being recommended for a backend role there.

The add-company flow suggests a variant from `role:` tags
(`role:ai-product → ai_product`, `role:backend → backend`,
`role:consulting → consulting`) with a single click to accept. Null is valid and
means "no tiebreak preference"; ties then resolve by `resume_variant.id` and the
review item is flagged `tie` so the operator can see it happened.

### 6.2 `cover_letter_worth`

Defaults true; set false for employers whose ATS has no cover-letter field —
most large tech (`DATA_MODEL.md` §3.1). Generation skips the letter for those,
saving tokens on letters nobody reads.

The interaction with tier, stated once so it is not re-litigated:

| Tier | `cover_letter_worth` | Generated? |
|---|---|---|
| any | `true` | Yes |
| `volume`, `strong` | `false` | No |
| `dream` | `false` | **Yes** — a short one, marked "no ATS field; use if you find a route in" |

The dream-tier exception exists because "there is no upload field" and "a letter
would not help" are different statements, and for fifteen to thirty employers
the operator cares most about, the second is not true.

The add-company flow **pre-fills this from the detected adapter**, because ATS
choice predicts it well: `workday`, `amazon`, `microsoft` and `google` default to
`false`; `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable` and
`recruitee` default to `true`. It is a pre-fill on an editable field, never a
lock.

### 6.3 Poll interval

`source.poll_interval_minutes` lives on the source, not the company
(`DATA_MODEL.md` §3.2), because one company can hold boards of very different
volatility. The registry sets it, seeded from the adapter's
`default_poll_interval_minutes` (`SOURCE_ADAPTERS.md` §3) and then adjusted by
tier:

| Tier | Interval | Effect |
|---|---|---|
| `dream` | 720 (12 h) | Polled on both the morning run and a midday pass |
| `strong` | 1440 (24 h) | Once daily |
| `volume` | 1440 (24 h) | Once daily |
| any, `paused` | n/a | Not polled |

A source is due when `last_run_at IS NULL OR last_run_at < now() - interval`,
which is exactly what `source_due_idx` supports. Nothing polls faster than 12
hours. Even for a dream-tier employer, the marginal value of learning about a
role six hours sooner is close to zero, and the cost — request volume against a
board that did not ask to be polled twice — is real (invariant 8).

`volume`-tier sources whose last five runs all returned `unchanged` are stepped
to 2880 minutes automatically, and stepped back to 1440 the first time they
change. This is a pure load reduction on the long tail and it is reported in the
health table so it is never a silent behaviour change.

### 6.4 Editing defaults in bulk

All four are bulk-editable from the Companies page (§7.3). Changing
`default_variant_id` or `cover_letter_worth` does **not** retroactively regenerate
anything; it applies to the next run. Re-scoring an existing posting is an
explicit act (`POST /postings/{id}/rescore`, `API.md` §3), because a bulk edit
silently triggering 200 LLM extractions is a cost incident.

---

## 7. The Companies page

Route `frontend/src/routes/Companies`. Desktop-first — this is a management
surface, and it is the only screen in the product where a wide table is the right
answer.

### 7.1 Table columns

| Column | Content | Sort |
|---|---|---|
| Company | Name, slug beneath in mono, favicon from `website` domain | Name |
| Tier | Pill: dream / strong / volume | Tier order |
| Status | Pill: tracking / paused / blacklisted | Status |
| Sources | One health dot per source with adapter name; hover for `describe()` and last error | Source count |
| Open roles | `stats.open_postings` | Numeric |
| New (7d) | `stats.new_this_week`, emphasised when > 0 | Numeric |
| Applications | `stats.applications`, linking to the filtered Applications view | Numeric |
| Cover letter | Yes / No / Dream-override | — |
| Default variant | Variant key, mono | — |
| Last polled | Relative (`4h ago`), red past 2× the poll interval | Timestamp |
| Tags | Up to three chips, `+N` overflow | — |

Default sort is `new_this_week DESC, name ASC` — the operator's actual question
on opening this page is "who has something new".

### 7.2 Filters

Mapping directly to `GET /companies` (`API.md` §2): `status`, `tier`, `tag`,
`adapter`, `q` (trigram name search), `has_new`. Plus two client-side toggles
computed from the same payload, because they are the two questions the health
data exists to answer:

- **Needs attention** — any source with `consecutive_failures > 0`, or
  `enabled = false`, or `last_status = 'auto_disabled'`, or last polled more than
  2× its interval ago.
- **No source** — companies relying entirely on `mail_alert` and manual import.
  This list is the backlog of detection work, and keeping it visible is what
  stops it becoming permanent.

Filters compose with AND and are reflected in the URL, so a filtered view is a
bookmark.

### 7.3 Bulk actions

Row checkboxes with a select-all that respects the active filter. Available:

| Action | Endpoint | Notes |
|---|---|---|
| Set tier | `PATCH /companies/{id}` per row | Immediate |
| Set status | `PATCH /companies/{id}` per row | Blacklisting prompts for a `notes` reason; the reason is required |
| Add / remove tag | `PATCH /companies/{id}` | Axis validation applies |
| Set cover-letter policy | `PATCH /companies/{id}` | Not retroactive (§6.4) |
| Set default variant | `PATCH /companies/{id}` | Not retroactive |
| Enable / disable sources | `PATCH /sources/{id}` | Resets `consecutive_failures` on enable |
| Re-test sources | `POST /sources/{id}/test` | Rate-limited client-side to 4 concurrent |
| Export CSV | Client-side | Same column set as the import format (§10), so export → edit → re-import round-trips |

There is deliberately **no bulk delete**. See §11.2 — deleting a company cascades
to its sources and postings, and a mis-clicked bulk delete on a filtered view is
unrecoverable. Bulk *blacklist* covers every legitimate case.

### 7.4 The add-company flow

One dialogue, three steps, designed to complete in under fifteen seconds.

```
┌─ Add company ─────────────────────────────────────────────┐
│                                                            │
│  Careers URL                                               │
│  ┌──────────────────────────────────────────────┐  ┌────┐ │
│  │ https://adobe.wd5.myworkdayjobs.com/exter…   │  │ ⟶  │ │
│  └──────────────────────────────────────────────┘  └────┘ │
│                                                            │
│  ── detected ───────────────────────────────────────────   │
│  ● Workday · adobe / external_experienced                  │
│    reachable · 25 postings in sample · 412 ms              │
│                                                            │
│  Name          [ Adobe                          ]          │
│  Tier          ( ) dream   ( ) strong   (•) volume         │
│  Tags          [ sector:enterprise-software × ] [ + ]      │
│  Default var.  [ ai_platform          ▾ ]                  │
│  Cover letter  [ ] worth writing   (pre-filled from ATS)   │
│  Locations     [ Bengaluru × ] [ remote:IN × ] [ + ]       │
│                                                            │
│  ⚠ Similar company already tracked: "Adobe Inc." (id 42)   │
│     ( ) Add as a new company   (•) Add board to Adobe Inc. │
│                                                            │
│                              [ Cancel ]  [ Add company ]   │
└────────────────────────────────────────────────────────────┘
```

Sequence: paste → `POST /companies/detect` fires on blur or Enter → the detected
block, name, and cover-letter pre-fill populate → the operator adjusts tier and
tags → save issues `POST /companies` (or `POST /companies/{id}/sources` when the
duplicate banner's second option is selected).

Everything below the detected block is editable. Nothing below it is required
except the name. Tier defaults to `volume`, which is the cheap failure (§3.1).

The duplicate banner is §8's output, rendered inline rather than as a post-save
error. Catching it before the write is what keeps the registry clean; catching it
after produces a merge task nobody does.

### 7.5 Source health indicator

Per source, a dot plus a short label, driven by `source.last_status`,
`consecutive_failures` and `enabled` (`DATA_MODEL.md` §3.2) — populated from the
last run's `source_results` (`SOURCE_ADAPTERS.md` §10.3):

| State | Dot | Label | Condition |
|---|---|---|---|
| Healthy | green | `ok · 214 roles` | `last_status = 'ok'`, `consecutive_failures = 0` |
| Empty | grey | `empty · N runs` | `last_status = 'empty'` — separate from ok, on purpose |
| Degraded | amber | `2 failures` | `1 ≤ consecutive_failures < 5` |
| Auto-disabled | red | `disabled after 5 failures` | `enabled = false`, `last_status = 'auto_disabled'` |
| Throttled | blue | `rate limited` | `last_status = 'rate_limited'` — our back-pressure, not their fault |
| Policy-blocked | red | `robots disallow` | `last_status = 'robots_denied'` |
| Never run | grey | `not yet polled` | `last_run_at IS NULL` |

Hovering shows `describe()`, `last_run_at`, and `last_error` verbatim — the
curated, capped, non-sensitive string from `SOURCE_ADAPTERS.md` §10.3, never a
stack trace.

The row action on a red or amber dot is **Re-test** (`POST /sources/{id}/test`),
which runs the same probe as detection and, on success, clears
`consecutive_failures` and re-enables the source. That single button is the whole
recovery path for the common case, and it is why detection and probing share one
code path.

`empty` deserves its own state because a board returning zero postings is usually
correct and occasionally means a token changed and the vendor answers with an
empty array rather than a 404. `empty · 7 runs` on a company that clearly hires
is the signal to re-detect.

---

## 8. Company deduplication

Three hundred rows accumulated from seeds, CSV imports, mail-alert promotions and
manual adds will collide. Detecting it at write time is cheap; merging afterwards
is not.

### 8.1 Signals, in precedence order

**1. Slug — exact, authoritative.** `company.slug` is `UNIQUE`
(`DATA_MODEL.md` §3.1). Generated as a lower-cased, ASCII-folded, hyphenated form
of the name with corporate suffixes stripped (`inc`, `ltd`, `pvt`, `private`,
`limited`, `llp`, `plc`, `gmbh`, `corp`, `corporation`, `technologies`,
`technology`, `labs`, `co`). `Adobe Inc.` and `Adobe` both slug to `adobe`, which
is the point. A slug collision is a definite duplicate — the API returns **409
`company.duplicate`** with the existing ID.

**2. Registrable domain — strong.** Extracted from `website`, and from
`careers_url` where the careers host is not a shared ATS host (a `careers_url` of
`boards.greenhouse.io/acme` says nothing about the company's domain; one of
`careers.acme.com` says a great deal). Compared on the registrable domain via the
public suffix list, so `acme.com`, `www.acme.com` and `jobs.acme.com` all match,
while `acme.co.in` is treated as distinct unless the operator says otherwise. A
domain match on differing slugs raises the duplicate banner but does not block.

**3. Trigram name similarity — advisory.** `pg_trgm` over
`company_name_trgm_idx` (`DATA_MODEL.md` §3.1):

```sql
SELECT id, name, slug, similarity(name, :candidate) AS sim
FROM company
WHERE deleted_at IS NULL
  AND name % :candidate                      -- pg_trgm.similarity_threshold
ORDER BY sim DESC
LIMIT 5;
```

`pg_trgm.similarity_threshold` is set to **0.45** for this query — chosen to
catch `Adobe` / `Adobe Systems`, `Zoho` / `Zoho Corporation`, `HPE` /
`Hewlett Packard Enterprise` (which it will *not* catch, and that is fine; the
domain signal does), while not merging `Zeta` and `Zetta`.

**4. Existing source config — definitive for boards.** Before creating a source,
`(adapter, config)` is checked across *all* companies, not just the target. The
same Greenhouse board token cannot belong to two companies. A hit returns **409
`company.duplicate_source`** with `field: "careers_url"` — the exact example in
`API.md` §1.

### 8.2 Behaviour

- **Slug collision** → 409, blocking. The UI offers "add this board to the
  existing company" as the one-click resolution.
- **Domain or trigram match** → 200, non-blocking, with `possible_duplicates[]`
  in the detect response and the inline banner in §7.4. The operator chooses.
  Automatic merging on a fuzzy signal is how `Zeta` and `Zetta` become one row
  and stay wrong forever.
- **CSV import** (§10) applies the same checks per row in dry-run and reports
  them, rather than failing the file.

### 8.3 Blacklisted collisions

If the matched existing company is `blacklisted`, the response says so
prominently and the default action is **Cancel**, not merge:

```jsonc
{
  "data": null,
  "message": "Acme Corp is blacklisted (\"reposts the same requisition weekly — 2026-06-11\"). Adding this board would resume tracking it.",
  "meta": { "code": "company.blacklisted_match", "company_id": 118 }
}
```

409, and unblocking it is an explicit status change on the existing company, not
a side effect of adding a board. This is the mechanism that makes §4.1's claim
true: a blacklist that a later paste can silently undo is not a blacklist.

### 8.4 Merging

There is no merge endpoint in v1. If two rows for one employer exist, the
resolution is: move the sources with `PATCH /sources/{id}` (which accepts
`company_id`), then soft-delete the emptied company. Postings already ingested
under the old company keep their `company_id` and are re-associated by a
one-off script, which is acceptable at this scale and better than shipping a
merge feature that has to handle applications, artifacts and events correctly.

This is a stated deferral, not an oversight. If merging becomes routine, the
right fix is better duplicate detection at write time, not a merge tool.

---

## 9. Seeding

A seed script (`DATA_MODEL.md` §11: seed data ships as an idempotent seed script,
not as a migration) creates roughly forty companies covering the operator's four
target segments. Idempotent on `slug`: re-running updates nothing that the
operator has since edited, and inserts only what is missing.

**The `expected adapter` column below is a detection hint, not an assertion.**
ATS assignments drift, and several of these employers run bespoke portals. The
seed script pastes each `careers_url` through the same detection path a human
would use (§2) and writes the source it actually probes. Where detection fails,
the company is created with **no source** and tagged `origin:seed`, which is a
valid and expected outcome (§2.5c) — the row still governs manual imports and
mail-alert matching. Every company here is real and its careers presence is
publicly reachable; the adapter guess is what gets verified.

### 9.1 Indian technology and product

| Company | Expected adapter | Tier | Tags |
|---|---|---|---|
| Zerodha | detect (bespoke likely) | strong | `sector:fintech`, `stage:private-large`, `geo:bengaluru` |
| Razorpay | detect | strong | `sector:fintech`, `stage:series-c-plus`, `geo:bengaluru` |
| CRED | detect | strong | `sector:fintech`, `stage:series-c-plus`, `geo:bengaluru` |
| Swiggy | detect | volume | `sector:logistics`, `stage:public`, `geo:bengaluru` |
| Zomato / Eternal | detect | volume | `sector:logistics`, `stage:public`, `geo:delhi-ncr` |
| Flipkart | detect (Workday likely) | strong | `sector:ecommerce`, `stage:private-large`, `geo:bengaluru` |
| Meesho | detect | volume | `sector:ecommerce`, `stage:series-c-plus`, `geo:bengaluru` |
| PhonePe | detect | strong | `sector:fintech`, `stage:private-large`, `geo:bengaluru` |
| Groww | detect | volume | `sector:fintech`, `stage:series-c-plus`, `geo:bengaluru` |
| Juspay | detect | volume | `sector:fintech`, `stage:series-c-plus`, `geo:bengaluru` |
| Postman | detect | strong | `sector:devtools`, `stage:series-c-plus`, `geo:bengaluru` |
| BrowserStack | detect | strong | `sector:devtools`, `stage:series-c-plus`, `geo:mumbai` |
| Hasura | detect | volume | `sector:devtools`, `stage:series-b`, `geo:bengaluru` |
| Freshworks | detect | volume | `sector:saas`, `stage:public`, `geo:chennai` |
| Zoho | detect (bespoke likely) | volume | `sector:saas`, `stage:private-large`, `geo:chennai` |
| Atlan | detect | strong | `sector:devtools`, `stage:series-b`, `geo:delhi-ncr` |
| InMobi | detect | volume | `sector:media`, `stage:private-large`, `geo:bengaluru` |
| Dream11 | detect | volume | `sector:gaming`, `stage:private-large`, `geo:mumbai` |

### 9.2 Global product and enterprise

| Company | Expected adapter | Tier | Tags |
|---|---|---|---|
| Adobe | `workday` | dream | `sector:enterprise-software`, `stage:public`, `role:ai-platform` |
| Nvidia | `workday` | dream | `sector:semiconductor`, `stage:public`, `role:ai-platform` |
| Salesforce | `workday` | strong | `sector:enterprise-software`, `stage:public` |
| Dell Technologies | `workday` | volume | `sector:enterprise-software`, `stage:public` |
| Cisco | `workday` | volume | `sector:enterprise-software`, `stage:public` |
| HPE | `workday` | volume | `sector:enterprise-software`, `stage:public` |
| Google | `google` | dream | `sector:saas`, `stage:public`, `role:ai-product` |
| Amazon / AWS | `amazon` | strong | `sector:ecommerce`, `stage:public`, `role:solutions` |
| Microsoft | `microsoft` | dream | `sector:enterprise-software`, `stage:public`, `role:ai-product` |
| Stripe | `greenhouse` | dream | `sector:fintech`, `stage:private-large` |
| Databricks | `greenhouse` | dream | `sector:devtools`, `stage:private-large`, `role:ai-platform` |
| Figma | `greenhouse` | strong | `sector:saas`, `stage:public` |
| Netflix | `lever` | strong | `sector:media`, `stage:public` |
| Visa | `smartrecruiters` | volume | `sector:fintech`, `stage:public` |
| Walmart Global Tech | `workday` | volume | `sector:ecommerce`, `stage:public`, `geo:bengaluru` |
| Uber | detect | volume | `sector:logistics`, `stage:public`, `geo:bengaluru` |

Meta and Apple are **deliberately absent**. Both are deferred adapters
(`SOURCE_ADAPTERS.md` §11); seeding them would create two companies whose sources
fail every night. If the operator wants them tracked, they are added with no
source and covered by `mail_alert` and manual import.

### 9.3 AI-first

| Company | Expected adapter | Tier | Tags |
|---|---|---|---|
| OpenAI | `ashby` | dream | `sector:ai-research`, `role:ai-product` |
| Anthropic | `greenhouse` | dream | `sector:ai-research`, `role:ai-product` |
| Perplexity | `ashby` | strong | `sector:ai-research`, `stage:series-c-plus` |
| Sarvam AI | detect | dream | `sector:ai-research`, `geo:bengaluru`, `role:ai-platform` |
| Krutrim | detect | strong | `sector:ai-research`, `geo:bengaluru` |
| Cohere | detect | strong | `sector:ai-research` |
| Scale AI | detect | volume | `sector:ai-research` |
| Hugging Face | detect | strong | `sector:devtools`, `sector:ai-research` |

### 9.4 GovTech and public-interest technology

Weighted deliberately. The operator's strongest and most citable work is
government-adjacent (`CLAIMS_LEDGER.md`), and it is the segment where a claims
ledger built on public-sector delivery converts best.

| Company | Expected adapter | Tier | Tags |
|---|---|---|---|
| eGov Foundation | detect | dream | `sector:govtech`, `stage:non-profit`, `geo:bengaluru` |
| Samagra — Transforming Governance | detect | dream | `sector:govtech`, `stage:non-profit`, `geo:delhi-ncr` |
| Wadhwani AI | detect | dream | `sector:govtech`, `sector:ai-research`, `stage:non-profit`, `geo:mumbai` |
| EkStep Foundation / Sunbird | detect | strong | `sector:govtech`, `sector:edtech`, `stage:non-profit` |
| Rocket Learning | detect | strong | `sector:edtech`, `stage:non-profit`, `geo:delhi-ncr` |
| Digital Green | detect | volume | `sector:govtech`, `stage:non-profit` |
| Gram Vaani | detect | volume | `sector:govtech`, `stage:non-profit`, `geo:delhi-ncr` |
| Karya | detect | volume | `sector:ai-research`, `stage:non-profit`, `geo:bengaluru` |

Total: 50 rows, of which roughly 20 are expected to resolve to a real adapter on
first detection and the rest to arrive with no source and be filled in over the
first fortnight. That distribution is the honest expectation and the reason
"companies with no source" is a first-class filter (§7.2), not an error state.

---

## 10. CSV bulk import

For adding thirty companies from a spreadsheet without thirty pastes.

### 10.1 Format

UTF-8, comma-delimited, header row required, column order irrelevant. Only
`name` is mandatory.

```csv
name,careers_url,website,tier,status,tags,cover_letter_worth,default_variant,location_filter,hq_location,size_band,notes
Adobe,https://adobe.wd5.myworkdayjobs.com/external_experienced,https://adobe.com,dream,tracking,"sector:enterprise-software;stage:public",false,ai_platform,"Bengaluru;remote:IN",San Jose,10000+,
Sarvam AI,https://jobs.ashbyhq.com/sarvam,https://sarvam.ai,dream,tracking,"sector:ai-research;geo:bengaluru",true,ai_product,Bengaluru,Bengaluru,50-200,Met their team at an event
Zoho,,https://zoho.com,volume,tracking,"sector:saas;geo:chennai",true,,IN,Chennai,10000+,Bespoke portal — mail alerts only
```

| Column | Required | Notes |
|---|---|---|
| `name` | **yes** | Slug is derived (§8.1); a `slug` column is accepted and overrides |
| `careers_url` | no | Empty means create the company with no source — a valid row |
| `website` | no | Used for domain-based dedup (§8.1) |
| `tier` | no | `dream`/`strong`/`volume`, default `volume` |
| `status` | no | Default `tracking`; `blacklisted` requires `notes` |
| `tags` | no | Semicolon-separated, axis-prefixed, validated (§5.1) |
| `cover_letter_worth` | no | `true`/`false`; default from the detected adapter (§6.2) |
| `default_variant` | no | A `resume_variant.key`, not an ID |
| `location_filter` | no | Semicolon-separated tokens (§5.2) |
| `hq_location`, `size_band`, `notes` | no | Free text |

Semicolons rather than commas inside multi-value cells, so the file survives being
opened and re-saved by a spreadsheet without quoting damage.

### 10.2 The two-phase run

**Phase 1 — dry run, always.** The file is parsed, every row validated, detection
run for every non-empty `careers_url`, and every dedup check performed. Nothing
is written. The result is a per-row report:

```jsonc
{
  "data": {
    "total_rows": 34,
    "will_create": 27,
    "will_add_source_to_existing": 3,
    "blocked": 2,
    "warnings": 6,
    "rows": [
      { "row": 2, "name": "Adobe", "action": "create",
        "detected": { "adapter": "workday", "sample_count": 25 } },
      { "row": 7, "name": "Adobe Systems", "action": "blocked",
        "code": "company.duplicate",
        "message": "Slug 'adobe' already used by row 2." },
      { "row": 11, "name": "Acme", "action": "create",
        "code": "source.undetectable",
        "message": "No supported ATS at that URL. Company will be created without a source." },
      { "row": 19, "name": "Beta Corp", "action": "blocked",
        "code": "source.denied_by_policy",
        "message": "careers_url points at linkedin.com." }
    ]
  },
  "message": "27 to create, 3 sources to add, 2 blocked."
}
```

**Phase 2 — commit.** The operator reviews and confirms. Rows are applied in a
single transaction per row, not one transaction for the file: a bad row must not
roll back thirty good ones. The response repeats the report with actual outcomes
and IDs.

### 10.3 Rules

- **Detection probes are rate-limited.** A 200-row file would otherwise fire 200
  probes at once. Import runs detection at `settings.source_concurrency` (8) with
  the same Redis buckets as a real run (`SOURCE_ADAPTERS.md` §4.3), so a file with
  40 Greenhouse boards does not burst the shared Greenhouse bucket. A large file
  is therefore a 202 with a job ID, not a synchronous call.
- **Never-scrape hosts are refused per row**, not per file. One bad row is
  blocked and reported; the rest proceed.
- **Re-import is idempotent on slug.** An existing slug becomes an update of only
  the columns present in the file — absent columns are not nulled. Export → edit
  two columns → re-import is the intended maintenance loop, and it must not wipe
  the columns the spreadsheet does not know about.
- **Blacklisted matches are blocked, never silently reactivated** (§8.3).
- Every imported row is tagged `origin:csv-import` automatically.

---

## 11. Maintenance

### 11.1 When a company migrates ATS

The most common registry failure, and it announces itself in one of three ways:

| Symptom | What happened |
|---|---|
| `http_error` / 404, then auto-disabled after 5 runs | Board token retired outright |
| `empty` for several consecutive runs on a company that clearly hires | Vendor returns an empty array instead of a 404 for a dead tenant |
| `ok` but `open_postings` collapses and stays low | Partial migration — one department moved, the old board is a rump |

The second is why `empty` is a distinct status (§7.5, `SOURCE_ADAPTERS.md`
§10.2). A migration that returns 404 is loud; one that returns `{"jobs": []}` is
silent, and silence is the failure mode that costs weeks.

**Procedure:**

1. Open the company's careers page in a browser and find the real board URL.
2. Paste it into detect (`POST /companies/detect`). The duplicate check reports
   `existing_company_id`, so the flow offers "add board to Adobe" rather than
   creating a second row.
3. Add the new source to the **same** company.
4. **Disable the old source. Do not delete it.** `PATCH /sources/{id}` with
   `enabled: false`. §11.2 explains why this is not merely a preference.
5. Set the new source's `poll_interval_minutes` from the company's tier (§6.3).
6. Confirm the next run: the new source `ok` with a plausible count, the old one
   `disabled`, and `open_postings` recovered.
7. Annotate `company.notes` with the date and the migration. In six months this
   is the only record of why there are two sources.

**What happens to postings on the old source.** They keep their identity and
history. They stop being seen, but the two-run close rule is scoped per source
and only advances on runs where that source returned `ok` or `empty`
(`SOURCE_ADAPTERS.md` §10.5) — a disabled source never returns either, so its
postings are **not** auto-closed. They are marked stale in the UI after 30 days
without a sighting. Roles that genuinely moved to the new board arrive with new
`(source_id, external_id)` identities and are collapsed against the old ones by
the cross-source dedup key `(company_id, normalised_title, location_city)`
(`DATA_MODEL.md` §4.1), with the new record winning on equal fidelity by the
earlier-seen tiebreak (`SOURCE_ADAPTERS.md` §8). The operator sees one role, not
two.

### 11.2 Why a source is disabled and never deleted

`job_posting.source_id` is `REFERENCES source(id) ON DELETE CASCADE`
(`DATA_MODEL.md` §4.1). Deleting a source therefore deletes every posting ever
discovered through it. Two consequences, both bad:

- **If applications exist**, `application.posting_id` references `job_posting`
  without a cascade, so the delete raises a foreign-key violation and fails —
  noisily, but only after the operator has already decided to delete.
- **If no applications exist**, the delete succeeds and silently destroys the
  discovery history for that employer: `first_seen_at` data, closed roles,
  everything the funnel view would later slice on.

`DELETE /api/v1/sources/{id}` (`API.md` §2) is therefore guarded: it returns
**409 `source.has_postings`** whenever `job_posting` rows exist for that source,
with a message pointing at `enabled: false`. Deletion is available only for a
source that has never successfully run — the mistyped-config case, which is the
only case where deletion is what the operator actually means.

The same reasoning applies one level up. `DELETE /api/v1/companies/{id}` is a
**soft delete** (`company.deleted_at`, `DATA_MODEL.md` §1), not a row removal,
for exactly this reason. A soft-deleted company's slug remains taken, which also
prevents a later CSV import from recreating it as a fresh row and losing the
association.

### 11.3 Routine registry hygiene

A short list, because a registry that is never reviewed silently decays into a
list of dead boards:

| Cadence | Action |
|---|---|
| Daily, in the digest | Source failures and newly auto-disabled sources are listed. Acting on them is the intended two-minute morning task. |
| Weekly | Work the **Needs attention** filter (§7.2) to zero. Re-test, re-detect or disable. |
| Monthly | Work the **No source** filter. Any company that has been sourceless for two months either gets a detection attempt or an explicit `notes` entry saying why it never will. |
| Monthly | Review `empty · N runs` sources with N > 10 against the company's live careers page. |
| Quarterly | Re-read the blacklist. Reasons expire — a company that rejected the operator in March is a legitimate target in December. |
| Quarterly | Re-tier against the funnel view (`DATA_MODEL.md` §10). A `volume` company with two interviews is mis-tiered, and the funnel is the only honest evidence available. |
| Quarterly | Re-run detect against the deferred employers (`SOURCE_ADAPTERS.md` §11). A migration to a supported ATS is the trigger to write nothing and simply add a source. |

Every one of these is a filtered view of the Companies page. None requires a
report, a script or a new screen — which is the reason the filters in §7.2 are
the ones they are.

---

## 12. Related documents

| Document | Covers |
|---|---|
| `ARCHITECTURE.md` | Invariants, pipeline stages, scale envelope |
| `DATA_MODEL.md` | `company` and `source` columns, indexes, enums, seed policy |
| `API.md` | `/companies`, `/companies/detect`, `/sources/{id}/test`, error codes |
| `SOURCE_ADAPTERS.md` | Adapter protocol, config models, probe, fidelity, never-fetch list |
| `MATCH_SCORING.md` | How tier multipliers and filter thresholds enter ranking |
| `DOCUMENT_GENERATION.md` | How `cover_letter_worth` and `default_variant_id` are consumed |
| `EMAIL_INGESTION.md` | The alerts mailbox and mail-alert company matching |
| `DATA_SOURCES_AND_COMPLIANCE.md` | Legal basis per source and the deny list |
