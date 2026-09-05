# APPLICATION PIPELINE — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the `tracking/` module — the application lifecycle
state machine, transition legality, event-log semantics, funnel metric
definitions, the spreadsheet export and retention. `ARCHITECTURE.md` wins on
invariants and module boundaries; `DATA_MODEL.md` wins on tables, columns and
views; `API.md` wins on endpoint contracts. Where this file appears to disagree
with any of those three, this file is wrong.

---

## 1. Scope

This document covers what happens to a role after it has been scored: how it
becomes a queued draft, how a human turns it into a submitted application, how
outcomes are recorded, and what can honestly be concluded from the resulting
numbers.

It does not cover discovery, extraction or scoring (`MATCH_SCORING.md`), the
generation of resumes and cover letters (`DOCUMENT_GENERATION.md`), or how
status events arrive from mail (`EMAIL_INGESTION.md`). It begins at stage ⑩ of
the pipeline in `ARCHITECTURE.md` §6 and runs to the spreadsheet the operator
opens.

The design constraint that shapes everything below: **the system observes; it
does not act.** It cannot submit, cannot email a third party, and therefore
cannot know anything it was not told or shown. Every state in this machine is
either an operator assertion or an inference from mail, and the model
distinguishes the two rather than blurring them.

---

## 2. The full state machine

### 2.1 Diagram

```
                            ┌──────────────┐
                            │  discovered  │  job_posting row exists,
                            └──────┬───────┘  filtered_out = false
                                   │
                    ┌──────────────┴──────────────┐
                    │ scored above threshold      │ below threshold /
                    │ (SYSTEM, stage ⑩)           │ low fidelity
                    ▼                             ▼
             ┌──────────────┐              (no review_item;
             │    queued    │               visible in Jobs only)
             │ review_item  │
             │ pending_review│
             └──────┬───────┘
                    │ generation succeeded, artifacts validated
                    │ (SYSTEM, stage ⑧–⑨)
                    ▼
             ┌──────────────┐
             │   drafted    │  still review_item.status = 'pending_review',
             │              │  now with validated artifacts attached
             └──┬────────┬──┘
                │        │
  HUMAN skips   │        │  HUMAN approves — "I am going to submit this"
                ▼        ▼
        ┌───────────┐  ┌──────────────┐
        │  skipped  │  │   approved   │  review_item.status = 'approved'
        └───────────┘  └──────┬───────┘
                              │ same transaction
                              ▼
                       ┌──────────────┐
        ═══════════════│  submitted   │══════════════════════════════
        HUMAN SUBMITS  └──────┬───────┘   application.status
        on the employer's     │           = 'submitted'
        own site. The system  │
        never does this.      │
                              │
       ┌──────────┬───────────┼───────────┬───────────┐
       │          │           │           │           │
       ▼          ▼           ▼           ▼           ▼
┌────────────┐ ┌─────────┐ ┌──────────┐ ┌───────┐ ┌───────────┐
│acknowledged│▶│screening│▶│interview │▶│ offer │ │  rejected │◀── from any
└─────┬──────┘ └────┬────┘ └────┬─────┘ └───┬───┘ └───────────┘    live state
      │             │           │           │
      └─────────────┴───────────┴───────────┴────▶ ┌───────────┐
                                                    │ withdrawn │◀── operator
                                                    └───────────┘    only
      ╎
      ╎ no event for N days
      ▼
 ┌ ─ ─ ─ ─ ─ ┐
 │  ghosted  │   DERIVED — a view (v_ghosted), never a stored status
 └ ─ ─ ─ ─ ─ ┘
```

Solid boxes are stored states. The dashed box is computed. The double rule
across `submitted` is the compliance boundary: everything above it happens
inside the system, and the act that crosses it happens in a browser the system
does not drive.

### 2.2 Where each state physically lives

The lifecycle in `ARCHITECTURE.md` §7.1 is conceptual. It is stored across three
tables, and confusing them is the most likely source of a wrong query.

| Conceptual state | Storage | Predicate |
|---|---|---|
| `discovered` | `job_posting` | row exists, `filtered_out = false`, `closed_at IS NULL`, no `review_item` |
| `queued` | `review_item` | `status = 'pending_review'` |
| `drafted` | `review_item` | `status = 'pending_review'` **and** `resume_artifact_id IS NOT NULL` with `artifact.validation_status = 'passed'` |
| `needs_manual_review` | `review_item` | `status = 'needs_manual_review'` — scoring or generation failed; the item is surfaced, never silently dropped (`ARCHITECTURE.md` §8, error policy) |
| `approved` | `review_item` | `status = 'approved'`, `decided_at` set |
| `skipped` | `review_item` | `status = 'skipped'`, `decided_at` set, `decision_note` optional |
| `submitted` … `withdrawn` | `application` | `status` column, materialised from `application_event` |
| `ghosted` | — | `v_ghosted` (`DATA_MODEL.md` §10) |

`drafted` is deliberately **not** a distinct `review_status` value. It is
`pending_review` plus the presence of validated artifacts. Adding an enum value
for it would create a state the operator cannot act on, and a second place for
the artifact-attachment invariant (`DATA_MODEL.md` §8.1) to be violated.

### 2.3 State definitions

| State | Meaning | Evidence class |
|---|---|---|
| `discovered` | The role exists and passed the deterministic filter. | System observation |
| `queued` | The role scored well enough to be worth the operator's attention. | System judgement |
| `drafted` | A tailored resume plan and (where worthwhile) a cover letter exist and passed ledger validation. | System output |
| `skipped` | The operator looked and declined. | Operator decision |
| `approved` | The operator committed to submitting this application. | Operator decision |
| `submitted` | The operator asserts they submitted it on the employer's site. | **Operator assertion — unverifiable by the system** |
| `acknowledged` | The employer's system confirmed receipt. No human has necessarily read it. | Mail inference or manual |
| `screening` | A screening call, assessment or take-home was requested. | Mail inference or manual |
| `interview` | A formal interview round was scheduled or held. | Mail inference or manual |
| `offer` | An offer was extended. | Mail inference or manual |
| `rejected` | The application was closed unsuccessfully. | Mail inference or manual |
| `withdrawn` | The operator withdrew. | Operator decision only |
| `ghosted` | No event for `GHOST_AFTER_DAYS`. **Not a decision by anyone.** | Derived |

`submitted` is the only state that is an unverifiable assertion, and it is worth
being blunt about why: the system creates the `application` row at approve time
because that is the last moment it has any knowledge, and the actual submission
happens somewhere it cannot see. If the operator approves and then never
submits, the row is wrong until they correct it. The correction path is
`PATCH /api/v1/applications/{id}` (adjust `submitted_at`, or set the status to
`withdrawn`). This is a real limitation of not automating submission, and it is
a much smaller cost than the alternative.

