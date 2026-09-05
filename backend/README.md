# scout-careers (backend)

FastAPI + SQLAlchemy 2.0 async + Postgres backend for Scout Careers.

Project overview, invariants and the delivery plan live in the repository root
[`README.md`](../README.md); the full documentation set is in [`../docs/`](../docs/).

## Quick start

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# from the repository root
docker compose up -d postgres redis
cp .env.example .env          # fill DATABASE_URL, REDIS_URL, SOURCE_USER_AGENT

scout-careers db upgrade
scout-careers seed companies
scout-careers run discovery
scout-careers runs list
```

## The gate

```bash
bash ../ci/run-checks.sh all      # ruff, format, compile, import-linter, mypy, pytest
```

Everything must pass before a merge. Unit tests are offline — no network, no
database. Integration tests require a live Postgres and skip when `DATABASE_URL`
is unset:

```bash
pytest tests/integration -m integration
```

## Layout

```
src/scout_careers/
├── common/     config, types, errors, logging, clock, hashing, text, ids
├── db/         models, session, declarative base
├── sources/    SourceAdapter protocol, HTTP client, policy, normalisation,
│               and one module per ATS (greenhouse, lever, ashby, mail_alerts)
├── ingest/     run orchestration, persistence, dedup, the close rule
├── registry/   company and source CRUD, ATS auto-detection
└── cli/        the scout-careers command
```

`sources/` never imports `db/` — adapters return DTOs and `ingest/` persists
them. Both import contracts are enforced by import-linter in the gate.
