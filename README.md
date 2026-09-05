# Scout Careers

A single-user job discovery and application-preparation platform.

It runs a scheduled pass across employer applicant tracking systems every
morning, scores each discovered role against a library of resume variants,
drafts a tailored resume plan and cover letter for the ones worth pursuing, and
presents them in a review queue. A human approves and submits. The system then
tracks the outcome by reading the reply mail.

**Status:** pre-implementation. The documentation set is complete; no executable
code has been written yet. See [`docs/DOCUMENTATION_INDEX.md`](docs/DOCUMENTATION_INDEX.md).

---

## What it is not

Scout Careers does not auto-apply. This is the central design decision, not a
missing feature.

Mass auto-submission converts at roughly 1–2%; tailored applications convert at
roughly 10–15%. Beyond being ineffective, volume submission through shared ATS
platforms — Workday, Greenhouse, Lever and SmartRecruiters each serve thousands
of employers — gets an applicant flagged as a spam applicant across all of them,
and that flag follows them to the employers they actually care about.

So the system automates everything up to the decision and stops:

| Stage | Automated | Human |
|---|---|---|
| Discovery, deduplication, filtering | ✓ | |
| Match scoring and gap analysis | ✓ | |
| Resume tailoring plan and cover letter draft | ✓ | |
| **Review and approve** | | **✓** |
| **Submission on the employer's site** | | **✓** |
| Status tracking from inbox, funnel metrics | ✓ | |

Ten minutes a day, five to ten strong applications a week.

---

## The four invariants

Enforced in code, proven by test, and not configurable.

1. **No automated submission.** No endpoint submits an application. The absence
   is the enforcement.
2. **No automated outbound mail to people.** Exactly one message class exists —
   the daily digest, sent to the operator's own address.
3. **Generation cites only the claims ledger.** Every numeric or factual
   assertion in a generated document must resolve to a verified claim. An
   artifact failing validation cannot attach to an application, enforced by a
   database trigger rather than by application code alone.
4. **The never-scrape list is absolute.** LinkedIn first. A code constant, not a
   setting. LinkedIn roles enter the system through job-alert email that
   LinkedIn itself sends — which is categorically different from scraping it.

Reasoning in [`docs/DATA_SOURCES_AND_COMPLIANCE.md`](docs/DATA_SOURCES_AND_COMPLIANCE.md).

---

## How roles are discovered

Roughly 80% of employers use one of eight ATS platforms, so the system has one
adapter per platform rather than one scraper per company. Adding Adobe is not
writing an Adobe scraper — it is a Workday config with `tenant=adobe`.

| Adapter | Notable coverage |
|---|---|
| Workday | Adobe, Nvidia, Salesforce, Dell, Cisco, HPE — the highest-coverage adapter |
| Greenhouse · Lever · Ashby | Most startups and scale-ups |
| SmartRecruiters · Workable · Recruitee | Mid-market |
| Google · Amazon · Microsoft | Bespoke, they roll their own |
| `mail_alerts` | LinkedIn, Naukri and Indeed alerts, parsed from a dedicated mailbox |

Paste any careers URL into the Companies page and the system detects the ATS,
probes the endpoint and registers it. That is what makes tracking 300 companies
practical rather than a chore that gets abandoned.

---

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 async · Alembic · PostgreSQL 16 · Redis 7
· APScheduler · React 18 + Vite + TypeScript · AWS Bedrock (Azure OpenAI
alternate) · Gmail API · Playwright where no API exists.

Docker Compose on a single small VM. ECS Fargate is documented as an alternative
and costs roughly six times more for no benefit at this scale.

---

## Repository layout

```
Scout Careers/
├── README.md
├── docs/                        28 documents — see DOCUMENTATION_INDEX.md
├── backend/
│   ├── src/scout_careers/       common · db · sources · registry · ingest ·
│   │                            extract · scoring · ledger · generate ·
│   │                            review · mail · tracking · scheduler · llm · api
│   ├── tests/
│   ├── alembic/
│   └── pyproject.toml
├── frontend/                    React + Vite + TypeScript
├── ci/run-checks.sh             the single pre-merge gate
└── docker-compose.yml
```

---

## Getting started

Nothing is implemented yet. When it is:

```bash
cp .env.example .env            # fill in database, LLM and Gmail credentials
docker compose up -d postgres redis
cd backend && alembic upgrade head && python -m scout_careers.seed
uvicorn scout_careers.api:app --reload
cd ../frontend && npm install && npm run dev
```

Full walkthrough in [`docs/INSTALLATION_GUIDE.md`](docs/INSTALLATION_GUIDE.md).

---

## Delivery plan

Five phases, each with an exit gate. **Phase 2 is the stop-able point** — it
delivers most of the value, and the honest risk with a personal tool is not
technical failure but abandonment.

| Phase | Delivers |
|---|---|
| 1 | Greenhouse, Lever, Ashby adapters plus mail-alert parsing. No UI. |
| 2 | Company registry with paste-to-detect, match scoring, the 08:00 run and 08:15 digest |
| 3 | Workday adapter, claims ledger, resume recommendation with coverage tables |
| 4 | Cover letter generation, tailoring layer, review queue UI |
| 5 | Bespoke adapters, Gmail status ingestion, funnel dashboard, spreadsheet export |

Detail in [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## On "selection percentage"

The system does not predict a probability of being selected for a role, and will
not. There is no ground truth to learn from, and the variables that decide the
outcome — an internal referral, a headcount freeze, a role already promised —
are invisible to it. A confident-looking percentage would be a fabricated number
that its own operator would start trusting.

What it produces instead: requirement coverage, an explicit list of what is
missing, and — after enough applications — the operator's own observed funnel
rates by resume variant and company tier. Measured, not predicted.

Argued in [`docs/MATCH_SCORING.md`](docs/MATCH_SCORING.md) §7.

---

## Lineage

The architecture is an adaptation of the Scout bid-discovery platform: ingest
from public portals, normalise, classify, score against a weighted model, surface
a ranked queue for human review, with every scoring decision explainable. The
domain changed; the shape did not.
