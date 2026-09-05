# INFRASTRUCTURE — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for runtime topology, service composition, host sizing,
volume and network layout, TLS, backup and restore, disaster-recovery targets,
monitoring, and capacity limits. `ARCHITECTURE.md` wins on system-level concerns,
invariants and the scale envelope; `DATA_MODEL.md` wins on schema; `API.md` wins
on endpoint contracts; `CONFIGURATION.md` wins on configuration keys and
defaults; `DEPLOYMENT_ENV_RUNBOOK.md` wins on procedures. Everything about *what
runs where, on what, and how it survives* is decided here.

---

## 1. What this document decides, and the posture behind it

Scout Careers serves **one user**, runs **one scheduled batch a day**, and holds
**low-gigabyte** state. Every infrastructure decision below follows from that and
from `ARCHITECTURE.md` §9, which sizes the system deliberately small because
over-engineering is the main risk to it ever being finished.

The default deployment is therefore **Docker Compose on a single small VM**. ECS
Fargate is documented (§8) because it was evaluated, not because it is
recommended. The honest recommendation is in §8.5 and it is "do not".

Three properties are non-negotiable regardless of substrate:

1. **Postgres and Redis are never reachable from the internet.** They bind to a
   private Compose network and publish no host ports.
2. **Exactly one process runs the scheduler.** APScheduler is in-process
   (`ARCHITECTURE.md` §4); two schedulers means two 08:00 discovery runs.
3. **The secret material — Gmail OAuth token, `MAIL_TOKEN_KEY`, LLM credentials,
   `SESSION_SECRET` — lives outside the image, outside the build context, and
   outside every backup that leaves the host unencrypted** (`ARCHITECTURE.md` §3,
   invariant 6).

---

## 2. Runtime topology

```
                          Internet
                              │
                        :80 / :443
                              ▼
        ┌──────────────────────────────────────────────┐
        │  web  (Caddy 2 + built React SPA)            │   network: edge
        │  · TLS termination, ACME, HTTP/3             │
        │  · serves /assets/* and index.html           │
        │  · reverse-proxies /api/* → api:8000         │
        └───────────────────┬──────────────────────────┘
                            │  edge
        ┌───────────────────▼──────────────────────────┐
        │  api  (FastAPI / uvicorn, 2 workers)         │   networks: edge,
        │  · HTTP only. SCHEDULER_ENABLED=false        │             internal
        │  · renders .docx on preview/approve          │
        │    (LibreOffice headless in-image)           │
        └───────────────────┬──────────────────────────┘
                            │  internal
   ┌────────────────────────┼─────────────────────────────────────┐
   │                        │                                     │
┌──▼───────────────┐  ┌─────▼──────────┐            ┌─────────────▼─────────────┐
│ postgres:16      │  │ redis:7        │            │ worker                    │
│ pg_trgm, FTS     │  │ locks, buckets │            │ APScheduler, in-process   │
│ APScheduler store│  │ robots cache   │            │ discovery · mail · digest │
│ no host port     │  │ no host port   │            │ export · prune · rescore  │
└──────────────────┘  └────────────────┘            │ replicas: 1 — always      │
                                                    └───────────┬───────────────┘
                                                                │ egress (443)
                                                                ▼
                                        ATS JSON APIs · Bedrock / Azure OpenAI
                                                     · Gmail API
```

Both `api` and `worker` are **the same image**, differing only in command and in
`SCHEDULER_ENABLED`. One image means one build, one dependency set, one
LibreOffice pin, and no possibility of the worker running different scoring code
than the API that displays its results.

### 2.1 Service roster

| Service | Image | Purpose | Restart | Publishes |
|---|---|---|---|---|
| `web` | built (`node:22` build stage → `caddy:2.8-alpine`) | TLS, ACME, static SPA, reverse proxy | `unless-stopped` | `80`, `443`, `443/udp` |
| `api` | built (`backend/Dockerfile`) | FastAPI, OpenAPI, `.docx` render on demand | `unless-stopped` | none |
| `worker` | same image as `api` | APScheduler; all scheduled jobs | `unless-stopped` | none |
| `postgres` | `postgres:16-alpine` | System of record + APScheduler job store | `unless-stopped` | none |
| `redis` | `redis:7-alpine` | Run locks, rate-limit buckets, robots cache | `unless-stopped` | none |
| `migrate` | same image as `api` | One-shot `alembic upgrade head` | `no` (profile) | none |

`web` merges reverse proxy and static serving into one container on purpose. A
separate nginx-serving-a-volume arrangement requires the frontend build artefact
to be synchronised into a shared volume at deploy time, which is a moving part
that exists only to satisfy a diagram. Baking the built SPA into the Caddy image
means the frontend and its server version together, and a frontend change is an
ordinary image build.

### 2.2 `docker-compose.yml`

```yaml
# /srv/scout/docker-compose.yml
name: scout

x-app-env: &app-env
  env_file: [.env]
  environment:
    SCOUT_ENV: production
    DATABASE_URL: postgresql+asyncpg://scout:${POSTGRES_PASSWORD}@postgres:5432/scout
    REDIS_URL: redis://redis:6379/0
    TZ: Asia/Kolkata

x-app-image: &app-image
  image: scout-careers/backend:${SCOUT_TAG:-latest}
  build:
    context: ./backend
    dockerfile: Dockerfile

services:

  web:
    image: scout-careers/web:${SCOUT_TAG:-latest}
    build:
      context: ./frontend
      dockerfile: Dockerfile          # node build stage → caddy:2.8-alpine
      args:
        VITE_API_BASE: /api/v1
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"                 # HTTP/3
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data              # ACME account + certificates
      - caddy_config:/config
    environment:
      SCOUT_DOMAIN: ${SCOUT_DOMAIN}
      ACME_EMAIL: ${ACME_EMAIL}
    depends_on:
      api:
        condition: service_healthy
    networks: [edge]
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1:80/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3

  api:
    <<: [*app-image, *app-env]
    restart: unless-stopped
    command: >
      uvicorn scout_careers.api.main:app
      --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers
      --forwarded-allow-ips '*' --timeout-keep-alive 30
    environment:
      SCHEDULER_ENABLED: "false"      # the API never schedules. See §2.3.
    volumes:
      - /var/lib/scout:/var/lib/scout
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    networks: [edge, internal]
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=5).status==200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    deploy:
      resources:
        limits: { memory: 1200M }

  worker:
    <<: [*app-image, *app-env]
    restart: unless-stopped
    command: ["python", "-m", "scout_careers.scheduler"]
    environment:
      SCHEDULER_ENABLED: "true"
    volumes:
      - /var/lib/scout:/var/lib/scout
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
      api:      { condition: service_healthy }
    networks: [internal]
    healthcheck:
      test: ["CMD", "python", "-m", "scout_careers.scheduler", "--liveness"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    deploy:
      replicas: 1                     # never raise. See §2.3.
      resources:
        limits: { memory: 1200M }

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: scout
      POSTGRES_USER: scout
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_INITDB_ARGS: "--data-checksums"
      TZ: UTC                         # the database is UTC. Always.
    command: >
      postgres
      -c shared_buffers=768MB
      -c effective_cache_size=2GB
      -c work_mem=16MB
      -c maintenance_work_mem=192MB
      -c max_connections=40
      -c random_page_cost=1.1
      -c wal_compression=on
      -c checkpoint_completion_target=0.9
      -c log_min_duration_statement=1000
      -c track_io_timing=on
    volumes:
      - pgdata:/var/lib/postgresql/data
    networks: [internal]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U scout -d scout"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits: { memory: 1500M }

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: >
      redis-server
      --save ""
      --appendonly no
      --maxmemory 192mb
      --maxmemory-policy noeviction
    networks: [internal]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    deploy:
      resources:
        limits: { memory: 256M }

  migrate:
    <<: [*app-image, *app-env]
    profiles: ["tools"]
    command: ["alembic", "upgrade", "head"]
    depends_on:
      postgres: { condition: service_healthy }
    networks: [internal]
    restart: "no"

volumes:
  pgdata:
  caddy_data:
  caddy_config:

networks:
  edge:
    driver: bridge
  internal:
    driver: bridge
    internal: false     # egress to ATS/LLM/Gmail is required; see §6.2
```