---

## 3. Legal transitions

### 3.1 Pre-application (review item)

| From | To | Trigger | Endpoint |
|---|---|---|---|
| — | `pending_review` | System, stage ⑩ | — |
| `pending_review` | `needs_manual_review` | System — scoring or generation failed | — |
| `pending_review` | `approved` | **Human** | `POST /review/{id}/approve` |
| `pending_review` | `skipped` | **Human** | `POST /review/{id}/skip` |
| `needs_manual_review` | `pending_review` | System — successful regeneration | `POST /review/{id}/generate` |
| `needs_manual_review` | `skipped` | **Human** | `POST /review/{id}/skip` |
| `approved` / `skipped` | anything | — | **Illegal.** 409 `review.already_decided` (`API.md` §1). A decided item is final; a change of mind is expressed on the `application`, not by rewinding the queue. |

### 3.2 Application

Rank order, with `rejected` and `withdrawn` terminal:

| Status | Rank | Terminal |
|---|---|---|
| `submitted` | 0 | no |
| `acknowledged` | 1 | no |
| `screening` | 2 | no |
| `interview` | 3 | no |
| `offer` | 4 | no |
| `rejected` | — | **yes** |
| `withdrawn` | — | **yes** |

| From | To | Legal | Trigger |
|---|---|---|---|
| `submitted` | `acknowledged` | ✓ | Mail (`acknowledgement`) or manual |
| `submitted` | `screening` | ✓ | Mail (`screening_invite`) or manual — many employers skip the acknowledgement |
| `submitted` | `interview` | ✓ | Mail or manual |
| `submitted` | `offer` | ✓ | Manual, realistically — direct offers follow off-system conversations |
| `acknowledged` | `screening` / `interview` / `offer` | ✓ | Mail or manual |
| `screening` | `interview` / `offer` | ✓ | Mail or manual |
| `interview` | `offer` | ✓ | Mail or manual |
| any live state | `rejected` | ✓ | Mail (`rejection`) or manual |
| any live state | `withdrawn` | ✓ | **Operator only.** Never from mail. |
| any state | same state | ✗ | No-op. A second interview invitation for the same application adds no state; the event is dropped. |
| higher rank | lower rank | ✗ | Dropped. An acknowledgement arriving after an interview invite is a re-sent ATS template. |
| terminal | anything | ✗ | Dropped. Nothing follows `rejected` or `withdrawn`. |

```python
# tracking/transitions.py
RANK: dict[ApplicationStatus, int] = {
    ApplicationStatus.SUBMITTED: 0,
    ApplicationStatus.ACKNOWLEDGED: 1,
    ApplicationStatus.SCREENING: 2,
    ApplicationStatus.INTERVIEW: 3,
    ApplicationStatus.OFFER: 4,
}
TERMINAL = frozenset({ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN})


def may_append(current: ApplicationStatus, proposed: ApplicationStatus) -> bool:
    """The single authority on transition legality. Used by both the mail
    classifier and the manual-event endpoint — there is no second copy."""
    if current in TERMINAL:
        return False
    if proposed in TERMINAL:
        return True
    return RANK[proposed] > RANK[current]
```

**One exception, and it is explicit.** `POST /api/v1/applications/{id}/events`
accepts `"force": true` for a manual event only. It permits a legitimate
reversal — a rescinded rejection, a role reopened, a status entered in error —
and writes the event with `is_manual = true`. Automated classification can never
set `force`; the field is rejected on that path with 400
`event.force_not_permitted`. A human overriding the machine is a supported
operation; the machine overriding itself is not.

### 3.3 Who triggers what

| Actor | May cause |
|---|---|
| Discovery run (system) | `discovered`, `queued`, `drafted`, `needs_manual_review` |
| Mail poller (system) | `acknowledged`, `screening`, `interview`, `offer`, `rejected` |
| **Operator** | `approved`, `skipped`, `submitted`, `withdrawn`, and any status via a manual event |
| Nobody | `ghosted` — it is computed (§4) |

---

## 4. Why `ghosted` is a view, not a status

`DATA_MODEL.md` §10 defines it:

```sql
CREATE VIEW v_ghosted AS
SELECT a.id, a.posting_id, a.submitted_at,
       max(e.occurred_at) AS last_event_at
FROM application a
LEFT JOIN application_event e ON e.application_id = a.id
WHERE a.status IN ('submitted','acknowledged')
GROUP BY a.id
HAVING coalesce(max(e.occurred_at), a.submitted_at) < now() - interval '30 days';
```

Note what it is not: it is not in the `application_status` enum
(`DATA_MODEL.md` §2), and no code sets it.

**Absence of evidence is not evidence.** "Ghosted" asserts that the employer
decided not to proceed and did not say so. The system cannot observe that. What
it can observe is that thirty days passed and no mail arrived — which is
consistent with a rejection nobody sent, but equally consistent with:

- a rejection that landed in spam and was never fetched;
- a reply the classifier scored below threshold and held for review;
- a hiring process that is genuinely slow — Workday-era enterprise pipelines
  routinely run six weeks with no contact;
- a reply sent to a different address, or made by phone;
- a role frozen pending headcount approval, to be revived later.

Every one of those has a different correct response from the operator. Writing
`ghosted` into the status column would flatten them into one, would be
irreversible in the event log (an append-only log of a non-event), and would
corrupt the funnel: a ghosted application is not a rejection and must not be
counted as a response.

Three further consequences of it being a view:

1. **It is retroactive.** A reply arriving on day 45 removes the application
   from `v_ghosted` automatically, because the view reads the event log. A
   stored status would need a compensating transition, and somebody would forget
   to write it.
2. **The threshold is a parameter, not history.** Changing `GHOST_AFTER_DAYS`
   from 30 to 45 reclassifies everything instantly and reversibly. With a stored
   status, the threshold is baked into rows and the old ones are wrong forever.
3. **It never appears in the funnel's rate denominators as a response.** In
   `v_funnel`, ghosted applications sit in `submitted`/`acknowledged` where they
   belong.

The digest and the export both surface ghosted applications prominently, because
they are the most actionable population the operator has — see §12.

---

## 5. The event log is the truth

### 5.1 The principle

`application_event` is **append-only**. `application.status` is a materialised
convenience maintained by trigger. If the two ever disagree, the event log is
right and the column is a bug.

```
application_event                      application.status
─────────────────                      ──────────────────
2026-08-14 submitted    (manual)  ┐
2026-08-15 acknowledged (mail 0.91)├──▶  materialise()  ──▶  'interview'
2026-08-29 interview    (mail 0.93)┘
                                   └──▶ the column is a cache of this fold
```

Reasons this is not over-engineering for a single-user tool:

