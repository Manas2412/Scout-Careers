# DEPLOYMENT & ENVIRONMENT RUNBOOK — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for environments, deploy and rollback procedure,
migration ordering, secret provisioning, the Gmail authorisation walkthrough,
smoke tests, the go-live checklist and operational troubleshooting.
`ARCHITECTURE.md` wins on invariants and module boundaries; `INFRASTRUCTURE.md`
wins on topology, sizing, volumes and backups; `CONFIGURATION.md` wins on
configuration keys, defaults and validation; `DATA_MODEL.md` wins on schema and
migration policy; `API.md` wins on endpoint contracts. Everything about
*how the thing gets deployed and what to do when it misbehaves* is decided here.

This document is written to be followed under pressure by one person, at 08:20,
when the digest did not arrive. Commands are complete and copy-pasteable. Where a
step is destructive it says so before the command, not after.

---

## 1. Environments

Two. There is no staging, and that is a decision rather than an omission: a
staging environment for a single-user tool doubles the operational surface to
rehearse a deploy that takes ninety seconds and rolls back in thirty. What
staging would have caught is caught instead by the offline test suite, the eval
gate (`AI_ARCHITECTURE.md` §10.3), and the smoke tests in §7.

| | **local** | **production** |
|---|---|---|
| Purpose | Development, migration authoring, eval runs | The system |
| Runs on | Operator's laptop | One VM (`INFRASTRUCTURE.md` §3) |
| `SCOUT_ENV` | `local` | `production` |
| Postgres | Container, throwaway | Container, backed up nightly |
| Redis | Container | Container |
| Scheduler | **Off** (`SCHEDULER_ENABLED=false`) — jobs triggered by hand | On, in the `worker` container only |
| LLM provider | Bedrock via personal credentials, or a recorded-fixture stub | Bedrock via instance role where available |
| LLM budget | `LLM_DAILY_BUDGET_INR=10` — a low ceiling so a runaway loop is cheap | `80` |
| Gmail | **Disabled** (`MAIL_ENABLED=false`) by default | Enabled, real token |
| Digest | Rendered to `exports/`, never sent | Sent at 08:15 IST |
| TLS | None. `http://localhost:5173` (Vite) → `http://localhost:8000` | Caddy, ACME, HSTS |
| Session cookie | `SESSION_COOKIE_SECURE=false` | `true`, `SameSite=Lax` |
| `RENDER_VERIFY_PAGES` | May be `false` to skip LibreOffice locally | **`true`. Never off in the deployed image** (`DOCUMENT_GENERATION.md` §13) |
| Frontend | `pnpm dev` with HMR, proxying `/api` | Built into the `web` image |
| Migrations | Authored and downgraded freely | Forward-only in normal operation |
| Seed data | Full mock registry | The real registry; seed script is idempotent |

### 1.1 The rules that differ, and why

**The scheduler is off locally.** APScheduler with a Postgres job store persists
jobs in the database. A developer laptop that acquires the production job
definitions and then runs them is one `DATABASE_URL` typo away from firing a real
discovery run against real sources. `SCHEDULER_ENABLED` defaults to `false`
(`CONFIGURATION.md` §5) so the safe state is the default.

**Gmail is off locally.** `MAIL_ENABLED=false` means no token is loaded, no
mailbox is read and no digest is sent. A local run that could send is a local run
that can violate invariant 2 by accident.

**The LLM budget is 10 rupees locally.** Not because development is cheap, but
because the failure mode being guarded is a repair-retry loop against a
mis-authored prompt, which `AI_ARCHITECTURE.md` §3.4 names explicitly as the
classic way to burn a daily budget in ninety seconds.

**Production never runs with `RENDER_VERIFY_PAGES=false`.** The page-count
verification loop is the only thing standing between the operator and a two-page
resume they did not intend to send (`DOCUMENT_GENERATION.md` §5.5). It is the one
setting in the whole surface that a boot-time assertion refuses in production
(`CONFIGURATION.md` §8).

---

## 2. First deploy

Assumes: a fresh VM matching `INFRASTRUCTURE.md` §3.5 (2 vCPU / 4 GB / 40 GB),
Debian 12 or Ubuntu 24.04, a domain, and SSH key access. Budget ninety minutes,
of which sixty are waiting.

### 2.1 Host preparation

```bash
# --- as root on the fresh VM ---
timedatectl set-timezone UTC          # the host is UTC; the app schedules in IST
hostnamectl set-hostname scout-prod

apt-get update && apt-get -y upgrade
apt-get -y install ca-certificates curl git jq ufw restic unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades

# 2 GB swap — INFRASTRUCTURE.md §3.3
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' >> /etc/sysctl.d/99-scout.conf

# Firewall. Docker bypasses ufw for published ports; the real defence is that
# postgres and redis publish none (INFRASTRUCTURE.md §6.1).
ufw default deny incoming && ufw default allow outgoing
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable

# SSH hardening
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/'  /etc/ssh/sshd_config
systemctl reload ssh

# Docker Engine + Compose v2
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get -y install docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker compose version    # expect v2.x
```

### 2.2 DNS

Point `A` (and `AAAA` if the VM has IPv6) for `SCOUT_DOMAIN` at the VM. Verify
**before** starting Caddy — a failed ACME challenge on first boot burns a retry
window and, repeated, a rate limit (`INFRASTRUCTURE.md` §7.3).

```bash
dig +short scout.example.com A
curl -sS ifconfig.me           # must match
```

### 2.3 Layout and checkout

```bash
mkdir -p /srv/scout /var/lib/scout/{artifacts,exports} /var/backups/scout/db
git clone <repo-url> /srv/scout && cd /srv/scout

# The bind mount is owned by the image's non-root user (INFRASTRUCTURE.md §4)
chown -R 10001:10001 /var/lib/scout
chmod 700 /var/lib/scout /var/lib/scout/artifacts /var/lib/scout/exports
chmod 700 /var/backups/scout
```

### 2.4 Secrets and `.env`

Full provisioning detail is §5. Minimum to get to a running system:

```bash
cd /srv/scout
cp .env.example .env
chmod 600 .env && chown root:root .env

python3 - <<'PY' >> /srv/scout/.env.generated
import secrets
print("SESSION_SECRET=" + secrets.token_urlsafe(48))
print("POSTGRES_PASSWORD=" + secrets.token_urlsafe(32))
PY
# MAIL_TOKEN_KEY must be a Fernet key, not arbitrary bytes (EMAIL_INGESTION.md §2.5)
python3 -c "from cryptography.fernet import Fernet;print('MAIL_TOKEN_KEY='+Fernet.generate_key().decode())" \
  >> /srv/scout/.env.generated

cat /srv/scout/.env.generated >> /srv/scout/.env && shred -u /srv/scout/.env.generated
```

Then edit `.env` by hand for `SCOUT_DOMAIN`, `ACME_EMAIL`,
`MAIL_OPERATOR_ADDRESS`, `SCOUT_BASE_URL`, `SOURCE_USER_AGENT`, the LLM model
pins and the app password hash (§5.5).