**Redis persistence is deliberately off.** Redis holds run locks, token buckets,
the 24-hour `robots.txt` cache and the `mail:history_id` convenience cache — all
of it either short-lived or reconstructible (`ARCHITECTURE.md` §4;
`EMAIL_INGESTION.md` §3.1 states the cursor's authoritative home is
`run_log.stats.gmail_history_id`). Persisting it would create a second source of
truth for the mail cursor, which is worse than losing the cache.

`maxmemory-policy noeviction` is the right choice precisely because every key has
a TTL: at 192 MB against a working set measured in kilobytes, the only way to hit
the ceiling is a bug, and evicting a run lock to make room for a rate-limit token
is a failure mode nobody wants to debug at 08:04.

### 2.3 Why `worker` is pinned to one replica

`ARCHITECTURE.md` §4 chose APScheduler with a Postgres job store over Celery. The
job store gives durability across restarts — the only durability property needed
— but it does **not** give leader election. Two `worker` containers means two
APScheduler instances, two 08:00 triggers, two discovery runs.

Three things stop that in practice, and all three are wanted:

1. `deploy.replicas: 1` on the service.
2. `SCHEDULER_ENABLED=false` on `api`, so scaling the API is always safe.
3. The Redis run lock behind `POST /api/v1/runs/discovery` (`API.md` §7), which
   returns **409** if a discovery run is in flight. This is what makes a manual
   run overlapping the scheduled one harmless, and it catches the case where
   somebody starts a second `worker` by hand.

The lock is the real defence; the replica count is the policy. Do not rely on
either alone.

### 2.4 `Caddyfile`

```caddyfile
# /srv/scout/Caddyfile
{
    email {$ACME_EMAIL}
    servers {
        protocols h1 h2 h3
    }
}

{$SCOUT_DOMAIN} {
    encode zstd gzip

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options    "nosniff"
        X-Frame-Options           "DENY"
        Referrer-Policy           "strict-origin-when-cross-origin"
        Content-Security-Policy   "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        -Server
    }

    @api path /api/*
    handle @api {
        reverse_proxy api:8000 {
            header_up X-Forwarded-Proto https
            transport http {
                read_timeout  120s      # POST /postings/import runs the chain inline
                write_timeout 120s
            }
        }
    }

    handle_path /healthz {
        respond "ok" 200
    }

    handle {
        root * /srv/www
        try_files {path} /index.html    # SPA history fallback
        file_server
    }

    log {
        output file /data/access.log {
            roll_size 20MiB
            roll_keep 5
        }
        format json
    }
}
```

The 120-second proxy timeout exists for exactly one endpoint —
`POST /api/v1/postings/import`, which runs extract → score → generate
synchronously (`API.md` §3, `AI_ARCHITECTURE.md` §3.3, ~12 s typical). Every
other long operation returns **202** and is polled. The generous ceiling covers
the tail; it is not an invitation to add more synchronous work.

---

## 3. Host sizing

### 3.1 The working assumption, checked

The working assumption is **2 vCPU / 4 GB RAM / 40 GB SSD**. It is verified below
against `ARCHITECTURE.md` §9 rather than asserted.

| Envelope dimension (§9) | Value | Resource it stresses | Verdict |
|---|---|---|---|
| Tracked companies | 300 | Postgres rows (trivial) | Not binding |
| Sources polled per run | ~320 | Outbound sockets, run wall clock | CPU-light, I/O-bound at `SOURCE_CONCURRENCY=8` |
| Raw postings per run | 2,000–5,000 | Peak worker RSS during parse | **Binding on RAM** — §3.3 |
| New postings after dedup | ~150 | Postgres write volume, disk growth | Not binding |
| Postings surviving filter | ~30/day | LLM concurrency 4, network waits | Not binding |
| Drafts generated | ≤ 10/day | `strong`-model latency; no local CPU | Not binding |
| `.docx` renders | ≤ 20/day, on demand | **LibreOffice RSS + CPU burst** | **Binding on CPU** — §3.2 |
| Run wall clock | < 15 min | Two cores against 320 sources | Comfortable — §3.2 |
| Concurrent users | 1 | Everything | Not binding |

Two things are binding, and neither is the thing people expect. It is not query
volume and it is not the LLM: the pipeline spends most of its wall clock waiting
on somebody else's HTTP endpoint. It is **peak memory during normalisation** and
**a LibreOffice CPU burst during rendering**.

### 3.2 CPU

| Consumer | Character | Peak demand |
|---|---|---|
| `selectolax` HTML → text over 2,000–5,000 postings | Single-threaded burst inside the async loop | ~0.6 core for 60–90 s |
| `sha256` content hashing, 5,000 × ~6 KB | Negligible | < 0.05 core |
| Postgres: 150 inserts + GIN `search_tsv` maintenance | Bursty, brief | ~0.3 core for ~20 s |
| `soffice --headless --convert-to pdf` | **Single-threaded, ~1.5 s per attempt, up to 5 attempts per document** (`DOCUMENT_GENERATION.md` §5.5) | 1.0 core for ≤ 8 s |
| uvicorn serving one user | Effectively idle | < 0.1 core |

Two cores is right. The render loop is single-threaded and serialised by design —
it runs on preview/approve, not in the nightly batch — so a second core is not
needed to make it faster, it is needed so a render does not stall the API for the
one person using it. A third core buys nothing measurable.

Run wall clock at 320 sources: `SOURCE_CONCURRENCY=8` (`SOURCE_ADAPTERS.md` §4.1)
with a 180 s per-source ceiling gives a theoretical worst case of
320 ÷ 8 × 180 s = 2 hours, but that assumes every source times out. The realistic
figure is dominated by Workday detail fan-out at 1 req/s per tenant host, and is
covered honestly in §12.1 — including the case where it does **not** fit.

### 3.3 RAM

| Consumer | Steady | Peak | Note |
|---|---|---|---|
| `postgres` | ~900 MB | 1.4 GB | `shared_buffers=768MB` + 40 × `work_mem=16MB` worst case |
| `worker` | ~250 MB | ~800 MB | Peak during normalisation of the largest boards |
| `api` (2 uvicorn workers) | ~320 MB | ~750 MB | `soffice` child adds ~350 MB for the render window |
| `redis` | ~15 MB | 192 MB (capped) | Real usage is kilobytes |
| `web` (Caddy) | ~30 MB | 60 MB | |
| Docker daemon + OS | ~250 MB | 350 MB | |
| **Total** | **~1.8 GB** | **~3.5 GB** | |

4 GB works with roughly 500 MB of headroom at simultaneous peak — and the peaks
do not in fact coincide: the discovery run is at 08:00, rendering happens when
the operator opens the review queue. Provision **2 GB of swap** anyway, so that a
pathological board (a 40 MB JSON response from a misbehaving tenant) degrades to
slow rather than to an OOM kill of Postgres.

**Memory-safety rules that make the 800 MB worker peak hold**, all of which are
already in the design and are restated here because they are what keeps this a
4 GB machine:

- `SourceAdapter.fetch()` is an `AsyncIterator` (`SOURCE_ADAPTERS.md` §2.1). The
  runner consumes postings as they are yielded. It must never materialise a
  whole board into a list.
- `SourceHttpClient` caps response size. A source returning an unbounded body is
  failed, not buffered.
- `MAX_DESCRIPTION_CHARS` truncates `description_text` at persistence.
- Untrusted text is truncated before it reaches a prompt
  (`EXTRACTION_MAX_JD_TOKENS`, `LETTER_MAX_JD_TOKENS`,
  `AI_ARCHITECTURE.md` §7.2) — a cost control that is also a memory control.

### 3.4 Disk

| Consumer | Year 1 | Note |
|---|---|---|
| Postgres data directory | ~1.5–2.0 GB | §5.2 |
| Generated artifacts (`.docx`) | < 60 MB | §5.3 |
| Exports (`.xlsx`) + rendered digests | ~90 MB before retention, ~15 MB after | §5.3 |
| Local `pg_dump` backups (7 daily, compressed) | ~1.4 GB | §9.2 |
| Docker images (3 tags retained × ~1.1 GB) | ~3.3 GB | LibreOffice is ~450 MB of each |
| Docker build cache, logs, OS | ~4 GB | Pruned weekly |
| **Total** | **~11 GB** | |

