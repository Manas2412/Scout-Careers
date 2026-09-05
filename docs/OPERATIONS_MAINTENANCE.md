# OPERATIONS & MAINTENANCE — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for operating cadence, health thresholds, retention
execution and maintenance procedure. `ARCHITECTURE.md` wins on system-level
concerns, `DATA_MODEL.md` on schema, `API.md` on endpoints. Where a per-module
document states a cadence (`COMPANY_REGISTRY.md` §11.3, `CLAIMS_LEDGER.md` §9.4),
this file collects it into one schedule and does not contradict it.

---

## 1. The operating contract

Scout Careers is designed to cost its operator **ten minutes a day**
(`ARCHITECTURE.md` §1.1). Everything in this document exists to protect that
number. A maintenance task that cannot be done inside the daily ten minutes is
scheduled weekly; one that cannot be done weekly is scheduled monthly; one that
cannot be done monthly is automated or deleted.

The system runs itself. The operator's job is three things and nothing else:

1. **Decide.** Approve or skip what is in the review queue.
2. **Submit.** Upload the artifacts to the employer's site, by hand
   (`ARCHITECTURE.md` §3, invariant 1).
3. **Keep the inputs honest.** The claims ledger and the company registry are
   the two inputs the system cannot maintain for itself. Everything else is
   recoverable; a rotten ledger is not (§9).

Operational failure in this system almost never looks like an outage. It looks
like **quiet degradation**: sources auto-disabling one at a time until discovery
covers half the companies it thinks it covers, and a ledger that has drifted
into claims the operator can no longer defend. Both are silent, both are
detectable from the digest, and both are what the checklists below are for.

---

## 2. The daily rhythm

### 2.1 Timetable

All times IST (`ARCHITECTURE.md` §8). The scheduler is APScheduler with a
Postgres job store, so a restart does not lose a schedule.

| Time | Job | `run_type` | Budget | Reference |
|---|---|---|---|---|
| 08:00 | Discovery run — stages ① → ⑩ | `discovery` | 15 min wall clock | `ARCHITECTURE.md` §6 |
| 08:10 | Mail poll — overnight replies and alerts | `mail` | 2 min | `EMAIL_INGESTION.md` §3.2 |
| 08:15 | Digest compose and send | `mail` (sub-stage) | 30 s | `EMAIL_INGESTION.md` §11 |
| 09:00–22:00, every 30 min | Mail poll | `mail` | 30 s | `EMAIL_INGESTION.md` §3.2 |
| 23:30 | Spreadsheet export | `export` | 1 min | `APPLICATION_PIPELINE.md` §11.4 |
| Sunday 04:00 | Retention prune | `prune` | 5 min | `APPLICATION_PIPELINE.md` §13 |
| Nightly 02:00 | Rescore sweep (`RESCORE_MAX_AGE_DAYS`) | `discovery` | 5 min | `MATCH_SCORING.md` §11.1 |

The 08:15 digest is the interface. If the operator reads nothing else, they read
that. It is designed to be read on a phone in under a minute
(`EMAIL_INGESTION.md` §11.1) and it always arrives — including on days when the
run failed, because a digest that silently stops arriving is indistinguishable
from a digest with no news.

### 2.2 The ten minutes

A worked shape of the daily loop, in the order the digest presents it:

| Minutes | Action | Where |
|---|---|---|
| 0:00–1:00 | Read the digest. Headline, then section 6 (source failures) before section 2. | Mail |
| 1:00–2:00 | If any source auto-disabled overnight, open it and decide: re-detect, reconfigure, or leave disabled. | Companies → Needs attention |
| 2:00–7:00 | Work the review queue. Per item: read the gap list first, then the tailoring plan diff, then approve or skip. | Queue |
| 7:00–9:00 | Submit approved applications on the employers' sites. Download the `.docx` pair from the review item. | Employer sites |
| 9:00–10:00 | Confirm any status changes the mail classifier held for review; enter anything that happened off-email. | Applications |

**Read the gaps before the plan.** The gap list (`MATCH_SCORING.md` §8.2) is the
highest-information object the system produces. A role with three missing hard
requirements is a skip regardless of how good the letter reads, and reading the
letter first is how an operator talks themselves into a bad application.

**Skipping is a first-class outcome.** `review_item` rows are retained forever
(`APPLICATION_PIPELINE.md` §13) precisely because skip decisions are data — they
are the only record of what the operator declined and why. Record a
`decision_note`. Two months later it is the input to re-tiering.

### 2.3 What the operator does *not* do daily

- Read logs. Anything log-worthy that the operator needs is in the digest's
  section 6 and section 7, and in `GET /api/v1/runs/{id}`.
- Check cost. The budget circuit breaker (§7) acts before the invoice does.
- Verify the run happened. A missing digest *is* the alarm.

---

## 3. Weekly maintenance

Thirty minutes, one sitting. Sunday evening or Monday morning — after the
Sunday 04:00 prune, so the numbers reflect a pruned database.

### 3.1 The checklist

```
[ ] 1. Source health — work the "Needs attention" filter to zero
[ ] 2. Auto-disabled sources — re-enable, reconfigure or accept each one
[ ] 3. Empty-board review — any source `empty` for ≥ 7 consecutive runs
[ ] 4. LLM cost — seven-day trend against the ₹80/day budget
[ ] 5. Funnel check — response rate, and whether n is yet large enough to read
[ ] 6. Expiring claims — clear the "expiring within 30 days" list to zero
[ ] 7. Held mail — clear v_mail_review_queue
[ ] 8. Run wall clock — is the 15-minute budget still comfortable
```

### 3.2 Item 1 — source health review

The Settings health table and the Companies page **Needs attention** filter
(`COMPANY_REGISTRY.md` §7.2) read the same data: `source.last_status`,
`source.consecutive_failures`, and the latest `run_log.source_results`.

The query behind it, for when the UI is not where you are:

```sql
SELECT s.id, c.slug, s.adapter, s.enabled, s.last_status,
       s.consecutive_failures, s.last_run_at, left(s.last_error, 120) AS err
FROM source s
JOIN company c ON c.id = s.company_id
WHERE s.consecutive_failures > 0
   OR s.enabled = FALSE
   OR s.last_status NOT IN ('ok', 'empty')
ORDER BY s.consecutive_failures DESC, c.slug;
```