**Copy `SESSION_SECRET`, `POSTGRES_PASSWORD`, `MAIL_TOKEN_KEY`,
`APP_PASSWORD_HASH` and `RESTIC_PASSWORD` into the offline password manager
now.** `INFRASTRUCTURE.md` §9.3 explains what losing `MAIL_TOKEN_KEY` costs. Do
not defer this to after go-live.

### 2.5 Build, migrate, seed

**Order matters and is the same order as every subsequent deploy (§3):
schema first, then traffic.**

```bash
cd /srv/scout
docker compose build                              # ~6 min; LibreOffice layer dominates

docker compose up -d postgres redis
docker compose exec -T postgres pg_isready -U scout -d scout

# Extensions the schema depends on (DATA_MODEL.md §3.1)
docker compose exec -T postgres psql -U scout -d scout \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

docker compose run --rm migrate                   # alembic upgrade head
docker compose run --rm api alembic current       # expect: <rev> (head)

# Seed: six resume variants, initial claims ledger, never-scrape list.
# Idempotent by design (DATA_MODEL.md §11) — safe to re-run.
docker compose run --rm api python -m scout_careers.db.seed --confirm
```

### 2.6 Start

```bash
docker compose up -d api worker web
docker compose ps                                  # all healthy
docker compose logs -f web | grep -i "certificate obtained"
```

Give ACME up to two minutes. Then §7's smoke tests, then §6 to authorise Gmail,
then §7 again.

### 2.7 Host cron

```bash
crontab -e
```

```cron
# Times are UTC. IST = UTC + 5:30.
30 21 * * *   /srv/scout/ops/backup.sh      >>/var/log/scout-backup.log 2>&1   # 03:00 IST
0  *  * * *   /srv/scout/ops/disk-check.sh  >>/var/log/scout-disk.log   2>&1
0  20 * * 0   docker image prune -af --filter "until=336h"                     # 01:30 IST Mon
```

Verify the backup path end to end before trusting it:

```bash
/srv/scout/ops/backup.sh && ls -lh /var/backups/scout/db/
```

---

## 3. Routine deploy

Ninety seconds of downtime for the API, none for the database. Run it from a
clean working tree on a tagged commit, never from an uncommitted local edit.

### 3.1 The ordering rule

> **Migrations run to completion before the new image serves traffic. Always.
> And every migration must be readable by the image that is still running.**

Both halves matter, and the second is the one that gets forgotten.

Between `alembic upgrade head` and the container swap there is a window —
seconds, but real — in which **the old code is talking to the new schema**. A
migration that drops a column the running image still selects takes the site
down during that window. SQLAlchemy selects every mapped column, so one dropped
column takes out every query against that model, which is exactly the failure
mode that looks like "the app broke for no reason".

Therefore schema changes are **expand / migrate / contract**, across two deploys:

| Deploy | Migration | Code |
|---|---|---|
| N | **Expand** — add the column nullable, add the table, add the index `CONCURRENTLY`, add the enum value | Writes both old and new; reads old |
| N | *(same deploy, after swap)* | — |
| N+1 | — | Reads new; stops writing old |
| N+2 | **Contract** — drop the old column, add the `NOT NULL`, drop the old index | — |

A single-deploy destructive change is permitted only when the operator accepts
the outage and stops the API first (§3.4). For a single-user tool that is often
the right trade; it just has to be a decision rather than a surprise.

### 3.2 Procedure

```bash
set -Eeuo pipefail
cd /srv/scout

# --- 1. Know what you are on, so rollback is a fact and not a memory.
git rev-parse --short HEAD          > /tmp/scout.prev.sha
docker compose run --rm api alembic current | tee /tmp/scout.prev.rev

# --- 2. Take a pre-deploy dump whenever the deploy carries a migration.
git fetch --all --tags
if ! git diff --quiet HEAD origin/main -- backend/migrations/; then
  /srv/scout/ops/backup.sh
fi

# --- 3. Update the tree.
git checkout "$TAG"                 # e.g. 2026.09.12-1. Never deploy from a branch.

# --- 4. Build. Nothing is swapped yet; the running system is untouched.
export SCOUT_TAG="$TAG"
docker compose build api web

# --- 5. Migrate. BEFORE the new image serves traffic.
docker compose run --rm migrate
docker compose run --rm api alembic current

# --- 6. Swap. api first, so a failed start is caught before the worker
#        inherits a broken image.
docker compose up -d --no-deps api
docker compose ps api | grep -q healthy || { echo "api unhealthy"; exit 1; }

docker compose up -d --no-deps worker web

# --- 7. Verify.
/srv/scout/ops/smoke.sh             # §7
```

### 3.3 Migration authoring rules

Restating `DATA_MODEL.md` §11 with the deploy-time consequences attached:

| Rule | Deploy consequence |
|---|---|
| One Alembic revision per schema change, ID ≤ 32 characters | A revision that does two things cannot be half-rolled-back |
| Revisions are forward-only in normal operation; downgrades are written but exercised only in development | §4.2 |
| Enum additions use `ALTER TYPE … ADD VALUE` **in a standalone revision** | It cannot run in a transaction block alongside other DDL, and **it cannot be reversed** — §4.2 |
| Any migration touching `claim` or `artifact` preserves `claim_usage` integrity | Verified by the query in `INFRASTRUCTURE.md` §9.4 step 3 |
| Seed data ships as an idempotent script, not as a migration | Re-runnable; not entangled with schema history |
| Index creation on a populated table uses `CREATE INDEX CONCURRENTLY` in a revision with `autocommit_block()` | A plain `CREATE INDEX` takes an `ACCESS EXCLUSIVE`-adjacent lock and stalls the API for the duration |
| A new column on a large table is added nullable, backfilled in batches, then constrained in a later revision | A `NOT NULL DEFAULT` on `job_posting` rewrites 50k rows under lock |

### 3.4 Deploying a destructive migration deliberately

```bash
cd /srv/scout
/srv/scout/ops/backup.sh                  # not optional
docker compose stop api worker            # accept the outage; be explicit about it
docker compose run --rm migrate
docker compose up -d api worker
/srv/scout/ops/smoke.sh
```

Announce it to yourself in the commit message. A destructive revision that was
deployed as though it were additive is the single most likely cause of an
unplanned restore.

---

## 4. Rollback

### 4.1 Code rollback — the normal case

```bash
cd /srv/scout
export SCOUT_TAG="$(cat /tmp/scout.prev.sha)"
git checkout "$SCOUT_TAG"
docker compose up -d --no-deps api worker web
/srv/scout/ops/smoke.sh
```

If the previous image is still in the local store — it is, unless `docker image
prune` ran in between — this is a container restart and takes about twenty
seconds. **This is the rollback you want in almost every case**, and it works
without touching the database precisely because §3.1's expand/contract discipline
keeps the old code compatible with the new schema.

### 4.2 Migration rollback

**Default position: do not.** Roll the code back, leave the schema forward, and
fix forward with a new revision. An additive migration is harmless to leave in
place; a downgrade path is code that has been written but, by policy
(`DATA_MODEL.md` §11), never exercised outside development.

