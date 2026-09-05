# INSTALLATION GUIDE — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for install procedure, prerequisite versions, external
provider setup and verification. `ARCHITECTURE.md` wins on system-level
concerns; `CONFIGURATION.md` wins on the full settings list — this file
documents only the keys needed to reach a working system.

Two paths are documented and both are supported: a **local development install**
(§3) on the operator's machine, and a **production install** (§8) on a single VM
via Docker Compose. The local path is the one to do first, including if the goal
is production — the verification steps in §9 are much easier to work when the
services are in front of you.

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | **3.12.x** | 3.13 is untested. `pyproject.toml` pins `requires-python = ">=3.12,<3.13"`. |
| PostgreSQL | **16.x** | 15 will not do: the schema uses generated `TSVECTOR` columns and `MERGE`-adjacent behaviour verified only on 16. |
| Redis | **7.x** | Run locks, rate-limit buckets, Gmail refresh lock. |
| Node.js | **20.x LTS** | Vite 5 requires ≥ 18; 20 is what CI builds on. |
| npm | **10.x** | Ships with Node 20. `pnpm` works; the lockfile committed is `package-lock.json`. |
| Docker Engine | **24+** | With the Compose v2 plugin (`docker compose`, not `docker-compose`). |
| `uv` | **0.4+** | Optional but recommended. `pip` instructions are given alongside throughout. |
| Git | any recent | |
| An AWS account | — | With Bedrock available in a supported region and model access enabled (§5). |
| A Google account | — | For Gmail read + digest send (§4). |

Postgres extensions required, both created by the first migration:

| Extension | Used by |
|---|---|
| `pg_trgm` | `company_name_trgm_idx`, fuzzy company matching, mail display-name resolution |
| `pgvector` | Reserved for company deduplication only (`ARCHITECTURE.md` §4.1). Installed now so a later migration does not need a superuser. |

Both need a superuser at creation time. The Docker Postgres image runs migrations
as the superuser by default, so this is only a consideration on a managed
Postgres where the app user is not a superuser — see §10.2.

**Playwright** is needed only if a source adapter requires it. None of the
adapters in `SOURCE_ADAPTERS.md` §5–§7 do; the dependency is present because the
architecture reserves it for the case where no API exists
(`ARCHITECTURE.md` §4). Installing the browser binaries is optional at first
install and is covered in §3.9.

---

## 2. What you are installing

```
scout-careers/
├── backend/            FastAPI application, package `scout_careers`
├── frontend/           React 18 + Vite 5 + TypeScript
├── ci/run-checks.sh    The single host-agnostic gate
├── docker-compose.yml           local dependencies (postgres, redis)
├── docker-compose.prod.yml      full stack for the VM
├── .env.example
└── docs/
```

Four processes when everything is running locally:

| Process | Port | Started by |
|---|---|---|
| Postgres | 5432 | `docker compose up -d postgres` |
| Redis | 6379 | `docker compose up -d redis` |
| FastAPI (API + APScheduler in-process) | 8000 | `uvicorn` |
| Vite dev server | 5173 | `npm run dev` |

The scheduler runs **in-process** with the API (`ARCHITECTURE.md` §4). There is
no separate worker. Running two API processes would run two schedulers, which is
why the local instruction below is `--workers 1` and why production runs a single
container.

---

## 3. Local development install

### 3.1 Clone

```bash
git clone <origin>/scout-careers.git
cd scout-careers
```

### 3.2 Python environment

With `uv` (recommended — the lockfile is `uv.lock`):

```bash
cd backend
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

With `pip`:

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

`-e` (editable) is not optional for development. The Alembic `env.py` imports
`scout_careers.db.models` to build target metadata, and the CLI entry points
(`scout-careers …`) are declared in `pyproject.toml` under
`[project.scripts]`. A non-editable install works but requires a reinstall after
every model change.

Confirm:

```bash
python -c "import scout_careers, sys; print(scout_careers.__version__, sys.version)"
scout-careers --help
```

### 3.3 Postgres and Redis

```bash
cd ..          # repo root
docker compose up -d postgres redis
```

`docker-compose.yml`, in full — this is the whole local dependency set:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: scout
      POSTGRES_PASSWORD: scout
      POSTGRES_DB: scout
    ports: ["5432:5432"]
    volumes:
      - scout-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U scout -d scout"]
      interval: 5s
      timeout: 3s
      retries: 20

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20

volumes:
  scout-pgdata:
```

Redis persistence is off deliberately. Everything Redis holds — run locks,
rate-limit buckets, the Gmail refresh lock, the cached mail cursor — is
short-lived and reconstructible. Persisting it would mean a stale run lock
surviving a restart, which is a failure mode with no compensating benefit.

Wait for health, then confirm both:

```bash
docker compose ps
psql "postgresql://scout:scout@localhost:5432/scout" -c "SELECT version();"
docker compose exec redis redis-cli ping     # → PONG
```

The `pgvector` extension is not in the stock `postgres:16` image. If the
migration reports `could not open extension control file`, either switch the
image to `pgvector/pgvector:pg16` (drop-in, same env vars) or follow §10.2.

### 3.4 Configuration

```bash
cp .env.example backend/.env
```

The keys that must be set before anything works. Everything else has a working
default and is documented in `CONFIGURATION.md`.

```bash
# backend/.env

# ── Core ────────────────────────────────────────────────────────────────
SCOUT_ENV=local
SCOUT_BASE_URL=http://localhost:5173
DATABASE_URL=postgresql+asyncpg://scout:scout@localhost:5432/scout
REDIS_URL=redis://localhost:6379/0
SESSION_SECRET=            # openssl rand -hex 32
APP_PASSWORD=              # the single-user login password (API.md §1)
TZ=Asia/Kolkata

# ── LLM (AWS Bedrock default) ───────────────────────────────────────────
LLM_PROVIDER=bedrock
AWS_REGION=ap-south-1
LLM_MODEL_FAST=anthropic.claude-3-5-haiku-20241022-v1:0
LLM_MODEL_STRONG=anthropic.claude-sonnet-4-20250514-v1:0
LLM_DAILY_BUDGET_INR=80
LLM_INR_PER_USD=88

# ── Mail ────────────────────────────────────────────────────────────────
MAIL_ENABLED=true
MAIL_OPERATOR_ADDRESS=you@gmail.com
MAIL_ALERT_ADDRESS=you+scout@gmail.com
MAIL_TOKEN_PATH=./var/gmail.token
MAIL_TOKEN_KEY=            # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
GOOGLE_CLIENT_SECRETS_PATH=./var/google_client_secret.json

# ── Storage ─────────────────────────────────────────────────────────────
ARTIFACT_DIR=./var/artifacts
EXPORT_DIR=./var/exports

# ── Local-only conveniences ─────────────────────────────────────────────
SCHEDULER_ENABLED=false    # do not fire 08:00 runs while developing
RENDER_VERIFY_PAGES=true   # keep on; off only if LibreOffice is unavailable
```

