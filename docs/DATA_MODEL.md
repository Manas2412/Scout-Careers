# DATA MODEL — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for schema. `ARCHITECTURE.md` wins on system-level
concerns; this file wins on tables, columns, types and constraints.

PostgreSQL 16. SQLAlchemy 2.0 declarative models in `db/models.py`. Every schema
change ships an Alembic revision with an ID of 32 characters or fewer.

---

## 1. Conventions

- Primary keys are `BIGINT GENERATED ALWAYS AS IDENTITY` unless the row is
  externally addressable, in which case a `ULID` stored as `CHAR(26)` is used so
  IDs sort by creation time and are safe in URLs.
- All timestamps are `TIMESTAMPTZ`, stored UTC, named `*_at`.
- Every table carries `created_at` and `updated_at` with a trigger on update.
- Soft delete is used only where history matters (`company`, `resume_variant`,
  `claim`); everything else hard-deletes.
- Enumerations are Postgres native `ENUM` types, created in the migration that
  first needs them.
- JSONB is used for genuinely schemaless payloads only — adapter config, raw
  source responses, gap structures. It is never used to avoid designing a table.
- Money and percentages are `NUMERIC`, never float.

---

## 2. Enumerations

```sql
CREATE TYPE ats_type AS ENUM (
  'greenhouse','lever','ashby','workday','smartrecruiters','workable',
  'recruitee','google','amazon','microsoft','mail_alert','manual'
);

CREATE TYPE company_tier   AS ENUM ('dream','strong','volume');
CREATE TYPE company_status AS ENUM ('tracking','paused','blacklisted');

CREATE TYPE requirement_kind AS ENUM ('hard','nice','responsibility','tool');

CREATE TYPE coverage_level AS ENUM ('met','partial','missing');

CREATE TYPE claim_confidentiality AS ENUM ('public','internal','restricted');

CREATE TYPE review_status AS ENUM (
  'pending_review','approved','skipped','needs_manual_review'
);

CREATE TYPE application_status AS ENUM (
  'submitted','acknowledged','screening','interview','offer',
  'rejected','withdrawn'
);

CREATE TYPE artifact_kind AS ENUM ('resume','cover_letter');

CREATE TYPE artifact_validation AS ENUM ('passed','failed','bypassed');

CREATE TYPE run_status AS ENUM ('running','completed','completed_with_errors','failed');

CREATE TYPE mail_class AS ENUM (
  'acknowledgement','rejection','screening_invite','interview_invite',
  'offer','recruiter_outreach','job_alert','unrelated'
);
```

---

## 3. Registry

### 3.1 `company`

```sql
CREATE TABLE company (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug                TEXT        NOT NULL UNIQUE,
  name                TEXT        NOT NULL,
  tier                company_tier   NOT NULL DEFAULT 'volume',
  status              company_status NOT NULL DEFAULT 'tracking',
  tags                TEXT[]      NOT NULL DEFAULT '{}',
  hq_location         TEXT,
  size_band           TEXT,
  website             TEXT,
  careers_url         TEXT,
  cover_letter_worth  BOOLEAN     NOT NULL DEFAULT TRUE,
  default_variant_id  BIGINT      REFERENCES resume_variant(id) ON DELETE SET NULL,
  location_filter     TEXT[]      NOT NULL DEFAULT '{}',
  notes               TEXT,
  deleted_at          TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX company_status_tier_idx ON company (status, tier)
  WHERE deleted_at IS NULL;
CREATE INDEX company_tags_idx        ON company USING GIN (tags);
CREATE INDEX company_name_trgm_idx   ON company USING GIN (name gin_trgm_ops);
```

`cover_letter_worth` defaults true but is set false for employers whose ATS has
no cover-letter field — most large tech. Generation skips those, saving tokens
on letters nobody reads.

`location_filter` empty means "accept all locations for this company".

### 3.2 `source`

One company may expose several boards (for example a Workday tenant with
separate experienced and campus sites).

```sql
CREATE TABLE source (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id            BIGINT   NOT NULL REFERENCES company(id) ON DELETE CASCADE,
  adapter               ats_type NOT NULL,
  config                JSONB    NOT NULL DEFAULT '{}',
  enabled               BOOLEAN  NOT NULL DEFAULT TRUE,
  poll_interval_minutes INTEGER  NOT NULL DEFAULT 1440,
  last_run_at           TIMESTAMPTZ,
  last_status           TEXT,
  last_error            TEXT,
  consecutive_failures  INTEGER  NOT NULL DEFAULT 0,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (company_id, adapter, config)
);

CREATE INDEX source_due_idx ON source (enabled, last_run_at)
  WHERE enabled;
```