| Revision type | Downgrade | Rule |
|---|---|---|
| Additive (new nullable column, new table, new index) | Safe, and unnecessary | **Leave it.** Roll back the code only |
| Backfill / data migration | Usually irreversible in practice | **Never downgrade.** Fix forward |
| `ALTER TYPE … ADD VALUE` | **Impossible.** Postgres cannot remove an enum value | **Never attempt.** The value is permanent; leave it unused |
| Destructive (drop column, drop table, tighten constraint) | Structurally reversible, but the data is gone | **Restore from the pre-deploy dump** (`INFRASTRUCTURE.md` §9.4). A downgrade recreates the column empty, which is worse than an honest restore because it looks like it worked |

When a downgrade genuinely is the answer — an additive revision that also
introduced a broken trigger, say:

```bash
docker compose stop api worker
docker compose run --rm api alembic current           # confirm where you are
docker compose run --rm api alembic downgrade -1      # exactly one step
docker compose run --rm api alembic current
docker compose up -d api worker
```

One step. Never `alembic downgrade base`, never a multi-step downgrade in
production. If more than one revision has to come off, the situation is a restore
(`INFRASTRUCTURE.md` §9.4), not a downgrade.

### 4.3 Configuration and flag rollback

Not every rollback is a deploy. Feature flags and provider selection are
settings, and `AI_ARCHITECTURE.md` §12 requires every flagged path's off-state to
be a complete, correct behaviour rather than a degraded one.

```bash
cd /srv/scout
sed -i 's/^FF_TAILORING_REPHRASE=.*/FF_TAILORING_REPHRASE=false/' .env
docker compose up -d --no-deps --force-recreate api worker
```

Thirty seconds, no build, no migration. Prefer this over a code rollback whenever
the regression is behind a flag — which, by the rollout discipline in
`AI_ARCHITECTURE.md` §12, everything new on the generation path is.

### 4.4 What cannot be rolled back

| Action | Why | Mitigation |
|---|---|---|
| Sent digest | Mail is delivered | There is only one recipient — the operator |
| Spent tokens | The provider billed them | Budget circuit breaker (`AI_ARCHITECTURE.md` §3.4) |
| An `ALTER TYPE … ADD VALUE` | Postgres limitation | Leave the value unused |
| A pruned `job_posting` row | `PRUNE_CRON` deleted it | Restore from backup, or re-discover |
| An enum value already written to rows | — | Fix forward with a data migration |

---

## 5. Secret provisioning

Six secrets. Every one of them comes from the environment or a secret store,
never from code, and never appears in a log line at any level
(`ARCHITECTURE.md` §3, invariant 6).

| Secret | Variable(s) | Source | Rotation |
|---|---|---|---|
| Database credential | `POSTGRES_PASSWORD`, embedded in `DATABASE_URL` | Generated at first deploy | On host compromise |
| Session signing key | `SESSION_SECRET` | Generated, ≥ 32 bytes | Annually; invalidates the session |
| App password | `APP_PASSWORD_HASH` | Argon2id hash, generated locally | On suspicion |
| LLM provider | Bedrock: instance role, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. Azure: `AZURE_OPENAI_API_KEY` | AWS IAM / Azure portal | 90 days for static keys; never needed with an instance role |
| Gmail OAuth | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, plus the token file at `MAIL_TOKEN_PATH` | Google Cloud console + §6 | On revocation |
| Token encryption key | `MAIL_TOKEN_KEY` (Fernet) | Generated at first deploy | Rotating it requires re-running §6 |

### 5.1 Bedrock (default provider)

**Preferred — no long-lived credential on the host.** If the VM is EC2, attach an
instance profile. `boto3` and therefore `BedrockClient` pick it up with no code
change (`AI_ARCHITECTURE.md` §13).

```jsonc
// IAM policy on the instance role. Two actions, pinned models, one region.
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:ap-south-1::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
      "arn:aws:bedrock:ap-south-1::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0"
    ]
  }]
}
```

Pinning the model ARNs is not tidiness. `AI_ARCHITECTURE.md` §4.1 forbids
`-latest` aliases because a silently upgraded model invalidates every eval result
and every `artifact.model` provenance record. An IAM policy that only permits the
pinned IDs makes an accidental unpinned call fail loudly rather than succeed
quietly.

Off EC2, fall back to a dedicated IAM user with the same policy and no other
permissions:

```dotenv
AWS_REGION=ap-south-1
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

Rotate every 90 days. `docker compose up -d --force-recreate api worker` after
editing `.env`.

Verify without spending real tokens:

```bash
docker compose exec -T api python -c "
import asyncio
from scout_careers.llm import build_client
from scout_careers.common.config import settings
print(asyncio.run(build_client(settings).healthcheck()))"
```

### 5.2 Azure OpenAI (alternate provider)

```dotenv
LLM_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_DEPLOYMENT_FAST=gpt-4o-mini
AZURE_OPENAI_DEPLOYMENT_STRONG=gpt-4o
```

Provisioned but not default. Switching providers is a settings change and a
restart; existing artifacts keep their recorded `model` and `prompt_version`, so
provenance survives (`AI_ARCHITECTURE.md` §12). A provider switch is gated on the
eval suite exactly like a prompt change — **run the eval before switching, not
after**, unless the switch is an outage response (§8.3).

### 5.3 Gmail client credentials

From the Google Cloud console (§6.1). The "secret" on a Desktop OAuth client is
not a secret in the cryptographic sense — `EMAIL_INGESTION.md` §2.2 says so
plainly, and PKCE is what actually binds the code to the session — but it is
still kept out of git and out of logs, because it identifies the project and
there is no benefit to leaking it.

```dotenv
GMAIL_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=GOCSPX-...
MAIL_TOKEN_PATH=/var/lib/scout/gmail.token
MAIL_TOKEN_KEY=<Fernet key, base64, 44 chars>
```

`MAIL_TOKEN_KEY` and the token file are **never stored together**
(`EMAIL_INGESTION.md` §2.5). The key is in `.env` (root-owned, `0600`); the token
is at `MAIL_TOKEN_PATH` (owned by UID 10001, `0600`). Both are in `.gitignore`
and `.dockerignore`.

### 5.4 Database URL

Assembled in Compose from `POSTGRES_PASSWORD` rather than written out, so the
password exists in exactly one place in `.env`:

```
DATABASE_URL=postgresql+asyncpg://scout:${POSTGRES_PASSWORD}@postgres:5432/scout
```

The `+asyncpg` driver marker is required — SQLAlchemy 2.0 async
(`ARCHITECTURE.md` §4). A plain `postgresql://` URL fails at boot with a driver
error, which `CONFIGURATION.md` §8 lists among the misconfigurations that refuse
to start rather than degrade.

### 5.5 App session password

Single-user, single-session, a long-lived local cookie issued against a password
from the environment (`API.md` §1). The plaintext password is **never** in the
environment; its Argon2id hash is.

```bash
# Generate on the laptop, not on the VM — the plaintext never reaches the host.
python3 - <<'PY'
import getpass
from argon2 import PasswordHasher
print(PasswordHasher().hash(getpass.getpass("password: ")))
PY
```

```dotenv
APP_PASSWORD_HASH=$argon2id$v=19$m=65536,t=3,p=4$...
SESSION_SECRET=<48 random bytes, urlsafe base64>
SESSION_TTL_DAYS=30
SESSION_COOKIE_SECURE=true
```

Boot refuses to start in production if `SESSION_SECRET` is shorter than 32 bytes
or equals the `.env.example` placeholder (`CONFIGURATION.md` §8). A development
secret that reached production is not a warning-level event.