Generate the two secrets:

```bash
openssl rand -hex 32                                   # → SESSION_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # → MAIL_TOKEN_KEY
mkdir -p backend/var/artifacts backend/var/exports
chmod 700 backend/var
```

`backend/.env`, `backend/var/` and everything under them are in `.gitignore` and
`.dockerignore`. Nothing here ever enters an image or a commit
(`ARCHITECTURE.md` §3, invariant 6).

`SCHEDULER_ENABLED=false` is the local default on purpose. An 08:00 run firing
while you are mid-refactor spends real tokens against real employer endpoints.
Trigger runs explicitly with `POST /api/v1/runs/discovery` instead.

### 3.5 Migrations

```bash
cd backend
alembic upgrade head
```

What the first revision does, in order: creates `pg_trgm` and `pgvector`; creates
every `ENUM` in `DATA_MODEL.md` §2; creates the tables in dependency order;
creates the `updated_at` trigger function and attaches it to every table carrying
that column; creates the indexes; creates `v_funnel` and `v_ghosted`.

Confirm:

```bash
alembic current                 # → <revision> (head)
psql "$PG_URL" -c "\dt"         # 14 tables
psql "$PG_URL" -c "\dv"         # v_funnel, v_ghosted, v_mail_review_queue, v_followup_due
psql "$PG_URL" -c "SELECT extname FROM pg_extension;"   # includes pg_trgm, vector
```

Migrations are forward-only in normal operation. Downgrades are written and are
exercised only in development (`DATA_MODEL.md` §11).

### 3.6 Seed

```bash
scout-careers seed --all
```

Idempotent, and safe to re-run. It is a script, not a migration — seed data never
ships as a migration (`DATA_MODEL.md` §11), because seeds are opinions about
content and migrations are facts about structure.

Three seed sets, individually runnable:

```bash
scout-careers seed --variants      # the six resume variants
scout-careers seed --claims        # the 63-row starter ledger
scout-careers seed --companies     # ~40 companies with detected sources
```

#### The six resume variants

Loaded from `seeds/variants/*.json` into `resume_variant`
(`DATA_MODEL.md` §5.1). Insert order is fixed so IDs are stable across a rebuild.

| id | `key` | Target | `skill_set` emphasis |
|---|---|---|---|
| 1 | `ai_product` | AI/ML product roles — PM-adjacent, delivery-owning | llm, rag, evals, product, delivery |
| 2 | `ai_enterprise` | Enterprise AI programmes, systems integration | llm, integration, govtech, stakeholder |
| 3 | `ai_platform` | AI platform and infrastructure engineering | llm, infra, aws, bedrock, serving, cost |
| 4 | `backend` | Backend and distributed systems | python, fastapi, postgres, async, redis |
| 5 | `combined` | Straddle roles where two variants are plausible | union of 1 and 4, trimmed to one page |
| 6 | `consulting` | Analyst, consulting, finance-adjacent | analysis, modelling, reporting, stakeholder |

`content` is the structured resume (summary, skill lines, experience blocks with
bullets, projects, achievements) in the same shape the `.docx` builder already
consumes, so a variant renders without a translation layer.

`skill_set` is the flattened, normalised skill vocabulary. It is what coverage
scoring matches `requirement.normalised_skill` against
(`MATCH_SCORING.md` §3), so a token missing here is a requirement that will score
`missing` no matter what the resume actually says.

#### The starter claims ledger

63 rows from `seeds/claims.yaml` (`CLAIMS_LEDGER.md` §8). Insert order is fixed
by the file so the IDs referenced from `resume_variant.content`, `API.md` and
`MATCH_SCORING.md` remain correct.

The governing principle is **one claim per independently assertable number**. A
figure a resume bullet could state on its own gets a row; a figure that only ever
appears as an attribute of another is a subordinate figure inside the parent's
`statement`.

| Project | Rows |
|---|---|
| Khelo India Assistant | 1–14 |
| Allatone | 15–19 |
| MYAS/SAI Parliamentary Question Bot | 20–33 |
| MYAS Task Tracker | 34–37 |
| Expenditure Tracker | 38–43 |
| PMIS AI Scope | 44–48 |
| Scout (EY) | 49–54 |
| 8Byte | 55–57 |
| BetterStack | 58–59 |
| Personal and education | 60–63 |

Two rows are `confidentiality = restricted` (13 and 14 — absolute rupee figures)
and are emitted only for employers on
`LEDGER_RESTRICTED_DISCLOSURE_COMPANIES`. Each is paired with a public sibling
in `ledger/pairs.yaml`, so the gating is invisible in normal use
(`CLAIMS_LEDGER.md` §3.3).

Volatile rows carry `expires_at = verified_at + 180 days`
(`LEDGER_DEFAULT_TTL_DAYS`). **After a seed on a fresh machine, check for rows
that are already stale**, because the seed file's `verified_at` dates are fixed
and the machine's clock is not:

```bash
curl -sS "http://localhost:8000/api/v1/claims?expired=true" | jq '.meta.total'
```

Anything returned needs re-verification before it is cited
(`OPERATIONS_MAINTENANCE.md` §4.2), not a config change to stop checking.

#### The starter company set

About forty companies across the operator's four target segments — Indian
technology and product, global product and enterprise, AI-first, and GovTech
(`COMPANY_REGISTRY.md` §9). Idempotent on `slug`.

The seed does **not** hard-code adapters. It pastes each `careers_url` through
the same detection path a human would use (`COMPANY_REGISTRY.md` §2) and writes
the source it actually probes. Where detection fails, the company is created with
**no source** and tagged `origin:seed`. That is a valid, expected outcome — the
row still governs manual imports and mail-alert matching.

Expect 25–32 of the 40 to acquire a source on the first run. The rest are the
monthly "No source" task (`OPERATIONS_MAINTENANCE.md` §4.3).

```bash
# What the seed actually produced
psql "$PG_URL" -c "
  SELECT c.slug, c.tier, coalesce(s.adapter::text, '— none —') AS adapter, s.enabled
  FROM company c LEFT JOIN source s ON s.company_id = c.id
  ORDER BY (s.id IS NULL) DESC, c.slug;"
```