- **Every funnel number is a question about history**, not about the present.
  "How long from submission to first response, by company tier?" is unanswerable
  from a status column. It is a two-line query over the event log.
- **A status change must be justifiable.** `application_event.excerpt` and
  `.confidence` (`DATA_MODEL.md` §7.3) let the operator ask "why does this say
  rejected?" and get the sentence that caused it. A mutated column answers
  nothing.
- **Classifier quality is measurable.** Comparing automated events against
  subsequent manual corrections is the only way to know whether the mail
  classifier is any good. That comparison needs both to survive.
- **Reprocessing is safe.** Because materialisation is a pure fold over the
  whole event set, re-running the mail poller, replaying a backlog, or importing
  events out of order all converge on the same status (`EMAIL_INGESTION.md`
  §7.4).
- **Corrections are additive.** A wrong event is superseded by a forced manual
  event, not deleted. The record of the mistake is part of the record.

`application.status` exists at all only because `application_status_idx`
(`DATA_MODEL.md` §7.2) makes list views and filters fast. It is an index-support
denormalisation, nothing more.

### 5.2 The trigger

Recorded in `DATA_MODEL.md` §10.1, which is canonical for the definitions;
reproduced here because the behaviour below is what they are for.

```sql
-- Rank helper. IMMUTABLE so it is usable in expressions and indexes.
CREATE FUNCTION application_status_rank(s application_status)
RETURNS INTEGER LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE s
    WHEN 'submitted'    THEN 0
    WHEN 'acknowledged' THEN 1
    WHEN 'screening'    THEN 2
    WHEN 'interview'    THEN 3
    WHEN 'offer'        THEN 4
    ELSE -1                       -- terminal states are handled separately
  END;
$$;

-- Materialise application.status as a pure fold over the whole event set.
-- Order-independent and idempotent: replaying events cannot corrupt the result.
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

CREATE TRIGGER trg_application_status_refresh
AFTER INSERT OR UPDATE OR DELETE ON application_event
FOR EACH ROW EXECUTE FUNCTION fn_application_status_refresh();
```

Behaviour worth stating explicitly:

- **`AFTER … FOR EACH ROW`**, not `BEFORE` — the fold must see the new row.
- **`DELETE` is handled** even though events are never deleted in normal
  operation, so that a data-repair migration cannot leave a stale status.
- **The fold reads all events**, not just the new one. This is what makes
  out-of-order arrival and replay harmless. At a lifetime scale of low thousands
  of applications and a handful of events each (`ARCHITECTURE.md` §9), the cost
  is irrelevant; `app_event_app_idx` covers both subqueries.
- **`IS DISTINCT FROM` guard** prevents a write when nothing changed, so
  reprocessing does not churn `updated_at` or wake the export's change detection.
- **Terminal beats rank.** A `rejected` at rank -1 still governs over an
  `offer` at rank 4, which is the correct reading of "we are not proceeding"
  arriving after an offer was floated and pulled.

### 5.3 Append discipline

Only `tracking/events.py` writes to `application_event`, and it does one thing:

```python
async def append_event(
    session: AsyncSession,
    *,
    application_id: str,
    status: ApplicationStatus,
    occurred_at: datetime,
    email_message_id: int | None = None,
    confidence: Decimal | None = None,
    excerpt: str | None = None,
    is_manual: bool = False,
    force: bool = False,
) -> ApplicationEvent | None:
    """Append one event, or return None if the transition is not legal.

    force=True is accepted only when is_manual=True; the caller is a human
    correcting the record. See §3.2.
    """
    if force and not is_manual:
        raise ValueError("force is only permitted on manual events")

    app = await session.get(Application, application_id, with_for_update=True)

    # Idempotency: the same class of evidence never lands twice. (§5.4)
    if email_message_id is not None:
        dup = await session.scalar(
            select(ApplicationEvent.id).where(
                ApplicationEvent.application_id == application_id,
                ApplicationEvent.status == status,
                ApplicationEvent.email_message_id == email_message_id,
            )
        )
        if dup is not None:
            log.debug("event.duplicate", application_id=application_id, status=status)
            return None

    if not force and not may_append(app.status, status):
        log.info("event.dropped", application_id=application_id,
                 current=app.status, proposed=status)
        return None

    ev = ApplicationEvent(...)
    session.add(ev)
    await session.flush()      # the trigger materialises application.status here
    return ev
```

`with_for_update=True` serialises concurrent appends on one application — the
mail poller and a manual entry from the UI can otherwise both read `submitted`
and both append. At this scale contention is theoretical; correctness is not.

### 5.4 Idempotency guarantees

| Layer | Guarantee |
|---|---|
| `email_message.gmail_id UNIQUE` | A Gmail message is ingested once, so it can propose an event once. |
| Service-layer dedup on `(application_id, status, email_message_id)` | The same message never produces the same transition twice, even if re-ingested. |
| `may_append` | A repeat of an already-reached status is not appended, whatever proposes it. |
| Order-independent trigger fold | Even a duplicated or out-of-order event leaves `application.status` correct. |

---

## 6. Manual events

Most of what actually happens in a job search does not happen in email. A phone
screen is scheduled by phone. A recruiter messages on LinkedIn — which the
system cannot read, by invariant. A hiring manager says "we're moving you to the
final round" in the interview itself. A friend at the company reports the role
was frozen.

```http
POST /api/v1/applications/{id}/events
```

```jsonc
// request
{
  "status": "interview",
  "occurred_at": "2026-09-04T11:30:00+05:30",
  "note": "Recruiter called — technical round scheduled for 11 Sep, 4pm IST.",
  "force": false
}

// 201
{
  "data": {
    "id": 4417,
    "application_id": "01JAX9F2K…",
    "status": "interview",
    "occurred_at": "2026-09-04T06:00:00Z",
    "detected_at": "2026-09-04T06:04:11Z",
    "email_message_id": null,
    "confidence": null,
    "excerpt": "Recruiter called — technical round scheduled for 11 Sep, 4pm IST.",
    "is_manual": true
  },
  "message": "Event recorded."
}
```

Rules:

- **`is_manual = true` is set by the endpoint**, never by the client. It is not
  a request field.
- **`confidence` is NULL** on manual events. A human is not a probabilistic
  classifier and a fabricated 1.00 would pollute every confidence analysis.
- **`email_message_id` is NULL.** The evidence is the human.
- **`occurred_at` is required** and may be backdated. Accurate timing is what
  makes time-to-response measurable; "when I got round to entering it" is not.
  It is rejected if it precedes `application.submitted_at` or lies in the future.
- **`note` becomes `excerpt`**, truncated to 200 characters, matching the mail
  path so both kinds of evidence render identically in the UI.
- **`force`** is accepted here and only here (§3.2).

### 6.1 Why the distinction is preserved