### 5.6 Verifying that nothing leaked

Run after every deploy that touched `.env`:

```bash
# No secret material in logs, at any level.
docker compose logs --since 24h \
  | grep -Ei 'AKIA|GOCSPX|refresh_token|Bearer |password=|SESSION_SECRET|MAIL_TOKEN_KEY' \
  && echo "LEAK — investigate immediately" || echo "clean"

# No secret material in the image.
docker run --rm --entrypoint sh scout-careers/backend:$SCOUT_TAG \
  -c 'ls -la /app; test ! -f /app/.env && echo "no .env in image"'

# File modes.
stat -c '%a %U:%G %n' /srv/scout/.env /var/lib/scout/gmail.token
# expect: 600 root:root /srv/scout/.env
#         600 <uid 10001> /var/lib/scout/gmail.token
```

---

## 6. Gmail OAuth — first-time authorisation

Once per Google account, and again only after a revocation. Twenty minutes,
of which fifteen are the Google Cloud console.

### 6.1 Google Cloud project

1. Create a project — `scout-careers`.
2. **APIs & Services → Library → Gmail API → Enable.**
3. **OAuth consent screen:**
   - User type: **External**. (Internal requires a Workspace domain.)
   - App name, support email, developer email: the operator's own.
   - **Scopes — add exactly two, and no others**
     (`EMAIL_INGESTION.md` §2.3):
     - `https://www.googleapis.com/auth/gmail.readonly`
     - `https://www.googleapis.com/auth/gmail.send`
   - Test users: add the operator's address.
   - **Publishing status: change from Testing to "In production".**
4. **Credentials → Create credentials → OAuth client ID → Application type:
   Desktop app.** Note the client ID and secret into `.env` (§5.3).

> **Step 3's last item is the one that breaks the system a week later if it is
> skipped.** Both scopes are *restricted*. While publishing status is
> **Testing**, Google issues refresh tokens that **expire after 7 days**, and the
> mail poller stops every Monday with `invalid_grant`. Publishing to **In
> production** without Google verification removes the 7-day expiry, at the cost
> of a one-time "unverified app" interstitial and a 100-user cap. One user is
> needed. `EMAIL_INGESTION.md` §2.3 records this decision and its consequences.
>
> Verification is not pursued, because verification of restricted scopes requires
> a CASA security assessment for *distribution*, and this app is never
> distributed.

`gmail.modify` is deliberately **not** requested. Do not add it for convenience —
`EMAIL_INGESTION.md` §2.4 gives four reasons, of which the operative one is that
read-only makes a whole class of "the loop mutated my real mail" bug
structurally impossible.

### 6.2 Running the flow on a headless VM

The flow uses a **loopback redirect** on `127.0.0.1` (Google deprecated the
out-of-band flow). The VM has no browser, so forward the loopback port over SSH
and complete the consent in the laptop's browser.

```bash
# --- Terminal 1, on the laptop. Bind the same ephemeral port on both ends.
ssh -L 8765:127.0.0.1:8765 scout-prod

# --- Inside that SSH session, on the VM:
cd /srv/scout
docker compose run --rm --service-ports -p 127.0.0.1:8765:8765 api \
  python -m scout_careers.cli auth gmail --port 8765
```

The command prints a consent URL. Open it in the laptop's browser:

```
Open this URL to authorise Scout Careers:

  https://accounts.google.com/o/oauth2/v2/auth?client_id=…&code_challenge=…
      &redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2F&access_type=offline
      &prompt=consent&scope=…gmail.readonly%20…gmail.send

Waiting for the authorisation code on 127.0.0.1:8765 …
```

1. Sign in as the operator.
2. **"Google hasn't verified this app"** → *Advanced* → *Go to scout-careers
   (unsafe)*. Expected, once, per §6.1.
3. Consent to both scopes. Grant **both** — declining `gmail.send` leaves the
   digest permanently undeliverable and the failure appears days later as "the
   digest did not arrive" (§8.7).
4. The browser redirects to `127.0.0.1:8765`, which the SSH tunnel carries to the
   container. It serves one response and shuts the listener down.

```
Authorisation code received.
Exchanging code (PKCE S256) …
Refresh token obtained.
Token written to /var/lib/scout/gmail.token (mode 0600, Fernet-encrypted).
Scopes granted: gmail.readonly, gmail.send
Operator address: manas@example.com

Verifying:
  profile read   ok  (messagesTotal=41,203)
  send capability ok  (dry run, recipient assertion passed)
```

`access_type=offline` and `prompt=consent` are on the first authorisation because
Google returns a refresh token only on the first grant unless consent is
re-forced (`EMAIL_INGESTION.md` §2.2).

### 6.3 Verify

```bash
curl -s https://$SCOUT_DOMAIN/api/v1/health | jq '.data.gmail'
# "ok"

stat -c '%a %U' /var/lib/scout/gmail.token       # 600, uid 10001

docker compose exec -T api curl -s localhost:8000/api/v1/runs/mail -X POST | jq
docker compose exec -T api python -m scout_careers.cli digest --dry-run
# renders to exports/digest-YYYY-MM-DD.html and sends nothing
```

### 6.4 Re-authorisation

Identical to §6.2. Trigger it whenever `/health` reports
`gmail: "unauthenticated"` — the response to `400 invalid_grant`, which is
permanent and is never retried (`EMAIL_INGESTION.md` §2.5). No data is lost: the
cursor is not advanced past what was committed, and digests are written to disk
while authorisation is broken.

Causes of `invalid_grant`, in order of likelihood: access revoked in the Google
account's security settings; the Google password changed; the token unused for
six months; the OAuth client deleted; publishing status reverted to Testing and
the 7-day clock resumed.

---

## 7. Smoke tests

`ops/smoke.sh`, run after every deploy. Fails loudly, exits non-zero, and checks
one invariant as well as liveness — because a deploy that silently disabled the
ledger gate would otherwise look exactly like a good deploy.

