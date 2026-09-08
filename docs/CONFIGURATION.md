# CONFIGURATION — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the complete configuration surface — every
environment variable, its type, default and requirement; the `Settings` object
and the no-magic-constants rule; every feature flag; the scoring and generation
tuning constants; boot-time validation; and per-environment overrides.
`ARCHITECTURE.md` wins on system-level concerns and invariants; each domain
document (`AI_ARCHITECTURE.md`, `MATCH_SCORING.md`, `DOCUMENT_GENERATION.md`,
`CLAIMS_LEDGER.md`, `EMAIL_INGESTION.md`, `SOURCE_ADAPTERS.md`,
`APPLICATION_PIPELINE.md`) wins on the *meaning and effect* of the keys it owns;
`INFRASTRUCTURE.md` wins on where values are provisioned;
`DEPLOYMENT_ENV_RUNBOOK.md` wins on how they are set. This file is the single
place where all of them are listed together, and where the *shape* of
configuration — types, defaults, validation, precedence — is decided.

---

## 1. The rule

> **No magic constants in modules.** Every tunable is a field on one `Settings`
> object, built by `pydantic-settings` from the environment, and read from there.

`ARCHITECTURE.md` §8 states it; this is what it means in practice.

```python
# WRONG — a threshold that nobody can find, change or test
if score.coverage_pct < 45.0:
    return None

# RIGHT
if score.coverage_pct < settings.GENERATION_MIN_COVERAGE_PCT:
    return None
```

The rule is not tidiness. Three concrete properties fall out of it:

1. **A tuning change is a settings change, not a deploy.** The rollback story in
   `AI_ARCHITECTURE.md` §12 and `DEPLOYMENT_ENV_RUNBOOK.md` §4.3 depends on it:
   thirty seconds and a container recreate, rather than a build.
2. **Every tunable is enumerable.** `GET /api/v1/settings` (`API.md` §7) can show
   the operator the effective configuration because there is a single object to
   read. A constant buried in `scoring/composite.py` could not appear there.
3. **A test can vary it.** An eval that pins `SCORING_BLEND_HARD` to compare two
   weightings requires the weighting to be a field, not a literal.

### 1.1 What is *not* configuration

Three categories are deliberately code, and moving them into settings would be a
review failure:

| Not configurable | Where it lives | Why |
|---|---|---|
| `NEVER_FETCH_HOSTS` — the never-scrape list | `sources/policy.py`, a frozen constant | `ARCHITECTURE.md` §3, invariant 4: the list is a code constant, not a config value. A test asserts it is **not reachable from `Settings`** |
| `DIGEST_RECIPIENT` | Resolved once from `MAIL_OPERATOR_ADDRESS` at startup; `send_digest()` takes no recipient argument | Invariant 2. There is no function in the codebase that accepts an arbitrary `to` address (`EMAIL_INGESTION.md` §1.1) |
| Adapter page sizes, bucket rates, retry status sets, per-adapter selectors | `sources/http.py`, `sources/*.py`, `mail/alerts/*.py` | Workday's `limit` is clamped to 20 by the server; making it configurable invites someone to set 100 and silently skip 80% of a board. A selector edited in a settings screen at 07:00 to fix a broken parse is a selector nobody ever tests (`SOURCE_ADAPTERS.md` §5.4, `EMAIL_INGESTION.md` §4.7) |

Nor is `LLM_MAX_CONCURRENCY`-style safety ever expressed as "unlimited". Every
bound is a number, and every number has a maximum.

### 1.2 The `Settings` object

```python
# common/config.py
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, EmailStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The entire configuration surface. Constructed once, at import of
    `common.config`, and injected everywhere. Frozen after construction."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,      # SESSION_SECRET, never session_secret
        extra="forbid",           # an unknown key is a typo; typos fail at boot
        frozen=True,              # nothing mutates settings at runtime
    )

    # ---- core ---------------------------------------------------------
    SCOUT_ENV: Literal["local", "production"] = "local"
    SCOUT_BASE_URL: str = "http://localhost:8000"
    TZ: str = "Asia/Kolkata"

    # ---- database -----------------------------------------------------
    DATABASE_URL: PostgresDsn
    DATABASE_POOL_SIZE: Annotated[int, Field(ge=1, le=50)] = 10
    # … one field per row of §3 …

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        return _run_boot_checks(self)     # §8


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

`extra="forbid"` is load-bearing. `SCOREING_BLEND_HARD=0.9` in a `.env` would
otherwise be silently ignored and the operator would spend an afternoon
wondering why their tuning had no effect. Here it refuses to start and names the
key.

`frozen=True` means there is no code path that mutates configuration at runtime.
`PATCH /api/v1/settings` (`API.md` §7) writes operator-facing preferences to the
database, not to this object; anything on `Settings` requires an `.env` edit and
a restart, which is the correct amount of friction for a value that changes
system behaviour.

### 1.3 Precedence

```
process environment  >  .env file  >  field default
```

Docker Compose `environment:` entries are process environment and therefore win
over `.env` — which is exactly how `SCHEDULER_ENABLED` is forced to `false` on
`api` and `true` on `worker` from one shared `env_file`
(`INFRASTRUCTURE.md` §2.2). There is no third layer, no per-host override file,
and no runtime reload.

### 1.4 Types and conventions

| Convention | Rule |
|---|---|
| Naming | `SCREAMING_SNAKE_CASE`, grouped by a domain prefix (`LLM_`, `MAIL_`, `SCORING_`, `SOURCE_`, `FF_`) |
| Booleans | `true` / `false` lower-case. `1`, `yes`, `on` also parse, but the canonical form is `true` |
| Durations | Suffixed with the unit: `_S` seconds, `_MS` milliseconds, `_DAYS`, `_MONTHS`, `_MINUTES` |
| Money | `_INR` or `_USD` in the name. `NUMERIC`-backed, never float where it reaches the database |
| Lists | Comma-separated, no spaces: `DEFAULT_LOCATION_FILTER=IN,Remote` |
| Maps | Avoided. A `key=value` string is a parser, and a parser here fails in the settings layer where the error names neither the key nor the format — see §9.3. Prefer one scalar per value |
| Cron | Standard five-field, **evaluated in `SCHEDULER_TIMEZONE`**, not UTC |
| Times of day | `HH:MM`, 24-hour, in `SCHEDULER_TIMEZONE` |
| Secrets | Typed `SecretStr`, so an accidental `repr()` prints `**********` |
| Paths | Absolute. `Path` typed and checked writable at boot |

---

## 2. Core

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `SCOUT_ENV` | `local` \| `production` | `local` | no | Selects the boot-validation profile (§8). Not a feature switch — behaviour differences are individual settings, not branches on this value |
| `SCOUT_BASE_URL` | url | `http://localhost:8000` | **prod** | Origin used for deep links in the digest (`EMAIL_INGESTION.md` §11.1). Must be the public HTTPS origin in production |
| `SCOUT_DOMAIN` | str | — | **prod** | Hostname Caddy serves and requests a certificate for (`INFRASTRUCTURE.md` §7) |
| `ACME_EMAIL` | email | — | **prod** | Let's Encrypt account contact |
| `TZ` | tz name | `Asia/Kolkata` | no | Container display timezone. Storage and computation are UTC regardless (`ARCHITECTURE.md` §8) |
| `APP_VERSION` | str | `dev` | no | Reported by `/health`; set to the deployed tag by CI |

---

## 3. Database

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `DATABASE_URL` | dsn | — | **yes** | `postgresql+asyncpg://user:pass@host:5432/scout`. The `+asyncpg` marker is mandatory — SQLAlchemy 2.0 async (`ARCHITECTURE.md` §4). A sync driver fails at boot |
| `DATABASE_POOL_SIZE` | int 1–50 | `10` | no | Base connection pool. One user; 10 is generous and exists so a burst of dashboard queries during a run does not queue |
| `DATABASE_MAX_OVERFLOW` | int 0–50 | `5` | no | Transient connections above the pool. Pool + overflow must stay under Postgres `max_connections` (40, `INFRASTRUCTURE.md` §2.2) across `api` **and** `worker` |
| `DATABASE_POOL_TIMEOUT_S` | float | `10.0` | no | Wait for a connection before raising. Fails fast rather than stacking a run behind a leaked session |
| `DATABASE_POOL_RECYCLE_S` | int | `1800` | no | Recycle connections older than this; avoids server-side idle disconnects |
| `DATABASE_STATEMENT_TIMEOUT_MS` | int | `30000` | no | Per-statement ceiling, set on connect. A runaway full-text query cannot hold the pool |
| `DATABASE_LOCK_TIMEOUT_MS` | int | `5000` | no | Refuse to wait indefinitely for a lock; a migration in flight fails a query rather than freezing the API |
| `DATABASE_ECHO` | bool | `false` | no | Log every SQL statement. **Local only** — refused in production (§8), because parameters would carry job-description text into logs |
| `DATABASE_MIGRATE_ON_BOOT` | bool | `false` | no | Run `alembic upgrade head` at application start. **Deliberately off.** Migrations run as a separate step before the new image serves traffic (`DEPLOYMENT_ENV_RUNBOOK.md` §3.1); two containers racing to migrate is not a supported state |
| `POSTGRES_PASSWORD` | secret | — | **yes** | Consumed by the `postgres` service and interpolated into `DATABASE_URL` by Compose, so the password exists once |