**40 GB.** The margin is not for data — data is small and grows slowly. It is for
image churn: a backend image carrying LibreOffice, fonts and a Python
environment is ~1.1 GB, and a fortnight of undisciplined deploys will fill 20 GB
without a `docker image prune`. The weekly prune in §11.3 is what actually keeps
this number honest.

### 3.5 Verdict

**2 vCPU / 4 GB / 40 GB SSD, with 2 GB swap.** Confirmed against the envelope,
not assumed. The next size up (4 vCPU / 8 GB) buys nothing until the company
count roughly triples — see §12, where the thing that breaks first is run wall
clock, not the host.

---

## 4. The image

One backend image, used by `api`, `worker` and `migrate`.

```dockerfile
# backend/Dockerfile (shape; the pin values are what matter)
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Kolkata

# LibreOffice is the reference renderer (DOCUMENT_GENERATION.md §5.5).
# Pinned, because page count is a function of the renderer version and
# artifact.validation depends on it being the same one CI used.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui=4:7.4.7-1+deb12u* \
      fonts-liberation2 fonts-dejavu-core \
      ca-certificates \
 && apt-get purge -y --auto-remove \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/

RUN useradd --system --uid 10001 --home /var/lib/scout scout \
 && mkdir -p /var/lib/scout/artifacts /var/lib/scout/exports \
 && chown -R scout:scout /var/lib/scout /app
USER scout

ENV SOFFICE_BIN=/usr/bin/soffice \
    ARTIFACT_DIR=/var/lib/scout/artifacts \
    EXPORT_DIR=/var/lib/scout/exports \
    MAIL_TOKEN_PATH=/var/lib/scout/gmail.token
```

Decisions worth stating:

- **LibreOffice is pinned to a Debian package version, not to `latest`.** The
  page-count verification loop in `DOCUMENT_GENERATION.md` §5.5 is only
  meaningful if the renderer that verified a one-page fit is the renderer that
  will render it again. The renderer version is recorded alongside the artifact
  for the same reason `artifact.model` records the model.
- **`libreoffice-writer-nogui`, not `libreoffice`.** The full suite adds ~1.2 GB
  of Calc, Impress, Draw and Java for a container that converts `.docx` to PDF
  and counts pages.
- **Fonts are installed explicitly.** LibreOffice silently substitutes a missing
  font, which changes line breaking, which changes the page count, which
  invalidates the one thing the render loop exists to prove.
- **Non-root, UID 10001**, matching ownership of the `/var/lib/scout` bind mount
  on the host.
- **`.dockerignore` excludes `.env`, `*.token`, `exports/`, `artifacts/`** —
  restated from `EMAIL_INGESTION.md` §2.5 because a secret that reaches a build
  context reaches an image layer, and layers are forever.

---

## 5. Storage and volume layout

### 5.1 Layout

```
/srv/scout/                       # deploy root, git checkout, root:root 0755
├── docker-compose.yml
├── Caddyfile
├── .env                          # 0600, owner root. Never in git.
└── ops/
    ├── backup.sh
    ├── restore.sh
    └── disk-check.sh

/var/lib/scout/                   # bind-mounted into api + worker, 10001:10001
├── artifacts/                    # generated .docx, 0700
│   └── {artifact_id}/{Manas_Sisodia}_{Company}_{RoleSlug}_{resume|cover}.docx
├── exports/                      # .xlsx workbooks + rendered digests, 0700
│   ├── scout-pipeline-2026-09-05.xlsx
│   └── digest-2026-09-05.html
└── gmail.token                   # Fernet-encrypted OAuth token, 0600

/var/backups/scout/               # local backup staging, 0700, root only
├── db/scout-2026-09-05T0300Z.dump
└── restic/                       # repository cache

docker volume  scout_pgdata       # Postgres data directory
docker volume  scout_caddy_data   # ACME account key + issued certificates
docker volume  scout_caddy_config
```

A **bind mount** for `/var/lib/scout` and **named volumes** for Postgres and
Caddy. The split is deliberate: artifacts and the token must be trivially
backed-up and inspectable by a host-level script, while the Postgres data
directory should never be touched by anything other than Postgres — and a named
volume makes accidental `rsync` of a live data directory harder to do by mistake.

The `.docx` naming scheme is fixed by `DOCUMENT_GENERATION.md` §5.7 and is
human-readable on purpose: the operator uploads these by hand into an employer's
form and has to pick the right one under time pressure. `artifact.path` stores
the key relative to `ARTIFACT_DIR`; `artifact.checksum` is the SHA-256 of the
bytes, so a file that changed on disk is detectable at download time.

### 5.2 Postgres growth over one year

Computed from `ARCHITECTURE.md` §9 (~150 new postings per day) and
`DATA_MODEL.md`, so the number can be checked rather than believed.

| Table | Rows/year | Bytes/row (post-TOAST) | Heap | Indexes | Total |
|---|---:|---:|---:|---:|---:|
| `job_posting` | 54,750 | ~5.5 KB | ~300 MB | ~180 MB (GIN `search_tsv` dominates) | **480 MB** |
| `requirement` | ~197,000 | ~210 B | 41 MB | 18 MB | **59 MB** |
| `match_score` | ~22,000 | ~2.1 KB | 46 MB | 4 MB | **50 MB** |
| `review_item` | ~3,650 | ~1.4 KB | 5 MB | 1 MB | **6 MB** |
| `application` + `application_event` | ~400 + ~1,200 | ~400 B | < 1 MB | < 1 MB | **2 MB** |
| `artifact` + `claim_usage` | ~1,500 + ~9,000 | ~300 B | 3 MB | 2 MB | **5 MB** |
| `email_message` | ~15,000 | ~420 B | 7 MB | 3 MB | **10 MB** |
| `run_log` | ~10,000 | ~4 KB (discovery rows carry 320 `source_results` entries) | 40 MB | 1 MB | **41 MB** |
| `company`, `source`, `resume_variant`, `claim` | ~1,000 | — | < 2 MB | < 1 MB | **3 MB** |
| APScheduler job store | 7 | — | — | — | **< 1 MB** |
| WAL, bloat, catalog, ~20% slack | | | | | **~130 MB** |
| **Year 1 total** | | | | | **≈ 790 MB** |

Add the WAL retention and autovacuum working space and the data directory lands
at **1.5–2.0 GB after a year**, and the retention sweep
(`RETENTION_POSTING_DAYS` = 90 unscored / 180 scored, `RETENTION_MAIL_MONTHS` =
24, `PRUNE_CRON` Sunday 04:00 IST — `APPLICATION_PIPELINE.md`) means it reaches a
**steady state** rather than growing linearly. `job_posting` is the only table
whose growth rate matters, `run_log.source_results` is the only surprising one,
and both are pruned.

### 5.3 Artifact and export space over a year

The number that people expect to be large is not.

Generated `.docx` files are produced on **preview and approve**, not in the
nightly run (`DOCUMENT_GENERATION.md` §5.5). The operating rate is five to ten
applications per week (`ARCHITECTURE.md` §1.1).

```
resume  .docx   ≈ 38 KB   (python-docx, no embedded images)
cover   .docx   ≈ 22 KB
per approved application            ≈ 60 KB
10 applications/week × 52 weeks     = 520 applications/yr
520 × 60 KB                         ≈ 31 MB/yr

preview renders (superseded, kept for provenance, ~2× approved)
                                    ≈ 25 MB/yr
                                      ─────────
artifacts, year 1                   ≈ 56 MB
```

```
nightly .xlsx export  ≈ 240 KB × 365      ≈ 88 MB   → EXPORT_KEEP_LAST=30 ⇒ 7 MB
rendered digest .html ≈  28 KB × 365      ≈ 10 MB   → kept, it is tiny
```

**Total generated-artifact footprint after one year: under 80 MB.** This is worth
stating plainly because it settles a question that would otherwise loom over the
design: object storage is not needed, S3 is not needed, and a lifecycle policy is
not needed. A directory on the VM is the correct answer for a decade of use, and
the backup job in §9 copies the whole tree in under two seconds.