```bash
#!/usr/bin/env bash
# /srv/scout/ops/smoke.sh
set -Eeuo pipefail
BASE="https://${SCOUT_DOMAIN}"
COOKIE=$(mktemp); trap 'rm -f "$COOKIE"' EXIT
ok(){ printf '  ok   %s\n' "$1"; }
die(){ printf '  FAIL %s\n' "$1"; exit 1; }

# 1. TLS and the SPA.
curl -fsSI "$BASE/healthz" | grep -qi 'strict-transport-security' || die "HSTS"
ok "TLS + HSTS"

# 2. Health: 200 with every dependency reporting.
H=$(curl -fsS "$BASE/api/v1/health")
for dep in postgres redis llm gmail; do
  echo "$H" | jq -e ".data.$dep" >/dev/null || die "health.$dep missing"
done
echo "$H" | jq -e '.data.postgres=="ok" and .data.redis=="ok"' >/dev/null \
  || die "postgres/redis degraded"
ok "health: $(echo "$H" | jq -c .data)"

# 3. Schema is at head.
docker compose run --rm api alembic current 2>/dev/null | grep -q '(head)' \
  || die "alembic not at head"
ok "schema at head"

# 4. Auth works and issues a session.
curl -fsS -c "$COOKIE" -X POST "$BASE/api/v1/auth/login" \
  -H 'content-type: application/json' \
  -d "{\"password\":\"${SMOKE_PASSWORD}\"}" >/dev/null || die "login"
ok "login"

# 5. Reads return the envelope shape (API.md §1).
curl -fsS -b "$COOKIE" "$BASE/api/v1/companies?limit=1" \
  | jq -e 'has("data") and has("message")' >/dev/null || die "envelope"
ok "companies read"

# 6. A source probe reaches the outside world.
SID=$(curl -fsS -b "$COOKIE" "$BASE/api/v1/companies?limit=1" | jq -r '.data[0].sources[0].id')
curl -fsS -b "$COOKIE" -X POST "$BASE/api/v1/sources/$SID/test" \
  | jq -e '.data.reachable == true' >/dev/null || die "source probe"
ok "egress + adapter"

# 7. INVARIANT: the ledger gate rejects an uncited number (ARCHITECTURE.md §3.3).
curl -fsS -b "$COOKIE" -X POST "$BASE/api/v1/claims/validate" \
  -H 'content-type: application/json' \
  -d '{"text":"delivered 4,721 widgets across 96 sites"}' \
  | jq -e '.data.passed == false' >/dev/null \
  || die "LEDGER GATE PASSED AN UNCITED NUMBER — do not use this deploy"
ok "ledger gate rejects uncited numeric"

# 8. INVARIANT: no submit endpoint exists (API.md §8).
for p in /api/v1/applications/submit /api/v1/review/1/submit; do
  [ "$(curl -s -o /dev/null -w '%{http_code}' -b "$COOKIE" -X POST "$BASE$p")" = "404" ] \
    || die "a submit endpoint answered at $p"
done
ok "no submit endpoint"

# 9. Rendering: LibreOffice present and a variant fits one page.
docker compose exec -T api python -m scout_careers.cli render-variant \
  --key backend --verify-pages | grep -q 'pages=1' || die "render/page verify"
ok "render + page verification"

# 10. Scheduler holds the seven jobs, in exactly one process.
docker compose exec -T worker python -m scout_careers.scheduler --list \
  | grep -c '^job ' | grep -qx '7' || die "scheduler job count"
[ "$(docker compose ps -q worker | wc -l)" = "1" ] || die "more than one worker"
ok "scheduler: 7 jobs, 1 worker"

echo "smoke: PASS"
```

Tests 7 and 8 are the ones worth having. They assert two of the invariants from
`ARCHITECTURE.md` §3 against the deployed system rather than against a unit-test
double, and they are cheap enough to run on every deploy forever.

---

## 8. Go-live checklist

Every box is verifiable by a command. Do not tick from memory.

**Host**

- [ ] VM matches `INFRASTRUCTURE.md` §3.5 — 2 vCPU, 4 GB, 40 GB, 2 GB swap (`free -h`, `df -h`)
- [ ] Host timezone is UTC (`timedatectl`)
- [ ] `ufw` allows 22/80/443 only (`ufw status`)
- [ ] SSH: key auth only, root login prohibited (`sshd -T | grep -E 'passwordauth|permitroot'`)
- [ ] `unattended-upgrades` enabled
- [ ] Docker Engine + Compose v2 installed (`docker compose version`)

**DNS and TLS**

- [ ] `A`/`AAAA` for `SCOUT_DOMAIN` resolve to this host (`dig +short`)
- [ ] Certificate issued by Let's Encrypt, > 60 days remaining (`openssl s_client`)
- [ ] HTTP redirects to HTTPS (`curl -sI http://$SCOUT_DOMAIN`)
- [ ] HSTS, CSP, `X-Frame-Options: DENY` present (`curl -sI https://$SCOUT_DOMAIN`)

**Secrets**

- [ ] `.env` is `0600 root:root` (`stat -c '%a %U'`)
- [ ] `SESSION_SECRET` ≥ 32 bytes and is not the `.env.example` placeholder
- [ ] `APP_PASSWORD_HASH` is an Argon2id hash; plaintext exists nowhere on the host
- [ ] `MAIL_TOKEN_KEY` is a valid Fernet key
- [ ] All six secrets copied to the offline password manager (`INFRASTRUCTURE.md` §9.3)
- [ ] LLM credentials: instance role attached, **or** IAM user scoped to the two pinned model ARNs
- [ ] §5.6 leak scan returns clean

**Data**

- [ ] `pg_trgm` extension created (`\dx`)
- [ ] `alembic current` reports `(head)`
- [ ] Seed applied: six resume variants active, claims ledger non-empty, never-scrape list loaded
- [ ] Company registry imported and each source probed green (`/api/v1/sources/{id}/test`)

**Runtime**

- [ ] `docker compose ps` — all services healthy
- [ ] Exactly **one** `worker` container (`docker compose ps -q worker | wc -l` = 1)
- [ ] `api` has `SCHEDULER_ENABLED=false`
- [ ] `postgres` and `redis` publish no host ports (`docker compose ps` shows no mapping; `ss -ltnp` shows no 5432/6379)
- [ ] `/var/lib/scout` owned by UID 10001, mode 700

**Scheduler**

- [ ] Seven jobs registered: discovery, mail poll, digest, export, prune, rescore, heartbeat
- [ ] `SCHEDULER_TIMEZONE=Asia/Kolkata`; next-fire times read 08:00, 08:10, 08:15, 23:30 IST
- [ ] A manual `POST /api/v1/runs/discovery` with one `source_id` completes and writes a `run_log` row

**Mail**

- [ ] `/health` reports `gmail: "ok"`
- [ ] Token file `0600`, owned by UID 10001
- [ ] `MAIL_OPERATOR_ADDRESS` set and correct
- [ ] Alert alias receives at least one live job alert
- [ ] `digest --dry-run` renders to `exports/`
- [ ] One real digest received and read on a phone

**Invariants** (`ARCHITECTURE.md` §3)

- [ ] No submit endpoint answers (smoke test 8)
- [ ] Ledger gate rejects an uncited numeric (smoke test 7)
- [ ] `POST /companies/detect` on a `linkedin.com` URL returns **403 `source.denied_by_policy`**
- [ ] Digest send to a foreign recipient raises `OutboundPolicyViolation` (`test_outbound_policy.py` green in CI)
- [ ] `RENDER_VERIFY_PAGES=true`

**Operations**

- [ ] `ops/backup.sh` completed once and `pg_restore --list` parsed the dump
- [ ] restic repository initialised; one snapshot present offsite
- [ ] Cron entries installed and in **UTC** (`crontab -l`)
- [ ] `ops/disk-check.sh` runs and reports under 80%
- [ ] `HEARTBEAT_URL` configured; the external service has seen a ping
- [ ] Log rotation configured (`max-size: 20m`, `max-file: 5`)
- [ ] **A restore drill has been performed once on a scratch host** (`INFRASTRUCTURE.md` §9.5) and the wall-clock time recorded
- [ ] First-quarter drill scheduled in the operator's calendar

**Sign-off**

- [ ] `ops/smoke.sh` — PASS
- [ ] Deployed tag recorded, with its Alembic revision, in the deploy log

---

## 9. Troubleshooting

Each entry: **symptom** as observed, **diagnosis** as commands, **fix**.