---

## 4. Redis

Redis holds run locks, per-host rate-limit token buckets, the 24-hour
`robots.txt` cache and the `mail:history_id` convenience cache — all ephemeral
and all reconstructible (`ARCHITECTURE.md` §4, `INFRASTRUCTURE.md` §2.2).

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `REDIS_URL` | dsn | `redis://redis:6379/0` | **yes** | Connection URL |
| `REDIS_MAX_CONNECTIONS` | int | `20` | no | Client pool ceiling |
| `REDIS_SOCKET_TIMEOUT_S` | float | `5.0` | no | Read/write timeout. Redis is never a slow dependency here; a timeout means it is unhealthy |
| `RUN_LOCK_TTL_S` | int | `3600` | no | Expiry on `lock:run:*`. Must exceed `RUN_WALL_CLOCK_BUDGET_S` (900) and stay under the interval between scheduled runs. **A lock without a TTL turns one crash into permanent downtime** (`DEPLOYMENT_ENV_RUNBOOK.md` §9.8) |
| `GMAIL_REFRESH_LOCK_TTL_S` | int | `30` | no | Serialises token refresh so a poll and the digest send never race (`EMAIL_INGESTION.md` §2.5) |
| `ROBOTS_CACHE_TTL_S` | int | `86400` | no | `robots.txt` cached per host per day (`SOURCE_ADAPTERS.md` §4.7) |

---

## 5. LLM provider

Owned by `AI_ARCHITECTURE.md` §13. Reproduced complete, with types and
requirement status added.

### 5.1 Provider selection and model pinning

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `LLM_PROVIDER` | `bedrock` \| `azure_openai` | `bedrock` | no | Whole-provider switch. Services depend on the `LLMClient` Protocol, never on a provider module, so this is a settings change and a restart |
| `LLM_MODEL_FAST` | str | `anthropic.claude-3-5-haiku-20241022-v1:0` | no | Resolves the `fast` alias — extraction, coverage judgement, mail classification |
| `LLM_MODEL_STRONG` | str | `anthropic.claude-sonnet-4-20250514-v1:0` | no | Resolves the `strong` alias — tailoring plan, cover letter |
| `LLM_MAX_CONCURRENCY` | int 1–16 | `4` | no | Parallel model calls per stage. Higher does not shorten the run — model work is under 4 minutes of a 15-minute budget — and makes provider throttling far likelier |

**Model IDs are pinned. Never use a `-latest` alias.** A silently upgraded model
invalidates every eval result and every `artifact.model` provenance record
without a deploy having happened (`AI_ARCHITECTURE.md` §4.1), which breaks
invariant 7. A model-ID change is treated exactly like a prompt change: eval
first, then the pinned bump.

### 5.2 Bedrock credentials

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `AWS_REGION` | str | `ap-south-1` | **if bedrock** | Bedrock region |
| `AWS_ACCESS_KEY_ID` | secret | — | conditional | **Omit when an instance role is available** (`DEPLOYMENT_ENV_RUNBOOK.md` §5.1). `boto3` resolves the role with no code change |
| `AWS_SECRET_ACCESS_KEY` | secret | — | conditional | As above |
| `BEDROCK_ENDPOINT_URL` | url | unset | no | Override for a VPC endpoint. Unset uses the public regional endpoint |

### 5.3 Azure OpenAI credentials

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | url | — | **if azure** | `https://<resource>.openai.azure.com` |
| `AZURE_OPENAI_API_KEY` | secret | — | **if azure** | |
| `AZURE_OPENAI_API_VERSION` | str | `2024-10-21` | no | Must support `response_format: json_schema` with `strict: true` (`AI_ARCHITECTURE.md` §3.2) |
| `AZURE_OPENAI_DEPLOYMENT_FAST` | str | `gpt-4o-mini` | **if azure** | Deployment name backing the `fast` alias |
| `AZURE_OPENAI_DEPLOYMENT_STRONG` | str | `gpt-4o` | **if azure** | Deployment name backing the `strong` alias |

Credentials appear in no configuration table the API returns, no log line and no
error message (`AI_ARCHITECTURE.md` §13, `ARCHITECTURE.md` §3 invariant 6). They
are `SecretStr`, and the `structlog` processor redacts by key name and by pattern
(`AKIA`-prefixed, `Bearer`, long base64-ish strings) as a second line of defence.

### 5.4 Cost, budget and the circuit breaker

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `LLM_DAILY_BUDGET_INR` | decimal | `80` | no | **The circuit breaker threshold.** At 100% no further model calls are made in that run; remaining items are left for tomorrow and the digest reports it |
| `LLM_BUDGET_WARN_PCT` | int 1–100 | `80` | no | At this fraction, a warning is logged and generation — the expensive stage — is capped to the top 5 items |
| `LLM_INR_PER_USD` | decimal | `88` | no | FX for cost accounting. Cost is computed from the `usage` on every response, not from estimates; only the conversion is configured |
| `LLM_PRICE_FAST_IN` | decimal | `0.80` | no | USD per million input tokens, `fast` |
| `LLM_PRICE_FAST_OUT` | decimal | `4.00` | no | USD per million output tokens, `fast` |
| `LLM_PRICE_STRONG_IN` | decimal | `3.00` | no | USD per million input tokens, `strong` |
| `LLM_PRICE_STRONG_OUT` | decimal | `15.00` | no | USD per million output tokens, `strong` |

The budget's headroom is thin on purpose — ₹66.66 modelled against ₹80. A budget
with 300% headroom does not constrain any decision, and the point of the number
is to make the next expensive idea argue for itself (`AI_ARCHITECTURE.md` §8.2).

### 5.5 Prompt input bounds and caching

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `EXTRACTION_MAX_JD_TOKENS` | int | `4000` | no | Truncation of the untrusted JD envelope for extraction, from the head — requirements appear before benefits boilerplate. **Also the bound on the cost attack** (a 400,000-token JD) |
| `LETTER_MAX_JD_TOKENS` | int | `900` | no | Bounded JD excerpt supplied to the letter writer as reference vocabulary, never as instruction |
| `LLM_CACHE_ENABLED` | bool | `true` | no | `content_hash`-keyed extraction cache. Key includes prompt version and resolved model ID, so a bump correctly misses rather than serving a stale extraction (`AI_ARCHITECTURE.md` §9) |
| `LLM_REQUEST_TIMEOUT_MULTIPLIER` | float 1.0–3.0 | `1.0` | no | Scales every per-family `CallPolicy.timeout_s`. For a slow region or a degraded provider; the per-family relative shape is not otherwise adjustable |

---

## 6. Gmail and mail

Owned by `EMAIL_INGESTION.md` §12.

### 6.1 Authorisation and transport

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `MAIL_ENABLED` | bool | `true` | no | Master switch for the whole `mail/` module. **`false` locally by default** — a local run that could send is a local run that can violate invariant 2 by accident |
| `MAIL_OPERATOR_ADDRESS` | email | — | **if `MAIL_ENABLED`** | **The only address the system may send to.** Resolved once at startup into the `DIGEST_RECIPIENT` constant. `GmailClient.send()` asserts every `To`/`Cc`/`Bcc` equals it and raises `OutboundPolicyViolation` otherwise; the exception is not caught |
| `MAIL_ALERT_ADDRESS` | email | `<operator>+scout` | no | Plus-alias (or separate mailbox) that job alerts are delivered to. Changing it to a dedicated account changes nothing else |
| `GMAIL_CLIENT_ID` | str | — | **if `MAIL_ENABLED`** | Desktop OAuth client ID |
| `GMAIL_CLIENT_SECRET` | secret | — | **if `MAIL_ENABLED`** | Desktop client secret. Not cryptographically a secret (PKCE binds the code), but still kept out of git and logs |
| `MAIL_TOKEN_PATH` | path | `/var/lib/scout/gmail.token` | no | Encrypted OAuth token file, `0600`, owned by the service user, outside the repo and outside every Docker build context |
| `MAIL_TOKEN_KEY` | secret | — | **if `MAIL_ENABLED`** | Fernet key encrypting the token file. **Never stored in the same file as the token.** Losing it makes the stored token permanently undecryptable |
| `MAIL_RATE_UNITS_PER_SEC` | int | `20` | no | Self-imposed Gmail quota-unit ceiling |