Target state after the sitting: **zero rows with `consecutive_failures >= 3`**,
and every `enabled = FALSE` row carrying a `company.notes` entry saying why.

### 3.3 Item 3 — the empty-board signal

`empty` is a separate status from `ok` for a reason (`SOURCE_ADAPTERS.md` §10.2).
A board returning zero postings is usually a small company with nothing open. It
occasionally means a board token changed and the vendor returns `[]` instead of a
404 — a silent failure that never increments `consecutive_failures` and would
otherwise never surface.

```sql
-- Sources that have returned zero postings on every run for a week
SELECT s.id, c.slug, s.adapter, count(*) AS empty_runs
FROM run_log r
CROSS JOIN LATERAL jsonb_array_elements(r.source_results) AS sr
JOIN source s  ON s.id = (sr->>'source_id')::bigint
JOIN company c ON c.id = s.company_id
WHERE r.run_type = 'discovery'
  AND r.started_at > now() - interval '7 days'
  AND sr->>'status' = 'empty'
GROUP BY s.id, c.slug, s.adapter
HAVING count(*) >= 7
ORDER BY empty_runs DESC;
```

Resolution is one browser tab: open `company.careers_url`. If the employer has
open roles and the adapter returns none, the config is stale — re-run
`POST /api/v1/companies/detect` and update the source. If the employer genuinely
has nothing open, leave it and move on.

### 3.4 Item 5 — the funnel check

```http
GET /api/v1/metrics/funnel?group_by=variant&source_channel=direct
```

The weekly question is **not** "which variant is winning." It is "is n large
enough to have that conversation yet." Below `MIN_N_FOR_RATE` (15) the API
returns counts with rates suppressed, and below `MIN_N_FOR_COMPARISON` (30) per
arm no variant comparison is displayed at all (`APPLICATION_PIPELINE.md` §14).
Those floors exist because at n = 8 a two-point difference in response rate is
noise, and acting on it is how an operator abandons a working resume.

What the weekly check is actually for: **submission volume**. The system is sized
for five to ten well-targeted applications per week. Two consecutive weeks below
three submitted means either the filter is too aggressive, the company set is too
narrow, or the operator is not working the queue — and the fix differs in each
case.

---

## 4. Monthly maintenance

Ninety minutes. First Sunday of the month.

### 4.1 The checklist

```
[ ] 1. Claims ledger — re-verify every claim whose verified_at is > 150 days old
[ ] 2. Expired claims — deprecate anything expired for more than 60 days
[ ] 3. Company list — work the "No source" filter; prune dead trackers
[ ] 4. Dependency updates — backend and frontend, with the gate green
[ ] 5. Database — size, index bloat, autovacuum effectiveness (§8)
[ ] 6. Artifact storage — size and orphan check (§10)
[ ] 7. Backup restore drill — restore last night's dump into a scratch database
[ ] 8. Read run_log for the month — any pattern the digest showed one day at a time
```

### 4.2 Item 1 — ledger re-verification

The single most consequential recurring task in the system. Its rules are in
`CLAIMS_LEDGER.md` §9.2 and the discipline in §9.4; the operational procedure is
here.

```sql
-- Claims due for re-verification: volatile rows approaching expiry,
-- plus any row not looked at in six months
SELECT id, key, project, metric_value, metric_unit,
       verified_at::date, expires_at::date,
       (expires_at < now())                       AS expired,
       (expires_at < now() + interval '30 days')  AS expiring
FROM claim
WHERE deleted_at IS NULL
  AND (expires_at IS NOT NULL AND expires_at < now() + interval '30 days'
       OR verified_at < now() - interval '180 days')
ORDER BY expires_at NULLS LAST, verified_at;
```

For each row, open the `evidence_ref` and answer one question: **is this number
still what the evidence says?**

- **Unchanged** → `PATCH /api/v1/claims/{id}` with a new `verified_at`. Nothing
  else changes; `claim_usage` is untouched.
- **Changed** → this is a *new claim*, not an edit. Retire the old row under a
  dated key and insert the new fact under the canonical key, in one transaction
  (`CLAIMS_LEDGER.md` §9.2). Documents already sent must keep resolving.
- **No longer true, or no longer the operator's** → `DELETE /api/v1/claims/{id}`
  (soft delete). It disappears from generation candidates and still resolves in
  provenance.

Every one of those three paths triggers a rescore of postings whose
`match_score.evidence` cites the claim (`MATCH_SCORING.md` §11.1). That is
automatic; do not do it by hand.

### 4.3 Item 3 — company list pruning

Three filtered views of the Companies page, worked in order
(`COMPANY_REGISTRY.md` §11.3):

| Filter | Action |
|---|---|
| **No source** | Any company sourceless for two months gets a detection attempt or an explicit `notes` entry saying why it never will. |
| **Zero postings, 90 days** | The board works and the employer is not hiring in scope. Drop `poll_interval_minutes` to weekly rather than removing the company. |
| **Blacklisted** | Re-read quarterly. Reasons expire — a company that rejected the operator in March is a legitimate target in December. |

**Never delete a source that has postings.** `DELETE /api/v1/sources/{id}`
returns 409 `source.has_postings` for exactly this reason
(`COMPANY_REGISTRY.md` §11.2): `job_posting.source_id` cascades, so deleting a
source destroys the discovery history it produced. Disable it instead. The same
applies one level up — company deletion is a soft delete.

### 4.4 Item 7 — the restore drill

A backup that has never been restored is a hypothesis.

```bash
# 1. Take the most recent nightly dump
LATEST=$(ls -1t /var/lib/scout/backups/*.dump | head -1)

# 2. Restore into a scratch database
createdb scout_restore_test
pg_restore --dbname=scout_restore_test --no-owner --jobs=4 "$LATEST"

# 3. Prove it is a real database, not an empty schema
psql scout_restore_test -c "
  SELECT
    (SELECT count(*) FROM company)           AS companies,
    (SELECT count(*) FROM claim)             AS claims,
    (SELECT count(*) FROM application)       AS applications,
    (SELECT count(*) FROM claim_usage)       AS provenance_rows,
    (SELECT max(version_num) FROM alembic_version) AS schema_version;"

# 4. Prove provenance survived — every claim_usage row still resolves
psql scout_restore_test -c "
  SELECT count(*) AS orphaned_provenance
  FROM claim_usage cu
  LEFT JOIN claim c ON c.id = cu.claim_id
  WHERE c.id IS NULL;"      -- must be 0

dropdb scout_restore_test
```

