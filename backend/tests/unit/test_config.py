"""Boot validation: the configuration that must refuse to start."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from scout_careers.common.config import DEFAULT_USER_AGENT, REPO_ROOT, Settings, get_settings
from scout_careers.common.errors import ConfigError
from tests.conftest import make_settings


def test_defaults_match_the_documented_ones() -> None:
    settings = make_settings()
    assert settings.scout_env == "local"
    assert settings.database_pool_size == 10
    assert settings.database_max_overflow == 5
    assert settings.database_statement_timeout_ms == 30_000
    assert settings.database_lock_timeout_ms == 5_000
    assert settings.run_lock_ttl_s == 3_600
    assert settings.run_wall_clock_budget_s == 900
    assert settings.robots_cache_ttl_s == 86_400
    assert settings.source_concurrency == 8
    assert settings.source_timeout_s == 180
    assert settings.source_retry_budget_s == 90
    assert settings.rate_limit_wait_s == 20
    assert settings.auto_disable_threshold == 5
    assert settings.circuit_breaker_failures == 5
    assert settings.max_description_chars == 60_000
    assert settings.max_response_bytes == 20_971_520
    assert settings.ingest_close_after_missed_runs == 2


def test_sync_dsn_is_refused_at_boot() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(database_url="postgresql://scout:scout@localhost:5432/scout")
    assert "asyncpg" in str(excinfo.value)


def test_psycopg_dsn_is_also_refused() -> None:
    with pytest.raises(ConfigError):
        make_settings(database_url="postgresql+psycopg://scout:scout@localhost:5432/scout")


def test_asyncpg_dsn_is_accepted() -> None:
    settings = make_settings(database_url="postgresql+asyncpg://u:p@db:5432/scout")
    assert "+asyncpg" in str(settings.database_url)


def test_sql_echo_in_production_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(
            scout_env="production",
            database_echo=True,
            source_user_agent="ScoutCareers/1.0 (+mailto:ops@example.com)",
        )
    assert "echo" in str(excinfo.value).lower()


def test_sql_echo_locally_is_fine() -> None:
    assert make_settings(database_echo=True).database_echo is True


def test_debug_logging_in_production_is_refused() -> None:
    with pytest.raises(ConfigError):
        make_settings(
            scout_env="production",
            log_level="DEBUG",
            source_user_agent="ScoutCareers/1.0 (+mailto:ops@example.com)",
        )


def test_run_lock_ttl_not_exceeding_the_run_budget_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(run_lock_ttl_s=900, run_wall_clock_budget_s=900)
    assert "run lock TTL must exceed the run budget" in str(excinfo.value)

    with pytest.raises(ConfigError):
        make_settings(run_lock_ttl_s=600, run_wall_clock_budget_s=900)

    # One second of headroom is enough for the invariant to hold.
    assert make_settings(run_lock_ttl_s=901, run_wall_clock_budget_s=900).run_lock_ttl_s == 901


def test_unreplaced_user_agent_is_refused_in_production() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(scout_env="production", source_user_agent=DEFAULT_USER_AGENT)
    assert "SOURCE_USER_AGENT" in str(excinfo.value)

    with pytest.raises(ConfigError):
        make_settings(scout_env="production", source_user_agent="   ")


def test_attributable_user_agent_passes_in_production() -> None:
    settings = make_settings(
        scout_env="production",
        source_user_agent="ScoutCareers/1.0 (personal job-search agent; +mailto:ops@example.com)",
    )
    assert settings.is_production is True
    assert settings.user_agent_is_attributable is True


def test_unknown_key_is_a_boot_failure() -> None:
    # extra="forbid": SCOREING_BLEND_HARD in a .env would otherwise be silently
    # ignored and cost an operator an afternoon.
    with pytest.raises(ValidationError) as excinfo:
        make_settings(scoreing_blend_hard=0.9)
    assert "scoreing_blend_hard" in str(excinfo.value).lower()


def test_settings_are_frozen() -> None:
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.source_concurrency = 32  # type: ignore[misc]


def test_out_of_bounds_numeric_is_refused() -> None:
    with pytest.raises(ValidationError):
        make_settings(source_concurrency=64)
    with pytest.raises(ValidationError):
        make_settings(database_pool_size=0)


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings.cache_info().currsize == 0
    finally:
        get_settings.cache_clear()


def test_no_unbuilt_phase_keys_leaked_into_settings() -> None:
    """Configuration must not run ahead of the code that reads it.

    ``extra="forbid"`` means every key in ``.env`` needs a field here, so a
    field added early forces its key into ``.env.example`` early too — and an
    operator then configures a capability that does not exist, which looks like
    a bug in the capability rather than in the shipping order.

    The list shrinks as phases land, and each removal is deliberate:

    - ``mail_`` came off when Phase 1 shipped the Gmail reader.
    - ``llm_`` came off when Phase 2's extraction-and-scoring slice began. The
      provider layer is being built now, so ``LLM_PROVIDER``, the pinned model
      IDs, the price table and the budget breaker are current keys.
    - ``scoring_`` came off when ``scoring/`` landed. The previous version of
      this docstring said it would stay "until the scoring code lands, not when
      the work is planned, or the guard means nothing" — and this test failing
      on the commit that added ``scoring/composite.py`` is that promise being
      kept. Every key is now read: ``composite.py`` reads the blend, the tier
      weights, the recency triple and the gate bands; ``coverage.py`` reads the
      partial credit; ``service.py`` reads the prompt version.

    What remains is genuinely unbuilt. ``generation_`` and ``export_`` belong to
    Phase 3 and later — ROADMAP.md §3.2 defers document generation and the
    export out of Phase 2 — and ``ff_`` gates behaviour that has no code at all.
    ``ledger_`` stays too, and deliberately: the claims ledger *table* now
    exists and is seeded, but no ``LEDGER_`` key does, because nothing in the
    ledger is yet configurable. A field would be configuration ahead of code
    exactly as this test describes.
    """
    prefixes = ("generation_", "ledger_", "ff_", "export_")
    for field_name in Settings.model_fields:
        assert not field_name.startswith(prefixes), f"{field_name} has no code that reads it"


def test_every_scoring_key_is_actually_read_by_the_scoring_code() -> None:
    """The successor to ``scoring_`` on the unbuilt-prefix list.

    That prefix guarded one thing: a key nothing reads. Deleting it when the
    code landed would hand back the guarantee rather than keep it — the next
    ``SCORING_SOMETHING`` added speculatively would pass every check and reach
    ``.env.example``, where an operator would set it and watch nothing happen.

    So the check moves from "does this prefix exist" to "is each key referenced
    where it belongs". Grep rather than execution, because the alternative is
    scoring a posting per setting to see whether the number moves — and a key
    whose absence changes no observable output is precisely the key this is
    looking for.
    """
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(__file__).resolve().parents[2] / "src" / "scout_careers").rglob("*.py")
    )
    unread = [
        name
        for name in Settings.model_fields
        if name.startswith(("scoring_", "skill_adjacency_")) and f"settings.{name}" not in source
    ]
    assert not unread, f"{unread} are configured but nothing reads them"


def test_no_send_side_mail_keys_exist_yet() -> None:
    # The digest, its recipient and its gmail.send scope are Phase 2. A field
    # here would mean the send capability had been configured before the code
    # that is allowed to use it exists.
    phase_two = {
        "digest_enabled",
        "digest_send_at",
        "digest_max_queue_items",
        "digest_max_alert_items",
        "mail_operator_address",
        "mail_poll_cron",
        "mail_rate_units_per_sec",
    }
    assert not (phase_two & set(Settings.model_fields))


ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


def test_env_example_constructs_settings() -> None:
    # extra="forbid" makes this a real contract: if .env.example carries a key
    # that is not a Phase 1 field, an operator who copies it cannot boot.
    settings = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    assert settings.scout_env == "local"
    assert "+asyncpg" in str(settings.database_url)


def test_env_example_declares_every_phase_1_key_and_no_others() -> None:
    declared = {
        line.split("=", 1)[0].strip().lower()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert declared == set(Settings.model_fields)


# ---------------------------------------------------------------------------
# GMAIL_CLIENT_SECRETS_PATH: a value in the wrong key must say so
# ---------------------------------------------------------------------------
#
# Every string is a valid Path, so a Fernet key pasted into this setting was
# accepted in silence and the boot failure that followed named MAIL_TOKEN_KEY —
# a different setting entirely. That cost a debugging round.


def test_a_client_secrets_path_that_is_not_a_file_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(gmail_client_secrets_path="xbJZmnNotAPathButAFernetKey0000000000000000=")

    message = str(excinfo.value)
    assert "GMAIL_CLIENT_SECRETS_PATH" in message
    # The message must point at the setting the operator probably meant.
    assert "MAIL_TOKEN_KEY" in message


def test_an_existing_client_secrets_file_is_accepted(tmp_path: Path) -> None:
    secrets = tmp_path / "client_secret.json"
    secrets.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')

    settings = make_settings(
        mail_enabled=False,
        gmail_client_secrets_path=str(secrets),
    )
    assert settings.gmail_client_secrets_path == secrets


def test_the_mail_token_key_error_tells_you_how_to_generate_one() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(
            mail_enabled=True,
            mail_token_key=None,
            gmail_client_id="x",
            gmail_client_secret="y",
        )
    assert "Fernet.generate_key" in str(excinfo.value)


# ---------------------------------------------------------------------------
# MAIL_TOKEN_PATH is anchored, not CWD-relative
# ---------------------------------------------------------------------------
#
# The token was written to backend/.secrets/ because `auth gmail` ran from
# backend/, then was invisible to anything launched from the repository root —
# which reports mail as `disabled` rather than as a missing file. Same trap as
# a relative env_file, same fix.


def test_a_relative_token_path_is_anchored_to_the_repo_root() -> None:
    from scout_careers.common.config import REPO_ROOT

    settings = make_settings(mail_enabled=False, mail_token_path=".secrets/gmail.token")

    assert settings.mail_token_path.is_absolute()
    assert settings.mail_token_path == REPO_ROOT / ".secrets/gmail.token"


def test_an_absolute_token_path_is_left_alone() -> None:
    """Production uses /var/lib/scout/gmail.token and must not be rewritten."""
    settings = make_settings(mail_enabled=False, mail_token_path="/var/lib/scout/gmail.token")

    assert settings.mail_token_path == Path("/var/lib/scout/gmail.token")


def test_the_token_path_is_the_same_from_any_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The property that actually matters: one token, every entry point."""
    from_here = make_settings(mail_enabled=False, mail_token_path=".secrets/gmail.token")
    monkeypatch.chdir(tmp_path)
    from_elsewhere = make_settings(mail_enabled=False, mail_token_path=".secrets/gmail.token")

    assert from_here.mail_token_path == from_elsewhere.mail_token_path