The seed is the only place the system makes live outbound requests without an
explicit run, and it honours the never-scrape list and every rate-limit bucket
while doing it (`SOURCE_ADAPTERS.md` §4.3, §4.7).

### 3.7 Run the backend

```bash
cd backend
uvicorn scout_careers.api.main:app --reload --port 8000 --workers 1
```

`--workers 1` is required, not stylistic. APScheduler runs in-process; a second
worker is a second scheduler and a second 08:00 run.

- API: `http://localhost:8000/api/v1`
- OpenAPI: `http://localhost:8000/api/v1/openapi.json`
- Docs: `http://localhost:8000/docs`

### 3.8 Run the frontend

```bash
cd frontend
npm ci
npm run generate:api     # regenerates src/api/ from the running backend's schema
npm run dev
```

`npm run generate:api` needs the backend up — it reads
`http://localhost:8000/api/v1/openapi.json`. Hand-written request types in the
frontend are a review failure (`API.md`, preamble); the generator is how that
rule is kept.

Vite proxies `/api` to `http://localhost:8000`, so the browser sees one origin
and the session cookie behaves the same locally as in production:

```ts
// frontend/vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
```

Open `http://localhost:5173` and sign in with `APP_PASSWORD`.

### 3.9 Playwright (optional)

Only needed if you are writing an adapter that requires a browser. No shipped
adapter does.

```bash
cd backend
playwright install chromium
playwright install-deps chromium     # Linux only; needs sudo
```

Skip this on first install. Install it the day an adapter needs it — and before
writing that adapter, re-read `SOURCE_ADAPTERS.md` §11, because "this needs a
browser" is usually the signal that the source is not permissible rather than
that a browser is warranted.

---

## 4. Gmail OAuth — first-run authorisation

The mail module needs a Google OAuth client and one browser consent. This is the
step most likely to be got wrong, so it is written out fully. The design
rationale is in `EMAIL_INGESTION.md` §2; the procedure is here.

### 4.1 Create the Google Cloud project and client

1. Open the **Google Cloud Console** → **Select a project** → **New Project**.
   Name it `scout-careers`. Create.
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - **User type: External.** Internal is only available on Workspace domains,
     and a personal Gmail account is not one.
   - App name `Scout Careers`; user support email and developer contact email
     both the operator's own address. No logo, no homepage, no privacy policy —
     none is required for an app that is never distributed.
4. **Scopes** — add exactly two, and no others:

   | Scope | Purpose |
   |---|---|
   | `https://www.googleapis.com/auth/gmail.readonly` | Read message metadata and bodies for alert parsing and reply classification |
   | `https://www.googleapis.com/auth/gmail.send` | Send the daily digest |

   Both are **restricted** scopes. Google will warn about verification. That is
   expected and handled in §4.2.

   **Do not add `gmail.modify` or `gmail.compose`**, however convenient the
   label-based work queue looks. `gmail.modify` is a write scope on the
   operator's primary correspondence: an off-by-one in a paging loop mutates real
   mail. Read-only makes that class of bug structurally impossible, and the
   processing cursor is a `historyId` plus the `UNIQUE` constraint on
   `email_message.gmail_id` instead (`EMAIL_INGESTION.md` §2.4). A test asserts
   both scopes are absent from the constant.

5. **Test users** — add the operator's own address. Then, critically, §4.2.
6. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - **Application type: Desktop app.** Not "Web application". There is no
     hosted redirect URI and no server-side multi-tenant flow — one operator, one
     machine, one consent.
   - Download the JSON. Save it to `GOOGLE_CLIENT_SECRETS_PATH`
     (`backend/var/google_client_secret.json`), `chmod 600`.

### 4.2 Publish the app — the seven-day trap

**This step is not optional and skipping it silently breaks the system a week
later.**

On the **OAuth consent screen** page, publishing status will read **Testing**.
An unverified app in Testing issues refresh tokens that **expire after seven
days** for restricted scopes. The mail poller would work perfectly, then die
every Monday, with `400 invalid_grant`.

Click **PUBLISH APP** → confirm → status becomes **In production**.

Google will offer to start verification. **Do not submit for verification.** The
consequences of publishing without it are understood and accepted
(`EMAIL_INGESTION.md` §2.3):

- The consent screen shows an "unverified app" interstitial once, at first
  authorisation. The operator clicks through it.
- The app is capped at 100 users. One is needed.
- **Refresh tokens no longer carry the seven-day expiry.** This is the point.

Verification — including the CASA security assessment that restricted scopes
require for distribution — is not pursued, because the app is never distributed.

### 4.3 Authorise

```bash
cd backend
scout-careers auth gmail
```

What happens, step by step:

```
1. The CLI binds an ephemeral port on 127.0.0.1 and prints a URL.
   Loopback redirect, not out-of-band — Google deprecated the `oob` flow.

2. Your browser opens Google's consent screen.

   → "Google hasn't verified this app"
     Click "Advanced" → "Go to Scout Careers (unsafe)".
     This is the interstitial from §4.2. It appears once.

   → "Scout Careers wants access to your Google Account"
     Two checkboxes:
       [x] Read all resources and their metadata          (gmail.readonly)
       [x] Send email on your behalf                      (gmail.send)
     BOTH must be ticked. Google presents restricted scopes as individually
     declinable, and declining either leaves the system half-configured:
     no readonly means no tracking; no send means no digest.

   → "Continue"

3. Google redirects to http://127.0.0.1:<port>/ with an authorization code.
   The CLI serves exactly one request and shuts the listener down.

4. The code plus the PKCE (S256) verifier is exchanged for an access token
   (1 hour) and a refresh token. PKCE is used even though the desktop client
   also has a secret, because a secret in a desktop client is not a secret —
   PKCE is what actually binds the code to this session.

5. The token is written to MAIL_TOKEN_PATH, Fernet-encrypted with
   MAIL_TOKEN_KEY, mode 0600, written to a temp file and atomically renamed.
```

The request carries `access_type=offline` and `prompt=consent` on the first
authorisation, because Google returns a refresh token only on the first grant
unless consent is re-forced.

Expected output:

```
✓ Consent granted for you@gmail.com
✓ Scopes: gmail.readonly, gmail.send
✓ Token written to ./var/gmail.token (0600, encrypted)
✓ Profile check: 48213 messages, historyId 8827311
```

Confirm from the health endpoint:

```bash
curl -sS http://localhost:8000/api/v1/health | jq '.data.dependencies.gmail'
# → "ok"
```

Then send yourself a digest immediately, before trusting the schedule:

```bash
scout-careers digest --send-now
```