### 9.1 The discovery run did not fire

**Symptom.** No 08:00 IST digest section for new roles; `GET /api/v1/runs` shows
no `discovery` row for today; the dashboard's last-run timestamp is yesterday.

**Diagnosis.**

```bash
# Is the worker alive at all?
docker compose ps worker
docker compose logs --since 24h worker | tail -50

# Does the scheduler hold the job, and when does it think it fires next?
docker compose exec -T worker python -m scout_careers.scheduler --list
# expect: job discovery  cron[hour=8,minute=0]  tz=Asia/Kolkata  next=…T08:00:00+05:30

# Timezone: the classic cause. The container must be IST-aware.
docker compose exec -T worker date
docker compose exec -T worker python -c \
  "from scout_careers.common.config import settings; print(settings.SCHEDULER_TIMEZONE, settings.SCHEDULER_ENABLED)"

# Is the job store holding a stale, paused or duplicated entry?
docker compose exec -T postgres psql -U scout -d scout \
  -c "SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time;"

# Did it fire and die immediately?
docker compose exec -T postgres psql -U scout -d scout \
  -c "SELECT id,status,started_at,finished_at,error FROM run_log
      WHERE run_type='discovery' ORDER BY started_at DESC LIMIT 5;"

# Is a stale lock refusing to let it start? (→ §9.8)
docker compose exec -T redis redis-cli --scan --pattern 'lock:*'
```

**Fix**, by cause:

| Cause | Fix |
|---|---|
| `worker` down or crash-looping | `docker compose up -d worker`; read the traceback in the logs first — usually a config validation failure after an `.env` edit |
| `SCHEDULER_ENABLED=false` on the worker | Correct `.env`, `docker compose up -d --force-recreate worker` |
| `SCHEDULER_TIMEZONE` wrong or unset | Set `Asia/Kolkata`. A UTC scheduler fires at 13:30 IST and looks like "it did not fire" for five and a half hours |
| Missed while the host was down; misfire grace elapsed | Trigger manually: `curl -X POST .../api/v1/runs/discovery`. Raise `SCHEDULER_MISFIRE_GRACE_S` if the host reboots often |
| Duplicate rows in `apscheduler_jobs` after a botched deploy | Stop the worker, `DELETE FROM apscheduler_jobs;`, restart — jobs re-register from code at boot |
| Stale Redis run lock | §9.8 |

**Recovery is always available:** `POST /api/v1/runs/discovery` re-runs the whole
thing. Discovery runs are idempotent by design (`ARCHITECTURE.md` §8) — posting
identity is `(source_id, external_id)`, change is detected by `content_hash`, and
re-running a completed stage is a no-op.

### 9.2 A source is failing repeatedly

**Symptom.** The digest's failures section names a source; the Settings health
table shows `consecutive_failures` climbing; at 5 the source is auto-disabled
with `last_status = 'auto_disabled'`.

**Diagnosis.**

```bash
docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT s.id, c.name, s.adapter, s.enabled, s.consecutive_failures,
         s.last_status, left(s.last_error,160) AS err, s.last_run_at
  FROM source s JOIN company c ON c.id=s.company_id
  WHERE s.consecutive_failures > 0 OR NOT s.enabled
  ORDER BY s.consecutive_failures DESC;"

# What did the runner see, per source, on the last run?
docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT jsonb_pretty(jsonb_path_query_first(source_results,'\$[*] ? (@.source_id == 77)'))
  FROM run_log WHERE run_type='discovery' ORDER BY started_at DESC LIMIT 1;"

# Re-probe live.
curl -fsS -b "$COOKIE" -X POST "https://$SCOUT_DOMAIN/api/v1/sources/77/test" | jq
```

**Fix**, by `last_error`:

| Error | Meaning | Fix |
|---|---|---|
| `HTTP 404` | Board token / tenant / site changed, or the board was removed | Re-detect: `POST /companies/detect` with the current careers URL; `PATCH /sources/{id}` with the new config. If the employer stopped using that ATS, disable the source |
| `HTTP 403` | **A policy signal, not a technical problem** | Retire the adapter for that source or disable it. **Do not change `SOURCE_USER_AGENT` to a browser string** — `SOURCE_ADAPTERS.md` §4.5 makes this rule explicit and it is the rule that stops the "just spoof Chrome" fix |
| `HTTP 429` persistently | The bucket rate is above what the host tolerates | Lower that adapter's bucket in `sources/http.py` and redeploy. Honour any `Retry-After` the host sent |
| `robots_disallow` | `robots.txt` now forbids the endpoint | Disable the source. Never bypassed (`SOURCE_ADAPTERS.md` §4.7) |
| `timeout` / `circuit_open` | Upstream slow or down | Usually transient. Note that `rate_limited` and `circuit_open` do **not** increment `consecutive_failures` — they are our own back-pressure |
| `AdapterConfigError` | Config rotted since it was saved | Fix `source.config` to match the adapter's Pydantic model (`SOURCE_ADAPTERS.md` §2.5) |
| `alert.layout_changed` | A mail-alert parser's structural fingerprint stopped matching | A parser fix is a code change through the normal gate. The affected messages are left unprocessed and reprocess after the fix — nothing is lost (`EMAIL_INGESTION.md` §4.7) |

Re-enable after fixing — this is deliberately a human act, because five
consecutive daily failures almost always means the board moved and needs new
config, not that the network was unlucky five times:

```bash
curl -fsS -b "$COOKIE" -X PATCH "https://$SCOUT_DOMAIN/api/v1/sources/77" \
  -H 'content-type: application/json' -d '{"enabled": true}'   # also resets the counter
curl -fsS -b "$COOKIE" -X POST "https://$SCOUT_DOMAIN/api/v1/runs/discovery" \
  -H 'content-type: application/json' -d '{"source_ids":[77]}'
```

### 9.3 LLM cost spiked

**Symptom.** The digest reports the budget breaker opened; `run_log.stats.llm_cost_inr`
is well above ~₹67; the Settings page's fourteen-day sparkline steps up.

**Diagnosis.**

```bash
docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT date_trunc('day',started_at) AS day,
         sum((stats->>'llm_cost_inr')::numeric)  AS inr,
         sum((stats->>'llm_calls')::int)         AS calls,
         sum((stats->>'repair_retries')::int)    AS repairs,
         sum((stats->>'cache_hits')::int)        AS cache_hits,
         sum((stats->>'extracted')::int)         AS extracted,
         sum((stats->>'generated')::int)         AS generated
  FROM run_log WHERE started_at > now()-interval '14 days'
  GROUP BY 1 ORDER BY 1 DESC;"

# Which family? Cost is 55% strong-model generation on 16% of calls by design.
docker compose logs --since 48h worker \
  | jq -rc 'select(.event=="llm.call") | [.family,.alias,.input_tokens,.output_tokens,.attempts,.cost_inr] | @tsv' \
  | awk -F'\t' '{c[$1]+=$6; n[$1]++; a[$1]+=$5} END {for(f in c) printf "%-24s %7.2f INR  %4d calls  %.2f avg attempts\n", f, c[f], n[f], a[f]/n[f]}'
```

**Fix**, by shape of the spike:

| Shape | Cause | Fix |
|---|---|---|
| `repairs` high, `attempts` > 1.3 | A schema-violation loop after a prompt or model change | Roll the prompt version back (`AI_ARCHITECTURE.md` §10.3). This is the failure `max_repair_retries` bounds; if it is not bounding it, the transport and repair counters have been conflated in code — a defect |
| `extracted` far above ~30 | Stage ④ is passing too much | Tighten the filter: `DEFAULT_LOCATION_FILTER`, seniority gates, keyword deny-list. The filter saves more per day than the entire budget (`AI_ARCHITECTURE.md` §8.3) — a filter regression is a cost regression |
| `generated` above `GENERATION_DAILY_CAP` | The cap is not being enforced | Defect. Verify the enqueue step at stage ⑩ applies `GENERATION_DAILY_CAP` |
| `cache_hits` collapsed to 0 | Prompt version or model ID changed, so the cache key changed | Expected for one run after a bump (`AI_ARCHITECTURE.md` §9). If it persists, check `LLM_CACHE_ENABLED` |
| Uniform rise across families | Provider price change, or `LLM_INR_PER_USD` stale | Update `LLM_PRICE_*` / `LLM_INR_PER_USD`. The meter reports actual usage; only the conversion is configured |
| Company count grew | Structural | A stricter filter, not a bigger budget (`AI_ARCHITECTURE.md` §8.4) |

Immediate containment while diagnosing — the breaker is designed for this, so
lowering the budget is a legitimate control action rather than a workaround:

```bash
sed -i 's/^LLM_DAILY_BUDGET_INR=.*/LLM_DAILY_BUDGET_INR=40/' /srv/scout/.env
docker compose up -d --force-recreate worker
```

### 9.4 Gmail token expired

**Symptom.** `GET /api/v1/health` reports `gmail: "unauthenticated"`; a persistent
dashboard banner; `run_log` has `run_type='mail'`, `status='failed'`,
`error='gmail_auth_invalid_grant'`; **no digest arrived**, but
`exports/digest-YYYY-MM-DD.html` exists.

**Diagnosis.**

```bash
curl -s "https://$SCOUT_DOMAIN/api/v1/health" | jq '.data.gmail'

docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT started_at, status, error FROM run_log
  WHERE run_type='mail' ORDER BY started_at DESC LIMIT 5;"

docker compose logs --since 24h worker | grep -i gmail_auth | tail -20
# Log lines carry only {"gmail_auth":"refreshed","expires_in_s":3599} —
# nothing about the token is ever logged (EMAIL_INGESTION.md §2.5).

stat -c '%a %U %y' /var/lib/scout/gmail.token
```

**Fix.** Re-run §6.2. That is the whole fix; `invalid_grant` is permanent and is
deliberately never retried, because retrying a permanent failure only burns
quota.

**Nothing is lost, and the reasons are worth knowing before panicking:**

- The Gmail cursor is **not** advanced on a failed run
  (`EMAIL_INGESTION.md` §3.1), so no message is skipped.
- Digests were written to `exports/` and shown in-app, so no day's output
  vanished (§2.5 of that document).
- Re-processing is a no-op — `email_message.gmail_id` is `UNIQUE` and the insert
  is `ON CONFLICT DO NOTHING`.

Afterwards, confirm the publishing status is still **In production** (§6.1). A
project reverted to Testing reintroduces the 7-day refresh-token expiry, and the
symptom recurs weekly.

### 9.5 Postgres disk full

**Symptom.** Writes fail; the API returns 500s on anything that writes; Postgres
logs `could not extend file … No space left on device`; possibly no `run_log` row
for the failure, because writing it also failed.

**Diagnosis.**

```bash
df -h / && df -i /                       # bytes and inodes both
du -sh /var/lib/docker/volumes/* /var/lib/scout/* /var/backups/scout/* 2>/dev/null | sort -h | tail
docker system df -v | head -40

docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)) AS total
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind='r'
  ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 12;"

# Two specific culprits worth checking by name.
docker compose exec -T postgres psql -U scout -d scout \
  -c "SELECT pg_size_pretty(sum(pg_column_size(source_results))) FROM run_log;"
docker compose exec -T postgres psql -U scout -d scout \
  -c "SELECT slot_name, active, restart_lsn FROM pg_replication_slots;"
```

**Fix**, cheapest and least destructive first:

```bash
# 1. Docker images and build cache. Almost always the largest single win —
#    a backend image carrying LibreOffice is ~1.1 GB (INFRASTRUCTURE.md §3.4).
docker image prune -af --filter "until=168h"
docker builder prune -af

# 2. Container logs, if rotation was not configured.
truncate -s 0 /var/lib/docker/containers/*/*-json.log

# 3. Old local dumps. Offsite copies in restic are unaffected.
find /var/backups/scout -type f -mtime +7 -delete

# 4. Run the retention sweep early rather than waiting for Sunday 04:00 IST.
docker compose exec -T api python -m scout_careers.ops prune --confirm

# 5. Reclaim heap from the pruned rows. Blocking; run when nothing else is.
docker compose exec -T postgres psql -U scout -d scout \
  -c "VACUUM (FULL, ANALYZE) job_posting;"
```

An inactive replication slot pins WAL forever and is the one cause that looks
like unbounded database growth with no rows to explain it. There should be none;
drop any found.

**Prevention.** §11.3 of `INFRASTRUCTURE.md` runs `disk-check.sh` hourly and
raises the alarm in the digest at 80%, which — against the growth rates in §3.4
of that document — is weeks of warning. If this entry was needed, the cron entry
was missing.

### 9.6 Artifacts missing

**Symptom.** A download from the review queue 404s or fails a checksum; the UI
shows an artifact row but the file is absent.

**Diagnosis.**

```bash
docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT id, kind, path, validation_status, generated_at
  FROM artifact ORDER BY generated_at DESC LIMIT 10;"

docker compose exec -T api ls -la /var/lib/scout/artifacts | tail
docker compose exec -T api stat /var/lib/scout/artifacts/<artifact_id>/…

# Checksum mismatch: the row is authoritative about what the bytes should be.
docker compose exec -T api sha256sum /var/lib/scout/artifacts/<path>
docker compose exec -T postgres psql -U scout -d scout \
  -c "SELECT checksum FROM artifact WHERE id='<artifact_id>';"

# Ownership after a restore — the classic cause.
ls -ln /var/lib/scout/artifacts | head
```

**Fix**, by cause:

| Cause | Fix |
|---|---|
| Bind mount not mounted (`ls` inside the container shows an empty directory that exists on the host) | Check `volumes:` in `docker-compose.yml`; `docker compose up -d --force-recreate api worker` |
| Ownership wrong after a restore | `chown -R 10001:10001 /var/lib/scout && chmod 700 /var/lib/scout/artifacts` |
| `validation_status = 'failed'` | **Working as designed.** A failed artifact is retained for diagnosis and can never attach to a `review_item` or `application` — enforced by a database trigger, not by application code (`DATA_MODEL.md` §8.1). Read `validation_notes`, fix the cause, regenerate. There is no override endpoint and there will not be one (`API.md` §8) |
| Row exists, file does not | The file was never written (render failed) or was removed out of band. Re-render: `POST /api/v1/variants/{id}/render` with the review item's `tailoring_plan` |
| Checksum mismatch | The file changed on disk. Do not serve it. Re-render and compare |
| `DoesNotFit` in the logs, no artifact at all | The plan promoted more than one page holds. **Not a fallback to two pages** — the item goes to `needs_manual_review` and the plan is the defect (`DOCUMENT_GENERATION.md` §5.5) |
| `soffice` missing or timing out | `docker compose exec api soffice --version`. If absent, the image was built without the LibreOffice layer — rebuild. If timing out, check host CPU contention |