`orphaned_provenance = 0` is the acceptance criterion, not "the restore
completed". Provenance is the property that is worth the drill.

---

## 5. Source health management

### 5.1 Reading `run_log.source_results`

One JSONB entry per attempted source, shape per `SOURCE_ADAPTERS.md` §10.3. The
fastest diagnostic path from "something is wrong" to "this source, this reason":

```sql
-- The most recent discovery run, expanded, failures first
WITH latest AS (
  SELECT id, started_at, status, stats, source_results
  FROM run_log
  WHERE run_type = 'discovery'
  ORDER BY started_at DESC
  LIMIT 1
)
SELECT sr->>'describe'      AS source,
       sr->>'status'        AS status,
       sr->>'error_code'    AS code,
       sr->>'error'         AS error,
       (sr->>'fetched')::int  AS fetched,
       (sr->>'new')::int      AS new,
       (sr->>'duration_ms')::int AS ms,
       (sr->>'retries')::int  AS retries
FROM latest
CROSS JOIN LATERAL jsonb_array_elements(latest.source_results) AS sr
WHERE sr->>'status' NOT IN ('ok', 'empty')
ORDER BY sr->>'status', ms DESC;
```

The same data is at `GET /api/v1/runs/{id}` and in the Settings health table.
Use the API in normal operation; use SQL when correlating across runs.

Trend across a week — which sources are *chronically* unhealthy rather than
unlucky once:

```sql
SELECT sr->>'describe' AS source,
       sr->>'status'   AS status,
       count(*)        AS occurrences
FROM run_log r
CROSS JOIN LATERAL jsonb_array_elements(r.source_results) AS sr
WHERE r.run_type = 'discovery'
  AND r.started_at > now() - interval '7 days'
  AND sr->>'status' NOT IN ('ok', 'empty')
GROUP BY 1, 2
ORDER BY occurrences DESC, source;
```

### 5.2 Diagnosing a failing adapter

Work `error_code` first — it is a stable machine code and it names the class of
problem (`SOURCE_ADAPTERS.md` §10.3).

| `error_code` | Almost always means | First action |
|---|---|---|
| `adapter.board_not_found` | The board token, tenant or site changed. The employer migrated or renamed. | Re-run `POST /companies/detect` on `company.careers_url`. Update `source.config`. |
| `adapter.schema_drift` | The vendor changed the response shape. The response model rejected it. | Capture a fresh fixture, diff against `tests/fixtures/sources/{adapter}/`, fix the model, add the case. This is a code change. |
| `adapter.timeout` | The upstream is slow, or a Workday detail fan-out is oversized. | Check `duration_ms` and `requests`. If it is one tenant, lower its rate bucket; if it is every source, check the host's network. |
| `adapter.rate_limited` | Our own back-pressure, not the source's fault. Does **not** increment `consecutive_failures`. | Nothing, unless it recurs. Then widen `rate_limit_wait_s` or lower `source_concurrency`. |
| `adapter.circuit_open` | Five consecutive failures on that `bucket_key` opened the in-run breaker; this source was skipped without a request. | Fix the source that opened it. This one is collateral. |
| `adapter.robots_denied` | robots.txt disallows the endpoint. Source disabled immediately. | Do not work around it. Move the employer to `mail_alert` or manual import. |
| `source.denied_by_policy` | A never-scrape host. Should be structurally unreachable. | This is a bug, not an operations issue. It means a config carries a denied host. File it. |
| `adapter.transport` | Connection reset, DNS, TLS. | Retry manually once. If it clears, it was transient. |
| `adapter.unknown` | An exception the classifier did not recognise. | Read the structured log by `run_id` and `source_id`. The stack trace is there, never in `error`. |

**Re-run one source in isolation** before changing anything:

```bash
curl -sS -X POST http://localhost:8000/api/v1/runs/discovery \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: manual-$(date +%s)" \
  -d '{"source_ids": [77]}' | jq .

# then read the result
curl -sS http://localhost:8000/api/v1/runs/{run_id} | jq '.data.source_results'
```

Or probe without running the pipeline at all:

```bash
curl -sS -X POST http://localhost:8000/api/v1/sources/77/test | jq .
# → { "data": { "reachable": true, "sample_count": 25, "latency_ms": 412 }, ... }
```

`POST /sources/{id}/test` is the right first move: it re-probes with a 10-second
budget and touches nothing in the database.

### 5.3 Auto-disable and re-enable

The cross-run breaker is `source.consecutive_failures` in Postgres
(`SOURCE_ADAPTERS.md` §4.8). It increments on any failed run for that source and
resets to zero on any success. At `>= 5` the source is set `enabled = false`,
`last_status = 'auto_disabled'`, and is called out explicitly in the digest's
section 6.

Two rules that are easy to get wrong:

- **Success clears history.** A source that works today is not on probation for
  a transient outage last week. The counter resets to 0, not decrements.
- **`rate_limited` and `circuit_open` do not count.** They are our own
  back-pressure. Counting them would auto-disable healthy boards during a busy
  run.

**Re-enabling is a human act.** The system never re-enables itself on a timer,
because five consecutive daily failures almost always means the board moved and
needs a new config, not that the network was unlucky five times.

```bash
# Wrong: flipping the flag and hoping
curl -X PATCH .../sources/77 -d '{"enabled": true}'

# Right: find out what changed first
curl -sS -X POST .../api/v1/companies/detect \
  -d '{"url": "https://adobe.wd5.myworkdayjobs.com/external_experienced"}' | jq .

# then update the config and re-enable in one call
curl -sS -X PATCH .../api/v1/sources/77 \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true,
       "config": {"host": "adobe.wd5.myworkdayjobs.com",
                  "tenant": "adobe", "site": "external_careers"}}' | jq .
```

`PATCH` with `enabled: true` also resets `consecutive_failures` to zero. Then run
that source alone (§5.2) and read `source_results`. `status: "ok"` with a
plausible `fetched` count is the acceptance criterion — not "the PATCH returned
200".

If the board is genuinely gone, leave it disabled and write the reason in
`company.notes`. A disabled source with a stated reason is maintained. A disabled
source with no note is rot.

---

## 6. LLM cost monitoring

### 6.1 What is metered