It arrives at `MAIL_OPERATOR_ADDRESS` and nowhere else. `GmailClient.send()`
raises `OutboundPolicyViolation` for any other recipient, including in `Cc` and
`Bcc`, and a static import-graph check asserts exactly one call site of the send
method, in `mail/digest.py` (`EMAIL_INGESTION.md` §14). Note honestly what that
means: **`gmail.send` grants sending to anyone.** Google's authorisation model
cannot express "send only to yourself." The restriction is enforced entirely in
code, by an assertion and a test, not by the platform.

### 4.4 Set up the alert alias

The alert stream and the reply stream are separated by a plus-alias
(`EMAIL_INGESTION.md` §3):

| Stream | Address | Contents |
|---|---|---|
| Alerts | `you+scout@gmail.com` | LinkedIn / Naukri / Indeed job alerts — high volume, low value per message |
| Replies | `you@gmail.com` | Employer and ATS replies — low volume, high value per message |

Subscribe to job alerts using the `+scout` address. Nothing else needs
configuring; Gmail delivers plus-aliased mail to the same mailbox and the query
`to:you+scout@gmail.com` separates it.

This is also **how LinkedIn roles enter the system without LinkedIn ever being
fetched**. LinkedIn sends the operator an alert email; the system parses mail the
operator already received. It never contacts a LinkedIn host, under any
configuration — the deny list is a code constant, not a config value
(`ARCHITECTURE.md` §3, invariant 4), and the shared HTTP client refuses a
LinkedIn host before opening a socket.

If a fully separate mailbox is preferred, only `MAIL_ALERT_ADDRESS` changes.

---

## 5. AWS Bedrock

### 5.1 Region and model access

Bedrock requires per-model access to be requested before `InvokeModel` will
work. A fresh account has access to nothing.

1. Open the **Bedrock console** in the region you will use. `ap-south-1`
   (Mumbai) keeps latency and data residency sensible for an India-based
   operator; confirm both models are offered there, and fall back to `us-east-1`
   if not.
2. **Model access** → **Modify model access** (older console: "Manage model
   access").
3. Request access to exactly the two pinned models:

   | Alias | Model ID | Used for |
   |---|---|---|
   | `fast` | `anthropic.claude-3-5-haiku-20241022-v1:0` | Extraction, coverage judgement, mail classification — ~85 calls/day |
   | `strong` | `anthropic.claude-sonnet-4-20250514-v1:0` | Tailoring plans, cover letters — ~16 calls/day |

4. Submit. Anthropic models are usually granted immediately; the console shows
   **Access granted**.

Model IDs live in configuration (`LLM_MODEL_FAST`, `LLM_MODEL_STRONG`), never in
code, and are **pinned exactly** — no `-latest` aliases, ever. A silently
upgraded model invalidates every eval result and every `artifact.model`
provenance record without a deploy having happened
(`AI_ARCHITECTURE.md` §4.1).

### 5.2 IAM policy

An IAM user or role scoped to invoking those two models and nothing else.
`bedrock:InvokeModel` on `Resource: "*"` grants every model in every region,
including ones costing an order of magnitude more per token. The budget breaker
would eventually catch it; the policy should mean it never arises.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeScoutModelsOnly",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:ap-south-1::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
        "arn:aws:bedrock:ap-south-1::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0"
      ]
    },
    {
      "Sid": "ListForHealthCheck",
      "Effect": "Allow",
      "Action": ["bedrock:ListFoundationModels"],
      "Resource": "*"
    }
  ]
}
```

Notes on the shape:

- Foundation-model ARNs have an **empty account field** —
  `arn:aws:bedrock:{region}::foundation-model/{modelId}` — because the models are
  AWS-owned. A policy with an account ID there matches nothing and produces
  `AccessDeniedException` with a message that looks like the model is
  unavailable.
- `InvokeModelWithResponseStream` is included because `FF_STREAMING_PREVIEW`
  exists (`AI_ARCHITECTURE.md` §12). It ships off; the permission is here so
  enabling a flag is not also an IAM change.
- `ListFoundationModels` is what `GET /api/v1/health` uses to report provider
  reachability. It cannot be scoped to a resource.
- Adding a **cross-region inference profile** later requires the profile ARN in
  `Resource` as well as the model ARNs. Add both, not a wildcard.

### 5.3 Credentials

Order of preference, best first:

1. **An instance role** (production on EC2). No credential exists on disk.
2. **A named profile** (`AWS_PROFILE=scout`) resolved from
   `~/.aws/credentials`, for local development.
3. **`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`** in `backend/.env`. Works;
   least good; rotate quarterly.

AWS credentials appear in **no configuration table, no log line and no error
message** (`AI_ARCHITECTURE.md` §13). They are not listed in `CONFIGURATION.md`
because they are not settings — they are ambient credentials resolved by boto3.

Verify before starting the app:

```bash
aws bedrock list-foundation-models --region ap-south-1 \
  --query 'modelSummaries[?contains(modelId, `claude`)].modelId' --output table

aws bedrock-runtime invoke-model \
  --region ap-south-1 \
  --model-id anthropic.claude-3-5-haiku-20241022-v1:0 \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":16,
           "messages":[{"role":"user","content":"Reply with the word: ready"}]}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout | jq -r '.content[0].text'