`is_manual` is not bookkeeping; it is what makes several questions answerable:

| Question | Query |
|---|---|
| How much of the pipeline does automation actually see? | `count(*) FILTER (WHERE NOT is_manual) / count(*)` over events |
| Is the mail classifier trustworthy? | Automated events later superseded by a forced manual event of a different status |
| Are the funnel rates measuring reality or measuring email? | Funnel computed over all events vs. automated-only events; a large gap means the numbers describe employers' mail habits, not outcomes |
| Which employers never email? | Companies whose applications advance only through manual events — usually the ones where a referral is doing the work |

Mixing the two would make all four unanswerable, permanently and silently. The
column costs one boolean.

---

## 7. The two human gates

`ARCHITECTURE.md` §6.1 shows them; this is what they are as pipeline stages.

```
   drafted
      │
 ┌────┴──────────────────────────────────────────────────────┐
 │ GATE 1 — APPROVE                                          │
 │ POST /api/v1/review/{id}/approve                          │
 │                                                            │
 │ The operator has read the posting, the coverage, the named │
 │ gaps and the drafts, and commits: "I am going to submit    │
 │ this myself."                                              │
 │                                                            │
 │ System does: review_item.status = 'approved'               │
 │              freeze the artifacts (checksum recorded)      │
 │              create application row, status 'submitted'    │
 │              return download links                         │
 │ System does NOT: contact the employer in any way           │
 └────┬───────────────────────────────────────────────────────┘
      │
 ┌────┴──────────────────────────────────────────────────────┐
 │ GATE 2 — SUBMIT                                           │
 │ (no endpoint — this happens in the operator's browser)    │
 │                                                            │
 │ The operator downloads the .docx files, opens the          │
 │ employer's careers page, fills the form, uploads, answers  │
 │ the screening questions, and presses Submit.               │
 │                                                            │
 │ System does: nothing. It cannot observe this and does not  │
 │              try to.                                       │
 └────────────────────────────────────────────────────────────┘
```

### 7.1 Approve does not submit

The endpoint is named `approve` and not `submit` deliberately (`API.md` §5).
Approve records a *commitment*; the submission is a separate physical act by a
human on a third party's website.

**There is no endpoint that submits. There will not be one at any version.** It
is invariant 1 from `ARCHITECTURE.md` §3, and `API.md` §8 records its absence as
the enforcement mechanism: the most reliable way to guarantee a rule is to give
it nowhere to be called from. Concretely, the system never:

- POSTs an application payload to an employer or ATS endpoint;
- drives Playwright against an application form (Playwright exists in the stack
  for *reading* boards without an API, and `sources/` is the only package
  permitted to instantiate it);
- completes a CAPTCHA, or routes one to a solving service;
- uploads a resume file anywhere;
- creates an account on an employer's ATS.

### 7.2 Why the gates are where they are

The gates are not a safety valve on an otherwise-automatic system. They are the
point of the design.

- **Submission is where the compliance exposure is.** Automated submission
  breaches essentially every ATS's terms of use. A shared platform like
  Greenhouse or Workday can flag an applicant across every employer on it
  simultaneously — the damage is not per-application, it is per-career.
- **Submission is where the judgement is.** Application forms carry
  employer-specific screening questions, salary expectations, notice periods,
  visa declarations. Answering those with a language model is how a truthful
  application becomes an untruthful one.
- **Volume is the failure mode being designed against.** Ten minutes a day and
  five to ten good applications a week (`ARCHITECTURE.md` §1.1) beats two
  hundred generic ones. A submit endpoint would delete the constraint that makes
  the whole thing work, and everything downstream — coverage scores, the claims
  ledger, honest gap paragraphs — would become decoration on a spam cannon.

### 7.3 The consequence for the data

Gate 2 is invisible to the system, so `submitted` is an assertion (§2.3). Two
mitigations, both cheap:

- Applications approved but with no event for `SUBMIT_CONFIRM_DAYS` (default 3)
  are surfaced in the digest as "approved but possibly not submitted — confirm
  or withdraw", so a forgotten download does not sit in the funnel as a real
  application forever.
- `PATCH /api/v1/applications/{id}` lets the operator correct `submitted_at` or
  set `withdrawn`. Withdrawn applications are excluded from every rate
  denominator (§8.3).

---

## 8. Funnel metrics

### 8.1 The view

`v_funnel` (`DATA_MODEL.md` §10) is the base aggregation, grouped by
`variant_id`, `company.tier` and ISO week. `GET /api/v1/metrics/funnel`
(`API.md` §6) serves it, grouped by `variant`, `tier`, `week` or
`source_channel`.

### 8.2 Counter definitions

| Counter | Definition | Notes |
|---|---|---|
| `submitted` | Every `application` row in the slice. | The gross denominator. Includes ghosted and withdrawn. |
| `responded` | `status NOT IN ('submitted','withdrawn')` — the employer moved the application off its initial state. | Per `v_funnel`. Withdrawals are excluded in the view itself: an application the operator withdrew says nothing about the employer's interest. |
| `withdrawn` | `status = 'withdrawn'` | Reported separately so the net denominator `submitted − withdrawn` is derivable without a second query. |
| `advanced` | `status IN ('screening','interview','offer')` | Someone chose to spend time on the candidate. |
| `interviewed` | `status = 'interview'` | **Current** status only. An application that reached interview and was then rejected is not counted here. §8.4. |
| `offers` | `status = 'offer'` | Same caveat as `interviewed`. |
| `response_rate` | `responded / (submitted − withdrawn)` | Net denominator throughout: a withdrawal belongs in neither numerator nor denominator. |
| `interview_rate` | `interviewed / (submitted − withdrawn)` | |
| `offer_rate` | `offers / (submitted − withdrawn)` | |

### 8.3 The correction the API layer applies

Withdrawals need no correction here — `v_funnel` (`DATA_MODEL.md` §10) already
excludes them from `responded` and reports them as their own column, so
`response_rate` is computed as `responded / NULLIF(submitted - withdrawn, 0)`
straight from the view. One counter, however, still answers a different question
from the one the operator is asking, so `GET /metrics/funnel` and the export
report an extra block alongside the view.

**Reached-stage, not current-stage.** `interviewed` above counts applications
*sitting at* `interview`. The operator wants to know how many ever *got to*
interview, which is a question about the event log:

```sql
-- Ever-reached counters. Computed over application_event, not application.status.
SELECT a.variant_id, c.tier,
       count(*)                                                   AS submitted,
       count(*) FILTER (WHERE ev.max_rank >= 1)                   AS ever_acknowledged,
       count(*) FILTER (WHERE ev.max_rank >= 2)                   AS ever_screened,
       count(*) FILTER (WHERE ev.max_rank >= 3)                   AS ever_interviewed,
       count(*) FILTER (WHERE ev.max_rank >= 4)                   AS ever_offered
FROM   application a
JOIN   job_posting p ON p.id = a.posting_id
JOIN   company     c ON c.id = p.company_id
LEFT   JOIN LATERAL (
         SELECT max(application_status_rank(e.status)) AS max_rank
         FROM   application_event e
         WHERE  e.application_id = a.id
       ) ev ON TRUE
WHERE  a.status <> 'withdrawn'
GROUP  BY 1, 2;
```

Both the current-stage and ever-reached numbers are reported. They answer
different questions — "where is my pipeline right now" and "what fraction of my
applications ever got a human's attention" — and conflating them is how a funnel
chart becomes misleading.

### 8.4 Time-to-event

Only computable because the event log exists:

```sql
SELECT c.tier,
       percentile_cont(0.5) WITHIN GROUP (
         ORDER BY EXTRACT(EPOCH FROM (e.occurred_at - a.submitted_at)) / 86400.0
       ) AS median_days_to_first_response,
       count(*) AS n
FROM   application a
JOIN   job_posting p ON p.id = a.posting_id
JOIN   company     c ON c.id = p.company_id
JOIN   LATERAL (
         SELECT min(occurred_at) AS occurred_at
         FROM   application_event
         WHERE  application_id = a.id AND status <> 'submitted'
       ) e ON e.occurred_at IS NOT NULL
GROUP  BY 1;
```

This is what turns `GHOST_AFTER_DAYS` from a guess into a measurement: if the
90th percentile of first response for `dream`-tier companies is 34 days, a
30-day ghosting threshold is wrong for that tier.

### 8.5 Slicing

| Slice | Column | Purpose |
|---|---|---|
| Resume variant | `application.variant_id` | Which of the six variants converts. The primary question. |
| Company tier | `company.tier` | Dream/strong/volume conversion differs by an order of magnitude. Never pool them. |
| Source channel | `application.source_channel` (`direct` \| `referral`) | See §9.4. Segment before every other comparison. |
| Week | `date_trunc('week', submitted_at)` | Trend, and the only way to see a market or resume change take effect. |
| Coverage band | `match_score.coverage_pct` bucketed | Does applying below 55% coverage ever work? A real, answerable question after enough data. |
| Cover letter | `cover_letter_artifact_id IS NOT NULL` | Whether the letters are worth generating at all. |

---

## 9. Statistical honesty

This is the section most likely to be ignored and most likely to cause harm if
it is. The funnel produces small numbers, and small numbers lie confidently.

### 9.1 What ten applications tell you

Nothing. Ten applications with three responses is a 30% response rate with a 95%
Wilson interval of roughly **11%–60%**. The true rate could be one in nine or
three in five, and the data cannot distinguish them.

| n | Observed 30% | 95% Wilson interval | Width |
|---|---|---|---|
| 10 | 3/10 | 11% – 60% | 49 pts |
| 20 | 6/20 | 15% – 52% | 37 pts |
| 40 | 12/40 | 18% – 46% | 28 pts |
| 80 | 24/80 | 21% – 41% | 20 pts |
| 150 | 45/150 | 23% – 38% | 15 pts |

At ten, the interval is wider than the plausible range of true values — the
measurement adds nothing to a prior guess. At forty it begins to exclude
possibilities: a 30% observed rate at n=40 rules out "this is basically not
working" (5%) and "this is working brilliantly" (60%). That is genuine
information, and it is the point at which the numbers start being worth looking
at.

At five to ten applications a week (`ARCHITECTURE.md` §1.1), **n=40 arrives at
week six.** The UI and export suppress computed rates below `MIN_N_FOR_RATE`
(default 15) and show the raw count instead. A rate is not displayed until it
means something.

```python
def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval. Correct at small n, unlike the normal
    approximation, which produces intervals extending below zero."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    d = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))
```

### 9.2 Comparing two resume variants

The tempting move — `ai_product` got 4 responses from 12, `backend` got 2 from
14, therefore use `ai_product` — is wrong. Those proportions (33% vs 14%) are
not distinguishable at that sample size; a two-proportion test gives p ≈ 0.24.
The apparent 19-point gap is well inside what coin-flipping produces.

Sample size actually required to detect a difference, at α=0.05 and 80% power:

| True rates | n **per arm** |
|---|---|
| 10% vs 20% | ~200 |
| 10% vs 25% | ~110 |
| 10% vs 30% | ~60 |
| 20% vs 40% | ~80 |
| 15% vs 45% | ~35 |

At a realistic pace, 200 applications per arm is **two years**. This is not a
reason to abandon the measurement; it is a reason to be honest about what it can
support.

**The operating rule** — all four conditions, or do not act:

1. **≥ 30 submissions in each arm.**
2. The arms are **matched on company tier** and **restricted to
   `source_channel = 'direct'`** (§9.4).
3. The observed difference is **at least 2×** in relative terms (not 15% vs 20%;
   15% vs 30%).
4. The **Wilson intervals do not overlap**.

Below that bar the correct action is to keep both variants in rotation and keep
collecting. The UI states the sample size next to every rate and greys out
comparisons that fail the bar, rather than leaving the operator to eyeball two
percentages and reach the obvious wrong conclusion.

### 9.3 Over-fitting to noise

The realistic failure is not one bad inference; it is a slow drift built from
many small ones. It looks like this: a fintech application gets an interview, so
fintech goes to the top of the target list; two rejections from a variant, so
that variant is retired; a Tuesday application converts, so Tuesday becomes the
submission day. Six weeks later the strategy is a monument to twelve coin flips.

Guards, all implemented rather than advised:

- **The dashboard never ranks slices by rate below `MIN_N_FOR_RATE`.** A
  leaderboard of noisy rates is an invitation to over-fit; it is not built.
- **Every rate ships with its interval and its `n`**, in the UI, the API
  (`meta.n`, `meta.ci_low`, `meta.ci_high`) and the spreadsheet. A bare
  percentage is never displayed.
- **Retiring a variant requires a deliberate action** (`active = false` on
  `resume_variant`) with a note. It is never suggested by the system.
- **Pre-register the comparison.** Decide which variant pairing is being tested
  before looking at the data, and record it in the settings note. Testing every
  pairing after the fact and reporting the winner is how a 5% false-positive
  rate becomes a 40% one across six variants.
- **The multiplicity is real.** Six variants, three tiers, two channels is 36
  cells. At n=40 total, most cells hold one application. The export therefore
  shows the tier × channel grid with counts only, and rates only where the cell
  clears `MIN_N_FOR_RATE`.

### 9.4 Referrals will dominate, and must be segmented out