# --------------------------------------------------------------------------
# .env.example and Settings must not drift apart
#
# `extra="forbid"` makes this a boot failure, not a warning: a key in an env
# file with no matching field takes down every CLI command with a validation
# error naming a setting the operator never touched. Adding the Phase 2 LLM
# block was exactly that risk — 21 new keys, all of which had to land in the
# same change as their fields.
#
# The reverse direction is the documented contract: CONFIGURATION.md says
# .env.example lists the keys, so a field added without one is a setting the
# operator has no way to discover.
# --------------------------------------------------------------------------

ENV_EXAMPLE = REPO_ROOT / ".env.example"
_ENV_LINE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=")

#: Read by Docker Compose rather than by the application, or otherwise not a
#: settings field. Listed explicitly so the exemption is a decision.
NOT_SETTINGS_KEYS: frozenset[str] = frozenset()


def _example_keys() -> list[str]:
    return [
        match.group("key")
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if (match := _ENV_LINE.match(line))
    ]


def test_the_example_file_exists_and_is_not_empty() -> None:
    assert ENV_EXAMPLE.is_file()
    assert _example_keys(), ".env.example lists no keys"


def test_every_example_key_has_a_settings_field() -> None:
    fields = set(Settings.model_fields)
    unmatched = [k for k in _example_keys() if k.lower() not in fields | NOT_SETTINGS_KEYS]
    assert unmatched == [], (
        f"keys with no Settings field (extra='forbid' would refuse boot): {unmatched}"
    )