`config` shape is adapter-specific and validated by a Pydantic model per adapter
— see `SOURCE_ADAPTERS.md`. Examples:

```jsonc
{ "board_token": "stripe" }                                  // greenhouse
{ "site": "netflix" }                                        // lever
{ "tenant": "adobe", "site": "external_experienced",
  "host": "adobe.wd5.myworkdayjobs.com" }                    // workday
```

A source with `consecutive_failures >= 5` is auto-disabled and reported in the
digest. It is never silently dropped.

---

## 4. Postings

### 4.1 `job_posting`

```sql
CREATE TABLE job_posting (
  id               CHAR(26)    PRIMARY KEY,            -- ULID
  company_id       BIGINT      NOT NULL REFERENCES company(id) ON DELETE CASCADE,
  source_id        BIGINT      NOT NULL REFERENCES source(id)  ON DELETE CASCADE,
  external_id      TEXT        NOT NULL,
  title            TEXT        NOT NULL,
  department       TEXT,
  location_raw     TEXT,
  location_city    TEXT,
  location_country TEXT,
  is_remote        BOOLEAN     NOT NULL DEFAULT FALSE,
  employment_type  TEXT,
  seniority_guess  TEXT,
  url              TEXT        NOT NULL,
  description_html TEXT,
  description_text TEXT        NOT NULL,
  content_hash     CHAR(64)    NOT NULL,               -- sha256 of description_text
  posted_at        TIMESTAMPTZ,
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at        TIMESTAMPTZ,
  filtered_out     BOOLEAN     NOT NULL DEFAULT FALSE,
  filter_reason    TEXT,
  raw              JSONB,
  search_tsv       TSVECTOR GENERATED ALWAYS AS (
                     to_tsvector('english',
                       coalesce(title,'') || ' ' || coalesce(description_text,''))
                   ) STORED,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_id, external_id)
);

CREATE INDEX posting_company_seen_idx ON job_posting (company_id, first_seen_at DESC);
CREATE INDEX posting_open_idx         ON job_posting (first_seen_at DESC)
  WHERE closed_at IS NULL AND filtered_out = FALSE;
CREATE INDEX posting_hash_idx         ON job_posting (content_hash);
CREATE INDEX posting_search_idx       ON job_posting USING GIN (search_tsv);
```

**Identity and change detection.** A posting is identified by
`(source_id, external_id)`. If a re-fetch yields the same identity with a
different `content_hash`, the row is updated and its `match_score` rows are
invalidated so it is rescored. `last_seen_at` is bumped every run; a posting not
seen for two consecutive runs gets `closed_at` set.

**Cross-source duplicates** (the same role appearing via both a company board
and a mail alert) are collapsed at ingest by `(company_id, normalised_title,
location_city)`, keeping the record whose source has the higher fidelity rank.

Two records sharing that key are collapsed **only** when they come from
different sources, or from the same source with the same `content_hash`. The
key is sufficient across sources — two ATSs render the same role differently, so
there is no comparable text and a title-and-place match is all the evidence
there is — but not within one, where several genuinely different openings
routinely share a title and a city. The first live registry collapsed 722
records and **685 of them had different descriptions**: three separate
"Software Engineer" roles in San Francisco read as one, nine per cent of every
posting held hidden from the operator. Inside one source the board renders the
same text every run, so an identical hash is one listing seen twice and a
different hash is a different job. `ingest/dedup.py::same_role` is the rule;
loosening it is a data-loss change, not a tuning change.

### 4.2 `requirement`

```sql
CREATE TABLE requirement (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  posting_id      CHAR(26)         NOT NULL REFERENCES job_posting(id) ON DELETE CASCADE,
  kind            requirement_kind NOT NULL,
  text            TEXT             NOT NULL,
  normalised_skill TEXT,
  weight          NUMERIC(3,2)     NOT NULL DEFAULT 1.00,
  ordinal         INTEGER          NOT NULL,
  extracted_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
  model           TEXT             NOT NULL,
  prompt_version  TEXT             NOT NULL
);

CREATE INDEX requirement_posting_idx ON requirement (posting_id, kind);
CREATE INDEX requirement_skill_idx   ON requirement (normalised_skill);
```