`application.source_channel` is `direct` or `referral` (`DATA_MODEL.md` §7.2).
Referral applications convert several times better than cold ones — an internal
referral typically bypasses the resume screen entirely, which is the stage where
most direct applications die.

Therefore:

> **Any comparison between resume variants, company tiers, coverage bands or
> weeks that pools referral and direct applications is meaningless.**

The mechanism is straightforward and completely destroys the comparison. Suppose
three referrals happen to be in the `ai_product` arm because the operator's
network is in AI product companies. Those three convert at 60% while direct
applications convert at 8%. `ai_product` now shows a "response rate" of 30%
against `backend`'s 9%, and the entire difference is the referrals — the resume
did no work at all. Acting on it means rewriting a perfectly good variant to
resemble one that was never tested.

Implementation:

- **`GET /metrics/funnel` defaults to `source_channel=direct`** for any grouping
  by `variant`. Pooling requires the caller to ask for it explicitly.
- The spreadsheet's Funnel sheet is **split into a Direct block and a Referral
  block**, never a combined one.
- Referral cells always display `n`, because the referral population will be
  small enough that it is never a rate — it is a list of individual outcomes,
  which is the honest way to present five data points.
- The referral rate is nonetheless the single most useful number the system
  produces. When it is 5× the direct rate, the correct conclusion is not "change
  the resume" but "spend the ten minutes a day on finding a referral instead of
  a fourth application." The pipeline is allowed to conclude that about itself.

---

## 10. Observed versus predicted

Every number in this document is **observed**: a count of things that happened,
divided by a count of things that were tried.

The system does not, at any version, produce a **predicted probability of
selection** for a given role. There is no "you have a 34% chance at this job"
anywhere in the UI, the API or the export. `match_score` deliberately has no
`selection_probability` column (`DATA_MODEL.md` §6.1), and the reasoning lives
in `MATCH_SCORING.md` §7.

The short form of that reasoning, restated so this document stands alone: the
outcome depends on the other applicants, the internal candidate, the referral
nobody told you about, the headcount freeze signed the day after the posting
went up, the interviewer's morning, and whether the requisition was ever real.
None of that is observable from a job description and a resume. A model can
produce a number; it cannot produce a *calibrated* one, and an uncalibrated
probability presented to a decision-maker is worse than no number, because it
will be believed.

What the system provides instead, and what the operator should use:

| Question | Answer the system gives |
|---|---|
| How well does my experience cover this posting's stated requirements? | `coverage_pct`, with the requirement-by-requirement evidence and the named gaps. Inspectable, defensible, not a probability. |
| What am I missing for this role? | `match_score.gaps` — "missing: Kubernetes operators, Go". Actionable. |
| Historically, what fraction of my applications at this coverage band got a response? | An observed rate over the operator's own history, with `n` and a confidence interval. |
| What are my chances here? | **Not answered.** The system does not know and says so. |

`API.md` §6 pins this into the API surface with
`meta.note: "Observed rates from your own history. Not a prediction."` That note
is load-bearing and is not to be removed.

---

## 11. Spreadsheet export

The `.xlsx` is what the operator actually opens. The React app is for the daily
ten minutes; the spreadsheet is for the Sunday hour, for sorting and filtering in
ways nobody will build a UI for, and for existing outside the system if the
system ever stops running.

`POST /api/v1/exports/spreadsheet` (202, `API.md` §6) and a nightly job at
**23:30 IST** both produce it, via `openpyxl`.

### 11.1 Workbook structure

Four sheets, one per view, in this order.

#### Sheet 1 — `Pipeline` (one row per application)

| Col | Header | Type | Source |
|---|---|---|---|
| A | Ref | text | `application.id` (ULID) |
| B | Company | text | `company.name` |
| C | Tier | text | `company.tier` |
| D | Role | text | `job_posting.title` |
| E | Location | text | `job_posting.location_city` / `Remote` |
| F | Variant | text | `resume_variant.key` |
| G | Coverage % | 0.0% | `match_score.coverage_pct` |
| H | Hard met | text | `"6 / 8"` |
| I | Channel | text | `application.source_channel` |
| J | Referral | text | `application.referral_contact` |
| K | Submitted | dd-mmm-yyyy | `application.submitted_at` (IST) |
| L | Status | text | `application.status` |
| M | Reached | text | Highest status ever reached (§8.3) |
| N | Last event | dd-mmm-yyyy | `max(application_event.occurred_at)` |
| O | Days since | 0 | Days since N, or since K if no events |
| P | Days to first reply | 0 | NULL if none yet |
| Q | Ghosted | text | `Yes` if in `v_ghosted` |
| R | Events | 0 | Count |
| S | Manual events | 0 | Count where `is_manual` |
| T | Cover letter | text | `Yes` / `No` / `N/A (no field)` |
| U | Posting URL | hyperlink | `job_posting.url` |
| V | Notes | text | `application.notes` |

#### Sheet 2 — `Companies` (one row per tracked company)

| Col | Header | Type |
|---|---|---|
| A | Company | text |
| B | Tier | text |
| C | Status | text |
| D | Tags | text (comma-joined) |
| E | Sources | 0 |
| F | Source health | text — `OK` / `n failing` / `disabled` |
| G | Open postings | 0 |
| H | New this week | 0 |
| I | Applications | 0 |
| J | Responses | 0 |
| K | Interviews | 0 |
| L | Offers | 0 |
| M | Response rate | 0.0% — **blank when I < `MIN_N_FOR_RATE`** |
| N | Median days to reply | 0.0 |
| O | Cover letter worth | text |
| P | Careers URL | hyperlink |

#### Sheet 3 — `Funnel`

Not one table — three blocks separated by a blank row, so the segmentation is
physically impossible to ignore:

```
BLOCK A — DIRECT APPLICATIONS ONLY  (source_channel = 'direct')
  rows: one per (variant × tier)
  cols: Variant | Tier | Submitted | Acknowledged | Screening | Interview |
        Offer | Rejected | Ghosted | Resp % | CI low | CI high | Int % | n

BLOCK B — REFERRAL APPLICATIONS  (source_channel = 'referral')
  Counts only. The Rate columns are literally absent from this block, not
  blank — there is no cell to misread.

BLOCK C — BY WEEK  (direct only)
  rows: one per ISO week, most recent first
  cols: Week starting | Submitted | Responded | Advanced | Interviewed |
        Offers | Resp % | n
```

Row 1 of the sheet carries a fixed caption:

> *Rates are observed outcomes from your own history, not predictions. Rates are
> blank below n=15. Direct and referral applications are never combined — see
> APPLICATION_PIPELINE.md §9.4.*

#### Sheet 4 — `Sources`