`cost.py` computes actual spend from the `usage` block on every model response,
not from the estimates in `AI_ARCHITECTURE.md` §8.1. Per-run cost lands in
`run_log.stats.llm_cost_inr` and is reported in the digest's section 7.

The design target is **₹66.66/day against an ₹80 budget — 17% headroom**
(`AI_ARCHITECTURE.md` §8.2). The headroom is thin on purpose: a budget with 300%
headroom does not constrain any decision.

```sql
-- 30-day cost trend, with the budget line for comparison
SELECT started_at::date                                   AS day,
       round((stats->>'llm_cost_inr')::numeric, 2)        AS cost_inr,
       (stats->>'extracted')::int                         AS extracted,
       (stats->>'generated')::int                         AS generated,
       round((stats->>'llm_cost_inr')::numeric
             / NULLIF((stats->>'extracted')::int, 0), 3)  AS inr_per_posting
FROM run_log
WHERE run_type = 'discovery'
  AND started_at > now() - interval '30 days'
ORDER BY day DESC;
```

`inr_per_posting` is the number to watch, not the daily total. The daily total
rises legitimately when the operator adds companies; cost *per posting* rising
means something structural changed — a prompt got longer, JDs got more verbose,
or the repair-retry rate climbed.

### 6.2 The two thresholds

| Threshold | Key | Default | Behaviour |
|---|---|---|---|
| Warning | `LLM_BUDGET_WARN_PCT` | `80` (i.e. ₹64) | Generation is capped to the top 5 items for the remainder of the day. Extraction and scoring continue. |
| Breaker | `LLM_DAILY_BUDGET_INR` | `80` | Generation stops entirely. Postings are still discovered, filtered, extracted and scored; `review_item` rows are created without artifacts and flagged. |

The breaker degrades in the right direction: **discovery never stops, generation
does.** A day with thirty scored postings and no drafts is a recoverable day. A
day with no discovery has a permanent hole in `first_seen_at` history.

### 6.3 When the budget is exceeded

Do not raise `LLM_DAILY_BUDGET_INR`. Diagnose first — the breaker fired because
something changed, and the change is almost always one of five things.

| Symptom | Cause | Correct response |
|---|---|---|
| `extracted` well above 30/day | The deterministic filter is passing too much — usually a widened location filter or a new tranche of companies. | Tighten stage ④ (`ARCHITECTURE.md` §6). The filter saves ₹87.85/day, more than the entire budget (`AI_ARCHITECTURE.md` §8.3). |
| `generated` at the cap every day | `GENERATION_MIN_COVERAGE_PCT` is too low; marginal items are clearing it. | Raise the coverage floor. Ten drafts a day the operator does not submit is worse than five they do. |
| Repair-retry rate above 8% | Structured output is failing — usually after a model-ID change. | Check `AI_ARCHITECTURE.md` §6. A model bump without an eval run is the usual cause. |
| Input tokens per extraction climbing | JDs are getting longer, or `EXTRACTION_MAX_JD_TOKENS` was raised. | Confirm the envelope truncation is applying (default 4,000, head-first). |
| A single run 10× normal | A cost attack — a 400,000-token JD (`AI_ARCHITECTURE.md` §7.1). | Envelope truncation should have bounded it. If it did not, that is a defect in `llm/guard.py`, and it is a security finding. |

**The one legitimate reason to raise the budget** is that the company set grew
deliberately and the extra spend buys extra coverage that the funnel shows is
converting. Growing from 300 to 600 companies costs about ₹89/day
(`AI_ARCHITECTURE.md` §8.4), and the documented response to that is a stricter
filter, not a bigger budget — because a filter that is not doing its job is the
cheaper thing to fix.

### 6.4 The extraction cache

Extraction is cached on `job_posting.content_hash` under the prompt version and
resolved model ID (`AI_ARCHITECTURE.md` §9). Hit rate across consecutive daily
runs is expected around 15–25% of the post-filter set.

```sql
-- Cache effectiveness: extractions served vs. paid for
SELECT started_at::date AS day,
       (stats->>'extracted')::int        AS extracted,
       (stats->>'extract_cache_hits')::int AS cache_hits,
       round(100.0 * (stats->>'extract_cache_hits')::int
             / NULLIF((stats->>'extracted')::int, 0), 1) AS hit_pct
FROM run_log
WHERE run_type = 'discovery' AND started_at > now() - interval '14 days'
ORDER BY day DESC;
```

A hit rate that collapses to zero means the key changed — a prompt version bump
or a model-ID change, both of which correctly invalidate the cache. If neither
happened, `content_hash` is unstable, which is a normalisation bug in stage ②.

---

## 7. Run health

### 7.1 Wall clock

The run budget is 900 seconds (`ARCHITECTURE.md` §9). The runner cancels
stragglers at that ceiling; individual sources are capped at 180 seconds.

```sql
SELECT started_at::date AS day,
       extract(epoch FROM (finished_at - started_at))::int AS seconds,
       status,
       jsonb_array_length(source_results) AS sources_attempted
FROM run_log
WHERE run_type = 'discovery' AND started_at > now() - interval '30 days'
ORDER BY day DESC;
```

At 320 sources with `source_concurrency = 8`, a healthy run lands between 400
and 700 seconds. Sustained times above 750 seconds mean one of: a slow tenant
eating retry budget, `source_concurrency` set too low for a grown company set, or
rate-limit waits accumulating. `sr->>'rate_limit_wait_ms'` in `source_results`
distinguishes the third case from the first two immediately.

### 7.2 `completed_with_errors` is normal

`run_log.status = 'completed_with_errors'` is **the normal state of a healthy
320-source system** (`SOURCE_ADAPTERS.md` §10.4). Some board somewhere changes
most weeks. The signal is not the status; it is the *count* and the *trend*.

`status = 'failed'` is the real alarm. It means the run could not proceed:
Postgres or Redis unavailable, the run lock could not be held, or the runner
itself raised. Adapter failures never produce `failed` — that is invariant 5
expressed as a state machine. A `failed` run therefore always means
infrastructure, never a source.

---

## 8. Database maintenance

### 8.1 Growth expectations

Sized against `ARCHITECTURE.md` §9: ~320 sources, ~150 new postings per run,
2,000–5,000 raw fetched.