# → ready
```

### 5.4 Cost expectations at install

The design target is **₹66.66/day against an ₹80 budget**
(`AI_ARCHITECTURE.md` §8.2) — about ₹2,000/month at steady state. The first week
runs higher: the extraction cache is empty, so every posting is paid for once
(`AI_ARCHITECTURE.md` §9), and the initial backfill sees every open role at forty
companies rather than the daily delta.

Set `LLM_DAILY_BUDGET_INR=150` for the **first three days only**, then return it
to 80. Leaving it raised defeats the point of the number — a budget with wide
headroom does not constrain any decision.

The eval suite (`DEVELOPMENT.md` §8) costs about **₹35 per full run** over the
40-JD golden set. Budget for that separately; it is not part of the daily figure.

---

## 6. Azure OpenAI (alternate provider)

Both providers sit behind one interface (`AI_ARCHITECTURE.md` §3.1) and both are
covered by the same eval suite. Switching is a settings change; a provider switch
is gated on the eval exactly like a prompt change.

### 6.1 Deployments

Create two deployments in an Azure OpenAI resource, named for the aliases:

| Alias | Model | Deployment name |
|---|---|---|
| `fast` | `gpt-4o-mini` | `scout-fast` |
| `strong` | `gpt-4o` | `scout-strong` |

### 6.2 Configuration

```bash
LLM_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_VERSION=2024-10-21
LLM_MODEL_FAST=scout-fast
LLM_MODEL_STRONG=scout-strong
```

Under `azure_openai`, `LLM_MODEL_*` hold **deployment names**, not model IDs.
This is the difference that catches people: the same key means something
different per provider, and a Bedrock model ID left in place produces a
`DeploymentNotFound` that reads like a permissions error.

Authentication is Entra ID (`DefaultAzureCredential`) where available, API key
otherwise. Either way the credential comes from the environment and appears in no
log line.

Existing artifacts keep their recorded `model` and `prompt_version`, so
provenance survives the switch (`AI_ARCHITECTURE.md` §12).

### 6.3 Prices

Update the price keys when switching, or cost accounting silently reports Bedrock
prices for Azure calls:

```bash
LLM_PRICE_FAST_IN=0.15
LLM_PRICE_FAST_OUT=0.60
LLM_PRICE_STRONG_IN=2.50
LLM_PRICE_STRONG_OUT=10.00
```

`cost.py` computes spend from the `usage` on every response using these; the
figures in `AI_ARCHITECTURE.md` §8 size the design, the meter reports the truth.

---

## 7. First-run checklist

Before the first real discovery run, in order:

```
[ ] alembic current shows (head)
[ ] Seed complete: 6 variants, 63 claims, ~40 companies
[ ] GET /health returns ok for postgres, redis, llm, gmail
[ ] scout-careers digest --send-now arrived
[ ] Bedrock invoke-model smoke test returned text
[ ] LLM_DAILY_BUDGET_INR temporarily 150 (three days)
[ ] Job alerts subscribed to the +scout alias
[ ] Claims already expired on seed: re-verified or deprecated
[ ] SCHEDULER_ENABLED — false locally, true in production
```

---

## 8. Production install on a VM

### 8.1 Sizing

| Resource | Minimum | Comfortable |
|---|---|---|
| vCPU | 2 | 2 |
| RAM | 4 GB | 8 GB |
| Disk | 40 GB SSD | 80 GB SSD |
| Region | `ap-south-1` | Same region as Bedrock — cross-region calls add latency to a 15-minute run budget for nothing |

One container set, one user (`ARCHITECTURE.md` §9). No horizontal scaling is
designed for because none is needed. 8 GB is the comfortable figure only because
`RENDER_VERIFY_PAGES` spawns LibreOffice to count pages
(`DOCUMENT_GENERATION.md` §5.5), which is the single largest transient memory
consumer in the system.

### 8.2 Host preparation

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker "$USER" && newgrp docker

sudo useradd --system --home /var/lib/scout --shell /usr/sbin/nologin scout
sudo mkdir -p /var/lib/scout/{artifacts,exports,backups,secrets}
sudo chown -R scout:scout /var/lib/scout
sudo chmod 700 /var/lib/scout/secrets
```

Firewall: expose 443 only. Postgres and Redis bind to the Compose network and are
never published to the host. There is no reason for either to have a host port in
production, and publishing 5432 to the internet is the single most common way a
personal deployment is lost.

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### 8.3 `docker-compose.prod.yml`

```yaml
x-logging: &default-logging
  driver: json-file
  options: { max-size: "20m", max-file: "5", compress: "true" }

services:
  postgres:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    logging: *default-logging
    environment:
      POSTGRES_USER: scout
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
      POSTGRES_DB: scout
    volumes:
      - pgdata:/var/lib/postgresql/data
    secrets: [pg_password]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U scout -d scout"]
      interval: 10s
      timeout: 5s
      retries: 12

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    logging: *default-logging
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 12

  api:
    build: { context: ., dockerfile: backend/Dockerfile }
    restart: unless-stopped
    logging: *default-logging
    env_file: [/var/lib/scout/secrets/scout.env]
    environment:
      SCOUT_ENV: production
      SCHEDULER_ENABLED: "true"
      TZ: Asia/Kolkata
    volumes:
      - /var/lib/scout/artifacts:/var/lib/scout/artifacts
      - /var/lib/scout/exports:/var/lib/scout/exports
      - /var/lib/scout/secrets/gmail.token:/var/lib/scout/gmail.token
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/api/v1/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 40s

  web:
    build:
      context: ./frontend
      args: { VITE_API_BASE: /api/v1 }
    restart: unless-stopped
    logging: *default-logging

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    logging: *default-logging
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on: [api, web]

volumes:
  pgdata:
  caddy_data:
  caddy_config:

secrets:
  pg_password:
    file: /var/lib/scout/secrets/pg_password
```

**`api` runs exactly one replica.** The scheduler is in-process; two replicas are
two 08:00 runs. The Redis run lock would make the second one return 409 rather
than duplicate work (`API.md` §7), but relying on a lock to compensate for a
deployment mistake is not a design.

`Caddyfile`, in full — TLS is automatic:

```
scout.example.com {
    encode gzip
    handle /api/* {
        reverse_proxy api:8000
    }
    handle {
        reverse_proxy web:80
    }
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options    "nosniff"
        X-Frame-Options           "DENY"
        Referrer-Policy           "no-referrer"
    }
}
```

### 8.4 Secrets on the host

```bash
sudo -u scout tee /var/lib/scout/secrets/scout.env >/dev/null <<'EOF'
DATABASE_URL=postgresql+asyncpg://scout:CHANGEME@postgres:5432/scout
REDIS_URL=redis://redis:6379/0
SESSION_SECRET=...
APP_PASSWORD=...
SCOUT_BASE_URL=https://scout.example.com
AWS_REGION=ap-south-1
LLM_PROVIDER=bedrock
LLM_MODEL_FAST=anthropic.claude-3-5-haiku-20241022-v1:0
LLM_MODEL_STRONG=anthropic.claude-sonnet-4-20250514-v1:0
LLM_DAILY_BUDGET_INR=80
MAIL_ENABLED=true
MAIL_OPERATOR_ADDRESS=you@gmail.com
MAIL_ALERT_ADDRESS=you+scout@gmail.com
MAIL_TOKEN_PATH=/var/lib/scout/gmail.token
MAIL_TOKEN_KEY=...
ARTIFACT_DIR=/var/lib/scout/artifacts
EXPORT_DIR=/var/lib/scout/exports
EOF
sudo chmod 600 /var/lib/scout/secrets/scout.env
```

The Gmail token is authorised **on a machine with a browser** and copied across,
because the loopback consent flow needs one:

```bash
# on the workstation, after `scout-careers auth gmail`
scp backend/var/gmail.token vm:/tmp/gmail.token
# on the VM
sudo install -o scout -g scout -m 600 /tmp/gmail.token /var/lib/scout/secrets/gmail.token
sudo shred -u /tmp/gmail.token
```

