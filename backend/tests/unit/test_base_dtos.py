"""The DTOs crossing the sources/ → ingest/ boundary."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scout_careers.common.types import AtsType, SourceStatus
from scout_careers.sources.base import (
    GreenhouseConfig,
    MailAlertConfig,
    ProbeResult,
    RawPosting,
    SourceResult,
    WorkdayConfig,
)

MINIMAL = {
    "external_id": "6789012",
    "url": "https://boards.greenhouse.io/stripe/jobs/6789012",
    "title": "Software Engineer, Payments Infrastructure",
    "description_text": "Stripe builds the economic infrastructure of the internet.",
}


def test_raw_posting_defaults() -> None:
    posting = RawPosting(**MINIMAL)
    assert posting.is_remote is False
    assert posting.employment_type == "unknown"
    assert posting.seniority_guess == "unknown"
    assert posting.posted_at is None  # never fabricated as now()
    assert posting.raw == {}


def test_raw_posting_is_frozen_so_ingest_cannot_mutate_it() -> None:
    posting = RawPosting(**MINIMAL)
    with pytest.raises(ValidationError):
        posting.title = "something else"  # type: ignore[misc]


def test_raw_posting_forbids_extras() -> None:
    # A vendor field somebody quietly started depending on is a test failure.
    with pytest.raises(ValidationError):
        RawPosting(**MINIMAL, salary="₹45L")  # type: ignore[call-arg]


def test_raw_posting_requires_a_description() -> None:
    with pytest.raises(ValidationError):
        RawPosting(**{**MINIMAL, "description_text": ""})


def test_location_country_must_be_iso_alpha_2_and_is_upper_cased() -> None:
    assert RawPosting(**MINIMAL, location_country="in").location_country == "IN"
    assert RawPosting(**MINIMAL, location_country=None).location_country is None
    for bad in ("IND", "I", "1N"):
        with pytest.raises(ValidationError):
            RawPosting(**MINIMAL, location_country=bad)


def test_probe_result_shape() -> None:
    probe = ProbeResult(
        reachable=False, latency_ms=42, http_status=404, detail="Board token not found"
    )
    assert probe.sample_count == 0
    assert probe.company_name_guess is None


def test_source_result_matches_the_documented_json_shape() -> None:
    result = SourceResult(
        source_id=77,
        company_id=42,
        adapter=AtsType.WORKDAY,
        describe="Workday · adobe / external_experienced",
        status=SourceStatus.OK,
        fetched=214,
        new=6,
        updated=3,
        unchanged=205,
        skipped={"unlisted": 0, "gone": 2, "no_description": 1},
        duration_ms=21043,
        requests=12,
        retries=1,
        rate_limit_wait_ms=4100,
    )
    payload = result.model_dump(mode="json")
    assert set(payload) == {
        "source_id",
        "company_id",
        "adapter",
        "describe",
        "status",
        "fetched",
        "new",
        "updated",
        "unchanged",
        "skipped",
        "duration_ms",
        "requests",
        "retries",
        "rate_limit_wait_ms",
        "error",
        "error_code",
    }
    assert payload["error"] is None
    assert payload["adapter"] == "workday"


def test_source_result_crashed_reports_the_type_not_the_message() -> None:
    # The exception text may quote an upstream body, which is untrusted and is
    # rendered in the digest. Only the type is safe to carry.
    result = SourceResult.crashed(
        source_id=1,
        company_id=2,
        adapter=AtsType.GREENHOUSE,
        describe="Greenhouse · acme-corp",
        exc=RuntimeError("<script>alert(1)</script>"),
    )
    assert result.status is SourceStatus.ERROR
    assert result.error == "runner crashed: RuntimeError"
    assert result.error_code == "adapter.unknown"


def test_error_is_capped_at_500_characters() -> None:
    with pytest.raises(ValidationError):
        SourceResult(
            source_id=1,
            company_id=2,
            adapter=AtsType.LEVER,
            describe="Lever · netflix",
            status=SourceStatus.ERROR,
            error="x" * 501,
        )


def test_config_models_constrain_url_interpolated_fields() -> None:
    assert GreenhouseConfig(board_token="stripe").board_token == "stripe"
    for bad in ("Stripe", "stripe/../evil", "stripe.evil.com", ""):
        with pytest.raises(ValidationError):
            GreenhouseConfig(board_token=bad)

    # The Workday host regex is a security control: config must not be able to
    # point the adapter at an arbitrary host.
    ok = WorkdayConfig(host="adobe.wd5.myworkdayjobs.com", tenant="adobe", site="external")
    assert ok.max_pages == 50
    for bad_host in ("evil.com", "adobe.myworkdayjobs.com.evil.com", "Adobe.WD5.myworkdayjobs.com"):
        with pytest.raises(ValidationError):
            WorkdayConfig(host=bad_host, tenant="adobe", site="external")


def test_config_models_serialise_canonically_for_the_unique_constraint() -> None:
    # UNIQUE (company_id, adapter, config) depends on this.
    a = GreenhouseConfig(board_token="stripe").model_dump(mode="json", exclude_defaults=False)
    b = GreenhouseConfig.model_validate({"board_token": "stripe"}).model_dump(
        mode="json", exclude_defaults=False
    )
    assert a == b


def test_mail_alert_defaults_list_the_documented_senders() -> None:
    config = MailAlertConfig()
    assert config.label == "job-alerts"
    assert config.lookback_hours == 26
    assert "jobalerts-noreply@linkedin.com" in config.senders