> **What Phase 1 actually ships.** Reading, and nothing else: `MAIL_ENABLED`,
> `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_CLIENT_SECRETS_PATH` (the
> downloaded desktop-client JSON, as an alternative to the previous two),
> `MAIL_TOKEN_PATH`, `MAIL_TOKEN_KEY`, `GMAIL_REFRESH_LOCK_TTL_S` and
> `GMAIL_MAX_MESSAGES`. `MAIL_ENABLED` defaults to **`false`**, not `true`: a
> Gmail grant is the highest-value secret in the system, and nothing should hold
> one because a default said so.
>
> **Only `gmail.readonly` is requested.** `gmail.send` belongs to the digest and
> arrives with it, which is why `MAIL_OPERATOR_ADDRESS`, `DIGEST_*` and the
> polling and classification keys below have no `Settings` fields yet — and, with
> `extra="forbid"`, cannot be set. Re-consent is one browser screen; holding the
> capability to send as the operator for a phase we do not use it is not.

### 6.2 Polling and linkage

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `MAIL_POLL_CRON` | cron list | `10 8 * * *;*/30 9-22 * * *` | no | Poll schedule, IST. The 08:10 entry exists so the 08:15 digest reports overnight status changes |
| `MAIL_LOOKBACK_DAYS` | int | `14` | no | Window for the full-sweep fallback on the reply stream when the `historyId` cursor is 404'd by Gmail |
| `MAIL_ALERT_LOOKBACK_DAYS` | int | `2` | no | Same, for the alert stream |
| `MAIL_LINK_WINDOW_DAYS` | int | `120` | no | How far back a `submitted_at` may make an application a linkage candidate |
| `MAIL_COMPANY_TRGM_THRESHOLD` | float 0–1 | `0.75` | no | Display-name → `company.name` trigram floor for rule R3b. Lowering it links `"Stripe via Greenhouse"` to more companies, including the wrong ones |
| `MAIL_MAX_BODY_CHARS` | int | `5000` | no | Classification truncation budget: 4,000 head + 1,000 tail, joined by an explicit marker |
| `MAIL_STORE_SUBJECT` | bool | `true` | no | `false` stores the first 40 characters plus an ellipsis. Subject is needed for the review queue and for reference matching (R4); the default is to store it |

### 6.3 Classification thresholds

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `MAIL_CONF_THRESHOLD_TERMINAL` | float 0–1 | `0.90` | no | Auto-apply floor for `rejection` and `offer`. Higher than the rest because a wrongly recorded rejection stops follow-up on a live application and a wrongly recorded offer corrupts the only outcome metric that matters |
| `MAIL_CONF_THRESHOLD_DEFAULT` | float 0–1 | `0.80` | no | Auto-apply floor for every other class |

Below threshold — and in every case where linkage was unresolved — the class and
confidence are stored, **no `application_event` is written**, and the message
enters `v_mail_review_queue`. Nothing is guessed.

### 6.4 Digest

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `DIGEST_ENABLED` | bool | `true` | no | `false` renders to `EXPORT_DIR/digest-YYYY-MM-DD.html` and sends nothing |
| `DIGEST_SEND_AT` | `HH:MM` | `08:15` | no | IST. Must be after the discovery run and the 08:10 mail poll |
| `DIGEST_MAX_QUEUE_ITEMS` | int | `10` | no | Cap on the review-queue section. Readable on a phone in under a minute is the constraint |
| `DIGEST_MAX_ALERT_ITEMS` | int | `10` | no | Cap on the alert section |

The digest **always sends**, including when there is nothing to report and when
the run failed. There is no `DIGEST_SEND_ONLY_IF_NEWS` setting and there will not
be one: a digest that silently stops arriving is indistinguishable from a digest
with no news, and that ambiguity is what breaks trust in a daily tool.

---

## 7. Scheduler

APScheduler, in-process, Postgres job store (`ARCHITECTURE.md` §4).

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `SCHEDULER_ENABLED` | bool | `false` | no | **Defaults off so the safe state is the default.** `true` on exactly one process — the `worker` container. Two schedulers means two 08:00 discovery runs (`INFRASTRUCTURE.md` §2.3) |
| `SCHEDULER_TIMEZONE` | tz name | `Asia/Kolkata` | no | Every cron expression and `HH:MM` in this document is evaluated here. The daily run fires at 08:00 IST regardless of host timezone. A UTC scheduler fires at 13:30 IST and looks like "it did not fire" |
| `SCHEDULER_JOBSTORE_TABLE` | str | `apscheduler_jobs` | no | Job-store table. Not managed by Alembic — APScheduler creates it |
| `SCHEDULER_MISFIRE_GRACE_S` | int | `1800` | no | How late a missed job may still run. 30 minutes covers a host reboot; beyond that the run is triggered by hand |
| `SCHEDULER_COALESCE` | bool | `true` | no | Collapse several missed fires of one job into one. Three skipped discovery runs should produce one run, not three |
| `SCHEDULER_MAX_INSTANCES` | int | `1` | no | Per job. Belt to the Redis run lock's braces |
| `DISCOVERY_CRON` | cron | `0 8 * * *` | no | The daily discovery run |
| `RESCORE_CRON` | cron | `0 2 * * *` | no | Nightly sweep re-scoring postings older than `RESCORE_MAX_AGE_DAYS` |
| `EXPORT_CRON` | cron | `30 23 * * *` | no | Spreadsheet export |
| `PRUNE_CRON` | cron | `0 4 * * 0` | no | Retention sweep, Sunday |
| `RUN_WALL_CLOCK_BUDGET_S` | int | `900` | no | Hard ceiling on a discovery run; the runner cancels stragglers. Must stay below `RUN_LOCK_TTL_S` |
| `HEARTBEAT_URL` | url | unset | no | Dead-man's-switch base URL pinged after each job. Unset disables it. Ping failures are suppressed and never fail a run (`INFRASTRUCTURE.md` §11.2) |

The seven registered jobs: `discovery`, `mail_poll`, `digest`, `export`, `prune`,
`rescore`, `heartbeat`. The go-live checklist asserts the count.

> **What Phase 1 actually ships.** `Settings` uses `extra="forbid"`, so a key in
> `.env` with no field behind it is a boot failure — which makes the difference
> between "designed" and "implemented" operationally real. Phase 1 declares
> `SCHEDULER_ENABLED`, `SCHEDULER_JOBSTORE_TABLE`, `SCHEDULER_MISFIRE_GRACE_S`,
> `SCHEDULER_SHUTDOWN_GRACE_S`, `DISCOVERY_CRON_HOUR`, `DISCOVERY_CRON_MINUTE`
> and `RUN_WALL_CLOCK_BUDGET_S`. Three deliberate differences from the table
> above:
>
> - **`DISCOVERY_CRON_HOUR` / `DISCOVERY_CRON_MINUTE` replace `DISCOVERY_CRON`.**
>   08:00 daily is the whole Phase 1 schedule, and two range-validated integers
>   cannot be a cron expression that parses but means something else. A cron
>   string comes back when a job needs a shape two integers cannot express.
> - **No `SCHEDULER_TIMEZONE`.** The trigger is built in `TZ`, the field the
>   rest of the system already uses for display. A second timezone setting is a
>   second thing that can disagree, and the failure it produces — a run at a
>   time nobody chose — is silent.
> - **No `SCHEDULER_COALESCE` / `SCHEDULER_MAX_INSTANCES`.** Both are code
>   constants (`scheduler/app.py`). `coalesce=false` has no correct value here:
>   it turns three missed windows into three runs, two of which the run lock
>   refuses and records as refusals that read like incidents.
>
> `RESCORE_CRON`, `EXPORT_CRON`, `PRUNE_CRON`, `HEARTBEAT_URL` and the other six
> jobs arrive with the stages they drive. The registered job count is one.


---

## 8. Ingestion and sources