| Col | Header | Type |
|---|---|---|
| A | Company | text |
| B | Adapter | text |
| C | Enabled | text |
| D | Last run | dd-mmm-yyyy hh:mm |
| E | Last status | text |
| F | Consecutive failures | 0 |
| G | Postings fetched (7d) | 0 |
| H | New postings (7d) | 0 |
| I | Last error | text |

Read from the latest `run_log.source_results` per source (`DATA_MODEL.md` §9.1),
which is the same data the digest's failure section uses.

### 11.2 Formatting

Applied uniformly; the workbook is meant to be usable without any manual tidying.

- **Header row:** bold, white on `#1F2937`, frozen (`freeze_panes = "A2"`,
  `"C2"` on Pipeline so Company and Role stay visible while scrolling right).
- **Autofilter** on the header row of every sheet.
- **Column widths** computed from the 95th-percentile content length, clamped to
  `[8, 46]`.
- **Dates** `dd-mmm-yyyy`, rendered in IST (`ARCHITECTURE.md` §8 — stored UTC,
  displayed Asia/Kolkata).
- **Percentages** `0.0%`, never a bare float.
- **URLs** as real hyperlinks with display text `Open`, not raw URLs — a column
  of 200-character Workday URLs makes the sheet unreadable.
- **Status column** uses a data bar and a colour scale, not conditional text:
  `submitted` grey, `acknowledged` blue, `screening`/`interview` green,
  `offer` bold green, `rejected` light red, `withdrawn` grey italic.
- **Sheet order and tab colours** fixed; Funnel's tab is amber to mark it as the
  one requiring interpretation.

### 11.3 Conditional highlighting for stale applications

The single most useful piece of formatting in the workbook. Applied to
`Pipeline` column O (*Days since*), with the whole row shaded:

| Condition | Fill | Meaning |
|---|---|---|
| Status terminal (`rejected`, `withdrawn`) | none, text greyed | Closed. No action. |
| Days since ≤ 7 | none | Normal. |
| 8 – 14 | `#FEF3C7` (pale amber) | Getting quiet. |
| 15 – 29 | `#FDE68A` (amber) | Warrants a nudge — appears in the digest's follow-up section. |
| ≥ 30 and status in (`submitted`,`acknowledged`) | `#FCA5A5` (red) | In `v_ghosted`. |
| Status = `approved` with no events and > 3 days | `#DDD6FE` (violet) | **Approved but possibly never submitted** (§7.3). |

The violet band is there because it catches the system's one structural blind
spot — Gate 2 being invisible — and turns it into a visible row the operator can
resolve in one click.

### 11.4 Generation and schedule

```python
# tracking/export.py
def build_workbook(session, *, as_of: datetime) -> Path:
    wb = Workbook()
    _pipeline(wb.active, session, as_of)
    _companies(wb.create_sheet("Companies"), session)
    _funnel(wb.create_sheet("Funnel"), session)
    _sources(wb.create_sheet("Sources"), session)
    wb.properties.title = f"Scout Careers pipeline — {as_of:%d %b %Y}"
    path = EXPORT_DIR / f"scout-pipeline-{as_of:%Y-%m-%d}.xlsx"
    wb.save(path)                                     # atomic: tmp + replace
    return path
```

- **Nightly at 23:30 IST**, after the day's mail polls have closed, so the file
  reflects a complete day.
- Written to `EXPORT_DIR` and registered as a `run_log` row with
  `run_type = 'export'`.
- `scout-pipeline-latest.xlsx` is a symlink to the newest file, so a saved
  desktop shortcut never goes stale.
- Generation is read-only against the database and holds no long transaction; at
  this scale the whole build is well under a second.
- Retention: 30 daily files, plus the last file of each month kept for 24 months.

---

## 12. Follow-up prompts

The system identifies applications that warrant a nudge. It does not nudge. This
is invariant 2 (`ARCHITECTURE.md` §3, `EMAIL_INGESTION.md` §1) at the point
where it is most tempting to break.

### 12.1 The query

Recorded in `DATA_MODEL.md` §10, which is canonical for the definition.

```sql
-- Applications worth a human follow-up. Surfaced only; nothing is sent.
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

### 12.2 Ranking and rules

Not every row is worth surfacing. The digest and dashboard apply:

| Rule | Reason |
|---|---|
| **Cap at `FOLLOWUP_MAX_PER_DAY` (default 3)**, ordered by tier (`dream` first), then `quiet_days` descending. | A list of forty overdue applications gets ignored. Three get acted on. |
| **Suppress where there is no human to contact** — the only known address is a shared ATS no-reply domain and no `referral_contact` exists. | Following up into `no-reply@greenhouse.io` accomplishes nothing and the prompt would be noise. |
| **Suppress `dream`-tier applications younger than 21 days.** | Large enterprise pipelines are genuinely slow; a two-week nudge reads as impatience. |
| **A given application is surfaced at most `FOLLOWUP_MAX_PER_APP` times (default 2)**, tracked by a `followup_surfaced` counter in `application.notes` metadata. | Two unanswered follow-ups is the answer. |
| **Never for terminal or ghosted-past-45-days applications.** | Closed is closed. |

### 12.3 What the operator gets

A prompt, context, and a link. Nothing more:

```text
Worth a nudge (2):

  Seagate Technology · Analyst II, Financial Modeling & AI   [strong]
    Submitted 21 days ago. No acknowledgement.
    You have a referral contact on file: R. Nair.
    Suggested: ask R. Nair whether the requisition is still open before
    contacting the recruiter directly.
    → https://scout.local/applications/01JAW2H8N…

  Zeta · Senior Backend Engineer                             [strong]
    Interview on 4 Sep. 9 days quiet.
    Last contact: recruiter, priya.s@zeta.tech
    → https://scout.local/applications/01JAX9F2K…