`MAIL_TOKEN_KEY` must be **identical** on both machines — the token is Fernet
encrypted, and a different key produces `InvalidToken` at load, which reads
nothing like a configuration mismatch. The key travels through the operator's
password manager, never in the same channel as the token file.

### 8.5 Bring it up

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
docker compose -f docker-compose.prod.yml exec api scout-careers seed --all
docker compose -f docker-compose.prod.yml ps
curl -fsS https://scout.example.com/api/v1/health | jq .
```

Migrations run as an explicit step, never on container start. A container that
migrates on boot migrates again on every restart and on every replica, and turns
a crash-loop into a schema incident.

### 8.6 Backups

Per `OPERATIONS_MAINTENANCE.md` §13:

```bash
sudo tee /etc/cron.d/scout-backup >/dev/null <<'EOF'
0 3 * * * root docker compose -f /opt/scout/docker-compose.prod.yml exec -T postgres \
  pg_dump -U scout -Fc scout > /var/lib/scout/backups/scout-$(date +\%F).dump && \
  find /var/lib/scout/backups -name 'scout-*.dump' -mtime +14 -delete
15 3 * * * root rsync -a --delete /var/lib/scout/artifacts/ /var/lib/scout/backups/artifacts/
EOF
```

The restore drill is monthly (`OPERATIONS_MAINTENANCE.md` §4.4). Do the first one
the week you install, not the month after.

---

## 9. Verification — proving the install works

Six checks. "It started" is not one of them.

### 9.1 Health is green

```bash
curl -sS http://localhost:8000/api/v1/health | jq .
```

```jsonc
{
  "data": {
    "status": "ok",
    "version": "1.0.0",
    "dependencies": {
      "postgres": "ok",
      "redis":    "ok",
      "llm":      "ok",
      "gmail":    "ok"
    },
    "scheduler": { "running": true, "jobs": 6, "next_discovery": "2026-09-06T02:30:00Z" }
  },
  "message": "OK"
}
```

`/health` returns 200 even when a dependency is degraded, and reports the
degradation in the map — a degraded LLM provider should not take the UI down
(`API.md` §7). So read the map, not the status code. `next_discovery` is UTC;
02:30Z is 08:00 IST.

### 9.2 A discovery run against one company returns postings

The real acceptance test for the source layer. Pick a seeded company with a
working Greenhouse or Ashby source — those return the full JD in one request.

```bash
SRC=$(psql "$PG_URL" -Atc \
  "SELECT s.id FROM source s JOIN company c ON c.id=s.company_id
    WHERE s.adapter='greenhouse' AND s.enabled LIMIT 1")

RUN=$(curl -sS -X POST http://localhost:8000/api/v1/runs/discovery \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: install-verify-$(date +%s)" \
  -d "{\"source_ids\": [$SRC]}" | jq -r '.data.run_id')

sleep 20
curl -sS "http://localhost:8000/api/v1/runs/$RUN" | jq '.data | {status, stats, source_results}'
```

Pass criteria:

```jsonc
{
  "status": "completed",
  "stats": { "fetched": 47, "new": 47, "filtered": 38,
             "extracted": 9, "scored": 9, "generated": 2,
             "validation_failures": 0, "llm_cost_inr": 8.10 },
  "source_results": [
    { "source_id": 12, "adapter": "greenhouse", "status": "ok",
      "fetched": 47, "new": 47, "duration_ms": 1840, "error": null }
  ]
}
```

`status: "ok"` with a plausible `fetched` count — not "it compiled", not "no
exception". `fetched > 0`, `new > 0`, and `filtered < fetched` together prove the
adapter, normalisation, dedup and the deterministic filter are all doing
something.

### 9.3 A posting is scored, with gaps

```bash
POSTING=$(curl -sS "http://localhost:8000/api/v1/postings?limit=1" | jq -r '.data[0].id')
curl -sS "http://localhost:8000/api/v1/postings/$POSTING/scores" | jq '.data[0]'
```

```jsonc
{
  "variant": { "id": 1, "key": "ai_product" },
  "coverage_pct": 62.5,
  "hard_met": 5, "hard_total": 8,
  "nice_met": 4, "nice_total": 5,
  "composite_score": 58.20,
  "is_recommended": true,
  "gaps": [
    { "text": "Kubernetes in production", "level": "missing", "kind": "hard",
      "note": "No ledger evidence of production Kubernetes ownership." }
  ],
  "model": "anthropic.claude-3-5-haiku-20241022-v1:0",
  "prompt_version": "extract.v3+vocab.2026-08-20"
}
```

The row that proves the install is `gaps`. Coverage without a populated gap list
means requirement extraction produced nothing to be missing from — which usually
means the JD text is empty, and that is a normalisation bug, not a scoring one.
Also confirm `hard_total > 0`: extraction returning zero hard requirements is
routed to `needs_manual_review`, never scored as a perfect match
(`MATCH_SCORING.md` §13.2).

### 9.4 A `.docx` renders and passes validation

```bash
ITEM=$(curl -sS "http://localhost:8000/api/v1/review?limit=1" | jq -r '.data[0].id')
curl -sS "http://localhost:8000/api/v1/review/$ITEM" | jq '.data.artifacts'
```

```jsonc
{
  "resume":       { "id": "01JC…", "validation_status": "passed" },
  "cover_letter": { "id": "01JD…", "validation_status": "passed" }
}
```

Download and open it:

```bash
ART=$(curl -sS "http://localhost:8000/api/v1/review/$ITEM" | jq -r '.data.artifacts.resume.id')
curl -sS -OJ "http://localhost:8000/api/v1/review/$ITEM/artifacts/$ART/download"
```

Three things to check in the file itself:

1. **One page.** `RESUME_MAX_PAGES=1` and page count is verified by rendering,
   never assumed (`DOCUMENT_GENERATION.md` §5.5). Two pages means the fit loop
   did not run — usually `RENDER_VERIFY_PAGES=false` or LibreOffice missing.
2. **Every number traces to the ledger.** Pick one and run the provenance query
   (`OPERATIONS_MAINTENANCE.md` §9.3). If it does not resolve, the validation gate
   is defective, and that is a build failure, not a warning
   (`ARCHITECTURE.md` §3, invariant 3).
3. **`validation_status` is `passed`, not `bypassed`.** `bypassed` on a fresh
   install means the gate is disabled.

Then prove the gate actually blocks, because a gate never seen to fail has not
been tested:

```bash
curl -sS -X POST http://localhost:8000/api/v1/claims/validate \
  -H 'Content-Type: application/json' \
  -d '{"text": "Led a team of 40 engineers across 6 countries."}' | jq .
```

```jsonc
{
  "data": {
    "passed": false,
    "assertions": [
      { "span": "40 engineers", "resolved": false, "claim_id": null,
        "note": "No ledger claim for team size." },
      { "span": "6 countries",  "resolved": false, "claim_id": null }
    ]
  },
  "message": "2 assertions could not be resolved."
}
```

`passed: false` is the correct, healthy result. If that returns `passed: true`,
stop and fix it before generating anything.

### 9.5 The digest sends

```bash
scout-careers digest --send-now
```

It arrives at `MAIL_OPERATOR_ADDRESS`, is multipart with a complete `text/plain`
part, contains no images and no remote assets, and its section 6 reads either the
failures list or "All N sources healthy."

### 9.6 The invariants hold

Four commands. Each must fail in the way described.

```bash
# 1. LinkedIn is refused before a socket opens — invariant 4
curl -sS -X POST http://localhost:8000/api/v1/companies/detect \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/jobs/search/?keywords=engineer"}' | jq .
# → 403, meta.code == "source.denied_by_policy"

# 2. No submit endpoint exists at any version — invariant 1
curl -sS http://localhost:8000/api/v1/openapi.json \
  | jq -r '.paths | keys[]' | grep -iE 'submit|apply' || echo "correct: none"

# 3. Outbound mail to a third party is refused — invariant 2
scout-careers digest --send-now --to someone-else@example.com
# → OutboundPolicyViolation

# 4. The whole gate is green
bash ci/run-checks.sh all
```

---

## 10. Troubleshooting

### 10.1 Alembic version mismatch

```
alembic.util.exc.CommandError: Can't locate revision identified by 'a3f8c21b9e04'
```

`alembic_version` holds a revision that is not in `versions/`. Almost always a
branch switch after migrating, or a database from a newer branch.

```bash
alembic current                                   # what the DB thinks
psql "$PG_URL" -c "SELECT * FROM alembic_version" # same, authoritative
alembic history --verbose | head -40              # what the code has
```

- **The revision is on another branch**: check that branch out and
  `alembic downgrade` to the merge base, then switch back and
  `alembic upgrade head`.
- **Local development, data disposable**: rebuild. This is the fast path and
  usually the right one.

  ```bash
  docker compose down -v && docker compose up -d postgres redis
  sleep 5 && alembic upgrade head && scout-careers seed --all
  ```

- **Production**: never edit `alembic_version` by hand. Write a merge revision
  (`alembic merge`) or an explicit corrective revision, review it, and apply it
  through the normal gate.

```
Target database is not up to date.
```

means pending revisions. `alembic upgrade head`. If it appears when generating a
revision, that is Alembic refusing to autogenerate against a stale schema — and
it is right to.

### 10.2 `pgvector` or `pg_trgm` extension missing

```
sqlalchemy.exc.ProgrammingError: (asyncpg.exceptions.UndefinedFileError)
could not open extension control file
"/usr/share/postgresql/16/extension/vector.control": No such file
```

The extension binary is not installed on the server. Distinguish this from a
permissions failure, which reads:

```
permission denied to create extension "vector"
HINT: Must be superuser to create this extension.
```

| Situation | Fix |
|---|---|
| Local Docker, stock `postgres:16` | Switch the image to `pgvector/pgvector:pg16`. Drop-in, same env vars, same volume. `docker compose down && docker compose up -d postgres`. |
| Debian/Ubuntu host Postgres | `sudo apt install postgresql-16-pgvector postgresql-contrib-16`, then restart. |
| macOS Homebrew | `brew install pgvector`. `pg_trgm` ships with the server. |
| Managed Postgres (RDS, Neon, Supabase) | Both are on the supported-extensions list. Enable them once, as the admin user, before running migrations: `CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector;` The migration's `IF NOT EXISTS` then succeeds as the app user. |

Verify:

```bash
psql "$PG_URL" -c "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
```

`pg_trgm` is not optional. It backs `company_name_trgm_idx`, ATS auto-detection's
company matching, and mail display-name resolution
(`EMAIL_INGESTION.md` §6.2). Without it the schema does not create.

### 10.3 Bedrock access denied

```
botocore.errorfactory.AccessDeniedException: An error occurred (AccessDeniedException)
when calling the InvokeModel operation: You don't have access to the model with
the specified model ID.
```

Four causes, in the order they actually occur:

1. **Model access not requested.** The most common by a wide margin. Bedrock
   console → Model access → the two models show **Access granted**, not
   "Available to request".
2. **Wrong region.** Access is per-region. `AWS_REGION` must match the region
   where access was granted. Check with
   `aws bedrock list-foundation-models --region "$AWS_REGION"` — if the model is
   absent from that list, it is a region problem, not a policy problem.
3. **Malformed resource ARN.** Foundation-model ARNs have an empty account
   field: `arn:aws:bedrock:ap-south-1::foundation-model/anthropic.claude-...`.
   An account ID inserted there matches nothing and the failure looks identical
   to no access at all.
4. **Credentials resolving to the wrong identity.** `aws sts get-caller-identity`
   is the one-line answer. A `AWS_PROFILE` in the shell, an instance role, and
   keys in `.env` compete, and boto3's precedence is not the one people expect.

Simulate the policy rather than guessing:

```bash
aws iam simulate-principal-policy \
  --policy-source-arn "$(aws sts get-caller-identity --query Arn --output text)" \
  --action-names bedrock:InvokeModel \
  --resource-arns "arn:aws:bedrock:ap-south-1::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0" \
  --query 'EvaluationResults[0].EvalDecision' --output text
# → allowed
```

`ValidationException: The provided model identifier is invalid` is a different
fault: a typo in `LLM_MODEL_FAST`/`LLM_MODEL_STRONG`, or an Azure deployment name
left in place after a provider switch (§6.2).

`ThrottlingException` on the first real run is on-demand capacity, not
configuration. The client already retries with exponential backoff and full
jitter; if it persists, lower `LLM_MAX_CONCURRENCY` from 4 to 2.

### 10.4 Gmail consent loop

**Symptom: the browser returns to the consent screen repeatedly, or the CLI
hangs after consent.**

- The loopback listener never received the redirect. A firewall or a VPN split
  tunnel is blocking `127.0.0.1:<ephemeral>`. Disable the VPN for the duration of
  the consent, then re-run.
- A browser extension is rewriting `127.0.0.1` URLs. Consent in a private window
  with extensions off.
- The client is registered as **Web application** rather than **Desktop app**, so
  Google rejects the loopback redirect. Recreate the client with the right type
  (§4.1, step 6).

**Symptom: `400 invalid_grant` a week after a working install.**

The seven-day trap (§4.2). The OAuth consent screen is still in **Testing**.
Publish the app to **In production** and re-run `scout-careers auth gmail`.

**Symptom: `Error 403: access_denied`, "Scout Careers has not completed the
Google verification process".**

The operator's address is not in **Test users** and the app is still in Testing;
or the app is in production and the interstitial was dismissed rather than
clicked through. Click **Advanced → Go to Scout Careers (unsafe)**.

**Symptom: authorisation succeeds but `GET /health` reports
`gmail: "unauthenticated"`.**

The token file loads but decrypts wrong, or is not where the app looks.

```bash
ls -l "$MAIL_TOKEN_PATH"      # must exist, mode 0600
scout-careers auth gmail --status
```

`cryptography.fernet.InvalidToken` means `MAIL_TOKEN_KEY` differs from the key
used to write the file — the classic workstation-to-VM copy mistake (§8.4). The
key must travel with the token.

**Symptom: `insufficient authentication scopes` when sending.**

Only one scope was ticked at consent. Google presents restricted scopes as
individually declinable. Re-run `scout-careers auth gmail` and tick both.

**Recovery is always cheap.** On refresh failure the mail run aborts without
retrying (`invalid_grant` is permanent), the Gmail cursor is **not** advanced, no
messages are skipped, and the digest is written to
`exports/digest-YYYY-MM-DD.html` and shown in-app so no day's output is lost
(`EMAIL_INGESTION.md` §2.5). Re-authorising resumes exactly where it stopped.

### 10.5 Playwright browsers not installed

```
playwright._impl._errors.Error: Executable doesn't exist at
/root/.cache/ms-playwright/chromium-1140/chrome-linux/chrome
```

```bash
playwright install chromium
playwright install-deps chromium      # Linux; installs system libraries, needs sudo
```

In Docker, the browser must be installed **at build time** and the cache must be
readable by the runtime user:

```dockerfile
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
 && chmod -R 755 /ms-playwright
```

The most common Docker failure is installing as root and running as a non-root
user, which fails with a permissions error rather than a missing-executable one.

Before reaching for any of this: **no shipped adapter needs Playwright.** If a
new one appears to, re-read `SOURCE_ADAPTERS.md` §11 first. Needing a browser is
usually the signal that the source requires defeating bot protection, which the
compliance boundary rules out — and the answer is `mail_alert` or manual import,
not a cleverer adapter.

### 10.6 Port conflicts

```
Error response from daemon: Ports are not available:
bind 0.0.0.0:5432: address already in use
```

```bash
lsof -nP -iTCP:5432 -sTCP:LISTEN     # macOS / Linux
sudo ss -lptn 'sport = :5432'        # Linux
```

Usually a host Postgres from Homebrew or a distribution package.

```yaml
# docker-compose.override.yml — git-ignored, per-machine
services:
  postgres:
    ports: ["55432:5432"]
  redis:
    ports: ["56379:6379"]
```

```bash
DATABASE_URL=postgresql+asyncpg://scout:scout@localhost:55432/scout
REDIS_URL=redis://localhost:56379/0
```

Other collisions:

| Port | Common occupant | Fix |
|---|---|---|
| 8000 | Another dev server | `uvicorn --port 8001`, and update the Vite proxy target |
| 5173 | Another Vite project | `npm run dev -- --port 5174` |
| 6379 | Host Redis | Override as above, or `brew services stop redis` |

**On macOS, port 5000 is AirPlay Receiver.** Nothing here uses 5000, but it is
the first thing to check if a future service picks it.

In production, none of this arises: Postgres and Redis have no host ports at all
(§8.2).

### 10.7 Other common failures

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: scout_careers` | Not installed editable, or the venv is not active | `source .venv/bin/activate && uv pip install -e ".[dev]"` |
| `asyncpg.InvalidCatalogNameError: database "scout" does not exist` | Compose volume created under different env vars | `docker compose down -v && docker compose up -d postgres` |
| `InterfaceError: cannot perform operation: another operation is in progress` | An `AsyncSession` shared across tasks | One session per request or per task. Never a module-level session. |
| Frontend 404s on `/api/v1/*` | Vite proxy not configured, or the backend is on another port | §3.8 |
| `npm run generate:api` fails | Backend not running | Start `uvicorn` first; the generator reads the live schema |
| Digest sends but has no content | No run has completed | Trigger `POST /runs/discovery` first; sections 2–6 are omitted when empty |
| Two discovery runs at 08:00 | Two API workers, or two replicas | `--workers 1`; one `api` replica (§8.3) |
| `.docx` renders as two pages | `RENDER_VERIFY_PAGES=false`, or LibreOffice missing | Turn it on; install LibreOffice in the image |
| Every posting scores 0% coverage | `resume_variant.skill_set` empty — seed did not run | `scout-careers seed --variants` |
| `409 run.already_in_flight` | A stale Redis run lock from a killed container | `redis-cli DEL lock:run:discovery`, only when no run is live |

---

## 11. Uninstall

```bash
docker compose -f docker-compose.prod.yml down -v     # -v destroys the database
sudo rm -rf /var/lib/scout
```

Then revoke access at **myaccount.google.com → Security → Third-party apps**, and
delete the IAM user or role. Deleting the containers does not revoke either
credential, and a live OAuth grant to an application that no longer exists is the
kind of thing that is forgotten for years.

---

## 12. Related documents

| Document | Relationship |
|---|---|
| `ARCHITECTURE.md` | Invariants, technology decisions, scale envelope |
| `CONFIGURATION.md` | The complete settings list; this file covers only what install needs |
| `OPERATIONS_MAINTENANCE.md` | What to do once it is running |
| `DEVELOPMENT.md` | Repository layout, the gate, migrations, adding adapters |
| `EMAIL_INGESTION.md` | §2 the authority for the Gmail integration |
| `AI_ARCHITECTURE.md` | §4 model routing and pinning, §8 cost, §13 configuration |
| `SOURCE_ADAPTERS.md` | §4 shared HTTP infrastructure, §11 the deferred adapters |
| `CLAIMS_LEDGER.md` | §8 the seeded starter ledger |
| `COMPANY_REGISTRY.md` | §9 the seeded company set, §2 ATS auto-detection |
| `INFRASTRUCTURE.md` | Runtime topology, sizing, the ECS Fargate alternative |
| `SECURITY_ARCHITECTURE.md` | Secret handling, threat model, the single-user auth decision |