Owned by `SOURCE_ADAPTERS.md`.

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `SOURCE_USER_AGENT` | str | `ScoutCareers/1.0 (personal job-search agent; +mailto:<operator>)` | **prod** | One identifying UA for all outbound traffic. **Honest — never a browser string.** It carries a contact address so an operator on the other end can ask us to stop. It never varies per source; a rotating UA is an evasion technique. Configuration rather than a code constant only so the address is not committed |
| `SOURCE_CONCURRENCY` | int 1–32 | `8` | no | Sources fetched in parallel. Each source's own requests are serialised. Raising it does not speed the run — per-host token buckets are the limit — and makes the system a worse citizen |
| `SOURCE_PROBE_TIMEOUT_S` | float | `10.0` | no | Whole-call ceiling for `probe()`. User-facing: `POST /companies/detect` must feel instant |
| `SOURCE_TIMEOUT_S` | int | `180` | no | Hard ceiling per source including retries and rate-limit waits. A cancelled source discards what it had yielded — **partial ingestion is not allowed**, because a half-fetched board looks like "everything else closed" to the two-run `closed_at` rule |
| `SOURCE_RETRY_BUDGET_S` | int | `90` | no | Total retry time per source, so one sick upstream cannot eat the run |
| `RATE_LIMIT_WAIT_S` | int | `20` | no | Deadline for acquiring a token-bucket lease. Past it the source is marked `rate_limited` for the run and the runner moves on. Waiting forever on a crowded bucket is how a 15-minute run becomes a 50-minute one |
| `AUTO_DISABLE_THRESHOLD` | int | `5` | no | `consecutive_failures` at which a source is set `enabled = false`, reported in the digest, and never silently dropped. `rate_limited` and `circuit_open` do **not** increment it — they are our own back-pressure, not the source's fault |
| `CIRCUIT_BREAKER_FAILURES` | int | `5` | no | Consecutive transport/5xx failures on one `bucket_key` that open the in-run breaker for the rest of the run |
| `MAX_DESCRIPTION_CHARS` | int | `60000` | no | Truncation of `description_text` at persistence. A cost control that is also a memory control (`INFRASTRUCTURE.md` §3.3) |
| `MAX_RESPONSE_BYTES` | int | `20971520` | no | Response-size cap in `SourceHttpClient`. A source returning an unbounded body is failed, not buffered |
| `INGEST_CLOSE_AFTER_MISSED_RUNS` | int | `2` | no | Consecutive runs a posting may be unseen before `closed_at` is set |
| `ALERT_COMPANY_MATCH_THRESHOLD` | float | `0.45` | no | Trigram similarity a parsed mail-alert employer name must reach to be filed under a registered company (`SOURCE_ADAPTERS.md` §7.3). Below it the lead goes to the reserved `unmatched` company. Lowering it mis-attributes, which is worse than missing: a mis-filed lead is indistinguishable from a real role at a tracked employer |
| `DEFAULT_LOCATION_FILTER` | list | `IN,Remote` | no | Stage-④ location gate applied where `company.location_filter` is empty. **Tightening this is the first lever on LLM cost** — the filter saves more per day than the entire daily budget |
| `FILTER_SENIORITY_DENY` | list | `intern,staff,principal,manager,director,executive` | no | Seniority values killed at stage ④. Measured against the live corpus rather than assumed: the original `intern,director,executive` left `staff`, `principal` and `manager` passing, which is 2,340 roles a candidate with months rather than years cannot reach |
| `FILTER_KEYWORD_DENY` | list | `sales,recruiter,teacher,nurse,driver,warehouse,account executive,counsel,customer success,technical support` | no | Title keywords killed at stage ④, whole-word against the **title only**. Most-specific entry wins, so a phrase names the reason rather than one of its words. Worth 1.1% of the kill rate — it catches obvious non-engineering titles and nothing else, because most such roles (Account Executive, Solutions Architect) contain no denied word at all |
| `FILTER_MAX_YEARS_EXPERIENCE` | int 0–30 | `5` | no | Years of experience a description may demand before the role is out of reach, compared against the **least** demanding figure it states — so "8+ years overall, 3+ with Go" is kept and left to scoring. A description stating nothing passes and is surfaced with the figure recorded as unknown. `0` disables it. This reads what a role asks for, which no title list can: "Senior Software Engineer" means two years at one employer and ten at another |
| `ALERT_FIDELITY_EXTRACT` | bool | `false` | no | Whether `fidelity: "low"` mail-alert postings are sent to extraction. **Off, and it should stay off**: a two-line snippet yields garbage requirements, and garbage requirements produce a confident, wrong coverage score |

---

## 9. Scoring

Owned by `MATCH_SCORING.md` §13. These are the constants that decide what
reaches the queue, and they are the ones most worth understanding before
changing.

### 9.1 Versions

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `EXTRACTION_PROMPT_VERSION` | str | `extract.v3` | no | Written to `requirement.prompt_version` |
| `SKILL_VOCAB_VERSION` | str | `vocab.2026-08-20` | no | Concatenated into the same column, so a vocabulary change is distinguishable from a prompt change |
| `SCORING_PROMPT_VERSION` | str | `score.v1` | no | Part of `UNIQUE (posting_id, variant_id, prompt_version)` on `match_score`. **Bumping it makes every existing score a separate row rather than an update** — that is the intent, and it is also why bumping it triggers a rescore sweep. `score.v1`, not the `score.v2` this table first carried: the implementation is the first one, and starting at v2 would imply a v1 whose rows nobody could produce. The value written to the column is this string joined to the adjacency version (§9.2), because an adjacency edit changes every score it touches |

### 9.2 Vocabulary matching

| Variable | Type | Default | Effect of raising | Effect of lowering |
|---|---|---|---|---|
| `SKILL_TRIGRAM_THRESHOLD` | float 0–1 | `0.62` | Fewer fuzzy matches; more requirements fall through to the model, raising cost and lowering deterministic coverage | More false skill matches; coverage inflates on near-miss strings, which promotes bad fits into a queue the operator trusts |
| `SKILL_ADJACENCY_MIN` | float 0–1 | `0.40` | Fewer `partial` credits; coverage understated | Adjacent-but-different skills score as partial; the gap list stops being honest |

Adjacency itself is **data, not configuration**: one score per vocabulary
`family`, in `scoring/adjacency.yaml`, carrying its own version. It is versioned
separately from `skills.yaml` on purpose — adjacency changes what a score is,
not what a requirement is, so an edit re-scores and reuses the extraction output
(`MATCH_SCORING.md` §11.1) instead of invalidating `requirement.prompt_version`
and forcing a full re-extraction. Families too coarse to score honestly are set
to `0.00` there rather than omitted, with the reason beside each one: `platform`
holds both `kubernetes` and `git`, and a single family score would let one earn
credit for the other.

### 9.3 Composite score

| Variable | Type | Default | Description and effect |
|---|---|---|---|
| `SCORING_BLEND_HARD` | float 0–1 | `0.80` | `w_hard` in the composite. Raising it makes hard-requirement coverage dominate — good for precision, and it will suppress straddle roles where a variant covers the interesting half. Lowering it lets a pile of `nice` matches carry a role over the line |
| `SCORING_PARTIAL_CREDIT` | float 0–1 | `0.50` | Credit for `partial`. At `1.0` a partial is a match and the gap list becomes decorative; at `0.0` the honest-gap paragraph loses most of its material |
| `SCORING_TIER_WEIGHT_DREAM` | dec 0–2 | `1.10` | Multiplier `T` by `company.tier`. A wider spread makes tier, not fit, the ranking |
| `SCORING_TIER_WEIGHT_STRONG` | dec 0–2 | `1.00` | |
| `SCORING_TIER_WEIGHT_VOLUME` | dec 0–2 | `0.90` | |
| `SCORING_HARD_GATE_PASS` | dec 0–1 | `0.60` | `H` at or above which `G` is 1.00 — the must-haves are genuinely covered |
| `SCORING_HARD_GATE_WARN` | dec 0–1 | `0.40` | `H` at or above which `G` is the warn factor; below it, the fail factor |
| `SCORING_HARD_GATE_WARN_FACTOR` | dec 0–1 | `0.85` | `G` in the middle band. Real gaps on the must-haves; apply with eyes open |
| `SCORING_HARD_GATE_FAIL_FACTOR` | dec 0–1 | `0.65` | `G` below the warn floor. **This is what stops a role with 2 of 7 hard requirements ranking above one with 6 of 7 on the strength of `nice` matches.** Flattening it removes that protection |

**Seven scalars, not the two composite strings this table first specified.**
`SCORING_TIER_WEIGHTS=dream=1.10,...` and `SCORING_HARD_GATE_BANDS=0.60:1.00,...`
are each a small parser, and a parser in a settings file is a class of failure
this project has already paid for: `NoDecode` exists on the list fields because
`pydantic-settings` JSON-decoded `IN,Remote` before any validator ran and died
with "Expecting value: line 1 column 1", naming neither the key nor the format.
A scalar is validated by `pydantic` for free, its bound is declared where it is
read, and a typo names the key that is wrong. The cost is five more lines in
`.env.example`, which is the cheaper side of that trade.
| `SCORING_RECENCY_GRACE_DAYS` | int | `14` | Days before recency decay starts |
| `SCORING_RECENCY_HALF_LIFE_DAYS` | int | `45` | Decay half-life |
| `SCORING_RECENCY_FLOOR` | float 0–1 | `0.65` | Minimum `R`. **Never 0.** An old posting that fits perfectly should still be visible; recency is a tiebreak, not a gate |