Failed artifacts are **retained**, not deleted (`DATA_MODEL.md` §8.1 — an
artifact with `validation_status = 'failed'` is kept for diagnosis and can never
attach to a `review_item` or `application`). They are counted in the figures
above.

---

## 6. Network model

### 6.1 Ingress

| Port | Bound by | Exposed to | Purpose |
|---|---|---|---|
| 443/tcp, 443/udp | `web` | Internet | HTTPS / HTTP-3 — the only real entry point |
| 80/tcp | `web` | Internet | ACME HTTP-01 challenge; everything else 308s to HTTPS |
| 22/tcp | host `sshd` | **Operator source addresses only**, key auth, no password, no root login | Administration |
| 8000/tcp | `api` | `edge` Compose network only | Not published to the host |
| 5432/tcp | `postgres` | `internal` Compose network only | Not published to the host |
| 6379/tcp | `redis` | `internal` Compose network only | Not published to the host |

The host firewall (`ufw` or the provider's cloud firewall — use both) allows
inbound **22, 80, 443** and nothing else. Docker's iptables integration bypasses
`ufw` for published ports, which is a well-known trap: the defence that actually
holds is that **Postgres and Redis have no `ports:` stanza at all**. There is
nothing to bypass.

`api` sits on both networks because Caddy must reach it; it never publishes a
host port, so it is reachable only through the proxy.

### 6.2 Egress

Egress is required and is not restricted at the network layer, because the system
must reach ~320 ATS hosts whose addresses are configuration, not a fixed list.
The controls are in code, where they can be tested:

| Destination | Purpose | Control |
|---|---|---|
| ~320 ATS / careers hosts, 443 | Discovery | `assert_fetch_allowed()` on every request **after redirect resolution** (`SOURCE_ADAPTERS.md` §4.7); `robots.txt` enforcement; per-host token buckets |
| `bedrock-runtime.{region}.amazonaws.com` **or** the Azure OpenAI endpoint | Extraction, judgement, planning, letters | `LLM_PROVIDER`; the budget circuit breaker (`AI_ARCHITECTURE.md` §3.4) |
| `gmail.googleapis.com`, `oauth2.googleapis.com` | Mail read + digest send | Two scopes only; `send()` asserts the sole recipient (`EMAIL_INGESTION.md` §1.1) |
| `acme-v02.api.letsencrypt.org` | Certificate issuance | Caddy |
| Distribution mirrors, container registry | Build and deploy only | Not reached at runtime |

**The never-scrape list is a code constant, not a firewall rule**
(`ARCHITECTURE.md` §3, invariant 4). This is deliberate and is the stronger
placement: a firewall rule can be edited by whoever holds the host, is invisible
to tests, and does not survive a move to a different substrate. `NEVER_FETCH_HOSTS`
in `sources/policy.py` is covered by a test asserting no code path constructs a
request to a listed host, and by a second test asserting the constant is not
reachable from `Settings`. An egress deny-list on the VM would be a redundant
copy of a rule that is already enforced somewhere it can be proven.

`trust_env=False` on the shared `httpx` client (`SOURCE_ADAPTERS.md` §4.1) means
an ambient `HTTP_PROXY` in the container cannot silently reroute outbound
traffic. Nothing in the Compose environment sets one.

---

## 7. TLS

### 7.1 Termination and issuance

TLS terminates at `web`. Caddy obtains and renews certificates from Let's Encrypt
automatically over ACME, using **HTTP-01 on port 80** by default — which requires
only that the DNS `A`/`AAAA` record for `SCOUT_DOMAIN` points at the VM and that
port 80 is open to the internet.

Certificate material lives in the `caddy_data` volume:

```
/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/{domain}/
    {domain}.crt   {domain}.key   {domain}.json
/data/caddy/acme/.../users/{email}/{email}.key      ← the ACME account key
```

### 7.2 Renewal

Caddy renews at **two-thirds of the certificate lifetime** — roughly 30 days
before a 90-day Let's Encrypt certificate expires — as an in-process background
task. There is no cron entry, no `certbot`, no renewal hook, and no deploy step.
The `caddy_data` volume is what makes this survive `docker compose down`; losing
it means re-issuance, which is harmless but counts against Let's Encrypt's
duplicate-certificate rate limit (5 per exact set of names per 168 hours).

Consequently: **`caddy_data` is in the backup set** (§9.3), and `docker compose
down -v` is never run on production. That flag deletes named volumes, which
means `pgdata` as well; §9.5's restore drill exists partly so that this is a
recoverable mistake rather than a terminal one.

### 7.3 Failure and verification

| Situation | Behaviour | Response |
|---|---|---|
| ACME challenge fails at first boot | Caddy retries with backoff; the site serves its internal self-signed certificate and browsers warn | Check that port 80 is reachable and DNS resolves to this host. `docker compose logs web \| grep -i acme` |
| Renewal fails 30 days out | Caddy retries for the remaining 30 days; certificate stays valid throughout | The disk/health check (§11) surfaces it; there is a month of slack |
| Rate limit hit after repeated `down -v` | Issuance blocked for the window | Restore `caddy_data` from backup rather than re-issuing |

Verification after any deploy: `curl -sI https://$SCOUT_DOMAIN/healthz` returns
`200` and `strict-transport-security`. Expiry is checked from the host, not from
the container, so a wedged Caddy is visible:

```bash
echo | openssl s_client -connect "$SCOUT_DOMAIN:443" -servername "$SCOUT_DOMAIN" 2>/dev/null \
  | openssl x509 -noout -enddate
```

HSTS is set with a one-year max-age and `includeSubDomains`. It is set from the
first deploy rather than "once we are sure", because adding HSTS later to a
domain that has served plain HTTP is the same decision made with less
information.

---

## 8. The ECS Fargate alternative

Evaluated properly so that the recommendation in §8.5 is a conclusion rather
than a preference.

### 8.1 Shape

Region **ap-south-1** (Mumbai) — same region as Bedrock, and the operator and
every target employer are in India.

```
Route 53 ──▶ ALB (public subnets, ACM certificate)
                │
                ├──▶ target group :8000 ──▶ ECS service "api"      desired 1–2
                │                            Fargate 0.5 vCPU / 1 GB
                │
                └──▶ S3 + CloudFront for the SPA (or api serves it)

            ECS service "worker"    desired EXACTLY 1, no autoscaling
                                    Fargate 0.5 vCPU / 1 GB

            RDS PostgreSQL 16, db.t4g.micro, 20 GB gp3, single-AZ
            ElastiCache Redis 7, cache.t4g.micro, single node
            EFS access point mounted at /var/lib/scout  (artifacts + token)
            Secrets Manager: 6 secrets (§ DEPLOYMENT_ENV_RUNBOOK §5)
            ECR: one repository, two tags retained
```

### 8.2 Task definition shape

```jsonc
{
  "family": "scout-api",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512", "memory": "1024",
  "runtimePlatform": { "cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX" },
  "executionRoleArn": "arn:aws:iam::<acct>:role/scoutExecutionRole",
  "taskRoleArn":      "arn:aws:iam::<acct>:role/scoutTaskRole",
  "volumes": [{
    "name": "scout-data",
    "efsVolumeConfiguration": {
      "fileSystemId": "fs-0abc…",
      "transitEncryption": "ENABLED",
      "authorizationConfig": { "accessPointId": "fsap-0def…", "iam": "ENABLED" }
    }
  }],
  "containerDefinitions": [{
    "name": "api",
    "image": "<acct>.dkr.ecr.ap-south-1.amazonaws.com/scout-careers:2026.09.05-1",
    "essential": true,
    "portMappings": [{ "containerPort": 8000, "protocol": "tcp" }],
    "mountPoints": [{ "sourceVolume": "scout-data", "containerPath": "/var/lib/scout" }],
    "environment": [
      { "name": "SCOUT_ENV",         "value": "production" },
      { "name": "SCHEDULER_ENABLED", "value": "false" },
      { "name": "LLM_PROVIDER",      "value": "bedrock" },
      { "name": "TZ",                "value": "Asia/Kolkata" }
    ],
    "secrets": [
      { "name": "DATABASE_URL",   "valueFrom": "arn:aws:secretsmanager:ap-south-1:<acct>:secret:scout/database_url" },
      { "name": "REDIS_URL",      "valueFrom": "arn:aws:secretsmanager:ap-south-1:<acct>:secret:scout/redis_url" },
      { "name": "SESSION_SECRET", "valueFrom": "arn:aws:secretsmanager:ap-south-1:<acct>:secret:scout/session_secret" },
      { "name": "APP_PASSWORD_HASH", "valueFrom": "arn:aws:secretsmanager:ap-south-1:<acct>:secret:scout/app_password_hash" },
      { "name": "MAIL_TOKEN_KEY", "valueFrom": "arn:aws:secretsmanager:ap-south-1:<acct>:secret:scout/mail_token_key" }
    ],
    "healthCheck": {
      "command": ["CMD-SHELL",
        "python -c \"import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=5).status==200 else 1)\""],
      "interval": 30, "timeout": 10, "retries": 3, "startPeriod": 60
    },
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/scout", "awslogs-region": "ap-south-1",
        "awslogs-stream-prefix": "api"
      }
    }
  }]
}
```

`scout-worker` is the same document with `SCHEDULER_ENABLED=true`, the uvicorn
command replaced by `python -m scout_careers.scheduler`, no `portMappings`, and
**`desiredCount: 1` with autoscaling explicitly disabled** — for the reason in
§2.3. On Fargate this is the sharpest edge in the whole alternative: a rolling
deploy with `minimumHealthyPercent: 100` starts the new task before draining the
old one, and for a window of a minute or two **two schedulers are live**. The
worker service must therefore run `minimumHealthyPercent: 0` / `maximumPercent:
100` — a deliberate brief outage in preference to a duplicate 08:00 run.

Bedrock credentials come from `taskRoleArn` rather than from environment
variables, which is the one genuine security improvement Fargate offers over the
VM (§8.4).

### 8.3 RDS sizing

| Parameter | Value | Reason |
|---|---|---|
| Engine | PostgreSQL 16 | Matches `DATA_MODEL.md`; `pg_trgm` available as an extension |
| Instance | `db.t4g.micro` (2 vCPU burstable, 1 GB) | §5.2 puts the working set under 2 GB and QPS in single digits |
| Storage | 20 GB gp3, autoscale to 100 GB | §5.2 puts year 1 under 2 GB |
| Multi-AZ | **No** | Doubles cost to protect a single-user tool against AZ loss. RTO of hours is acceptable (§10) |
| Backup retention | 7 days, automated | Plus the logical `pg_dump` in §9 — physical snapshots do not protect against a bad migration |
| Public accessibility | No | Private subnets, security group from the ECS tasks only |
| Performance Insights | Off | 7-day free tier is fine if wanted; nothing here needs it |

`db.t4g.micro` has 1 GB of RAM and burstable CPU. It is adequate — the GIN index
on `search_tsv` is the only thing that would want more, and it is queried by one
person — but it is a genuine step down from the 768 MB `shared_buffers` the VM
gives Postgres. `db.t4g.small` (2 GB) removes the doubt for another $13/month;
at that point the cost comparison below gets worse, not better.

### 8.4 Cost delta

Monthly, ap-south-1, on-demand, at doc date. Approximate and rounded; the
conclusion does not turn on the third significant figure.

| Line | Fargate | Notes |
|---|---:|---|
| `api` task, 0.5 vCPU / 1 GB, 730 h | $22.5 | $0.05056/vCPU-h + $0.00553/GB-h (ARM64) |
| `worker` task, 0.5 vCPU / 1 GB, 730 h | $22.5 | Runs continuously; APScheduler is in-process |
| RDS `db.t4g.micro`, single-AZ | $13.1 | |
| RDS storage, 20 GB gp3 + backups | $2.8 | |
| ElastiCache `cache.t4g.micro` | $12.4 | For run locks and token buckets |
| Application Load Balancer | $18.0 | $0.0225/h + minimal LCU |
| EFS, 1 GB + throughput | $0.4 | Artifacts are tiny (§5.3) |
| ECR storage, CloudWatch Logs, Secrets Manager (6 × $0.40) | $4.5 | |
| Data transfer out | $1.0 | One user |
| **Fargate total** | **≈ $97 / month** | ≈ ₹8,500 |

| Line | Single VM | Notes |
|---|---:|---|
| Hetzner CX22 (2 vCPU / 4 GB / 40 GB, Falkenstein) | $5.5 | Cheapest credible option; ~120 ms further from IN users |
| — or — AWS Lightsail 2 vCPU / 4 GB / 80 GB, ap-south-1 | $24.0 | In-region, one bill, snapshots included |
| — or — EC2 `t4g.small` + 40 GB gp3, ap-south-1 | $15.4 | Reserved/Savings Plan takes this to ~$10 |
| Backblaze B2 offsite backups, ~10 GB | $0.1 | |
| Domain (amortised) | $1.0 | |
| **VM total** | **$7 – $25 / month** | ≈ ₹600 – ₹2,200 |

**The delta is roughly 4× to 14× — $70 to $90 per month, ₹6,000 to ₹8,000.**

For context that matters: the entire LLM budget is ₹80/day ≈ ₹2,400/month
(`ARCHITECTURE.md` §9). **The Fargate premium alone would exceed the system's
model spend by a factor of three.** A personal tool whose hosting costs more than
its intelligence has its priorities inverted.

The premium is not buying nothing. Honestly stated, Fargate gives:

- IAM task roles instead of static AWS keys in a `.env` file — a real reduction
  in the blast radius of a host compromise, and the only item on this list that
  is a genuine security improvement rather than an operational convenience;
- managed Postgres backups, patching and point-in-time recovery;
- no host to patch, and no `apt upgrade` in anyone's calendar;
- a deployment story that survives the operator not touching it for six months.

And it costs, beyond money:

- **the scheduler-duplication hazard in §8.2**, which does not exist on Compose;
- EFS for a directory of 60 MB of `.docx` files, with EFS's latency and its
  permission model, in place of a bind mount;
- five AWS services to reason about instead of one `docker compose ps`;
- an `apt`-pinned LibreOffice that now has to be reasoned about across ECR image
  promotion rather than rebuilt in place;
- slower iteration: a two-minute image push and service update in place of
  `docker compose up -d --build api`.

### 8.5 Recommendation

**Deploy Docker Compose on a single VM. Do not use Fargate.**

The system is one user, one daily batch, and under 2 GB of state. Fargate solves
elasticity, multi-tenancy and fleet operations — three problems this system does
not have and is explicitly designed never to acquire (`ARCHITECTURE.md` §9: "no
horizontal scaling is designed for, because none is needed"). Choosing it here
would be the same category of error as the vector database and the Celery queue
that §4.1 of that document already rejected.

Prefer **EC2 `t4g.small` or Lightsail in ap-south-1** over the cheaper European
host: same region as Bedrock, one vendor relationship, sub-50 ms to the operator,
and Indian data residency for a database that holds the operator's job-search
history.

Revisit Fargate only if one of these becomes true, and none of them plausibly
will:

- the system acquires a second user with separate data (it will not — `API.md`
  §1 has no multi-user model and `SECURITY_ARCHITECTURE.md` explains why);
- the daily run stops fitting on one host even after the sharding in §12.1;
- a compliance obligation requires a managed data plane and audited patching.

Take one thing from the Fargate design regardless: **use an EC2 instance profile
for Bedrock rather than static `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in
`.env`.** If the VM runs on EC2, this is free, and it removes the
longest-lived credential from the host filesystem. `boto3` picks up the instance
role with no code change (`AI_ARCHITECTURE.md` §13: credentials come from the
environment or the instance role).

---

## 9. Backup and restore

### 9.1 What must survive, and what must not be trusted to survive

| Data | Recoverable without a backup? | In the backup set |
|---|---|---|
| `claim` ledger | **No.** Hand-curated, verified once, irreplaceable | **Yes — the highest-value rows in the system** |
| `resume_variant.content` | Partly — the six seeds re-seed, but operator edits do not | **Yes** |
| `company` + `source` registry | No. 300 companies of manual curation | **Yes** |
| `application`, `application_event` | No. This is the operator's own history and the only honest funnel data (`DATA_MODEL.md` §10) | **Yes** |
| `artifact` rows + `.docx` files | No | **Yes** |
| `job_posting`, `requirement`, `match_score` | Yes — a discovery run regenerates them, at LLM cost | Yes (cheaper to restore than to re-derive) |
| `email_message` metadata | Partly — Gmail holds the mail; classification would re-run | Yes |
| `run_log` | No, but it is diagnostic | Yes |
| Gmail OAuth token | Yes — one browser consent (`DEPLOYMENT_ENV_RUNBOOK.md` §6) | Yes, encrypted |
| `MAIL_TOKEN_KEY`, `SESSION_SECRET`, `APP_PASSWORD_HASH` | **No.** Losing `MAIL_TOKEN_KEY` makes the stored token permanently unreadable | **Yes — offline, separately from everything else** |
| Caddy `caddy_data` | Yes, by re-issuance, subject to ACME rate limits (§7.2) | Yes |
| Docker images | Yes, by rebuild from git | No |
| Redis | Yes, entirely — it holds only ephemeral state (§2.2) | **No** |

### 9.2 Database backup

Logical, nightly, at **03:00 IST** — after the 23:30 export and before the Sunday
04:00 prune, in the quietest part of the schedule and well clear of the 08:00
discovery run.

```bash
#!/usr/bin/env bash
# /srv/scout/ops/backup.sh — run from cron as root
set -Eeuo pipefail

STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
DB_DIR=/var/backups/scout/db
mkdir -p "$DB_DIR"

# 1. Database — custom format: parallel restore, selective restore, compressed.
docker compose -f /srv/scout/docker-compose.yml exec -T postgres \
  pg_dump -U scout -d scout --format=custom --compress=9 \
  > "$DB_DIR/scout-$STAMP.dump"

# Refuse to keep a dump that pg_restore cannot read. A backup that has never
# been parsed is a hope, not a backup.
pg_restore --list "$DB_DIR/scout-$STAMP.dump" > /dev/null

# 2. Artifacts, exports and the encrypted Gmail token.
tar -C /var/lib -czf "$DB_DIR/../scout-data-$STAMP.tar.gz" scout

# 3. Caddy certificate material (§7.2).
docker run --rm -v scout_caddy_data:/data:ro -v "$DB_DIR/..":/out alpine \
  tar -C /data -czf "/out/caddy-$STAMP.tar.gz" .

# 4. Offsite, encrypted. restic encrypts client-side; B2 never sees plaintext.
export RESTIC_REPOSITORY RESTIC_PASSWORD B2_ACCOUNT_ID B2_ACCOUNT_KEY
restic backup /var/backups/scout --tag nightly --host scout-prod

# 5. Retention.
find /var/backups/scout -type f -mtime +7 -delete
restic forget --tag nightly \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

# 6. Record the outcome where the digest will read it (§11.2).
docker compose -f /srv/scout/docker-compose.yml exec -T api \
  python -m scout_careers.ops record-run --type backup --status completed \
  --stat "dump_bytes=$(stat -c%s "$DB_DIR/scout-$STAMP.dump")"
```

```cron
30 21 * * *  /srv/scout/ops/backup.sh  >>/var/log/scout-backup.log 2>&1   # 03:00 IST
```

Cron runs in the host's timezone. The host is set to **UTC** — the database is
UTC (`DATA_MODEL.md` §1) and only the application schedules in `Asia/Kolkata`
(`ARCHITECTURE.md` §8) — so 03:00 IST is `30 21 * * *`. Getting this wrong is the
most common way a backup silently runs during the discovery window.

`--format=custom` rather than plain SQL because it restores in parallel, restores
selectively (a single table, when a bad migration damaged one), and compresses.
The `pg_restore --list` check is not decoration: it is the cheapest possible
proof that the file is a readable archive rather than a truncated write.

### 9.3 Retention

| Tier | Kept | Where | Rationale |
|---|---|---|---|
| Nightly | 7 | Local `/var/backups/scout` **and** restic/B2 | Same-week recovery; the local copy makes restore fast |
| Weekly | 4 | restic/B2 only | Recovery from damage noticed a fortnight later |
| Monthly | 6 | restic/B2 only | Recovery from a slow logical corruption — a bad claim edit, a mis-run prune |
| Secrets bundle | 1 current + 1 previous | **Offline**, encrypted, not on the VM and not in B2 with the data | Restoring data with a lost `MAIL_TOKEN_KEY` is a partial restore |

Total offsite footprint after a year: well under 10 GB, because restic
deduplicates and the `.dump` files are largely similar night to night.

**The secrets bundle is held separately on purpose.** A restic repository whose
password is stored beside the data it encrypts is an encrypted repository in name
only. Keep `RESTIC_PASSWORD`, `MAIL_TOKEN_KEY`, `SESSION_SECRET`,
`POSTGRES_PASSWORD` and `APP_PASSWORD_HASH` in a password manager, on a device
that is not the VM.

### 9.4 Restore procedure

```bash
# 0. Stop everything that writes. Leave postgres up.
docker compose stop api worker

# 1. Retrieve. Local first; B2 if the host was lost.
restic restore latest --target /restore --tag nightly

# 2. Restore the database into a NEW database, never over the live one.
docker compose exec -T postgres createdb -U scout scout_restore
docker compose exec -T postgres pg_restore -U scout -d scout_restore \
  --jobs=2 --no-owner --exit-on-error < /restore/var/backups/scout/db/scout-<STAMP>.dump

# 3. Verify against known invariants before promoting.
docker compose exec -T postgres psql -U scout -d scout_restore -c "
  SELECT 'claims',       count(*) FROM claim WHERE deleted_at IS NULL
  UNION ALL SELECT 'companies',   count(*) FROM company WHERE deleted_at IS NULL
  UNION ALL SELECT 'sources',     count(*) FROM source
  UNION ALL SELECT 'variants',    count(*) FROM resume_variant WHERE active
  UNION ALL SELECT 'applications',count(*) FROM application
  UNION ALL SELECT 'events',      count(*) FROM application_event
  UNION ALL SELECT 'artifacts',   count(*) FROM artifact
  UNION ALL SELECT 'orphan_usage',count(*) FROM claim_usage cu
      LEFT JOIN artifact a ON a.id = cu.artifact_id WHERE a.id IS NULL;"
# orphan_usage MUST be 0 — provenance is never orphaned (DATA_MODEL.md §11).

# 4. Promote.
docker compose exec -T postgres psql -U scout -d postgres -c \
  "ALTER DATABASE scout RENAME TO scout_old; ALTER DATABASE scout_restore RENAME TO scout;"

# 5. Restore the file tree.
tar -C /var/lib -xzf /restore/var/backups/scout/scout-data-<STAMP>.tar.gz
chown -R 10001:10001 /var/lib/scout
chmod 600 /var/lib/scout/gmail.token

# 6. Reconcile schema, then start.
docker compose run --rm migrate            # restored dump may predate head
docker compose up -d api worker

# 7. Verify.
curl -s https://$SCOUT_DOMAIN/api/v1/health | jq
```

**Restoring into a new database and renaming** rather than dropping and
recreating is the difference between a bad restore that is recoverable and one
that is not. `scout_old` is kept for a week, then dropped by hand.

Step 6 matters and is easy to forget: a dump taken before a migration is a
database at the old revision. `alembic upgrade head` after restore is part of the
procedure, not an afterthought.

### 9.5 The restore drill

**A backup that has not been restored is a belief.** The drill is quarterly, is
written down here so it is not improvised, and takes about twenty minutes.

Cadence: **1 January, 1 April, 1 July, 1 October** — and additionally after any
schema migration that touches `claim`, `artifact` or `claim_usage`
(`DATA_MODEL.md` §11).

```
1.  Provision a scratch VM, or use a local Docker host. Never the production VM.
2.  Copy the latest restic snapshot. Time this step and record it.
3.  Run §9.4 steps 1–6 against the scratch host, from this document, without
    improvising. Every deviation you have to make is a defect in this document
    and is fixed here before the drill is considered passed.
4.  Assert:
      · the row counts in §9.4 step 3 match production within one day's activity
      · orphan_usage = 0
      · `GET /api/v1/health` returns 200 with postgres and redis ok
      · the review queue renders and one artifact downloads with a matching
        SHA-256 against `artifact.checksum`
      · Gmail reports `unauthenticated` — expected, the token key is not on the
        scratch host. Restoring it there would defeat the point of §9.3.
5.  Record in the drill log: date, snapshot ID, wall-clock time to a working
    system, and every defect found.
6.  Destroy the scratch host.
```

The wall-clock figure from step 5 is what makes the RTO in §10 a measurement
rather than an aspiration. If a drill comes in materially over four hours, the
RTO is wrong and this document is what changes.

---

## 10. Disaster recovery

### 10.1 Targets

These are set for what this is: **a personal tool, used once a day, by one
person, that competes for attention with a job search.** Targets a business would
set here would be dishonest, because nobody is going to be paged at 03:00 to meet
them.

| Target | Value | Basis |
|---|---|---|
| **RPO** — worst-case data loss | **24 hours** | Nightly logical backup at 03:00 IST. Losing a day means losing at most one discovery run's postings, the day's status events, and any claims edited that day |
| **RTO** — time to a working system | **4 hours** | Measured by the §9.5 drill: provision a VM (20 min), install Docker and clone (15 min), restore (30 min), DNS propagation (up to 60 min), Gmail re-auth (10 min), verify (15 min), plus slack |
| **RTO, degraded** — read-only access to the data | **1 hour** | `pg_restore` into a local Postgres on a laptop. The operator can read their pipeline without any of the rest existing |

### 10.2 Why RPO is 24 hours and not smaller

Continuous archiving (WAL-E / pgBackRest / RDS PITR) would take the RPO to
minutes. It is not implemented, and the reason is a straight comparison of what
is actually lost:

| Lost in a 24-hour window | Cost to recover |
|---|---|
| One discovery run's postings | Re-run it. `POST /api/v1/runs/discovery`. Costs ~₹67 of tokens and 15 minutes |
| The day's mail-derived status events | The mail is still in Gmail. The cursor did not advance past what was committed (`EMAIL_INGESTION.md` §3.1), so the next poll re-reads it. **Zero loss.** |
| A claim added that day | Re-enter it. Minutes |
| An approved application and its artifacts | The most painful item, and the operator knows they submitted it. Re-approve and re-render |
| A day of `run_log` | Diagnostic only |

Nothing in that table is worth the operational weight of continuous archiving on
a single-user system. The RPO that would be worth improving is the one on the
**claims ledger and the company registry**, and the cheaper mitigation is
already in `DATA_MODEL.md` §11: seed data — the six variants, the initial ledger,
the never-scrape list — ships as an idempotent seed script in git, so the
irreplaceable core is version-controlled independently of any backup.

### 10.3 Scenarios

| Scenario | Blast radius | Response | Recovery |
|---|---|---|---|
| Container crash-loops | One service | `restart: unless-stopped` retries; `depends_on: service_healthy` holds dependents back | Seconds. `docker compose logs -f <svc>` |
| Postgres data-directory corruption | Total | §9.4 restore | ≤ 1 h, RPO ≤ 24 h |
| Bad migration in production | Schema | `DEPLOYMENT_ENV_RUNBOOK.md` §4 — roll forward; restore only if a destructive revision ran | ≤ 1 h |
| Host lost (provider failure, mistaken destroy) | Total | Provision, restore from restic/B2, repoint DNS, re-auth Gmail | ≤ 4 h |
| `docker compose down -v` run by mistake | `pgdata` + `caddy_data` | Restore both from §9.2's outputs | ≤ 1 h. This is why `caddy_data` is in the backup set |
| Gmail refresh token revoked | Mail ingestion + digest | Re-run the OAuth flow. Cursor did not advance; digest is written to disk meanwhile (`EMAIL_INGESTION.md` §2.5) | 10 min, no data loss |
| Bedrock unavailable in ap-south-1 | Extraction and generation only | Switch `LLM_PROVIDER=azure_openai` and restart. Provenance survives the switch (`AI_ARCHITECTURE.md` §12) | 5 min |
| Disk full | Writes fail across the board | §11.3 alarm fires days earlier; `DEPLOYMENT_ENV_RUNBOOK.md` §8 has the runbook | ≤ 30 min |
| Restic repository or B2 account lost | Offsite history | Local 7-day copies survive; re-initialise the repository | Same-day |
| **`MAIL_TOKEN_KEY` lost with no offline copy** | The stored Gmail token is permanently undecryptable | Re-run OAuth from scratch | 10 min — **and this is exactly why §9.3 keeps the secrets bundle offline** |

---

## 11. Monitoring and alerting

### 11.1 Proportion

One user, one daily batch. Prometheus, Grafana, Loki and an alertmanager would be
more moving parts than the application, would consume a third of the RAM budget,
and would be watched by nobody. The system already has an alerting channel that
the operator reads every morning — **the digest** (`EMAIL_INGESTION.md` §11) —
and the right design is to route operational signal into it rather than to build
a second one.

Three mechanisms. That is all.

### 11.2 The three mechanisms

**(a) Healthcheck — is it up?**

`GET /api/v1/health` already reports per-dependency status and deliberately never
returns 500 for a degraded dependency (`API.md` §7). Compose healthchecks
(§2.2) restart a wedged container. What Compose cannot do is tell the operator
that the whole host is gone.

For that, a **dead-man's switch**: the scheduler pings an external ping URL after
every successful job. If the ping stops, the external service emails the
operator. This inverts the usual polling arrangement, and the inversion is the
point — an uptime monitor running on the VM cannot report that the VM is down.

```python
# scheduler/jobs.py — after every scheduled job completes
async def _heartbeat(job: str, ok: bool) -> None:
    if not settings.HEARTBEAT_URL:
        return
    suffix = "" if ok else "/fail"
    with contextlib.suppress(Exception):        # never let telemetry fail a run
        await http.get(f"{settings.HEARTBEAT_URL}/{job}{suffix}", timeout=5)
```

`contextlib.suppress` is deliberate: a monitoring endpoint being down must never
turn a successful discovery run into a failed one.

**(b) Run failure — did it work?**

Already built. Every job writes a `run_log` row with per-source results
(`DATA_MODEL.md` §9.1), and the digest's failure section renders it
(`EMAIL_INGESTION.md` §11). Nothing new is needed for:

- a source failing (`source_results[].status = "error"`),
- a source auto-disabled at `consecutive_failures >= 5` (`SOURCE_ADAPTERS.md`
  §4.8),
- the LLM budget breaker opening (`AI_ARCHITECTURE.md` §3.4),
- an alert parser's layout fingerprint breaking (`EMAIL_INGESTION.md` §4.7),
- ledger validation failures.

And the digest **always sends**, including when there is nothing to report and
when the run failed (`EMAIL_INGESTION.md` §11.1). A digest that silently stops
arriving is indistinguishable from a digest with no news; that ambiguity is what
breaks trust in a daily tool, and it is also what would make a missing failure
report invisible.

**(c) Disk space — will it work tomorrow?**

The one condition the application cannot report on, because by the time it
matters Postgres can no longer write the `run_log` row that would report it.

```bash
#!/usr/bin/env bash
# /srv/scout/ops/disk-check.sh — hourly from cron
set -Eeuo pipefail
USED=$(df --output=pcent / | tail -1 | tr -dc '0-9')
INODES=$(df --output=ipcent / | tail -1 | tr -dc '0-9')
[ "$USED" -lt 80 ] && [ "$INODES" -lt 80 ] && exit 0

docker compose -f /srv/scout/docker-compose.yml exec -T api \
  python -m scout_careers.ops record-run \
    --type ops --status failed \
    --error "disk ${USED}% used, inodes ${INODES}% used"
```

`run_log.run_type` is `TEXT`, not an enum (`DATA_MODEL.md` §9.1), so `'ops'`
needs no migration. The digest's failure section reads `run_log`, so the alarm
arrives in the operator's inbox at 08:15 the next morning — days before 80% used
becomes 100% used, given §3.4's growth rates.

The alarm rides the digest rather than sending its own mail, and that is not
laziness: the system sends **exactly one class of email**
(`ARCHITECTURE.md` §3, invariant 2). An operational alerter that opened a second
outbound path would weaken an invariant to save writing one function.

### 11.3 Host hygiene

```cron
30 21 * * *   /srv/scout/ops/backup.sh                    # 03:00 IST
0  *  * * *   /srv/scout/ops/disk-check.sh
0  20 * * 0   docker image prune -af --filter "until=336h" # 01:30 IST Monday
0  19 * * *   docker compose -f /srv/scout/docker-compose.yml exec -T postgres \
                psql -U scout -d scout -c "SELECT 1" >/dev/null    # connectivity canary
```

Unattended security upgrades are enabled at the OS level. Docker image upgrades
are not automatic: a `postgres:16-alpine` that silently became a different minor
version during an unattended reboot is a change nobody decided to make.

### 11.4 Logging

`structlog` JSON to stdout, collected by the Docker `json-file` driver with
rotation, which is the whole logging stack:

```yaml
logging:
  driver: json-file
  options: { max-size: "20m", max-file: "5" }
```

100 MB per service, which at one line per pipeline stage per run is months of
history. No log shipper, no aggregator, no retention policy beyond rotation.

What is never logged is not an infrastructure decision and is not restated here;
`ARCHITECTURE.md` §8 and `AI_ARCHITECTURE.md` §11.2 own it. The one
infrastructure-level consequence: **because logs stay on the host and are never
shipped anywhere, a log leak requires a host compromise**, which is a materially
smaller surface than a hosted log aggregator holding the same lines.

---

## 12. Capacity: 300 → 3,000 companies

Everything above is sized for the §9 envelope. This section is the honest answer
to "what happens if the registry grows tenfold", in the order things break.

### 12.1 First to break: run wall clock

**This breaks before anything else, and it is close to breaking at 300.**

The binding constraint is not CPU, RAM or database throughput. It is the
per-tenant rate limit on Workday — 1 req/s per host (`SOURCE_ADAPTERS.md` §4.3) —
combined with Workday's requirement of a detail fetch per posting
(`SOURCE_ADAPTERS.md` §5.4). A Workday site with 1,000 listed postings costs
50 list requests plus 1,000 detail requests, serialised at 1 req/s:
**~17.5 minutes for one source**, against a 15-minute whole-run budget and a
180-second per-source ceiling that would cancel it long before that.

This is survivable at 300 companies only because of two things already in the
design, and it is worth naming them so they are not "optimised away":

- `applied_facets` narrows a Workday site to India, typically taking a 1,483-posting
  board to 50–150 postings — 60–160 seconds, inside the ceiling;
- `max_pages` (default 50) bounds the worst case, and a tenant that needs more is
  split into several `source` rows with different facets, which is exactly the
  case `UNIQUE (company_id, adapter, config)` was designed for.

At **3,000 companies (~3,200 sources)**, with `SOURCE_CONCURRENCY=8` and a
realistic 25-second mean per source, the arithmetic is
3,200 ÷ 8 × 25 s = **167 minutes**. Eleven times the budget.

**The fix is not more concurrency.** Raising `SOURCE_CONCURRENCY` does not help,
because the limit is per-host token buckets, not local parallelism — and raising
it makes the system a worse citizen of the endpoints it depends on, which
`SOURCE_ADAPTERS.md` §4.5 is explicit about not doing.

The fix is **to stop running everything at 08:00**. The schema already supports
it: `source.poll_interval_minutes` and the partial index
`source_due_idx ON source (enabled, last_run_at) WHERE enabled`
(`DATA_MODEL.md` §3.2) exist precisely so a run can select *due* sources rather
than *all* sources.

```
Today   08:00 → fetch all ~320 sources                   → ~12 min
At 3k   hourly → fetch sources where last_run_at is due   → ~130 sources/hour
        08:00  → the full downstream pipeline (dedupe → filter → extract →
                 score → generate) over everything ingested in the last 24 h
```

Discovery becomes continuous and cheap; the expensive, model-bearing stages stay
a single daily batch, because the digest is daily and the operator is daily. This
is a scheduler change and a query change. No new infrastructure.

### 12.2 Second: LLM cost

`AI_ARCHITECTURE.md` §8.4 already prices this: 300 → 600 companies takes daily
spend from ₹67 to ₹89, over the ₹80 budget. Linear extrapolation to 3,000 gives
**roughly ₹500–700/day, ₹15,000–20,000/month** — an order of magnitude past
budget, and past every hosting option in §8.4 combined.

The response is stated there and is restated because it is a capacity decision,
not a cost decision: **a stricter filter, not a bigger budget.** Stage ④ already
kills 80% of postings and saves more per day than the entire daily budget
(`AI_ARCHITECTURE.md` §8.3). At ten times the input it must kill ~98%, which
means tier-gating extraction — `dream` and `strong` companies extract
automatically, `volume` companies extract only on operator request. The budget
circuit breaker caps the damage in the meantime, by code rather than by invoice.

### 12.3 Third: Postgres

At 1,500 new postings a day (10×), `job_posting` grows ~3 GB/year of heap and
the GIN index on `search_tsv` reaches 1.5–2 GB. The index alone then exceeds
`shared_buffers`, and full-text queries start hitting disk.

In order of what to do:

1. **Prune harder.** `RETENTION_POSTING_DAYS` at 90/180 is generous for postings
   that never scored. 30/120 costs nothing real.
2. **Partition `job_posting` by `first_seen_at`, monthly.** Retention becomes a
   `DETACH PARTITION`, which is instant, instead of a `DELETE` that bloats the
   heap and then has to be vacuumed.
3. **Raise `shared_buffers` to 2 GB**, which needs 8 GB of host RAM.

Only step 3 costs money, and it arrives well after §12.1 and §12.2 have already
forced a rethink.

### 12.4 Fourth: host RAM

The peak in §3.3 scales with postings-per-run, not with companies-tracked — and
once §12.1's sharding is in place, postings-per-run **falls**, because the run is
hourly instead of daily. Sharding fixes the memory ceiling as a side effect. On
the unsharded path, 4 GB stops being enough somewhere around 800–1,000 companies.

### 12.5 Fifth, and the real ceiling: the operator

**Ten minutes a day** (`ARCHITECTURE.md` §1.1) and five to ten applications a
week. That number does not change when the registry grows tenfold. The queue is
already capped at 10 drafts per run (`GENERATION_DAILY_CAP`) and the digest at
10 items (`DIGEST_MAX_QUEUE_ITEMS`), so at 3,000 companies the operator sees
**exactly the same ten items** — drawn from a larger pool, and therefore better
ones, but ten.

Which is the honest conclusion of this section: **growing the registry past a few
hundred companies buys better selection, not more output, and it costs run time
and tokens linearly to do it.** The infrastructure question ("can the box take
it?") is answerable — shard the run, prune harder, add RAM. The prior question is
whether tripling the token spend to reorder the same ten items is worth it. Below
about 1,000 companies, on the current shape, nothing needs to change at all.

---

## 13. Related documents

| Document | Covers |
|---|---|
| `ARCHITECTURE.md` | Invariants, module boundaries, pipeline stages, scale envelope |
| `DEPLOYMENT_ENV_RUNBOOK.md` | First deploy, routine deploy, rollback, secrets, Gmail OAuth, smoke tests, troubleshooting |
| `CONFIGURATION.md` | Every environment variable, feature flags, tuning constants, boot validation |
| `DATA_MODEL.md` | Tables, indexes, retention, migration policy |
| `API.md` | `/health`, `/runs/*`, envelopes, the deliberate absences |
| `AI_ARCHITECTURE.md` | Provider endpoints, cost model, budget breaker, growth sensitivity |
| `SOURCE_ADAPTERS.md` | HTTP client, rate limits, robots, circuit breakers, egress behaviour |
| `EMAIL_INGESTION.md` | Gmail OAuth, token storage, schedule, the digest |
| `DOCUMENT_GENERATION.md` | LibreOffice as reference renderer, artifact naming and storage |
| `APPLICATION_PIPELINE.md` | Export schedule, prune schedule, retention settings |
| `SECURITY_ARCHITECTURE.md` | Threat model, secret handling, session model |
