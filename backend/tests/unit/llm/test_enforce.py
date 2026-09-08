"""Two retry budgets, and the promise never to coerce.

The distinction between them is the point. A 429 means "try again later"; a
schema violation means "the model produced something wrong, and asking again
identically will probably produce it again". Conflating them is the classic
failure where a malformed-output loop spends the day's budget in ninety seconds
— which is why these are counted separately and why the repair budget is 1 or 2
while the transport budget is 2 or 3.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from scout_careers.llm.base import (
    MAX_OUTPUT_TOKENS,
    LLMProviderUnavailable,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    SchemaEnforcementFailed,
    SchemaViolation,
    Usage,
    policy_for,
)
from scout_careers.llm.cost import RunCost
from scout_careers.llm.enforce import call_structured, repair_message, with_transport_retries
from scout_careers.llm.registry import Prompt
from tests.conftest import make_settings


class Extracted(BaseModel):
    title: str


PROMPT = Prompt(
    family="requirement_extraction",
    version="2026-09-01.1",
    system="You extract requirements.",
    user_template="Extract for {company}.\n<<UNTRUSTED:jd>>",
    sha256="a" * 64,
)


def ok_response(**overrides: Any) -> LLMResponse[Extracted]:
    defaults: dict[str, Any] = {
        "value": Extracted(title="Backend Engineer"),
        "raw_text": "{}",
        "model_id": "us.anthropic.claude-sonnet-5",
        "prompt_version": "",
        "usage": Usage(input_tokens=1_000, output_tokens=100),
        "latency_ms": 10,
        "attempts": 1,
        "stop_reason": "tool_use",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)


class ScriptedClient:
    """Returns or raises whatever the script says, one entry per call."""

    name = "scripted"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.script.pop(0) if self.script else ok_response()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def stream_text(self, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def healthcheck(self) -> bool:  # pragma: no cover - unused
        return True

    async def aclose(self) -> None:  # pragma: no cover - unused
        return None


async def no_sleep(_seconds: float) -> None:
    """Backoff without the waiting."""
    return None


async def enforce(script: list[Any], **kwargs: Any):
    client = ScriptedClient(script)
    response = await call_structured(
        client,
        family="requirement_extraction",
        prompt=PROMPT,
        schema=Extracted,
        fields={"company": "Acme"},
        untrusted={"jd": "We are hiring."},
        max_untrusted_tokens=4_000,
        sleep=no_sleep,
        **kwargs,
    )
    return response, client


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_a_valid_answer_returns_first_time() -> None:
    response, client = await enforce([ok_response()])
    assert response.value.title == "Backend Engineer"
    assert response.attempts == 1
    assert len(client.calls) == 1


async def test_the_prompt_version_is_stamped_on_the_response() -> None:
    """What ``requirement.prompt_version`` records. Invariant 7 depends on it."""
    response, _ = await enforce([ok_response()])
    assert response.prompt_version == "requirement_extraction@2026-09-01.1"


async def test_the_untrusted_content_arrives_enveloped() -> None:
    _, client = await enforce([ok_response()])
    user = client.calls[0]["user"]
    assert "<<<UNTRUSTED_JOB_DESCRIPTION>>>" in user
    assert "We are hiring." in user
    assert "for Acme" in user.replace("Extract for Acme.", "for Acme")


async def test_the_family_selects_the_alias_and_temperature() -> None:
    _, client = await enforce([ok_response()])
    assert client.calls[0]["model"] == "fast"
    assert client.calls[0]["temperature"] == 0.0, "extraction is not a creative act"


# --------------------------------------------------------------------------
# Transport retries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [LLMRateLimited("429"), LLMProviderUnavailable("503"), LLMTimeout("slow")]
)
async def test_a_transport_failure_is_retried(error: Exception) -> None:
    response, client = await enforce([error, ok_response()])
    assert response.value.title == "Backend Engineer"
    assert len(client.calls) == 2


async def test_transport_retries_are_bounded() -> None:
    policy = policy_for("requirement_extraction")
    errors = [LLMRateLimited("429")] * (policy.max_transport_retries + 1)
    with pytest.raises(LLMRateLimited):
        await enforce(errors)


async def test_a_timeout_gets_a_longer_deadline_next_time() -> None:
    """Retrying a slow model at the same deadline just buys the same timeout."""
    _, client = await enforce([LLMTimeout("slow"), ok_response()])
    first, second = client.calls[0]["timeout_s"], client.calls[1]["timeout_s"]
    assert second > first


async def test_a_transport_retry_does_not_consume_the_repair_budget() -> None:
    """The two budgets are independent, which is the whole design.

    Three transport failures followed by one schema violation must still leave
    a repair attempt available.
    """
    script = [
        LLMRateLimited("429"),
        LLMProviderUnavailable("503"),
        SchemaViolation([{"loc": ("title",), "msg": "field required"}], raw=""),
        ok_response(),
    ]
    response, client = await enforce(script)
    assert response.attempts == 2, "one repair, not four"
    assert len(client.calls) == 4


# --------------------------------------------------------------------------
# Repair retries
# --------------------------------------------------------------------------


async def test_a_schema_violation_is_repaired_once() -> None:
    violation = SchemaViolation([{"loc": ("title",), "msg": "field required"}], raw="")
    response, client = await enforce([violation, ok_response()])
    assert response.attempts == 2
    assert len(client.calls) == 2


async def test_the_repair_message_carries_the_errors_not_the_bad_output() -> None:
    """Showing the model its own wrong answer produces a patched wrong answer.

    It anchors on what it already wrote. The repair restates the request and
    appends the validation failures instead.
    """
    message = repair_message(
        "Extract for Acme.", [{"loc": ("years",), "msg": "Input should be a valid integer"}]
    )
    assert "Extract for Acme." in message
    assert "years: Input should be a valid integer" in message
    assert "corrected object" in message


async def test_repair_retries_are_bounded_and_fail_closed() -> None:
    policy = policy_for("requirement_extraction")
    violations = [
        SchemaViolation([{"loc": ("title",), "msg": "field required"}], raw="")
        for _ in range(policy.max_repair_retries + 1)
    ]
    with pytest.raises(SchemaEnforcementFailed) as caught:
        await enforce(violations)
    assert caught.value.family == "requirement_extraction"
    assert caught.value.prompt_id == "requirement_extraction@2026-09-01.1"


async def test_nothing_is_coerced_on_the_way_out() -> None:
    """No stripping, no defaulting, no parsing a number out of a string.

    A model that answered `"level": "mostly met"` did not understand the task,
    and mapping it to `partial` invents an answer nobody gave. The only two
    outcomes are a validated object or a failure.
    """
    violations = [SchemaViolation([{"msg": "bad"}], raw='{"title": 42}')] * 2
    with pytest.raises(SchemaEnforcementFailed):
        await enforce(violations)


# --------------------------------------------------------------------------
# Post-validation: constraints a schema cannot express
# --------------------------------------------------------------------------


async def test_a_post_validation_failure_triggers_a_repair() -> None:
    seen: list[str] = []

    def reject_first(value: Extracted) -> list[str]:
        seen.append(value.title)
        return ["title must not be empty of meaning"] if len(seen) == 1 else []

    response, client = await enforce([ok_response(), ok_response()], post_validate=reject_first)
    assert response.attempts == 2
    assert len(client.calls) == 2


async def test_a_post_validator_that_never_passes_fails_closed() -> None:
    with pytest.raises(SchemaEnforcementFailed):
        await enforce(
            [ok_response(), ok_response(), ok_response()],
            post_validate=lambda _value: ["always wrong"],
        )


# --------------------------------------------------------------------------
# The budget breaker
# --------------------------------------------------------------------------


async def test_usage_is_recorded_against_the_run() -> None:
    cost = RunCost.from_settings(make_settings())
    await enforce([ok_response()], cost=cost)
    assert cost.calls == 1
    assert cost.input_tokens == 1_000
    assert cost.spent_inr > 0


async def test_the_breaker_is_checked_before_the_call_not_after() -> None:
    """After is too late: the money is gone and the ceiling did nothing."""
    from scout_careers.llm.base import BudgetExhausted

    cost = RunCost.from_settings(make_settings(llm_daily_budget_inr=Decimal("0.01")))
    cost.record(Usage(input_tokens=1_000_000, output_tokens=0), "fast")
    assert cost.is_open is True

    client = ScriptedClient([ok_response()])
    with pytest.raises(BudgetExhausted):
        await call_structured(
            client,
            family="requirement_extraction",
            prompt=PROMPT,
            schema=Extracted,
            fields={"company": "Acme"},
            untrusted={"jd": "x"},
            max_untrusted_tokens=100,
            cost=cost,
            sleep=no_sleep,
        )
    assert client.calls == [], "no call should have been made"


async def test_a_repair_counts_against_the_run_even_though_it_is_unpriced() -> None:
    """A failed call is not free — the input tokens were billed.

    The usage block does not survive the error, so the spend is unattributable
    rather than zero. Counting it separately says that honestly.
    """
    cost = RunCost.from_settings(make_settings())
    violation = SchemaViolation([{"loc": ("title",), "msg": "required"}], raw="")
    await enforce([violation, ok_response()], cost=cost)
    assert cost.repair_retries == 1
    assert cost.failures == 1
    assert cost.calls == 1, "only the successful call had usage to price"


# --------------------------------------------------------------------------
# with_transport_retries in isolation
# --------------------------------------------------------------------------


async def test_the_last_failure_is_the_one_that_propagates() -> None:
    attempts: list[float] = []

    async def always_fails(timeout_s: float) -> None:
        attempts.append(timeout_s)
        raise LLMProviderUnavailable("503")

    policy = policy_for("mail_classification")
    with pytest.raises(LLMProviderUnavailable):
        await with_transport_retries(
            always_fails, policy=policy, family="mail_classification", sleep=no_sleep
        )
    assert len(attempts) == policy.max_transport_retries + 1


def test_the_output_ceiling_can_carry_the_schema_maximum() -> None:
    """The two numbers that silently disagreed for a whole backfill.

    `MAX_REQUIREMENTS` is 40 and the ceiling was 2,600 — about 65 tokens a
    requirement, which is what a requirement actually costs. So the schema
    permitted an answer the token budget could not carry, and a verbose job
    description truncated. A truncated tool call is not a partial answer; it is
    no answer, and 46 of 100 postings failed that way in one run.

    Pinned as a relationship rather than a literal: raising `MAX_REQUIREMENTS`
    without raising the ceiling reintroduces exactly this, and the failure looks
    like a model problem rather than an arithmetic one.
    """
    from scout_careers.extract.schema import MAX_REQUIREMENTS

    ceiling = MAX_OUTPUT_TOKENS["requirement_extraction"]
    tokens_per_requirement = 70  # measured on the 2026-09-07 backfill
    assert ceiling >= MAX_REQUIREMENTS * tokens_per_requirement, (
        f"{ceiling} tokens cannot carry {MAX_REQUIREMENTS} requirements"
    )