### 9.4 Thresholds into generation

| Variable | Type | Default | Description |
|---|---|---|---|
| `GENERATION_MIN_COMPOSITE` | float | `30.0` | Composite floor for creating a `review_item` at all. Below it, the posting stays queryable with its scores and gaps but never reaches the queue |
| `GENERATION_DAILY_CAP` | int | `10` | Max drafts per day — one discovery run per day, so this is equivalently the per-run cap. The single name for this bound across every document; see §10.1 |
| `RESCORE_MAX_AGE_DAYS` | int | `30` | Age at which the nightly sweep re-scores a posting |
| ~~`SCORING_MIN_HARD_REQUIREMENTS`~~ | — | — | **Not implemented, and deliberately not.** The behaviour is: extraction returning zero `hard` requirements is not scored as a perfect match; the posting is flagged for manual review and the empty bucket is logged. That is a rule, not a tunable. Setting it to `0` would mean "score a posting whose requirements we failed to read as a 100% match", which is the single most dangerous default in the formula (`scoring/service.py:plan_posting`) |

---

## 10. Generation

Owned by `DOCUMENT_GENERATION.md` §13 and `CLAIMS_LEDGER.md` §11.

### 10.1 Master switches and caps

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `GENERATION_ENABLED` | bool | `true` | no | Master switch for stage ⑧ |
| `GENERATION_MIN_COVERAGE_PCT` | float | `45.0` | no | Coverage floor for **automatic** generation. Manual imports via `POST /postings/import` bypass it — the operator asked for that one specifically |
| `GENERATION_DAILY_CAP` | int | `10` | no | Hard cap on drafts per day (declared in §9.4, repeated here for the generation reader). Matches the scale envelope. Generation is 55% of spend on 16% of calls, so this is the cap that keeps the budget honest |
| `COVER_LETTER_ENABLED` | bool | `true` | no | Master switch for the letter branch |
| `COVER_LETTER_TARGET_WORDS` | int | `400` | no | Prompt target. The lint checks ±20%. 400 is the length at which a letter is read rather than skimmed |
| `GENERATION_VALIDATION_RETRIES` | int 0–3 | `1` | no | Regeneration attempts after a ledger-validation failure, before `needs_manual_review` |

### 10.2 Rendering

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `RESUME_MAX_PAGES` | int | `1` | no | **Not raised.** Present so the fit loop has no magic constant, not so it can be changed |
| `RENDER_VERIFY_PAGES` | bool | `true` | no | Render via headless LibreOffice and count the pages in the PDF. **Off only in local development, never in the deployed image** — refused at boot in production (§12.2). No heuristic substitutes: `python-docx` writes a document, it does not lay one out |
| `SOFFICE_BIN` | path | `/usr/bin/soffice` | no | Reference renderer. Pinned by package version in the image (`INFRASTRUCTURE.md` §4); the version is recorded alongside the artifact |
| `RENDER_TIMEOUT_S` | int | `60` | no | Per conversion. Five ladder attempts at ~1.5 s each is the normal case |
| `ARTIFACT_DIR` | path | `/var/lib/scout/artifacts` | no | Where generated `.docx` files live. Must be writable by the service user; checked at boot |
| `SIMILARITY_WARN` | float 0–1 | `0.55` | no | Paragraph-level MinHash similarity against recent letters: warn |
| `SIMILARITY_BLOCK` | float 0–1 | `0.72` | no | Block. Raising it lets the letters converge, which is the specific failure the warm temperature exists to avoid |

### 10.3 Claims ledger

The enforcement layer. Its defaults are the strict ones, and the reason is
invariant 3: free-form model invention is a build failure, not a warning.

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `LEDGER_DEFAULT_TTL_DAYS` | int | `180` | no | `expires_at` applied to volatile claims — a test count or a corpus size is true on a date and drifts afterwards |
| `LEDGER_EXPIRED_CLAIM_POLICY` | `fail` \| `warn` | `fail` | no | Behaviour when a cited claim is stale. **`fail` is the default and should stay there**: `warn` ships a number the operator has not re-verified into a document they will send |
| `LEDGER_APPROXIMATION_TOLERANCE` | float | `0.05` | no | ±5% band for spans marked approximate (`~60%` resolving against a claim of `60`) |
| `LEDGER_RESTRICTED_DISCLOSURE_COMPANIES` | list of slugs | `[]` | no | The **only** employers to whom a `restricted` claim may be emitted. Empty means none — an empty allow-list is the safe default, and adding a slug is a deliberate disclosure decision |
| `LEDGER_VALIDATION_PROMPT_VERSION` | str | `validate.v2` | no | Assertion-extraction prompt version |
| `LEDGER_EXPIRY_WARNING_DAYS` | int | `30` | no | Digest lookahead for claims about to expire |

There is deliberately **no** setting that bypasses ledger validation. `API.md` §8
records the absence of an override endpoint as intentional, and the database
trigger on `artifact` refuses to let a `failed` artifact attach to a
`review_item` or `application` regardless of configuration.

---

## 11. Security, logging and export

### 11.1 Security

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `SESSION_SECRET` | secret | — | **yes** | Signs the session cookie. ≥ 32 bytes. Rotating it invalidates the session — which is the intended effect |
| `APP_PASSWORD_HASH` | secret | — | **yes** | Argon2id hash of the single password. The plaintext is never in the environment, never on the host, and is generated on the operator's laptop |
| `SESSION_TTL_DAYS` | int | `30` | no | Cookie lifetime. Long, because there is one user, one device, and no registration or password-reset flow to fall back on |
| `SESSION_COOKIE_NAME` | str | `scout_session` | no | |
| `SESSION_COOKIE_SECURE` | bool | `true` | no | `false` only for `http://localhost`. Refused in production (§12.2) |
| `SESSION_COOKIE_SAMESITE` | `lax`\|`strict` | `lax` | no | |
| `LOGIN_RATE_LIMIT_PER_MIN` | int | `5` | no | Failed-login attempts per minute before 429 |
| `CORS_ALLOW_ORIGINS` | list | `[]` | no | Empty in production — the SPA is same-origin behind Caddy. **`*` is refused at boot**, always, in every environment |
| `TRUSTED_HOSTS` | list | derived from `SCOUT_DOMAIN` | no | Host-header allow-list |
| `API_RATE_LIMIT_PER_MIN` | int | `120` | no | Local rate limit; the source of the 429 in `API.md` §1 |

### 11.2 Logging

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `LOG_LEVEL` | `DEBUG`…`ERROR` | `INFO` | no | `DEBUG` is refused in production (§12.2): debug paths log payload shapes that can carry job-description text |
| `LOG_FORMAT` | `json` \| `console` | `json` | no | `console` for local readability |
| `LOG_REDACT_EXTRA_KEYS` | list | `[]` | no | **Adds to**, never replaces, the built-in redaction set. The processor redacts by key name and by pattern as a second line of defence, on the principle that a rule enforced only by discipline is not enforced |
| `LOG_SQL_SLOW_MS` | int | `1000` | no | Log statements slower than this, with the statement text but never the parameters |

What is never logged is not configurable: credentials, OAuth material, full JD
text, generated resume or letter content, claim statements and metric values,
full email bodies and addresses, prompt text, `LLMResponse.raw_text`
(`ARCHITECTURE.md` §8, `AI_ARCHITECTURE.md` §11.2). There is no
`LOG_INCLUDE_BODIES` setting. Diagnosis works off identifiers — given `run_id`,
`posting_id` and `prompt_version`, the exact call is reconstructible from stored
rows.

