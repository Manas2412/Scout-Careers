"""The entire Phase 1 configuration surface.

One ``Settings`` object, built by ``pydantic-settings``, injected everywhere.
There is no second place a tunable may live (DEVELOPMENT.md §4.1). The narrow
exception is policy rather than configuration — ``NEVER_FETCH_HOSTS`` is a code
constant in ``sources/policy.py`` precisely so that invariant 4 has nowhere to
be switched off, and nothing here references it.

Field names are lower-case; ``pydantic-settings`` matches them
case-insensitively against ``SCREAMING_SNAKE_CASE`` environment keys, so
``source_user_agent`` is populated from ``SOURCE_USER_AGENT``.

``extra="forbid"`` is load-bearing: an unknown key in ``.env`` is a typo, and a
typo that is silently ignored costs an afternoon. It also means ``.env.example``
must list Phase 1 keys and nothing else.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from scout_careers.common.errors import ConfigError

#: Repository root, resolved from this file rather than the working directory:
#: ``backend/src/scout_careers/common/config.py`` → four parents up.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The unreplaced contact placeholder shipped in ``.env.example``. Outbound
#: traffic carrying it is not attributable to a contactable operator, which is
#: the whole point of the user-agent policy (SOURCE_ADAPTERS.md §4.5).
USER_AGENT_PLACEHOLDER = "<operator>"

DEFAULT_USER_AGENT = (
    f"ScoutCareers/1.0 (personal job-search agent; +mailto:{USER_AGENT_PLACEHOLDER})"
)


class Settings(BaseSettings):
    """Phase 1 configuration. Frozen after construction; never mutated at runtime."""

    model_config = SettingsConfigDict(
        # Anchored to the repository root, not the process working directory.
        # Every entry point runs from `backend/` (the venv and pyproject live
        # there) while `.env` lives at the root, so a relative ".env" silently
        # loads nothing and every setting falls back to its default — which for
        # `database_url` means a boot failure with no hint as to why.
        # A second, optional `backend/.env` overrides it for one-off local work.
        env_file=(REPO_ROOT / ".env", Path("backend/.env"), Path(".env")),
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    # ---- core ---------------------------------------------------------
    scout_env: Literal["local", "production"] = "local"
    scout_base_url: str = "http://localhost:8000"
    tz: str = "Asia/Kolkata"
    app_version: str = "dev"

    # ---- database -----------------------------------------------------
    database_url: PostgresDsn
    database_pool_size: Annotated[int, Field(ge=1, le=50)] = 10
    database_max_overflow: Annotated[int, Field(ge=0, le=50)] = 5
    database_pool_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    database_pool_recycle_s: Annotated[int, Field(ge=0)] = 1800
    database_statement_timeout_ms: Annotated[int, Field(ge=0)] = 30_000
    database_lock_timeout_ms: Annotated[int, Field(ge=0)] = 5_000
    database_echo: bool = False
    database_migrate_on_boot: bool = False
    # Read by Docker Compose to initialise the postgres service, and declared
    # here so one `.env` serves both. Without the field, `extra="forbid"` would
    # make Compose's own key a boot failure for the application.
    #
    # Required, with no default: a default database password in source is a
    # credential in source, and the one place it belongs is `.env`. SecretStr
    # keeps it out of reprs and structured log payloads.
    postgres_password: SecretStr

    # ---- redis --------------------------------------------------------
    redis_url: RedisDsn = RedisDsn("redis://redis:6379/0")
    redis_max_connections: Annotated[int, Field(ge=1, le=200)] = 20
    redis_socket_timeout_s: Annotated[float, Field(gt=0)] = 5.0
    run_lock_ttl_s: Annotated[int, Field(ge=1)] = 3_600
    robots_cache_ttl_s: Annotated[int, Field(ge=1)] = 86_400

    # ---- ingestion and sources ----------------------------------------
    source_user_agent: str = DEFAULT_USER_AGENT
    source_concurrency: Annotated[int, Field(ge=1, le=32)] = 8
    source_probe_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    source_timeout_s: Annotated[int, Field(ge=1)] = 180
    source_retry_budget_s: Annotated[int, Field(ge=1)] = 90
    rate_limit_wait_s: Annotated[int, Field(ge=1)] = 20
    auto_disable_threshold: Annotated[int, Field(ge=1)] = 5
    circuit_breaker_failures: Annotated[int, Field(ge=1)] = 5
    max_description_chars: Annotated[int, Field(ge=1_000)] = 60_000
    max_response_bytes: Annotated[int, Field(ge=1_024)] = 20_971_520
    ingest_close_after_missed_runs: Annotated[int, Field(ge=1)] = 2
    # SOURCE_ADAPTERS.md §7.3. Trigram similarity a parsed alert company name
    # must reach before its posting is attached to a registered company; below
    # it the posting goes to the reserved `unmatched` row rather than being
    # guessed onto the wrong employer. Raising it costs recall, lowering it
    # mis-attributes — and a mis-attributed posting is worse, because it is
    # invisible: it looks like a real role at a company being tracked.
    alert_company_match_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.45

    # ---- stage ④, the deterministic filter (CONFIGURATION.md §8) ------
    # Zero model calls, and the reason the cost model works at all: it removes
    # the large majority of postings before a token is spent. Tightening
    # `default_location_filter` is the first lever on LLM cost — it saves more
    # per day than the entire daily budget.
    # `NoDecode` is load-bearing. Without it `pydantic-settings` JSON-decodes
    # a `list[str]` inside the dotenv source, *before* any validator runs, and
    # `IN,Remote` dies as "Expecting value: line 1 column 1" — an error that
    # names neither the key nor the expected format. It suppresses that decode
    # so the string reaches `_split_comma_list` below.
    default_location_filter: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["IN", "Remote"]
    )
    # Measured, not guessed. The first version of this list was written before
    # any postings existed and left `staff`, `principal` and `manager` passing —
    # 2,340 roles on the live corpus that six months of full-time experience
    # cannot reach.
    filter_seniority_deny: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "intern",
            "staff",
            "principal",
            "manager",
            "director",
            "executive",
        ]
    )
    # Every entry here was simulated against the live corpus and removed zero
    # engineering roles. `marketing` and `audit` were candidates and are
    # deliberately absent: they name an *org*, not a role, and one in four of
    # what they removed was a backend job serving that org — "Senior Software
    # Engineer, Marketing Platform Tooling", "Full Stack Engineer - Internal
    # Audit". `solutions architect` and `strategist` are also absent; each cost
    # a role worth seeing, including Palantir's Forward Deployed Strategist.
    filter_keyword_deny: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "sales",
            "recruiter",
            "teacher",
            "nurse",
            "driver",
            "warehouse",
            "account executive",
            "counsel",
            "customer success",
            "technical support",
        ]
    )
    # Years of experience a job description may demand before the role is out
    # of reach. Compared against the *least* demanding figure the description
    # states, so a role wanting "8+ years overall, 3+ with Go" is kept. A
    # description stating nothing passes. 0 disables the predicate.
    #
    # This reads what a role actually asks for, which a title deny-list cannot:
    # "Senior Software Engineer" means two years at one employer and ten at
    # another, and no list of words can tell those apart.
    filter_max_years_experience: Annotated[int, Field(ge=0, le=30)] = 5
    # Off, and it should stay off: a two-line mail-alert snippet yields garbage
    # requirements, and garbage requirements produce a confident, wrong
    # coverage score — worse than no score at all.
    alert_fidelity_extract: bool = False

    # ---- LLM provider and model pinning (CONFIGURATION.md §5.1) -------
    llm_provider: Literal["bedrock", "azure_openai"] = "bedrock"
    # Aliases, not IDs, cross the service interface: code asks for `fast` or
    # `strong` and the router resolves it (AI_ARCHITECTURE.md §4). `fast` does
    # extraction, coverage judgement and mail classification; `strong` does
    # judgement and composition, where a worse answer costs more than the token
    # difference.
    llm_model_fast: str = "anthropic.claude-3-5-haiku-20241022-v1:0"
    llm_model_strong: str = "anthropic.claude-sonnet-4-20250514-v1:0"
    llm_max_concurrency: Annotated[int, Field(ge=1, le=16)] = 4

    # ---- Bedrock credentials (CONFIGURATION.md §5.2) ------------------
    aws_region: str = "ap-south-1"
    # Omitted when an instance role is available; boto3 walks its own chain and
    # needs no code change. SecretStr so the value cannot reach a log line or an
    # error message by accident (ARCHITECTURE.md §3 invariant 6).
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    bedrock_endpoint_url: str | None = None

    # ---- cost and the budget breaker (CONFIGURATION.md §5.4) ----------
    # Cost is computed from the `usage` block on every response, never
    # estimated; only the FX rate and the per-million prices are configured.
    llm_daily_budget_inr: Annotated[Decimal, Field(gt=0)] = Decimal("80")
    llm_budget_warn_pct: Annotated[int, Field(ge=1, le=100)] = 80
    llm_inr_per_usd: Annotated[Decimal, Field(gt=0)] = Decimal("88")
    llm_price_fast_in: Annotated[Decimal, Field(ge=0)] = Decimal("0.80")
    llm_price_fast_out: Annotated[Decimal, Field(ge=0)] = Decimal("4.00")
    llm_price_strong_in: Annotated[Decimal, Field(ge=0)] = Decimal("3.00")
    llm_price_strong_out: Annotated[Decimal, Field(ge=0)] = Decimal("15.00")

    # ---- prompt bounds and caching (CONFIGURATION.md §5.5) ------------
    # Also the bound on the cost attack: a 400,000-token job description is a
    # bill, and truncating from the head keeps requirements and drops benefits
    # boilerplate.
    extraction_max_jd_tokens: Annotated[int, Field(ge=100)] = 4_000
    letter_max_jd_tokens: Annotated[int, Field(ge=100)] = 900
    llm_cache_enabled: bool = True
    llm_request_timeout_multiplier: Annotated[float, Field(ge=1.0, le=3.0)] = 1.0

    # ---- scheduler ----------------------------------------------------
    run_wall_clock_budget_s: Annotated[int, Field(ge=1)] = 900
    # Off by default so the safe state is the default: two processes with the
    # scheduler on means two 08:00 discovery runs, and the Redis run lock would
    # turn the second one into a refusal rather than into nothing happening
    # (CONFIGURATION.md §7).
    scheduler_enabled: bool = False
    # APScheduler owns this table and creates it. It is deliberately absent from
    # ``db/models.py`` and from Alembic: modelling a library's private table is
    # how a migration ends up fighting a library upgrade (SDD.md §6.2).
    scheduler_jobstore_table: str = "apscheduler_jobs"
    # 30 minutes. Long enough to cover a host reboot or a laptop that woke at
    # 08:20; short enough that a run three hours late — whose output the 08:15
    # digest has already missed — is skipped and triggered by hand instead.
    scheduler_misfire_grace_s: Annotated[int, Field(ge=0)] = 1_800
    # How long SIGTERM waits for an in-flight run before cancelling it. Bounded
    # on purpose: an unbounded wait turns "stop the container" into "the
    # container never stops".
    scheduler_shutdown_grace_s: Annotated[int, Field(ge=0)] = 60
    # The daily discovery trigger, expressed in ``tz``. Two integer fields
    # rather than a cron string: 08:00 is the whole schedule in Phase 1, and a
    # validated hour and minute cannot be a cron expression that parses but
    # means something else.
    discovery_cron_hour: Annotated[int, Field(ge=0, le=23)] = 8
    discovery_cron_minute: Annotated[int, Field(ge=0, le=59)] = 0

    # ---- gmail --------------------------------------------------------
    # Master switch for ``mail/``. Off by default: a Gmail grant is the
    # highest-value secret in the system (SECURITY_ARCHITECTURE.md §7.1), and
    # nothing should hold one because a default said so.
    mail_enabled: bool = False
    gmail_client_id: str | None = None
    gmail_client_secret: SecretStr | None = None
    # The client-secrets JSON downloaded from the Google Cloud console, as an
    # alternative to the two fields above. Whichever is set, the values are read
    # once at authorisation time and never logged.
    gmail_client_secrets_path: Path | None = None
    #: The encrypted OAuth token, mode 0600, outside the repository and outside
    #: every build context.
    mail_token_path: Path = Path("/var/lib/scout/gmail.token")
    #: The Fernet key that encrypts the token file. Never stored beside it;
    #: without it the token is not stored at all, rather than stored in clear.
    mail_token_key: SecretStr | None = None
    #: Serialises token refresh across callers, so a poll and a manual run never
    #: race and invalidate each other's access token (EMAIL_INGESTION.md §2.5).
    gmail_refresh_lock_ttl_s: Annotated[int, Field(ge=1)] = 30
    #: Ceiling on messages read per call. A bound, not a target: the lookback
    #: window is the real limit and this stops one runaway label from becoming
    #: an unbounded read.
    gmail_max_messages: Annotated[int, Field(ge=1, le=500)] = 100

    # ---- logging ------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ---- normalisation ------------------------------------------------
    @field_validator(
        "gmail_client_id",
        "gmail_client_secret",
        "gmail_client_secrets_path",
        "mail_token_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty environment value as absent.

        ``.env.example`` must declare every field and nothing else — a test
        asserts the set equality — so an optional key with no value is written
        as ``GMAIL_CLIENT_ID=`` rather than omitted or commented out. Without
        this, that line would produce ``""`` for a string and ``Path(".")`` for
        a path, and ``Path(".")`` is emphatically not "no client secrets file
        was configured".
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ---- derived ------------------------------------------------------
    @property
    def is_production(self) -> bool:
        """True when running under the production boot-validation profile."""
        return self.scout_env == "production"

    @property
    def database_url_sync(self) -> str:
        """The same database over a blocking driver, for APScheduler's job store.

        Derived rather than configured. APScheduler's ``SQLAlchemyJobStore`` is
        synchronous and cannot use asyncpg, so it needs a second URL — but a
        second *setting* is a second thing to keep pointing at the same
        database, and the day it drifts the scheduler silently persists its jobs
        somewhere the application never reads (SDD.md §6.2).
        """
        return str(self.database_url).replace("+asyncpg", "+psycopg", 1)

    @property
    def gmail_configured(self) -> bool:
        """True when ``mail/`` has everything it needs to read the mailbox.

        Deliberately not "is the token present": the token arrives from
        ``scout-careers auth gmail`` and its absence is an unauthorised install,
        not a misconfigured one. This answers the narrower question the runner
        asks — may a mailbox reader be built at all.
        """
        if not self.mail_enabled:
            return False
        if self.mail_token_key is None or not self.mail_token_key.get_secret_value().strip():
            return False
        return self.oauth_client_configured

    @property
    def oauth_client_configured(self) -> bool:
        """True when a desktop OAuth client is available, from env or from file."""
        if self.gmail_client_secrets_path is not None:
            return True
        return bool(self.gmail_client_id) and self.gmail_client_secret is not None

    @property
    def user_agent_is_attributable(self) -> bool:
        """True when the user agent carries a replaced contact address."""
        return bool(self.source_user_agent.strip()) and (
            USER_AGENT_PLACEHOLDER not in self.source_user_agent
        )

    @field_validator("mail_token_path", mode="after")
    @classmethod
    def _anchor_token_path(cls, value: Path) -> Path:
        """Resolve a relative token path against the repository root.

        The same trap as ``env_file``: every entry point runs from ``backend/``
        while the operator thinks in repository-root terms, so a relative
        ``.secrets/gmail.token`` writes to ``backend/.secrets/`` and is then
        invisible to anything launched from the root — which reports mail as
        merely `disabled`, not as a missing file. Anchoring makes one token
        serve every working directory. An absolute path is left alone, so the
        production ``/var/lib/scout/gmail.token`` is unaffected.

        Args:
            value: The configured path, absolute or relative.

        Returns:
            An absolute path.
        """
        return value if value.is_absolute() else (REPO_ROOT / value)

    #: Spellings that mean "whatever is newest". CONFIGURATION.md §5.1 forbids
    #: all of them: a silently upgraded model invalidates every eval result and
    #: every ``artifact.model`` provenance record without a deploy having
    #: happened, which is invariant 7 — no artifact whose model cannot be named.
    _UNPINNED_MARKERS: ClassVar[tuple[str, ...]] = ("-latest", ":latest", "-v1:latest")

    @field_validator(
        "default_location_filter",
        "filter_seniority_deny",
        "filter_keyword_deny",
        mode="before",
    )
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        """Parse ``IN,Remote`` into a list.

        Args:
            value: Whatever the environment or a default supplied.

        Returns:
            A list of trimmed, non-empty entries when given a string; anything
            else unchanged, so a default list passes through.

        CONFIGURATION.md §4 specifies comma-separated lists. ``pydantic-settings``
        would otherwise try to JSON-decode a ``list[str]`` field and reject
        ``IN,Remote`` with a parse error that names neither the file nor the
        expected format — a boot failure whose message points nowhere.
        """
        if isinstance(value, str):
            return [entry.strip() for entry in value.split(",") if entry.strip()]
        return value

    @field_validator("llm_model_fast", "llm_model_strong", mode="after")
    @classmethod
    def _refuse_unpinned_model(cls, value: str) -> str:
        """Refuse a model ID that can change under us.

        Args:
            value: The configured model ID.

        Returns:
            It, unchanged.

        Raises:
            ValueError: When the ID carries a floating-version marker.
        """
        lowered = value.strip().lower()
        if any(marker in lowered for marker in cls._UNPINNED_MARKERS):
            raise ValueError(
                f"model IDs must be pinned; {value!r} names a floating version. "
                "A model that changes without a deploy invalidates every eval "
                "result and every artifact's provenance (CONFIGURATION.md §5.1)."
            )
        return value

    @model_validator(mode="after")
    def _boot_checks(self) -> Settings:
        _run_boot_checks(self)
        return self


def _run_boot_checks(settings: Settings) -> None:
    """Refuse to start on anything touching data integrity or the compliance boundary.

    Args:
        settings: The freshly constructed settings object.

    Raises:
        ConfigError: On any refusing condition from CONFIGURATION.md §14.2 that
            is in Phase 1 scope.
    """
    dsn = str(settings.database_url)
    if "+asyncpg" not in dsn.split("://", 1)[0]:
        raise ConfigError("DATABASE_URL must use the asyncpg driver")

    if settings.run_lock_ttl_s <= settings.run_wall_clock_budget_s:
        raise ConfigError(
            "run lock TTL must exceed the run budget: "
            f"RUN_LOCK_TTL_S={settings.run_lock_ttl_s} "
            f"RUN_WALL_CLOCK_BUDGET_S={settings.run_wall_clock_budget_s}"
        )

    # GMAIL_CLIENT_SECRETS_PATH is typed `Path | None`, and every string is a
    # valid Path — so a value pasted into the wrong key is accepted in silence
    # and surfaces later as a confusing error about a different setting. This
    # happened: a Fernet key landed here while MAIL_TOKEN_KEY was left empty,
    # and the reported failure named MAIL_TOKEN_KEY. Checking the file exists
    # turns "wrong value in the wrong key" into a message that says so.
    if settings.gmail_client_secrets_path is not None:
        path = settings.gmail_client_secrets_path
        if not path.exists() or not path.is_file():
            raise ConfigError(
                f"GMAIL_CLIENT_SECRETS_PATH does not name a readable file: {path}. "
                "It is the client-secrets JSON downloaded from the Google Cloud "
                "console. If you meant to set the Fernet key, that is MAIL_TOKEN_KEY; "
                "if you are using GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET, leave "
                "this empty."
            )

    if settings.mail_enabled:
        if (
            settings.mail_token_key is None
            or not settings.mail_token_key.get_secret_value().strip()
        ):
            raise ConfigError(
                "MAIL_TOKEN_KEY is required when MAIL_ENABLED is true: the OAuth "
                "token is encrypted at rest or it is not stored at all. Generate one "
                'with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        if not settings.oauth_client_configured:
            raise ConfigError(
                "MAIL_ENABLED is true but no desktop OAuth client is configured: "
                "set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET, or GMAIL_CLIENT_SECRETS_PATH"
            )

    if not settings.is_production:
        return

    if settings.database_echo:
        raise ConfigError("SQL echo is not permitted in production")

    if settings.log_level == "DEBUG":
        raise ConfigError("DEBUG logging is not permitted in production")

    if not settings.user_agent_is_attributable:
        raise ConfigError(
            "SOURCE_USER_AGENT is required in production and must carry a real "
            "contact address, not the .env.example placeholder"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object.

    Cached: configuration is read once, at first use, and never reloaded. A
    value that changes system behaviour requires an ``.env`` edit and a
    restart, which is the correct amount of friction.

    Returns:
        The validated ``Settings`` instance.

    Raises:
        ConfigError: When boot validation refuses the configuration.
        pydantic.ValidationError: When a field is missing or out of bounds.
    """
    return Settings()


__all__ = ["DEFAULT_USER_AGENT", "USER_AGENT_PLACEHOLDER", "Settings", "get_settings"]