def test_no_settings_field_is_missing_from_the_example() -> None:
    keys = {k.lower() for k in _example_keys()}
    undocumented = sorted(f for f in Settings.model_fields if f not in keys)
    assert undocumented == [], f"settings absent from .env.example: {undocumented}"


#: Keys whose names contain a secret-ish word but which hold no secret. Named
#: individually rather than loosening the pattern, so a genuinely new secret is
#: still caught by default and an exemption is a decision someone made.
NOT_ACTUALLY_SECRET: frozenset[str] = frozenset(
    {
        "EXTRACTION_MAX_JD_TOKENS",  # a token *count*
        "LETTER_MAX_JD_TOKENS",  # a token count
        "GMAIL_MAX_MESSAGES",
        "MAIL_TOKEN_PATH",  # where the token lives, not the token
        "GMAIL_CLIENT_SECRETS_PATH",  # likewise
    }
)

#: Values that are placeholders rather than credentials.
PLACEHOLDER_VALUES: frozenset[str] = frozenset({"", "scout", "changeme"})


def test_the_example_ships_no_credentials() -> None:
    """`.env.example` is committed, so every secret-bearing key must be empty.

    The first version of this test matched any key containing ``TOKEN`` and
    flagged ``EXTRACTION_MAX_JD_TOKENS`` — a token budget — as a leaked
    credential. A check that cries wolf gets a wider exemption next time and
    then catches nothing, so the false positives are named here instead.
    """
    secretish = ("SECRET", "PASSWORD", "TOKEN", "API_KEY", "ACCESS_KEY", "CLIENT_ID")
    populated = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key in NOT_ACTUALLY_SECRET or not any(word in key for word in secretish):
            continue
        if line.split("=", 1)[1].strip() not in PLACEHOLDER_VALUES:
            populated.append(key)
    assert populated == [], f"credential-shaped keys with values in a committed file: {populated}"


def test_the_exemption_list_does_not_cover_a_key_that_is_gone() -> None:
    """An exemption for a key that no longer exists hides the next real one."""
    keys = set(_example_keys())
    stale = sorted(NOT_ACTUALLY_SECRET - keys)
    assert stale == [], f"exemptions for keys not in .env.example: {stale}"


def test_a_pinned_env_key_is_distinguishable_from_a_default() -> None:
    """`model_fields_set` is what makes the override warning possible.

    `.env` is normally seeded by copying `.env.example`, which pins every key.
    A later change to a default in `config.py` is then inert, and the only
    symptom is a command that reports no change and looks like it had already
    run. That cost a full round-trip: 34 deny-list entries added, 153 postings
    expected to move, none moved, and nothing said why.
    """
    pinned = make_settings(filter_role_marker_min=1)
    assert "filter_role_marker_min" in pinned.model_fields_set
    assert "filter_keyword_deny" not in pinned.model_fields_set