### 11.3 Export, tracking and retention

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `EXPORT_DIR` | path | `/var/lib/scout/exports` | no | Workbooks and rendered digests. Writable-checked at boot |
| `EXPORT_KEEP_LAST` | int | `30` | no | Nightly workbooks retained on disk |
| `GHOST_AFTER_DAYS` | int | `30` | no | Threshold for `v_ghosted`. **Ghosting is computed, never stored** — absence of evidence is not evidence, so this is a view parameter, not a status |
| `SUBMIT_CONFIRM_DAYS` | int | `3` | no | Approved-with-no-events grace before the "possibly not submitted" prompt |
| `MIN_N_FOR_RATE` | int | `15` | no | Below this, counts are shown and **rates are suppressed everywhere**. A 1-of-2 response rate is not 50% |
| `MIN_N_FOR_COMPARISON` | int | `30` | no | Per-arm minimum before a variant comparison is displayed at all |
| `FOLLOWUP_MAX_PER_DAY` | int | `3` | no | Cap on surfaced follow-up prompts. **The system surfaces the prompt; the operator writes the message** — invariant 2 |
| `FOLLOWUP_MAX_PER_APP` | int | `2` | no | How many times one application may be surfaced |
| `FOLLOWUP_QUIET_DAYS_LIVE` | int | `7` | no | Quiet period for `screening` / `interview` |
| `FOLLOWUP_QUIET_DAYS_ACK` | int | `10` | no | Quiet period for `acknowledged` |
| `FOLLOWUP_QUIET_DAYS_SUBMITTED` | int | `14` | no | Quiet period for `submitted` |
| `RETENTION_POSTING_DAYS` | int pair | `90,180` | no | Closed postings: unscored, then scored |
| `RETENTION_MAIL_MONTHS` | int | `24` | no | `email_message` metadata. Bodies were never stored |
| `RETENTION_ARTIFACT_MONTHS` | int | `36` | no | Generated files. Long, because an artifact attached to an application is that application's evidence |

---

## 12. Feature flags

### 12.1 The roster

| Flag | Default | Gates | Off-state behaviour |
|---|---|---|---|
| `FF_COVERAGE_JUDGEMENT` | **`true`** | The residual coverage-judgement model call for requirements the vocabulary could not resolve | Deterministic-only coverage. **Understated, never overstated** — fewer items clear the threshold, nothing is wrong |
| `FF_TAILORING_REPHRASE` | **`false`** | The `rephrase` op — the only model-composed resume text | `apply_plan` ignores `rephrase` ops. Plans authored while it was on still apply their other operations; nothing is orphaned |
| `FF_GAP_IN_OPENING` | **`false`** | Gap-first letter placement (the gap carried in ¶1 rather than ¶3) | The gap paragraph stays at its default placement |
| `FF_STREAMING_PREVIEW` | **`false`** | Streamed cover-letter preview in the review UI | The letter is generated batch and shown when complete. **What is streamed is never what is saved** either way — the artifact always comes from a subsequent batch `structured()` call through the full gate |

**Every new behaviour on the generation path ships off** (`ARCHITECTURE.md` §8),
and the three flags above that touch generation are all `false`.
`FF_COVERAGE_JUDGEMENT` is `true` and is not an exception to that rule: it gates
a *scoring*-path call that shipped as part of the original design, is priced into
the cost model, and whose off-state is the conservative one. It is a flag so that
it can be turned off in an incident, not because it is new.

### 12.2 The rollout discipline

Uniform, from `AI_ARCHITECTURE.md` §12, restated because it is what makes a flag
worth having:

1. Ship the flag off. The code path exists in production and is unreachable.
2. Enable for **one named slice** — `volume`-tier companies, or a fixed set of
   source IDs. **Never a percentage:** at ten generations a day a percentage
   rollout is noise, and a named slice is reproducible.
3. Run for two weeks. Compare validation pass rate, repair-retry rate, cost per
   item, operator acceptance rate, and — where there is enough data — response
   rate from `v_funnel`.
4. Promote to default only if the eval holds and the slice metrics did not
   regress.

Rollback is a settings change and a container recreate — thirty seconds, no build
(`DEPLOYMENT_ENV_RUNBOOK.md` §4.3). Every flagged path is written so the off
state is a complete, correct behaviour rather than a degraded one, which is what
makes that rollback safe to perform at 08:20 without thinking about it.

`LLM_PROVIDER`, `LLM_MODEL_FAST` and `LLM_MODEL_STRONG` behave as flags for the
same purposes and are gated by the same eval rule: a provider switch or a model
pin bump runs the eval first, exactly like a prompt change.

---

## 13. `.env.example`

Reproduced in full. Copy to `.env`, `chmod 600`, and replace every `CHANGE_ME`.
Boot refuses to start in production if any placeholder survives (§14).