| Table | Rows after 1 year | Dominant cost | Note |
|---|---|---|---|
| `job_posting` | 40,000–55,000 | `description_text`, `raw`, `description_html` | `raw` and `description_html` are nulled at 30 days (`APPLICATION_PIPELINE.md` §13); that is most of the size. |
| `requirement` | 250,000–350,000 | Row count | ~8 per extracted posting. Cascades with the posting. |
| `match_score` | 60,000–100,000 | `gaps` / `evidence` JSONB | Two variants scored per posting, superseded prompt versions pruned to the latest 2. |
| `application_event` | Low thousands | Nothing | Never pruned. Never will be a problem. |
| `email_message` | 15,000–25,000 | Metadata only | Bodies are never stored (`EMAIL_INGESTION.md` §9.1). |
| `run_log` | ~2,500 | `source_results` JSONB | ~320 entries per discovery row. Reset to `'[]'` at 90 days. |
| `claim`, `claim_usage` | Hundreds / low thousands | Nothing | Never pruned. Soft delete only. |

**Total expected size after one year: 3–6 GB.** Storage is not the constraint at
this scale; relevance is. A `job_posting` table with 400,000 dead rows makes
search worse and the UI slower for no benefit.

### 8.2 Autovacuum

The tables that matter are `job_posting` (heavy `UPDATE` on `last_seen_at` every
run) and `run_log` (large JSONB writes). Default autovacuum thresholds are too
lax for the first.

```sql
-- job_posting is updated ~2,000 times per run; vacuum it more eagerly
ALTER TABLE job_posting SET (
  autovacuum_vacuum_scale_factor  = 0.05,
  autovacuum_analyze_scale_factor = 0.02,
  autovacuum_vacuum_cost_limit    = 1000
);

-- requirement and match_score are insert/delete heavy, not update heavy
ALTER TABLE match_score SET (autovacuum_vacuum_scale_factor = 0.1);
```

The `last_seen_at` bump every run is the single largest source of dead tuples in
the system. It is an unavoidable consequence of the two-run `closed_at` rule
(`DATA_MODEL.md` §4.1), and the answer is tuned autovacuum, not a schema change.

Monthly check:

```sql
SELECT relname,
       n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY dead_pct DESC;
```

Action threshold: **`dead_pct > 20` on any table** warrants a manual
`VACUUM (ANALYZE, VERBOSE)` and a look at whether autovacuum is being starved.
`dead_pct > 20` on `job_posting` specifically means the scale factor above was
never applied.

### 8.3 Index bloat

The GIN indexes are the ones that bloat: `posting_search_idx` (tsvector),
`company_name_trgm_idx`, `company_tags_idx`, `claim_tags_idx`.

```sql
SELECT schemaname, relname AS table_name, indexrelname AS index_name,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size,
       idx_scan
FROM pg_stat_user_indexes
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 20;
```

Two readings:

- **A large index with `idx_scan = 0`** after a month of real traffic is an index
  nobody uses. Drop it in a migration; do not drop it by hand.
- **`posting_search_idx` growing faster than `job_posting`** is GIN pending-list
  accumulation. Rebuild it concurrently:

```sql
REINDEX INDEX CONCURRENTLY posting_search_idx;
```

Quarterly is sufficient. `CONCURRENTLY` matters — this is a single-instance
deployment and a blocking reindex on the search index takes the UI down.

### 8.4 Pruning old postings

The prune job runs Sunday 04:00 IST as `run_type = 'prune'` and implements the
retention table in `APPLICATION_PIPELINE.md` §13. It is not optional maintenance;
it is a scheduled job, and this section is about verifying it worked.

```sql
-- What the last prune did
SELECT started_at, status, stats
FROM run_log
WHERE run_type = 'prune'
ORDER BY started_at DESC
LIMIT 4;
```

The two rules that override the retention table, and that every prune query is
written to honour:

1. **Nothing referenced by an `application` or `application_event` is ever
   pruned.** Every prune statement is `DELETE … WHERE NOT EXISTS (…)`, and every
   one is covered by a test that inserts a referenced row and asserts it
   survives.
2. **Pruning is never cascading-by-accident.** `job_posting` deletion cascades to
   `requirement` and `match_score` by design, and the predicate — not
   `ON DELETE CASCADE` — is what guarantees such a posting has no application.

Verification after a prune, which should always return zero:

```sql
SELECT count(*) AS orphaned_applications
FROM application a
LEFT JOIN job_posting p ON p.id = a.posting_id
WHERE p.id IS NULL;
```

If the prune job has not run for three weeks — check `run_log` — the scheduler
job is missing from the Postgres job store. Restarting the application
re-registers it.

---

## 9. The claims ledger review discipline

### 9.1 Why this section exists

Every other form of decay in this system is loud. A dead source shows up in the
digest. A blown budget trips a breaker. A failed run is a missing digest.

**Ledger rot is silent, and it is the only failure that leaves the building.**

The validation gate (`CLAIMS_LEDGER.md` §5) guarantees that nothing enters a
generated document unless it resolves to a `claim` row. What it cannot guarantee
is that the row is *still true*. A validator that resolves a span against a row
asserting "604 offline tests across 44 modules" has done its entire job
correctly even when the number has been 812 for four months. The document is
internally consistent, provenance-complete, reproducible — and wrong in an
interview.

The ledger's failure mode is therefore **not fabrication**. It is **drift toward
flattery**: a statement that was precise when written and has quietly become a
claim it cannot support. That drift is detectable only by a human reading the
table with the evidence references open, which is why it is a scheduled habit
and not a scheduled job.

Consequence ranking, plainly:

| Rot | Costs |
|---|---|
| A dead source | Some roles are not discovered. Recoverable the day it is noticed. |
| A blown budget | A day of drafts. Recoverable tomorrow. |
| A stale registry | Wasted polling. Recoverable in a monthly sitting. |
| **A stale ledger** | **A number the operator cannot defend, in a document already in an employer's hands.** Not recoverable. |

### 9.2 The cadence

Reproduced from `CLAIMS_LEDGER.md` §9.4 because it is an operating schedule and
belongs in the operating schedule.

