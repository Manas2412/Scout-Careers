"""The Converse request we send, and how a botocore error is classified.

No AWS. The boto3 client is injectable, so the request shape and the error
translation are testable offline — which matters, because the request shape is
what forces structured output and the classification is what decides whether a
failure is retried or reported to the operator as a console step.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from scout_careers.llm.base import (
    LLMConfigError,
    LLMProviderUnavailable,
    LLMRateLimited,
    SchemaViolation,
    Usage,
)
from scout_careers.llm.bedrock import TOOL_NAME, BedrockClient
from tests.conftest import make_settings


class Extracted(BaseModel):
    """A stand-in response schema."""

    title: str
    years: int


def tool_response(payload: dict[str, Any], **usage: int) -> dict[str, Any]:
    """A Converse response that called the tool."""
    return {
        "output": {"message": {"content": [{"toolUse": {"name": TOOL_NAME, "input": payload}}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 100, "outputTokens": 20, **usage},
    }


class FakeBedrock:
    """Records the request, returns what the test says."""

    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def client(fake: FakeBedrock, **overrides) -> BedrockClient:
    return BedrockClient(make_settings(**overrides), client=fake)


async def call(fake: FakeBedrock, *, cache_system: bool = False, **overrides):
    return await client(fake, **overrides).structured(
        model="fast",
        system="You extract requirements.",
        user="<<<UNTRUSTED_JOB_DESCRIPTION>>>\nA job.\n<<<END_UNTRUSTED_JOB_DESCRIPTION>>>",
        schema=Extracted,
        temperature=0.0,
        max_output_tokens=1_000,
        timeout_s=10.0,
        cache_system=cache_system,
    )


# --------------------------------------------------------------------------
# The request forces structured output
# --------------------------------------------------------------------------


async def test_the_model_is_compelled_to_call_the_tool() -> None:
    """`toolChoice`, not a polite request for JSON.

    Asking for JSON in prose gets preambles, markdown fences and trailing
    commentary, which then have to be parsed off. Forcing the tool removes that
    whole class of failure rather than handling it.
    """
    fake = FakeBedrock(tool_response({"title": "Backend Engineer", "years": 3}))
    await call(fake)

    config = fake.requests[0]["toolConfig"]
    assert config["toolChoice"] == {"tool": {"name": TOOL_NAME}}
    assert config["tools"][0]["toolSpec"]["name"] == TOOL_NAME


async def test_the_pydantic_schema_is_what_the_tool_accepts() -> None:
    fake = FakeBedrock(tool_response({"title": "Backend Engineer", "years": 3}))
    await call(fake)

    schema = fake.requests[0]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert set(schema["properties"]) == {"title", "years"}
    assert schema["required"] == ["title", "years"]


async def test_the_alias_is_resolved_to_the_pinned_model_id() -> None:
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    await call(fake, llm_model_fast="us.anthropic.claude-sonnet-5")
    assert fake.requests[0]["modelId"] == "us.anthropic.claude-sonnet-5"


async def test_inference_settings_are_passed_through() -> None:
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    await call(fake)
    config = fake.requests[0]["inferenceConfig"]
    assert config["temperature"] == 0.0
    assert config["maxTokens"] == 1_000


async def test_a_truncated_answer_is_refused_even_when_it_validates() -> None:
    """The failure this exists to stop is silent, not loud.

    `ExtractionResult` permits an empty requirement list on purpose, so that a
    model reading a benefits page says "nothing here" rather than inventing
    something. A generation cut off at the output limit can produce that same
    empty list — and the first real backfill did exactly that, storing a posting
    as asking for nothing. The response validates; it is still not an answer.
    """
    response = tool_response({"title": "x", "years": 1})
    response["stopReason"] = "max_tokens"

    with pytest.raises(SchemaViolation) as caught:
        await call(FakeBedrock(response))
    assert caught.value.errors[0]["type"] == "max_tokens"
    assert "cut off" in str(caught.value.errors[0]["msg"])


async def test_an_ordinary_stop_reason_is_not_refused() -> None:
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    assert (await call(fake)).stop_reason == "tool_use"


async def test_the_schema_title_is_dropped_but_a_field_named_title_survives() -> None:
    """Pydantic's root `"title": "Extracted"` is what a model nests under.

    It is documentation with no validation meaning, so it goes. But `title` is
    also an ordinary field name — stripping by key name alone deletes it from
    the contract, and the model then has no way to know the field exists. Both
    halves are asserted because the first version of this only got one right.
    """
    fake = FakeBedrock(tool_response({"title": "Backend Engineer", "years": 3}))
    await call(fake)

    schema = fake.requests[0]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert "title" not in schema, "the schema's own name invites a wrapper key"
    assert set(schema["properties"]) == {"title", "years"}
    assert "title" not in schema["properties"]["title"], "the field's own title goes too"


async def test_the_tool_description_names_the_top_level_fields() -> None:
    """ "Return a single Extracted object" reads as an instruction to build a
    container called that. Naming the fields and forbidding nesting does not."""
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    await call(fake)

    description = fake.requests[0]["toolConfig"]["tools"][0]["toolSpec"]["description"]
    assert "title" in description and "years" in description
    assert "Do not nest" in description


async def test_an_enveloped_answer_is_unwrapped() -> None:
    """The first live run: three of five postings failed because the model
    called the tool with `{"ExtractionResult": {...}}` or `{"parameters": {...}}`.

    Provider normalisation, not coercion — every field and every value is
    exactly what the model wrote; only a container is removed. That is why it
    lives behind the provider boundary rather than in the enforcement loop,
    which is forbidden to repair an answer.
    """
    fake = FakeBedrock(tool_response({"Extracted": {"title": "x", "years": 1}}))
    response = await call(fake)
    assert response.value.title == "x"


async def test_the_openai_function_envelope_is_unwrapped_too() -> None:
    fake = FakeBedrock(tool_response({"parameters": {"title": "x", "years": 1}}))
    assert (await call(fake)).value.title == "x"


async def test_a_key_that_is_a_real_field_is_never_unwrapped() -> None:
    """The guard that keeps the unwrap narrow.

    A one-key payload is not evidence of an envelope: `ExtractionResult` gives
    every field but `requirements` a default, so `{"requirements": [...]}` is a
    complete and correct answer that happens to have one key. The check is
    against the schema's own field names, so it is exact rather than a
    heuristic — and a wrong-typed real field stays a validation failure, which
    is the honest outcome, instead of being unwrapped into something else.
    """
    fake = FakeBedrock(tool_response({"title": {"nested": "value"}}))
    with pytest.raises(SchemaViolation) as caught:
        await call(fake)
    # It failed validation as a `title` of the wrong type — which is the honest
    # outcome — rather than being silently unwrapped into something else.
    assert caught.value.errors


async def test_temperature_is_omitted_when_the_model_has_deprecated_it() -> None:
    """Omitted, not defaulted. Newer Anthropic models answer a `temperature` of
    any value with a ValidationException, so the request fails outright rather
    than the field being ignored — "omit" and "send 0.0" are different requests.
    """
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    await call(fake, llm_temperature_supported_fast=False)
    config = fake.requests[0]["inferenceConfig"]
    assert "temperature" not in config
    assert config["maxTokens"] == 1_000


async def test_the_two_aliases_are_declared_independently() -> None:
    """`fast` and `strong` can sit on different model generations, so one
    alias losing the parameter must not silently drop it from the other."""
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    await call(fake, llm_temperature_supported_fast=True, llm_temperature_supported_strong=False)
    assert fake.requests[0]["inferenceConfig"]["temperature"] == 0.0


# --------------------------------------------------------------------------
# Prompt caching
# --------------------------------------------------------------------------


async def test_the_cache_point_marks_the_system_block_only() -> None:
    """The system prompt repeats; the user message never does.

    A cache point on the user message would be pure write cost — billed above
    the input rate, read back never, because the next job description is
    different.
    """
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    await call(fake, cache_system=True)

    system = fake.requests[0]["system"]
    assert system[0] == {"text": "You extract requirements."}
    assert system[-1] == {"cachePoint": {"type": "default"}}
    assert all("cachePoint" not in block for block in fake.requests[0]["messages"][0]["content"])


async def test_caching_is_not_requested_when_the_setting_is_off() -> None:
    """The fallback for a model or region that rejects the cachePoint block."""
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    await call(fake, cache_system=True, bedrock_prompt_cache_enabled=False)
    assert fake.requests[0]["system"] == [{"text": "You extract requirements."}]


async def test_cache_token_counts_are_read_back_separately() -> None:
    """Reported apart from fresh input, because they are priced apart."""
    fake = FakeBedrock(
        tool_response(
            {"title": "x", "years": 1},
            cacheReadInputTokens=800,
            cacheWriteInputTokens=50,
        )
    )
    response = await call(fake)
    assert response.usage == Usage(
        input_tokens=100, output_tokens=20, cached_input_tokens=800, cache_write_tokens=50
    )


async def test_missing_cache_fields_default_to_zero() -> None:
    """A model without prompt caching is a missing saving, not a failed call."""
    fake = FakeBedrock(tool_response({"title": "x", "years": 1}))
    response = await call(fake)
    assert response.usage.cached_input_tokens == 0
    assert response.usage.cache_write_tokens == 0


# --------------------------------------------------------------------------
# Local validation is not optional
# --------------------------------------------------------------------------


async def test_a_valid_payload_is_returned_as_the_model_instance() -> None:
    fake = FakeBedrock(tool_response({"title": "Backend Engineer", "years": 3}))
    response = await call(fake)
    assert isinstance(response.value, Extracted)
    assert response.value.title == "Backend Engineer"
    assert response.model_id
    assert response.stop_reason == "tool_use"


async def test_a_payload_the_schema_refuses_raises_a_schema_violation() -> None:
    """Provider-side enforcement is a hint. This is where a violation dies."""
    fake = FakeBedrock(tool_response({"title": "Backend Engineer", "years": "three"}))
    with pytest.raises(SchemaViolation) as caught:
        await call(fake)
    assert caught.value.errors, "the validation errors must survive for the repair step"


async def test_prose_instead_of_a_tool_call_is_a_schema_violation_not_a_transport_error() -> None:
    """The distinction decides which retry budget is spent.

    Asking a rate limiter again later usually works. Asking a model the same
    question identically usually gets the same wrong answer, so this belongs to
    the repair loop, not the transport loop.
    """
    fake = FakeBedrock(
        {
            "output": {"message": {"content": [{"text": "Sure! Here is the JSON: ..."}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
    )
    with pytest.raises(SchemaViolation):
        await call(fake)


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------


def client_error(code: str) -> BaseException:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "..."}}, "Converse")


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "TooManyRequestsException", "RequestLimitExceeded"]
)
async def test_throttling_is_retryable(code: str) -> None:
    with pytest.raises(LLMRateLimited):
        await call(FakeBedrock(error=client_error(code)))


@pytest.mark.parametrize(
    "code", ["ServiceUnavailableException", "InternalServerException", "ModelNotReadyException"]
)
async def test_a_transient_provider_failure_is_retryable(code: str) -> None:
    with pytest.raises(LLMProviderUnavailable):
        await call(FakeBedrock(error=client_error(code)))


@pytest.mark.parametrize(
    ("code", "expected_hint"),
    [
        ("AccessDeniedException", "IAM policy"),
        ("ResourceNotFoundException", "per region"),
        ("ValidationException", "model ID"),
        ("UnrecognizedClientException", "credentials"),
    ],
)
async def test_a_configuration_failure_names_its_remedy(code: str, expected_hint: str) -> None:
    """Each of these is a console step, and the message has to say which.

    `ResourceNotFoundException` in particular: model access is granted per
    region, so a model enabled in us-east-1 is genuinely absent in ap-south-1.
    That cost a debugging session on the Gmail API and it will cost another here
    unless the error says so.
    """
    with pytest.raises(LLMConfigError) as caught:
        await call(FakeBedrock(error=client_error(code)))
    assert expected_hint in str(caught.value)


async def test_an_unknown_error_code_is_treated_as_transient() -> None:
    """Unknown means unknown. Retrying once is cheaper than refusing a run."""
    with pytest.raises(LLMProviderUnavailable):
        await call(FakeBedrock(error=client_error("SomeNewExceptionAWSAdded")))


async def test_the_upstream_message_never_reaches_the_exception() -> None:
    """botocore quotes the request, and the request carries the job description.

    Every raise in the classifier uses `from None` for this reason.
    """
    from botocore.exceptions import ClientError

    leaky = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad input: SECRET_JD_TEXT"}},
        "Converse",
    )
    with pytest.raises(LLMConfigError) as caught:
        await call(FakeBedrock(error=leaky))
    assert "SECRET_JD_TEXT" not in str(caught.value)
    assert caught.value.__cause__ is None


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_constructing_the_client_touches_nothing() -> None:
    """No network, no credential resolution, no boto3 import at construction.

    The client is built lazily, so a `BedrockClient` can exist in a process that
    will never call it — which is what makes the health endpoint and the CLI
    startable without AWS configured.
    """
    built = BedrockClient(make_settings())
    assert built.name == "bedrock"