Restoring artifacts alone from backup:

```bash
restic restore latest --target /restore --include '/var/backups/scout/scout-data-*'
tar -C /var/lib -xzf /restore/var/backups/scout/scout-data-<STAMP>.tar.gz
chown -R 10001:10001 /var/lib/scout
```

### 9.7 The digest did not arrive

**Symptom.** No 08:15 IST email. Everything else looks fine.

Work the causes in this order — the first two are far more likely than the last.

**Diagnosis.**

```bash
# 1. Was it composed at all?
ls -la /var/lib/scout/exports/digest-$(date +%F).html

# 2. Did the mail run execute?
docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT started_at, status, error, stats->>'digest_sent' AS sent
  FROM run_log WHERE run_type='mail' AND started_at > now()-interval '1 day'
  ORDER BY started_at DESC;"

# 3. Is Gmail authorised? (→ §9.4)
curl -s "https://$SCOUT_DOMAIN/api/v1/health" | jq '.data.gmail'

# 4. Is sending switched off?
docker compose exec -T worker python -c \
  "from scout_careers.common.config import settings
print(settings.MAIL_ENABLED, settings.DIGEST_ENABLED, settings.DIGEST_SEND_AT,
      settings.MAIL_OPERATOR_ADDRESS)"

# 5. Did it send and get filed elsewhere?
docker compose logs --since 24h worker | grep -E 'digest\.(composed|sent|failed)'
```

**Fix**, by cause:

| Cause | Signal | Fix |
|---|---|---|
| Digest rendered to disk, not sent | HTML file exists, `gmail: "unauthenticated"` | §9.4. This is the designed degradation: no day's output is lost |
| `DIGEST_ENABLED=false` | Setting | Set `true`, recreate the worker |
| Discovery run never fired, so the 08:10 mail run had nothing to follow | No `discovery` row | §9.1. Note the digest still sends — it always sends, including when there is nothing to report and when the run failed (`EMAIL_INGESTION.md` §11.1). If it did not send at all, the cause is elsewhere on this list |
| Sent, but filtered by Gmail | `digest.sent` logged, nothing in the inbox | Check Spam and Promotions. Add a filter: never send to spam, from the operator's own address. The digest carries no images, no tracking pixels and no remote assets, which helps |
| `OutboundPolicyViolation` raised | Exception in logs | **This is the invariant working.** Something attempted to send to an address other than the operator's. Do not "fix" it by relaxing the assertion — find the call site. `test_outbound_policy.py` should have caught it in CI |
| `gmail.send` scope not granted | Read works, send 403s | Re-run §6.2 and grant both scopes |
| Wrong `MAIL_OPERATOR_ADDRESS` | Sent successfully to an address nobody reads | Correct it and recreate the worker |

Send today's digest by hand once the cause is fixed:

```bash
docker compose exec -T api python -m scout_careers.cli digest --send --date "$(date +%F)"
```

### 9.8 Stuck run lock in Redis

**Symptom.** `POST /api/v1/runs/discovery` returns **409** with
`run.already_in_flight`, but no run is executing; the scheduled 08:00 run is
skipped for the same reason; `run_log` shows a `running` row that never finished.

Cause: the process holding the lock died between acquisition and release — an
OOM kill, a `docker compose kill`, a host reboot mid-run.

**Diagnosis.**

```bash
docker compose exec -T redis redis-cli --scan --pattern 'lock:*'
docker compose exec -T redis redis-cli GET  lock:run:discovery
docker compose exec -T redis redis-cli TTL  lock:run:discovery
# TTL -1  ⇒ no expiry set. That is a defect: every lock must carry a TTL.
# TTL >0  ⇒ it will clear itself; wait rather than intervene.

# Is anything actually running?
docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT id, run_type, status, started_at, now()-started_at AS age
  FROM run_log WHERE status='running' ORDER BY started_at DESC;"

docker compose exec -T postgres psql -U scout -d scout -c "
  SELECT pid, state, now()-query_start AS dur, left(query,80)
  FROM pg_stat_activity WHERE datname='scout' AND state<>'idle';"

docker compose top worker
```

**Fix.** Confirm nothing is running — a `run_log` row `running` for less than
fifteen minutes with active Postgres queries and a busy worker means the run is
alive and the answer is to wait; the run wall-clock budget is 15 minutes and is
enforced.

If it is genuinely orphaned:

```bash
# 1. Close the orphaned run_log row. Never leave it 'running' — the next run
#    would inherit an inconsistent view of what completed.
docker compose exec -T postgres psql -U scout -d scout -c "
  UPDATE run_log
     SET status='failed', finished_at=now(),
         error='orphaned: lock holder died, released manually'
   WHERE status='running' AND started_at < now() - interval '30 minutes';"

# 2. Release the lock.
docker compose exec -T redis redis-cli DEL lock:run:discovery

# 3. Re-run. Idempotent by design.
curl -fsS -b "$COOKIE" -X POST "https://$SCOUT_DOMAIN/api/v1/runs/discovery"
```

Never `redis-cli FLUSHALL`. It clears the rate-limit buckets and the `robots.txt`
cache alongside the lock, so the re-run starts with empty buckets and bursts
against every upstream at once — turning a stuck lock into a rate-limit incident
across 320 sources.

**If a lock has no TTL, that is the bug.** Every lock in the system must be
acquired with an expiry longer than the operation's ceiling and shorter than the
next scheduled fire: `lock:run:discovery` at 3,600 s against a 900-second run
budget, `lock:gmail:refresh` at 30 s (`EMAIL_INGESTION.md` §2.5). A lock without
a TTL converts one crash into permanent downtime, and the fix belongs in code,
not in this runbook.

---

## 10. Related documents

| Document | Covers |
|---|---|
| `INFRASTRUCTURE.md` | Topology, Compose file, sizing, volumes, TLS, backup, restore, DR, capacity |
| `CONFIGURATION.md` | Every environment variable, feature flags, tuning constants, boot validation |
| `ARCHITECTURE.md` | Invariants, pipeline stages, idempotency, error policy |
| `DATA_MODEL.md` | Schema, migration policy, retention |
| `API.md` | `/health`, `/runs/*`, `/claims/validate`, the deliberate absences |
| `EMAIL_INGESTION.md` | Gmail OAuth, scopes, token storage, the digest, failure handling |
| `AI_ARCHITECTURE.md` | Model routing, budget breaker, prompt versioning, flag rollout |
| `SOURCE_ADAPTERS.md` | Adapter failure semantics, rate limits, robots, circuit breakers |
| `DOCUMENT_GENERATION.md` | Rendering, page verification, artifact validation |
| `SECURITY_ARCHITECTURE.md` | Threat model, session model, secret handling |