| Cadence | Action |
|---|---|
| **On every claim added** | State the evidence reference *before* the statement. If the reference cannot be written, the claim is not ready. |
| **Weekly, in the digest** | Clear the "expiring within 30 days" list. Re-verify or let it lapse — both acceptable; ignoring it is not. |
| **On every validation failure** | Read the unresolved span. Either the ledger is missing a true fact — add it — or the generator invented one — a prompt problem worth a version bump. **Never resolve it by loosening the check.** |
| **Monthly** | `GET /claims?expired=true`. Anything expired for more than 60 days is deprecated rather than left to rot. |
| **Before every interview** | Run the provenance query for the artifact that employer received. Every claim in it must be defensible that day. |
| **Quarterly** | Re-read the whole table as if it were someone else's. Any statement that reads as marketing rather than fact gets rewritten or removed. |

### 9.3 The pre-interview provenance query

The most valuable query in the system, and the reason `claim_usage` exists
(`CLAIMS_LEDGER.md` §7.2). Given an application, it answers "what did I claim to
these people, and can I defend each of it today?"

```sql
SELECT c.key,
       c.statement,
       c.metric_value, c.metric_unit,
       c.evidence_ref,
       c.confidentiality,
       c.verified_at::date,
       c.expires_at::date,
       (c.expires_at IS NOT NULL AND c.expires_at < now()) AS expired_now,
       (c.deleted_at IS NOT NULL)                          AS deprecated_since,
       cu.location,
       a.kind AS artifact_kind
FROM application app
JOIN artifact a  ON a.id IN (app.resume_artifact_id, app.cover_letter_artifact_id)
JOIN claim_usage cu ON cu.artifact_id = a.id
JOIN claim c        ON c.id = cu.claim_id
WHERE app.id = :application_id
ORDER BY a.kind, cu.location;
```

`expired_now` or `deprecated_since` being true on any row is not an emergency —
the document was correct when sent, and `claim_usage` references `claim.id` not
`claim.key` precisely so it keeps resolving. It is a **preparation note**: that
is the number to be ready to update out loud.

### 9.4 Handling a validation failure

`artifact.validation_status = 'failed'` blocks attachment to any `review_item` or
`application` — enforced by a CHECK trigger, not application code alone
(`DATA_MODEL.md` §8.1). Failed artifacts are retained 30 days for diagnosis.

```sql
SELECT a.id, a.kind, a.prompt_version, a.generated_at,
       jsonb_pretty(a.validation_notes) AS notes
FROM artifact a
WHERE a.validation_status = 'failed'
  AND a.generated_at > now() - interval '30 days'
ORDER BY a.generated_at DESC;
```

Read the unresolved span in `validation_notes` and take exactly one of two
actions:

- **The fact is true and the ledger lacks it** → add the claim, evidence
  reference first. Then `POST /api/v1/review/{id}/generate` again.
- **The generator invented it** → that is a prompt defect. It gets a prompt
  version bump and an eval run (`DEVELOPMENT.md` §8), not a one-off edit.

There is no third action. There is deliberately **no bypass endpoint**
(`API.md` §8, `CLAIMS_LEDGER.md` §6.2). The `bypassed` validation status exists
only for a documented maintenance path and never for a live draft.

---

## 10. Artifact storage

### 10.1 Growth

Artifacts are `.docx` files under `ARTIFACT_DIR`, named
`{artifact_id}/{Name}_{Company}_{RoleSlug}_{resume|cover}.docx`
(`DOCUMENT_GENERATION.md` §5.7). A one-page resume is 30–60 KB; a cover letter
15–30 KB.

At ten drafts a day with roughly 60% carrying a letter, that is about 16 files
and 700 KB a day — **under 300 MB a year before retention**. Storage is not a
capacity problem. It is a hygiene problem: an artifact directory that grows
without pruning eventually contains more failed and skipped drafts than sent
ones, and the operator picking a file under time pressure picks the wrong one.

### 10.2 What is deleted and what is kept

| Artifact | File | Row |
|---|---|---|
| Attached to an `application` | **Forever** | Forever |
| Attached only to a skipped `review_item` | Deleted at 90 days | Kept, `path = NULL` |
| `validation_status = 'failed'` | Deleted at 30 days | Deleted at 30 days |

Keeping the row with a null path when the bytes go is deliberate: provenance
survives, and `claim_usage` never orphans (`DATA_MODEL.md` §11).

### 10.3 Orphan reconciliation

Files on disk with no `artifact` row, and rows with no file. Both are bugs; both
are cheap to detect monthly.

```bash
# Rows whose file is missing
psql "$DATABASE_URL" -Atc \
  "SELECT id, path FROM artifact WHERE path IS NOT NULL" |
while IFS='|' read -r id path; do
  [ -f "$ARTIFACT_DIR/$path" ] || echo "MISSING FILE: $id  $path"
done

# Files with no row
find "$ARTIFACT_DIR" -name '*.docx' -printf '%P\n' | sort > /tmp/on_disk.txt
psql "$DATABASE_URL" -Atc \
  "SELECT path FROM artifact WHERE path IS NOT NULL" | sort > /tmp/in_db.txt
comm -23 /tmp/on_disk.txt /tmp/in_db.txt
```

A missing file for an artifact attached to an `application` is the serious case:
it means the record of what was actually sent is gone. Restore it from the
nightly backup rather than regenerating — a regenerated document under a newer
prompt version is not the document the employer received, and treating it as such
breaks invariant 7.

### 10.4 Checksums

`artifact.checksum` is the SHA-256 of the bytes, so a file that changed on disk
is detectable. Quarterly, over artifacts attached to applications only:

```bash
psql "$DATABASE_URL" -Atc \
  "SELECT a.id, a.path, a.checksum FROM artifact a
    WHERE a.path IS NOT NULL
      AND EXISTS (SELECT 1 FROM application ap
                   WHERE ap.resume_artifact_id = a.id
                      OR ap.cover_letter_artifact_id = a.id)" |
while IFS='|' read -r id path expected; do
  actual=$(sha256sum "$ARTIFACT_DIR/$path" | cut -d' ' -f1)
  [ "$actual" = "$expected" ] || echo "CHECKSUM MISMATCH: $id  $path"
done
```

---

## 11. Logs

### 11.1 What is written

Structured JSON via `structlog`, one line per pipeline stage per run, with
`run_id` correlation (`ARCHITECTURE.md` §8). To stdout, captured by the Docker
json-file driver.

**Never logged, at any level:** credentials, OAuth tokens or any prefix, hash or
length of one; cookies; full email bodies; raw resume content; PII. Log lines
about auth record only `{"gmail_auth": "refreshed", "expires_in_s": 3599}`
(`EMAIL_INGESTION.md` §2.5).