```dotenv
# =====================================================================
# Scout Careers — environment configuration
# Copy to .env, chmod 600, replace every CHANGE_ME.
# Never commit .env. It is in .gitignore and .dockerignore.
# Cron expressions and HH:MM times are evaluated in SCHEDULER_TIMEZONE.
# =====================================================================

# --- core ------------------------------------------------------------
SCOUT_ENV=local
SCOUT_BASE_URL=http://localhost:8000
SCOUT_DOMAIN=scout.example.com
ACME_EMAIL=CHANGE_ME@example.com
TZ=Asia/Kolkata
APP_VERSION=dev

# --- database --------------------------------------------------------
POSTGRES_PASSWORD=CHANGE_ME
DATABASE_URL=postgresql+asyncpg://scout:CHANGE_ME@postgres:5432/scout
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=5
DATABASE_POOL_TIMEOUT_S=10.0
DATABASE_POOL_RECYCLE_S=1800
DATABASE_STATEMENT_TIMEOUT_MS=30000
DATABASE_LOCK_TIMEOUT_MS=5000
DATABASE_ECHO=false
DATABASE_MIGRATE_ON_BOOT=false

# --- redis -----------------------------------------------------------
REDIS_URL=redis://redis:6379/0
REDIS_MAX_CONNECTIONS=20
REDIS_SOCKET_TIMEOUT_S=5.0
RUN_LOCK_TTL_S=3600
GMAIL_REFRESH_LOCK_TTL_S=30
ROBOTS_CACHE_TTL_S=86400

# --- LLM provider ----------------------------------------------------
LLM_PROVIDER=bedrock
LLM_MODEL_FAST=anthropic.claude-3-5-haiku-20241022-v1:0
LLM_MODEL_STRONG=anthropic.claude-sonnet-4-20250514-v1:0
LLM_MAX_CONCURRENCY=4

# Bedrock. Omit the two keys entirely when an EC2 instance role is attached.
AWS_REGION=ap-south-1
#AWS_ACCESS_KEY_ID=CHANGE_ME
#AWS_SECRET_ACCESS_KEY=CHANGE_ME
#BEDROCK_ENDPOINT_URL=

# Azure OpenAI (alternate). Required only when LLM_PROVIDER=azure_openai.
#AZURE_OPENAI_ENDPOINT=https://CHANGE_ME.openai.azure.com
#AZURE_OPENAI_API_KEY=CHANGE_ME
#AZURE_OPENAI_API_VERSION=2024-10-21
#AZURE_OPENAI_DEPLOYMENT_FAST=gpt-4o-mini
#AZURE_OPENAI_DEPLOYMENT_STRONG=gpt-4o

# Cost and the circuit breaker.
LLM_DAILY_BUDGET_INR=80
LLM_BUDGET_WARN_PCT=80
LLM_INR_PER_USD=88
LLM_PRICE_FAST_IN=0.80
LLM_PRICE_FAST_OUT=4.00
LLM_PRICE_STRONG_IN=3.00
LLM_PRICE_STRONG_OUT=15.00

# Untrusted-input bounds and caching.
EXTRACTION_MAX_JD_TOKENS=4000
LETTER_MAX_JD_TOKENS=900
LLM_CACHE_ENABLED=true
LLM_REQUEST_TIMEOUT_MULTIPLIER=1.0

# --- Gmail and mail --------------------------------------------------
MAIL_ENABLED=false
MAIL_OPERATOR_ADDRESS=CHANGE_ME@gmail.com
MAIL_ALERT_ADDRESS=CHANGE_ME+scout@gmail.com
GMAIL_CLIENT_ID=CHANGE_ME.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=CHANGE_ME
MAIL_TOKEN_PATH=/var/lib/scout/gmail.token
MAIL_TOKEN_KEY=CHANGE_ME
MAIL_RATE_UNITS_PER_SEC=20

MAIL_POLL_CRON=10 8 * * *;*/30 9-22 * * *
MAIL_LOOKBACK_DAYS=14
MAIL_ALERT_LOOKBACK_DAYS=2
MAIL_LINK_WINDOW_DAYS=120
MAIL_COMPANY_TRGM_THRESHOLD=0.75
MAIL_MAX_BODY_CHARS=5000
MAIL_STORE_SUBJECT=true
MAIL_CONF_THRESHOLD_TERMINAL=0.90
MAIL_CONF_THRESHOLD_DEFAULT=0.80

DIGEST_ENABLED=true
DIGEST_SEND_AT=08:15
DIGEST_MAX_QUEUE_ITEMS=10
DIGEST_MAX_ALERT_ITEMS=10

# --- scheduler -------------------------------------------------------
# true on the worker container ONLY. Two schedulers = two 08:00 runs.
SCHEDULER_ENABLED=false
SCHEDULER_TIMEZONE=Asia/Kolkata
SCHEDULER_JOBSTORE_TABLE=apscheduler_jobs
SCHEDULER_MISFIRE_GRACE_S=1800
SCHEDULER_COALESCE=true
SCHEDULER_MAX_INSTANCES=1
DISCOVERY_CRON=0 8 * * *
RESCORE_CRON=0 2 * * *
EXPORT_CRON=30 23 * * *
PRUNE_CRON=0 4 * * 0
RUN_WALL_CLOCK_BUDGET_S=900
#HEARTBEAT_URL=https://hc-ping.com/CHANGE_ME

# --- ingestion and sources -------------------------------------------
SOURCE_USER_AGENT=ScoutCareers/1.0 (personal job-search agent; +mailto:CHANGE_ME@example.com)
SOURCE_CONCURRENCY=8
SOURCE_PROBE_TIMEOUT_S=10.0
SOURCE_TIMEOUT_S=180
SOURCE_RETRY_BUDGET_S=90
RATE_LIMIT_WAIT_S=20
AUTO_DISABLE_THRESHOLD=5
CIRCUIT_BREAKER_FAILURES=5
MAX_DESCRIPTION_CHARS=60000
MAX_RESPONSE_BYTES=20971520
INGEST_CLOSE_AFTER_MISSED_RUNS=2
ALERT_COMPANY_MATCH_THRESHOLD=0.45
DEFAULT_LOCATION_FILTER=IN,Remote
FILTER_SENIORITY_DENY=intern,staff,principal,manager,director,executive
FILTER_KEYWORD_DENY=sales,recruiter,teacher,nurse,driver,warehouse,account executive,counsel,customer success,technical support
FILTER_MAX_YEARS_EXPERIENCE=5
ALERT_FIDELITY_EXTRACT=false

# --- scoring ---------------------------------------------------------
EXTRACTION_PROMPT_VERSION=extract.v3
SKILL_VOCAB_VERSION=vocab.2026-08-20
SCORING_PROMPT_VERSION=score.v1
SKILL_TRIGRAM_THRESHOLD=0.62
SKILL_ADJACENCY_MIN=0.40
SCORING_BLEND_HARD=0.80
SCORING_PARTIAL_CREDIT=0.50
SCORING_TIER_WEIGHT_DREAM=1.10
SCORING_TIER_WEIGHT_STRONG=1.00
SCORING_TIER_WEIGHT_VOLUME=0.90
SCORING_HARD_GATE_PASS=0.60
SCORING_HARD_GATE_WARN=0.40
SCORING_HARD_GATE_WARN_FACTOR=0.85
SCORING_HARD_GATE_FAIL_FACTOR=0.65
SCORING_RECENCY_GRACE_DAYS=14
SCORING_RECENCY_HALF_LIFE_DAYS=45
SCORING_RECENCY_FLOOR=0.65
GENERATION_MIN_COMPOSITE=30.0
GENERATION_DAILY_CAP=10
RESCORE_MAX_AGE_DAYS=30

# --- generation ------------------------------------------------------
GENERATION_ENABLED=true
GENERATION_MIN_COVERAGE_PCT=45.0
GENERATION_VALIDATION_RETRIES=1
COVER_LETTER_ENABLED=true
COVER_LETTER_TARGET_WORDS=400
RESUME_MAX_PAGES=1
RENDER_VERIFY_PAGES=true
SOFFICE_BIN=/usr/bin/soffice
RENDER_TIMEOUT_S=60
ARTIFACT_DIR=/var/lib/scout/artifacts
SIMILARITY_WARN=0.55
SIMILARITY_BLOCK=0.72

# --- claims ledger ---------------------------------------------------
LEDGER_DEFAULT_TTL_DAYS=180
LEDGER_EXPIRED_CLAIM_POLICY=fail
LEDGER_APPROXIMATION_TOLERANCE=0.05
LEDGER_RESTRICTED_DISCLOSURE_COMPANIES=
LEDGER_VALIDATION_PROMPT_VERSION=validate.v2
LEDGER_EXPIRY_WARNING_DAYS=30

# --- feature flags ---------------------------------------------------
# Everything new on the generation path ships off.
FF_COVERAGE_JUDGEMENT=true
FF_TAILORING_REPHRASE=false
FF_GAP_IN_OPENING=false
FF_STREAMING_PREVIEW=false

# --- security --------------------------------------------------------
SESSION_SECRET=CHANGE_ME
APP_PASSWORD_HASH=CHANGE_ME
SESSION_TTL_DAYS=30
SESSION_COOKIE_NAME=scout_session
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_SAMESITE=lax
LOGIN_RATE_LIMIT_PER_MIN=5
CORS_ALLOW_ORIGINS=
API_RATE_LIMIT_PER_MIN=120

# --- logging ---------------------------------------------------------
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_REDACT_EXTRA_KEYS=
LOG_SQL_SLOW_MS=1000

# --- export, tracking and retention ----------------------------------
EXPORT_DIR=/var/lib/scout/exports
EXPORT_KEEP_LAST=30
GHOST_AFTER_DAYS=30
SUBMIT_CONFIRM_DAYS=3
MIN_N_FOR_RATE=15
MIN_N_FOR_COMPARISON=30
FOLLOWUP_MAX_PER_DAY=3
FOLLOWUP_MAX_PER_APP=2
FOLLOWUP_QUIET_DAYS_LIVE=7
FOLLOWUP_QUIET_DAYS_ACK=10
FOLLOWUP_QUIET_DAYS_SUBMITTED=14
RETENTION_POSTING_DAYS=90,180
RETENTION_MAIL_MONTHS=24
RETENTION_ARTIFACT_MONTHS=36
```

---

## 14. Boot validation

### 14.1 The principle

**Fail closed on anything touching data integrity or the compliance boundary;
fail open on enrichment** (`ARCHITECTURE.md` §8). Applied to configuration:

- a misconfiguration that could produce a **wrong or unsafe artifact, an
  unauthorised send, or a credential leak** refuses to start;
- a misconfiguration that only **degrades a capability** logs a warning at
  `WARN`, sets a degraded status on `/health`, and starts.

The distinction is not stylistic. A system that refuses to start is visible
immediately; a system that started with the ledger gate disabled is visible only
after the operator sends something.

### 14.2 Refuses to start

Raises `ConfigError` before the first request. `SCOUT_ENV=production` unless
noted.

| Condition | Message | Reason |
|---|---|---|
| Any unknown key in `.env` | `unknown setting: SCOREING_BLEND_HARD` | `extra="forbid"`. A typo that is silently ignored costs an afternoon |
| `DATABASE_URL` missing, or not `postgresql+asyncpg://` | `DATABASE_URL must use the asyncpg driver` | SQLAlchemy 2.0 async |
| `SESSION_SECRET` shorter than 32 bytes, or equal to the `.env.example` placeholder | `SESSION_SECRET is weak or is the example placeholder` | A development secret that reached production is not a warning-level event |
| `APP_PASSWORD_HASH` unset, or not a parseable Argon2 hash | `APP_PASSWORD_HASH must be an argon2id hash` | An unauthenticated deployment of a personal job-search database |
| `CORS_ALLOW_ORIGINS` contains `*` | `wildcard CORS is never permitted` | **All environments**, not just production |
| `SESSION_COOKIE_SECURE=false` | `insecure session cookie in production` | |
| `LLM_PROVIDER=bedrock` and no credential resolves — no instance role, no keys | `no AWS credentials resolvable for bedrock` | The system would start and fail every extraction silently at 08:00 |
| `LLM_PROVIDER=azure_openai` and endpoint, key or either deployment missing | `azure_openai selected but <field> is unset` | Same |
| `LLM_MODEL_FAST` or `LLM_MODEL_STRONG` matching `-latest`, `:latest` or an unpinned alias | `model IDs must be pinned` | Invariant 7 — an artifact whose model cannot be named |
| `RENDER_VERIFY_PAGES=false` | `page verification cannot be disabled in production` | The only thing between the operator and a two-page resume they did not intend to send |
| `MAIL_ENABLED=true` and `MAIL_OPERATOR_ADDRESS` unset or not a valid address | `MAIL_OPERATOR_ADDRESS required when MAIL_ENABLED` | `DIGEST_RECIPIENT` is resolved from it; the send assertion has nothing to compare against otherwise |
| `MAIL_ENABLED=true` and `MAIL_TOKEN_KEY` is not a valid Fernet key | `MAIL_TOKEN_KEY is not a valid Fernet key` | The token file would be written unreadably |
| `ARTIFACT_DIR` or `EXPORT_DIR` absent or not writable by the service user | `ARTIFACT_DIR is not writable` | Generation would fail per item, hours later, looking like a model problem |
| `DATABASE_ECHO=true` | `SQL echo is not permitted in production` | Statement parameters carry job-description text into logs |
| `LOG_LEVEL=DEBUG` | `DEBUG logging is not permitted in production` | Same class of leak |
| `RUN_LOCK_TTL_S` ≤ `RUN_WALL_CLOCK_BUDGET_S` | `run lock TTL must exceed the run budget` | The lock would expire mid-run and admit a second run |
| Prompt registry content hash mismatching `PROMPTS.lock` | `prompt <family>@<version> hash mismatch` | An edited prompt cannot ship without a version bump — which is what makes `artifact.prompt_version` meaningful |
| An `ats_type` enum member with no registered adapter class | `no adapter registered for <type>` | A boot failure, not a runtime 500 |
| Any numeric outside its declared bound (`SCORING_BLEND_HARD > 1.0`, `LLM_MAX_CONCURRENCY > 16`, …) | Pydantic's own message | Bounds are declared on the field, so there is one place to change them |