Nothing has been sent. These are reminders for you to act on.
```

That closing line is fixed text and appears in every digest that contains a
follow-up section. It exists so that the operator never has to wonder, and so
that anyone reading the codebase later understands the section is a reminder and
not a log of sent mail.

The UI offers a **copy-to-clipboard** draft of a follow-up message — composed
from the application's own facts, subject to the same ledger validation as every
other generated text (`ARCHITECTURE.md` §3.3). It goes to the clipboard. It does
not go to a `To:` field, because there is no code path that has one.

---

## 13. Retention

Storage is not the constraint at this scale (`ARCHITECTURE.md` §9); relevance
is. A `postings` table with 400,000 dead rows makes search worse and the UI
slower for no benefit. Pruning runs weekly, Sunday 04:00 IST, as `run_type =
'prune'`.

| Data | Retained | Pruned | Rationale |
|---|---|---|---|
| `job_posting` — closed, never scored, no application | 90 days after `closed_at` | Hard delete | A closed role nobody looked at is noise. |
| `job_posting` — scored but never queued | 180 days after `closed_at` | Hard delete | Keeps enough history to analyse what the filter and scorer rejected. |
| `job_posting` — referenced by a `review_item` or `application` | **Forever** | Never | Deleting it would orphan the funnel. FK-protected. |
| `job_posting.raw` (JSONB) | 30 days | Column nulled, row kept | The raw payload is a debugging aid for the adapter, not a record. Largest single consumer of table size. |
| `job_posting.description_html` | 30 days | Column nulled | `description_text` is what everything reads. HTML is kept briefly for parser diagnosis. |
| `requirement` | Life of the posting | Cascade | |
| `match_score` — superseded `prompt_version` | Latest 2 versions per posting | Hard delete | Enough to compare a prompt change; not a full archive. |
| `review_item` | **Forever** | Never | The skip decisions are data — they are the only record of what the operator declined and why. |
| `application`, `application_event` | **Forever** | Never | The entire evidential basis of every metric. Never pruned, never edited. |
| `artifact` — attached to an `application` | **Forever** | Never | What was actually sent. Needed to answer "what did I claim to them?" |
| `artifact` — attached only to a skipped `review_item` | 90 days | File deleted, row kept with `path = NULL` | Provenance survives; the bytes do not. |
| `artifact` — `validation_status = 'failed'` | 30 days | File and row deleted | Retained only long enough to diagnose the failure (`DATA_MODEL.md` §8.1). |
| `claim`, `claim_usage` | **Forever** | Never | Soft delete only. Provenance is never orphaned (`DATA_MODEL.md` §11). |
| `email_message` | 24 months | Hard delete, **except** rows referenced by an `application_event` | Bodies were never stored (`EMAIL_INGESTION.md` §9); this prunes the metadata. Rows justifying an event are kept forever — `ON DELETE SET NULL` would break the audit trail, so the prune query excludes them. |
| `run_log` | 12 months | Hard delete | |
| `run_log.source_results` | 90 days | Column reset to `'[]'` | The per-source detail is operationally useful for a quarter; the summary stats stay for a year. |
| Spreadsheet exports | 30 daily + monthly for 24 months | File deleted | §11.4 |
| Digest HTML fallbacks | 30 days | File deleted | |

Two rules that override the table:

1. **Nothing referenced by an `application` or `application_event` is ever
   pruned.** The prune job is written as a set of `DELETE … WHERE NOT EXISTS
   (…)` statements and every one of them is covered by a test that inserts a
   referenced row and asserts it survives.
2. **Pruning is never cascading-by-accident.** `job_posting` deletion cascades to
   `requirement` and `match_score` by design (`DATA_MODEL.md` §4.2, §6.1), and
   the prune query's `WHERE` clause guarantees such a posting has no
   `application`. `ON DELETE CASCADE` is never relied on as the safety
   mechanism; the predicate is.

---

## 14. Configuration

| Key | Default | Purpose |
|---|---|---|
| `GHOST_AFTER_DAYS` | `30` | Threshold for `v_ghosted`. Should be tuned against the §8.4 measurement. |
| `SUBMIT_CONFIRM_DAYS` | `3` | Approved-with-no-events grace before the "possibly not submitted" prompt. |
| `MIN_N_FOR_RATE` | `15` | Below this, counts are shown and rates are suppressed everywhere. |
| `MIN_N_FOR_COMPARISON` | `30` | Per-arm minimum before a variant comparison is displayed at all. |
| `FOLLOWUP_MAX_PER_DAY` | `3` | Cap on surfaced follow-up prompts. |
| `FOLLOWUP_MAX_PER_APP` | `2` | How many times one application may be surfaced. |
| `FOLLOWUP_QUIET_DAYS_LIVE` | `7` | Quiet period for `screening` / `interview`. |
| `FOLLOWUP_QUIET_DAYS_ACK` | `10` | Quiet period for `acknowledged`. |
| `FOLLOWUP_QUIET_DAYS_SUBMITTED` | `14` | Quiet period for `submitted`. |
| `EXPORT_DIR` | `/var/lib/scout/exports` | Where the workbook is written. |
| `EXPORT_CRON` | `30 23 * * *` | IST. |
| `PRUNE_CRON` | `0 4 * * 0` | IST. Sunday. |
| `RETENTION_POSTING_DAYS` | `90` / `180` | Unscored / scored closed postings. |
| `RETENTION_MAIL_MONTHS` | `24` | `email_message` metadata. |

---

## 15. Acceptance criteria

| # | Criterion |
|---|---|
| 1 | No route in the OpenAPI schema submits an application to an external system; a test asserts the absence of any outbound POST to a non-allowlisted host from `api/`, `review/` and `tracking/`. |
| 2 | `POST /review/{id}/approve` creates exactly one `application` row and zero outbound HTTP requests. |
| 3 | Approving an already-decided review item returns 409 `review.already_decided`. |
| 4 | `may_append` rejects `rejected → acknowledged`, `interview → acknowledged` and `screening → screening`; accepts `interview → rejected` and `acknowledged → interview`. |
| 5 | Appending the same event twice — same `(application_id, status, email_message_id)` — creates one row. |
| 6 | Inserting events out of chronological order yields the same `application.status` as inserting them in order. |
| 7 | `ghosted` does not appear in the `application_status` enum, and no code path assigns it. Grep-level test. |
| 8 | An event arriving on day 45 removes the application from `v_ghosted` with no compensating write. |
| 9 | `POST /applications/{id}/events` sets `is_manual = true`, leaves `confidence` NULL, and rejects `force` from any non-manual caller. |
| 10 | `GET /metrics/funnel` grouped by variant defaults to `source_channel=direct` and returns `meta.n`, `meta.ci_low`, `meta.ci_high` on every row. |
| 11 | A rate with `n < MIN_N_FOR_RATE` is returned as null with the count present — never as a computed percentage. |
| 12 | The Funnel sheet contains no cell combining direct and referral applications. |
| 13 | Pruning does not delete any `job_posting`, `artifact` or `email_message` referenced by an `application` or `application_event`. |
| 14 | The export completes and the workbook opens cleanly with zero applications, zero companies and zero runs. |

---

## 16. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | Invariants 1 and 2, the lifecycle diagram (§7.1), the pipeline (§6) |
| `DATA_MODEL.md` | `application`, `application_event`, `review_item`, `v_funnel`, `v_ghosted` |
| `API.md` | Review, application, event, metrics and export endpoints |
| `EMAIL_INGESTION.md` | Where automated status events come from, and the digest that reports them |
| `MATCH_SCORING.md` | Coverage, gaps, and §7 — why selection probability is not computed |
| `DOCUMENT_GENERATION.md` | What `approve` freezes and hands to the operator |
| `CLAIMS_LEDGER.md` | Validation that gates artifact attachment, and the follow-up draft |