`source_results.error` is a curated, non-sensitive string capped at 500
characters, rendered in the digest and the health table. **Stack traces go to the
structured log, never to `error`** — upstream bodies are untrusted
(`ARCHITECTURE.md` §2).

### 11.2 Rotation

Compose handles it. This is the whole configuration:

```yaml
# docker-compose.yml — applied to every service
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "20m"
    max-file: "5"
    compress: "true"
```

100 MB per service, ~3 weeks of retention at observed volume. That is longer than
any log-based investigation this system justifies — anything older is answered
from `run_log`, which is the durable record and is retained 12 months.

If logs are shipped to a host syslog or journald instead, the equivalent
constraint is a 100 MB cap and no shipping to a third party. There is no log
aggregation service in this design, because one user's daily batch does not
justify one and because it would put curated-but-untrusted upstream error strings
somewhere the operator does not control.

### 11.3 Useful queries

```bash
# Everything from one run, in order
docker compose logs api --since 24h --no-log-prefix \
  | jq -c 'select(.run_id == "01JE7…")'

# Every source that logged an exception this week
docker compose logs api --since 168h --no-log-prefix \
  | jq -c 'select(.event | test("source_")) | select(.exc_info != null)
           | {ts: .timestamp, source_id, adapter, event}'

# Model calls and their token usage
docker compose logs api --since 24h --no-log-prefix \
  | jq -c 'select(.event == "llm_call")
           | {family, prompt_version, model, in: .input_tokens,
              out: .output_tokens, cost_inr, repairs}'
```

---

## 12. Dependencies and security updates

### 12.1 Cadence

| Cadence | Scope | Gate |
|---|---|---|
| **Weekly, automated** | `pip-audit` and `npm audit` run as part of `ci/run-checks.sh` on every push; a scheduled Sunday run catches advisories published against unchanged code. | Fails the gate on any HIGH or CRITICAL. |
| **Monthly** | Patch and minor bumps for both lockfiles. One merge request, gate green. | `ci/run-checks.sh all` |
| **Quarterly** | Major version review. FastAPI, SQLAlchemy, Pydantic, React, Vite. Each gets its own merge request. | Gate plus a manual smoke of a full discovery run. |
| **On advisory** | Anything with a known exploit path reachable from untrusted input — the HTTP client, HTML parsing, `.docx` generation — is patched the day it is known. | Gate, then deploy. |

### 12.2 Pinning

Dependencies are pinned. `uv.lock` (or `requirements.lock`) and
`package-lock.json` are committed and are what the images build from. The system
does not build from ranges, because a build that resolves differently on two days
is a build whose behaviour cannot be reproduced — and invariant 7 requires that
every artifact be reproducible.

**Model IDs are pinned the same way and for the same reason**
(`AI_ARCHITECTURE.md` §4.1). No `-latest` aliases, ever. A silently upgraded model
invalidates every eval result and every `artifact.model` provenance record
without a deploy having happened. A model-ID change is treated exactly like a
prompt change: eval first, then a pinned bump in configuration.

### 12.3 The dependencies that matter most

| Package | Why it is on this list |
|---|---|
| `httpx` | Every byte of untrusted upstream content arrives through it. |
| `beautifulsoup4` / `lxml` | Parses attacker-controllable HTML at stage ②. |
| `python-docx` | Writes the files the operator sends to employers. |
| `google-auth` / `google-api-python-client` | Holds the highest-value secret in the system. |
| `cryptography` | Encrypts the OAuth token at rest. |
| `playwright` | Only where no API exists; browser binaries carry their own advisories. |

An advisory against any of these six is a same-day patch, not a monthly bump.

---

## 13. Backups

| What | How | Cadence | Retention |
|---|---|---|---|
| Postgres | `pg_dump -Fc` to `/var/lib/scout/backups/` | Nightly 03:00 IST | 14 daily, 12 monthly |
| Artifacts | `rsync` of `ARTIFACT_DIR` | Nightly 03:15 IST | Mirror, plus 30-day snapshot |
| Gmail OAuth token | **Not backed up** | — | — |
| `.env` | Manual, to the operator's password manager | On change | — |

The token is deliberately not in the backup set. It is a 0600 file encrypted with
a key that lives elsewhere, and a backup copy is a second place it can leak from.
Recovery is one command and one browser consent (`INSTALLATION_GUIDE.md` §6.4),
which is cheaper than the risk.

```bash
# /etc/cron.d/scout-backup  (or a Compose sidecar)
0 3 * * * scout pg_dump -Fc "$DATABASE_URL" \
  > /var/lib/scout/backups/scout-$(date +\%F).dump && \
  find /var/lib/scout/backups -name 'scout-*.dump' -mtime +14 -delete
```

The restore drill is monthly and is item 7 of §4.1. A backup that has never been
restored is a hypothesis.

---

## 14. Metrics to watch

The operating dashboard, as a table. Every row has a source, a threshold and an
action — a metric without an action is decoration.