`normalised_skill` maps free text to a controlled vocabulary ("Strong C/C++
skills" → `cpp`) so coverage can be computed against the variant's skill set
without another model call.

---

## 5. Resume variants and the claims ledger

### 5.1 `resume_variant`

```sql
CREATE TABLE resume_variant (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key            TEXT        NOT NULL UNIQUE,   -- 'ai_product', 'backend', ...
  name           TEXT        NOT NULL,
  target         TEXT        NOT NULL,          -- who it is aimed at
  content        JSONB       NOT NULL,          -- structured resume (see §5.2)
  skill_set      TEXT[]      NOT NULL DEFAULT '{}',
  source_path    TEXT,                          -- the .docx builder input
  active         BOOLEAN     NOT NULL DEFAULT TRUE,
  deleted_at     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Seeded with the six existing variants: `ai_product`, `ai_enterprise`,
`ai_platform`, `backend`, `combined`, `consulting`.

`content` mirrors the structure already used by the resume builder — summary,
skill lines, experience blocks with bullets, projects, achievements — so a
variant can be rendered to `.docx` without a translation layer.

`skill_set` is the flattened, normalised skill vocabulary for that variant. It
is what coverage scoring matches `requirement.normalised_skill` against.

### 5.2 `claim` — the ledger

The single most important table in the system. Nothing may be asserted in a
generated document unless it resolves here.

```sql
CREATE TABLE claim (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key             TEXT        NOT NULL UNIQUE,   -- 'khelo.cost_reduction'
  statement       TEXT        NOT NULL,          -- canonical phrasing
  metric_value    TEXT,                          -- '60' | '924' | '1615'
  metric_unit     TEXT,                          -- 'percent' | 'crore_inr' | 'rating'
  project         TEXT        NOT NULL,          -- 'Khelo India Assistant'
  evidence_ref    TEXT        NOT NULL,          -- where it was verified from
  confidentiality claim_confidentiality NOT NULL DEFAULT 'internal',
  tags            TEXT[]      NOT NULL DEFAULT '{}',
  verified_at     TIMESTAMPTZ NOT NULL,
  expires_at      TIMESTAMPTZ,
  deleted_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX claim_tags_idx    ON claim USING GIN (tags);
CREATE INDEX claim_project_idx ON claim (project) WHERE deleted_at IS NULL;
```

`confidentiality` gates use: `restricted` claims are never emitted into a
document sent outside a named allow-list of employers. This is how internal cost
figures stay controllable per application rather than per resume file.

`expires_at` supports facts that decay — a test count or a corpus size is true
on a date and drifts afterwards.

### 5.3 `claim_usage`

```sql
CREATE TABLE claim_usage (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  claim_id     BIGINT   NOT NULL REFERENCES claim(id)    ON DELETE CASCADE,
  artifact_id  CHAR(26) NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
  location     TEXT     NOT NULL,   -- 'summary' | 'experience.2.bullet.1'
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (claim_id, artifact_id, location)
);
```

This is the provenance trail. Given any generated document, every number in it
can be traced to the ledger row that authorised it.

---

## 6. Scoring

### 6.1 `match_score`

```sql
CREATE TABLE match_score (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  posting_id      CHAR(26) NOT NULL REFERENCES job_posting(id)    ON DELETE CASCADE,
  variant_id      BIGINT   NOT NULL REFERENCES resume_variant(id) ON DELETE CASCADE,
  hard_met        INTEGER  NOT NULL,
  hard_total      INTEGER  NOT NULL,
  nice_met        INTEGER  NOT NULL,
  nice_total      INTEGER  NOT NULL,
  coverage_pct    NUMERIC(5,2) NOT NULL,
  composite_score NUMERIC(5,2) NOT NULL,
  gaps            JSONB    NOT NULL DEFAULT '[]',
  evidence        JSONB    NOT NULL DEFAULT '[]',
  is_recommended  BOOLEAN  NOT NULL DEFAULT FALSE,
  model           TEXT     NOT NULL,
  prompt_version  TEXT     NOT NULL,
  scored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (posting_id, variant_id, prompt_version)
);

CREATE INDEX match_posting_score_idx ON match_score (posting_id, composite_score DESC);
CREATE INDEX match_recommended_idx   ON match_score (composite_score DESC)
  WHERE is_recommended;
```

`gaps` is a list of `{requirement_id, kind, text, level, note}`. It is what the
cover letter's honest-gap paragraph is generated from, and what the UI shows as
"missing: RTOS, device drivers, RAID controllers".

`evidence` links each met requirement to the variant bullet and claim IDs that
satisfy it, so a coverage claim is always inspectable.

Exactly one row per posting has `is_recommended = TRUE` — the winning variant.

There is deliberately **no** `selection_probability` column. See
`MATCH_SCORING.md` §7 for why that number is not computable.

---

## 7. Review and applications

### 7.1 `review_item`

```sql
CREATE TABLE review_item (
  id                    CHAR(26)      PRIMARY KEY,
  posting_id            CHAR(26)      NOT NULL REFERENCES job_posting(id) ON DELETE CASCADE,
  recommended_variant_id BIGINT       NOT NULL REFERENCES resume_variant(id),
  match_score_id        BIGINT        NOT NULL REFERENCES match_score(id),
  status                review_status NOT NULL DEFAULT 'pending_review',
  tailoring_plan        JSONB         NOT NULL DEFAULT '{}',
  resume_artifact_id    CHAR(26)      REFERENCES artifact(id),
  cover_letter_artifact_id CHAR(26)   REFERENCES artifact(id),
  queued_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
  decided_at            TIMESTAMPTZ,
  decision_note         TEXT,
  UNIQUE (posting_id)
);

CREATE INDEX review_pending_idx ON review_item (queued_at DESC)
  WHERE status = 'pending_review';
```

`tailoring_plan` holds the proposed edits to the base variant — bullet swaps,
block reordering, skills-line adjustments — as a diff rather than a whole new
resume, so the change is reviewable at a glance.

### 7.2 `application`

```sql
CREATE TABLE application (
  id              CHAR(26)           PRIMARY KEY,
  posting_id      CHAR(26)           NOT NULL REFERENCES job_posting(id),
  review_item_id  CHAR(26)           REFERENCES review_item(id),
  variant_id      BIGINT             NOT NULL REFERENCES resume_variant(id),
  status          application_status NOT NULL DEFAULT 'submitted',
  submitted_at    TIMESTAMPTZ        NOT NULL,
  source_channel  TEXT               NOT NULL DEFAULT 'direct',  -- direct | referral
  referral_contact TEXT,
  resume_artifact_id       CHAR(26) REFERENCES artifact(id),
  cover_letter_artifact_id CHAR(26) REFERENCES artifact(id),
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (posting_id)
);

CREATE INDEX application_status_idx ON application (status, submitted_at DESC);
```

### 7.3 `application_event`

Append-only. Status on `application` is a materialised view of the latest event,
maintained by trigger; the event log is the truth.

```sql
CREATE TABLE application_event (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id   CHAR(26)           NOT NULL REFERENCES application(id) ON DELETE CASCADE,
  status           application_status NOT NULL,
  occurred_at      TIMESTAMPTZ        NOT NULL,
  detected_at      TIMESTAMPTZ        NOT NULL DEFAULT now(),
  email_message_id BIGINT             REFERENCES email_message(id),
  confidence       NUMERIC(3,2),
  excerpt          TEXT,
  is_manual        BOOLEAN            NOT NULL DEFAULT FALSE
);

CREATE INDEX app_event_app_idx ON application_event (application_id, occurred_at DESC);
```

`excerpt` stores a short quoted fragment of the classifying email so a status
change is always justifiable. Full bodies are not retained.

---

## 8. Artifacts and mail

### 8.1 `artifact`

```sql
CREATE TABLE artifact (
  id                CHAR(26)            PRIMARY KEY,
  kind              artifact_kind       NOT NULL,
  path              TEXT                NOT NULL,
  checksum          CHAR(64)            NOT NULL,
  variant_id        BIGINT              REFERENCES resume_variant(id),
  posting_id        CHAR(26)            REFERENCES job_posting(id),
  model             TEXT                NOT NULL,
  prompt_version    TEXT                NOT NULL,
  validation_status artifact_validation NOT NULL,
  validation_notes  JSONB               NOT NULL DEFAULT '[]',
  generated_at      TIMESTAMPTZ         NOT NULL DEFAULT now()
);

CREATE INDEX artifact_posting_idx ON artifact (posting_id, kind);
```

An artifact with `validation_status = 'failed'` is retained for diagnosis but
can never be attached to a `review_item` or `application` — enforced by a CHECK
trigger, not by application code alone.

### 8.2 `email_message`

```sql
CREATE TABLE email_message (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  gmail_id       TEXT        NOT NULL UNIQUE,
  thread_id      TEXT        NOT NULL,
  from_address   TEXT        NOT NULL,
  from_domain    TEXT        NOT NULL,
  subject        TEXT,
  received_at    TIMESTAMPTZ NOT NULL,
  classified_as  mail_class,
  confidence     NUMERIC(3,2),
  application_id CHAR(26)    REFERENCES application(id) ON DELETE SET NULL,
  company_id     BIGINT      REFERENCES company(id)     ON DELETE SET NULL,
  processed_at   TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX mail_unprocessed_idx ON email_message (received_at)
  WHERE processed_at IS NULL;
CREATE INDEX mail_thread_idx      ON email_message (thread_id);
```

Bodies are **not** stored. Classification runs on the fetched body in memory;
only the class, confidence and a short excerpt (on the event row) persist. This
keeps the blast radius of a database leak small and avoids retaining
correspondence the operator did not choose to keep.

---

## 9. Operations

### 9.1 `run_log`

```sql
CREATE TABLE run_log (
  id            CHAR(26)   PRIMARY KEY,
  run_type      TEXT       NOT NULL,   -- 'discovery' | 'mail' | 'export'
  status        run_status NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ,
  stats         JSONB      NOT NULL DEFAULT '{}',
  source_results JSONB     NOT NULL DEFAULT '[]',
  error         TEXT
);

CREATE INDEX run_log_recent_idx ON run_log (run_type, started_at DESC);
```

`source_results` is one entry per source: `{source_id, adapter, status,
fetched, new, errors, duration_ms}`. This is what the digest's failure section
and the Settings page health table read from.

---

## 10. Derived views

```sql
-- Funnel, sliced by whatever the UI groups on.
-- `responded` excludes 'withdrawn': an application the operator withdrew says
-- nothing about the employer's interest, so it is not a response. `withdrawn`
-- is reported separately so a net denominator (submitted - withdrawn) is
-- derivable from the view without a second query.
CREATE VIEW v_funnel AS
SELECT a.variant_id, c.tier, date_trunc('week', a.submitted_at) AS week,
       count(*)                                        AS submitted,
       count(*) FILTER (WHERE a.status NOT IN ('submitted','withdrawn')) AS responded,
       count(*) FILTER (WHERE a.status = 'withdrawn')   AS withdrawn,
       count(*) FILTER (WHERE a.status IN ('screening','interview','offer')) AS advanced,
       count(*) FILTER (WHERE a.status = 'interview')   AS interviewed,
       count(*) FILTER (WHERE a.status = 'offer')       AS offers
FROM application a
JOIN job_posting p ON p.id = a.posting_id
JOIN company c     ON c.id = p.company_id
GROUP BY 1,2,3;

-- Ghosting is computed, never stored
CREATE VIEW v_ghosted AS
SELECT a.id, a.posting_id, a.submitted_at,
       max(e.occurred_at) AS last_event_at
FROM application a
LEFT JOIN application_event e ON e.application_id = a.id
WHERE a.status IN ('submitted','acknowledged')
GROUP BY a.id
HAVING coalesce(max(e.occurred_at), a.submitted_at) < now() - interval '30 days';

-- Mail the classifier read but did not act on: below the auto-apply confidence
-- threshold, or not linkable to an application. Held for the operator, never
-- auto-expired. Behaviour and thresholds: EMAIL_INGESTION.md §7.3.
CREATE VIEW v_mail_review_queue AS
SELECT m.id, m.gmail_id, m.thread_id, m.from_address, m.from_domain,
       m.subject, m.received_at, m.classified_as, m.confidence,
       m.company_id, m.application_id
FROM   email_message m
WHERE  m.processed_at IS NOT NULL
  AND  m.classified_as IS NOT NULL
  AND  m.classified_as NOT IN ('job_alert', 'unrelated')
  AND  NOT EXISTS (SELECT 1 FROM application_event e
                   WHERE e.email_message_id = m.id)
  AND  (m.application_id IS NULL
        OR m.confidence < CASE
             WHEN m.classified_as IN ('rejection','offer') THEN 0.90
             ELSE 0.80 END);

-- Live applications that have gone quiet for longer than their status warrants.
-- Surfaced only; the system never sends a follow-up. Ranking rules and the
-- quiet-day thresholds: APPLICATION_PIPELINE.md §12.
CREATE VIEW v_followup_due AS
WITH last_event AS (
  SELECT application_id,
         max(occurred_at) AS at,
         count(*)         AS n
  FROM   application_event
  GROUP  BY 1
)
SELECT a.id            AS application_id,
       c.name          AS company_name,
       c.tier,
       p.title,
       a.status,
       a.source_channel,
       a.referral_contact,
       a.submitted_at,
       le.at           AS last_event_at,
       EXTRACT(DAY FROM now() - COALESCE(le.at, a.submitted_at))::int AS quiet_days,
       CASE
         WHEN a.status = 'submitted'    AND a.source_channel = 'referral' THEN 'ping_referrer'
         WHEN a.status = 'submitted'                                      THEN 'no_ack'
         WHEN a.status = 'acknowledged'                                   THEN 'no_progress'
         WHEN a.status IN ('screening','interview')                       THEN 'awaiting_next_step'
       END AS reason
FROM   application a
JOIN   job_posting p ON p.id = a.posting_id
JOIN   company     c ON c.id = p.company_id
LEFT   JOIN last_event le ON le.application_id = a.id
WHERE  a.status IN ('submitted','acknowledged','screening','interview')
  AND  COALESCE(le.at, a.submitted_at) < now() - interval '1 day'
    * CASE
        WHEN a.status IN ('screening','interview') THEN 7    -- live process: chase fast
        WHEN a.status = 'acknowledged'             THEN 10
        ELSE 14                                              -- no ack at all: wait longer
      END;
```

The funnel view is what tells the operator, after forty applications, which
resume variant and which company tier actually convert. That is the only honest
"selection percentage" the system will ever produce: **observed**, from their own
history, not predicted.

### 10.1 Functions and triggers

`application_event` is append-only and `application.status` is a materialised
fold over it (`APPLICATION_PIPELINE.md` §5, which is canonical for the semantics
of the fold). Two functions and one trigger implement that:

```sql
-- Orders the non-terminal statuses so the fold can pick the furthest-advanced
-- event. IMMUTABLE so it is usable in expressions and indexes. Terminal states
-- ('rejected','withdrawn') rank -1 and are handled separately by the fold.
CREATE FUNCTION application_status_rank(s application_status)
RETURNS INTEGER LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE s
    WHEN 'submitted'    THEN 0
    WHEN 'acknowledged' THEN 1
    WHEN 'screening'    THEN 2
    WHEN 'interview'    THEN 3
    WHEN 'offer'        THEN 4
    ELSE -1
  END;
$$;

-- Recomputes application.status from the whole event set on every change.
-- A pure fold, so it is order-independent and idempotent: replaying events,
-- importing a backlog, or a repair DELETE all converge on the same status.
CREATE FUNCTION fn_application_status_refresh()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
  v_app_id CHAR(26) := COALESCE(NEW.application_id, OLD.application_id);
  v_status application_status;
BEGIN
  SELECT COALESCE(
    -- A terminal event governs. The latest one wins if there are several.
    (SELECT e.status
       FROM application_event e
      WHERE e.application_id = v_app_id
        AND e.status IN ('rejected','withdrawn')
      ORDER BY e.occurred_at DESC, e.id DESC
      LIMIT 1),
    -- Otherwise the highest-rank live event.
    (SELECT e.status
       FROM application_event e
      WHERE e.application_id = v_app_id
      ORDER BY application_status_rank(e.status) DESC, e.occurred_at DESC, e.id DESC
      LIMIT 1),
    'submitted'::application_status      -- an application with no events yet
  ) INTO v_status;

  UPDATE application
     SET status = v_status, updated_at = now()
   WHERE id = v_app_id
     AND status IS DISTINCT FROM v_status;   -- no-op writes produce no row version

  RETURN NULL;
END;
$$;

-- AFTER, so the fold sees the new row. DELETE is covered so that a data-repair
-- migration cannot leave a stale status behind.
CREATE TRIGGER trg_application_status_refresh
AFTER INSERT OR UPDATE OR DELETE ON application_event
FOR EACH ROW EXECUTE FUNCTION fn_application_status_refresh();
```

`application.status` is therefore an index-support denormalisation only
(`application_status_idx`, §7.2). Where the column and the event log disagree,
the event log is right and the column is a bug.

---

## 11. Migration policy

- One Alembic revision per schema change, ID ≤ 32 characters.
- Revisions are forward-only in normal operation; downgrades are written but
  only exercised in development.
- Enum additions use `ALTER TYPE ... ADD VALUE` in a standalone revision, since
  that cannot run inside a transaction block alongside other DDL.
- Any migration touching `claim` or `artifact` must preserve `claim_usage`
  integrity — provenance is never orphaned.
- Seed data (the six resume variants, the initial claims ledger, the never-scrape
  list) ships as a separate idempotent seed script, not as a migration.