### 14.3 Starts, degraded

Logged at `WARN` and reflected in `GET /api/v1/health` — which returns 200 with a
per-dependency status map and never 500 for a degraded dependency, since a
degraded LLM provider should not take the UI down (`API.md` §7).

| Condition | Degradation | Health |
|---|---|---|
| Gmail token missing or `invalid_grant` | Mail ingestion and digest send stop. Digests render to `EXPORT_DIR`; the cursor is not advanced, so nothing is skipped | `gmail: "unauthenticated"` |
| `MAIL_ENABLED=false` | No mail features. Intentional in local | `gmail: "disabled"` |
| LLM provider unreachable at boot | Extraction, judgement and generation fail per item and retry next run; discovery, dedup, filtering and the UI are unaffected | `llm: "unreachable"` |
| `HEARTBEAT_URL` unset | No dead-man's switch | not reported |
| `SOURCE_USER_AGENT` left at the default with an unreplaced contact address | Outbound traffic is not attributable to a contactable operator | `WARN` at boot |
| `LEDGER_RESTRICTED_DISCLOSURE_COMPANIES` empty | No `restricted` claim may be emitted anywhere. **This is the safe default**, not a fault | not reported |
| `LLM_CACHE_ENABLED=false` | Higher token spend; correctness unaffected | `WARN` |
| `FF_COVERAGE_JUDGEMENT=false` | Deterministic-only coverage; understated, safe | not reported |
| `SCHEDULER_ENABLED=false` | Nothing runs on a schedule. Correct for `api`; a silent outage on `worker` | `WARN` naming the process |
| Redis unreachable at boot | Run locks and rate-limit buckets unavailable. **Discovery refuses to start a run** rather than running unlocked and unthrottled | `redis: "unreachable"` |

Redis is the one degraded dependency that still blocks work, and deliberately so:
running 320 sources with no token buckets and no run lock would be an unthrottled
burst against every upstream, which crosses the compliance boundary rather than
merely degrading a capability.

---

## 15. Per-environment overrides

### 15.1 The set that differs

Everything else is identical between `local` and `production` — which is the
point. A configuration surface where the two environments share almost every
value is one where local behaviour predicts production behaviour.

| Variable | local | production | Why |
|---|---|---|---|
| `SCOUT_ENV` | `local` | `production` | Selects the validation profile |
| `SCOUT_BASE_URL` | `http://localhost:8000` | `https://scout.example.com` | Digest deep links |
| `SCHEDULER_ENABLED` | `false` | `true` **on `worker` only** | §7 |
| `MAIL_ENABLED` | `false` | `true` | No accidental sends from a laptop |
| `DIGEST_ENABLED` | `false` | `true` | |
| `LLM_DAILY_BUDGET_INR` | `10` | `80` | A repair-retry loop against a mis-authored prompt is cheap |
| `LLM_MAX_CONCURRENCY` | `2` | `4` | Gentler on a shared laptop |
| `RENDER_VERIFY_PAGES` | `false` (only if LibreOffice is not installed) | `true` | Never off in the deployed image |
| `SESSION_COOKIE_SECURE` | `false` | `true` | No TLS on localhost |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173` | *(empty)* | Vite dev server is cross-origin; production is same-origin behind Caddy |
| `LOG_FORMAT` | `console` | `json` | Readability against machine-parseability |
| `LOG_LEVEL` | `DEBUG` | `INFO` | |
| `DATABASE_ECHO` | `true` when needed | `false` | |
| `ARTIFACT_DIR` / `EXPORT_DIR` / `MAIL_TOKEN_PATH` | `./var/…` under the checkout | `/var/lib/scout/…` | Local paths are gitignored |
| `SOURCE_CONCURRENCY` | `2` | `8` | Local development should not fan out across 320 sources |
| `DEFAULT_LOCATION_FILTER` | as production | as production | **Do not loosen locally.** A filter that behaves differently locally is a filter whose cost effect cannot be reasoned about |

### 15.2 How to override

Locally, edit `.env`. In production, the same `env_file` is shared by `api`,
`worker` and `migrate`, and the only per-service divergence goes in the Compose
`environment:` block — which is process environment and therefore wins over
`.env` (§1.3):

```yaml
api:
  env_file: [.env]
  environment:
    SCHEDULER_ENABLED: "false"      # the API never schedules

worker:
  env_file: [.env]
  environment:
    SCHEDULER_ENABLED: "true"       # exactly one process does
```

Two rules about how *not* to do it:

1. **No `.env.production` / `.env.staging` family.** One file per host. Multiple
   environment files that drift apart is how a production system ends up running
   with a development session secret, and the boot check in §14.2 exists because
   that failure is common enough to be worth a named error.
2. **No secret enters git, at any stage, in any environment.** `.env.example`
   carries `CHANGE_ME` placeholders and nothing else; boot refuses to start in
   production if a placeholder survives.

### 15.3 Verifying the effective configuration

```bash
# What the process actually resolved. Secrets print as **********.
docker compose exec -T worker python -m scout_careers.cli config show

# Diff against the documented defaults — the fastest way to find an
# accidental override.
docker compose exec -T worker python -m scout_careers.cli config diff
```

`GET /api/v1/settings` (`API.md` §7) exposes the same view to the UI, with the
same redaction. It exists because "no magic constants in modules" (§1) is only
useful if the resulting values are inspectable.

---

## 16. Related documents

| Document | Owns the meaning of |
|---|---|
| `ARCHITECTURE.md` | The no-magic-constants rule, the invariants, the scale envelope, the error policy |
| `AI_ARCHITECTURE.md` | `LLM_*`, `EXTRACTION_MAX_JD_TOKENS`, `LETTER_MAX_JD_TOKENS`, `FF_*`, the cost model and the budget breaker |
| `MATCH_SCORING.md` | `SCORING_*`, `SKILL_*`, `EXTRACTION_PROMPT_VERSION`, `RESCORE_MAX_AGE_DAYS`, `GENERATION_MIN_COMPOSITE`, `GENERATION_DAILY_CAP` |
| `DOCUMENT_GENERATION.md` | `GENERATION_*`, `COVER_LETTER_*`, `RESUME_MAX_PAGES`, `RENDER_VERIFY_PAGES`, `SIMILARITY_*` |
| `CLAIMS_LEDGER.md` | `LEDGER_*`, `GENERATION_VALIDATION_RETRIES` |
| `EMAIL_INGESTION.md` | `MAIL_*`, `DIGEST_*`, `GMAIL_*` |
| `SOURCE_ADAPTERS.md` | `SOURCE_*`, `RATE_LIMIT_WAIT_S`, `AUTO_DISABLE_THRESHOLD`, `MAX_DESCRIPTION_CHARS` |
| `APPLICATION_PIPELINE.md` | `GHOST_AFTER_DAYS`, `SUBMIT_CONFIRM_DAYS`, `MIN_N_*`, `FOLLOWUP_*`, `EXPORT_*`, `RETENTION_*` |
| `API.md` | `GET`/`PATCH /settings`, `/health`, the envelope, the deliberate absences |
| `INFRASTRUCTURE.md` | Where values are provisioned; Compose environment wiring; volumes |
| `DEPLOYMENT_ENV_RUNBOOK.md` | Secret provisioning, Gmail authorisation, deploy and rollback, troubleshooting |
| `SECURITY_ARCHITECTURE.md` | The session model, secret handling, the threat model |