| Metric | Source | Healthy | Investigate | Act |
|---|---|---|---|---|
| Digest arrived | Inbox, 08:15 | Daily | Missed once | **Missed twice** — check scheduler and Gmail auth (§5, `EMAIL_INGESTION.md` §2.5) |
| Run status | `run_log.status` | `completed` or `completed_with_errors` | — | **`failed`** — infrastructure, never a source (§7.2) |
| Run wall clock | `run_log` timestamps | < 700 s | 700–850 s | **> 850 s** — near the 900 s cancel ceiling (§7.1) |
| Sources failing | `source_results` status ≠ `ok`/`empty` | < 5% of ~320 | 5–10% | **> 10%** — likely a shared host or our own rate limiting |
| Sources auto-disabled | `source.consecutive_failures >= 5` | 0 new/week | 1–2 new/week | **> 2 new/week** — a systemic adapter or network problem |
| Sources `empty` ≥ 7 runs | §3.3 query | 0 | 1–3 | **> 3** — silent config drift (§3.3) |
| New postings after dedup | `stats.new` | 100–200/run | 50–100 | **< 50** — sources are silently failing, not the market being quiet |
| Postings past the filter | `stats.filtered` vs `extracted` | ~30/day | 40–60 | **> 60** — the filter is too loose; token spend follows (§6.3) |
| Drafts generated | `stats.generated` | 5–10/day | — | **0 for two days** — coverage floor or budget breaker |
| LLM cost | `stats.llm_cost_inr` | ≤ ₹70/day | ₹70–80 | **> ₹80** — breaker fired; diagnose before raising (§6.3) |
| Cost per posting | derived (§6.1) | ≤ ₹0.80 | ₹0.80–1.00 | **> ₹1.00** — structural, not volume |
| Validation failures | `stats.validation_failures` | ≤ 1/day | 2–3/day | **> 3/day** — ledger gap or prompt regression (§9.4) |
| Repair-retry rate | `llm_call` logs | < 8% | 8–15% | **> 15%** — schema enforcement degrading (`AI_ARCHITECTURE.md` §6) |
| Applications submitted | `application` rows | 5–10/week | 3–5 | **< 3 for two weeks** — the loop has stalled (§3.4) |
| Response rate | `v_funnel`, direct only | Read only at n ≥ 15 | — | Below `MIN_N_FOR_RATE`, **no action is correct** |
| Held mail | `v_mail_review_queue` | 0 after weekly | 1–3 | **> 3** — linkage is failing (`EMAIL_INGESTION.md` §6) |
| Gmail auth | `GET /health` | `ok` | — | **`unauthenticated`** — re-run `scout-careers auth gmail` |
| Claims expiring ≤ 30 d | `GET /claims?expiring=true` | 0 after weekly | 1–5 | **> 5** — the weekly habit has lapsed (§9.2) |
| Claims expired > 60 d | `GET /claims?expired=true` | 0 | 1–3 | **> 3** — deprecate them; do not let them rot |
| Table dead tuple % | `pg_stat_user_tables` | < 10% | 10–20% | **> 20%** — autovacuum starved (§8.2) |
| Database size | `pg_database_size` | < 6 GB/yr | 6–10 GB | **> 10 GB** — the prune job is not running (§8.4) |
| Prune job last run | `run_log` `run_type='prune'` | ≤ 7 days ago | 7–14 | **> 14 days** — job missing from the store (§8.4) |
| Backup restore drill | §4.4 | Monthly, passing | Skipped once | **Skipped twice** — you do not have backups |

Three of these are the ones that actually predict trouble, and they are worth
knowing by heart:

1. **Sources auto-disabled per week.** The leading indicator of discovery
   coverage silently halving.
2. **Cost per posting.** The leading indicator that something structural changed
   upstream of the meter.
3. **Claims expiring, uncleared.** The leading indicator of the only failure that
   leaves the building.

---

## 15. Incident playbooks

Short, because at this scale there are only five things that go wrong.

### 15.1 No digest arrived

1. `GET /api/v1/health` — is the API up, and what does `gmail` report?
2. If `gmail: "unauthenticated"`, the refresh token is invalid — revoked,
   password changed, or unused for six months. Re-run
   `scout-careers auth gmail` (`INSTALLATION_GUIDE.md` §6.4). The cursor was not
   advanced, so nothing was skipped.
3. If Gmail is fine, check `run_log` for a `mail` row today. No row means the
   scheduler is not running: restart the API container, which re-registers jobs
   from the Postgres job store.
4. The day's digest was not lost. It is at `exports/digest-YYYY-MM-DD.html` and
   in-app (`EMAIL_INGESTION.md` §2.5).

### 15.2 The run failed

`status = 'failed'` is always infrastructure (§7.2).

1. `run_log.error` names it: Postgres unreachable, Redis unreachable, or the run
   lock could not be held.
2. A stuck lock — from a container killed mid-run — is the common case:
   ```bash
   docker compose exec redis redis-cli GET lock:run:discovery
   docker compose exec redis redis-cli DEL lock:run:discovery   # only if no run is live
   ```
3. Re-trigger: `POST /api/v1/runs/discovery`. The run is idempotent
   (`ARCHITECTURE.md` §8); re-running a completed stage is a no-op.

### 15.3 Many sources failing at once

1. Group `source_results` by `error_code` (§5.1). One code dominating points at
   one cause.
2. `adapter.circuit_open` dominating means one `bucket_key` opened the in-run
   breaker and short-circuited every source sharing it. Find the source that
   opened it; the rest are collateral.
3. `adapter.transport` dominating across unrelated hosts is the host's network or
   DNS, not the sources.
4. Do **not** mass re-enable. Fix the cause, then re-run the affected
   `source_ids` and read the results.

### 15.4 Budget breaker fired

Work §6.3 in order. Do not raise `LLM_DAILY_BUDGET_INR` as a first move — the
breaker did its job, discovery is unaffected, and tomorrow's run is not at risk.

### 15.5 A generated document contains a number the operator cannot defend

The most serious incident in this system, and the only one with an external
blast radius.

1. Run the provenance query (§9.3) for the artifact. The number resolves to a
   `claim` row; find it.
2. If the claim is wrong: **retire it as a changed value** (`CLAIMS_LEDGER.md`
   §9.2). Do not edit it — the document already sent must keep resolving to what
   it asserted.
3. If the number does **not** resolve to any claim, the validation gate was
   bypassed or defective. That is a build failure, not a warning
   (`ARCHITECTURE.md` §3, invariant 3). Stop generation
   (`GENERATION_ENABLED=false`), reproduce it as a test, and fix it before the
   next run.
4. Rescore affected postings. Regenerate nothing that has already been sent.

---

## 16. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | Invariants, pipeline stages, scale envelope, error policy |
| `INSTALLATION_GUIDE.md` | Getting to a running system; Gmail and Bedrock setup |
| `DEVELOPMENT.md` | The gate, migrations, prompt changes, adding adapters |
| `SOURCE_ADAPTERS.md` | §4.8 circuit breaking, §10 failure isolation and `source_results` |
| `COMPANY_REGISTRY.md` | §11 registry maintenance and the filtered views behind it |
| `CLAIMS_LEDGER.md` | §9 ledger maintenance, the authority for §9 here |
| `AI_ARCHITECTURE.md` | §8 cost model, §9 caching, §12 flags and rollback |
| `EMAIL_INGESTION.md` | §2.5 token failure handling, §11 digest structure |
| `APPLICATION_PIPELINE.md` | §13 retention, §14 configuration |
| `INFRASTRUCTURE.md` | Runtime topology, sizing, backup targets |
