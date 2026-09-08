"""AWS Bedrock, through the Converse API with forced tool use.

**Converse, not ``invoke_model``.** ``invoke_model`` takes a provider-native
body — the Anthropic Messages shape for Anthropic models, something else for
everyone else — so the request builder becomes model-family-specific and the
"swap the model ID" promise quietly stops being true. Converse is one request
shape across every Bedrock model, and it is the shape that carries a tool
schema. (PQ-Bot uses ``invoke_model`` because it is committed to one Anthropic
deployment and uses Anthropic-only features; this system is not.)

**Forced tool use, not "please reply with JSON".** ``toolChoice`` compels the
model to answer by calling a tool whose input schema is the Pydantic model.
That removes preambles, markdown fences and trailing commentary as a class,
rather than parsing them off afterwards. It is a mechanism, not a preference.

**The local validation is not optional.** Provider-side schema enforcement is a
hint. Both providers emit structurally valid JSON that violates constraints the
JSON Schema could not express, so ``model_validate`` runs on every response and
is where those die.

boto3 is synchronous, so every call is dispatched with ``asyncio.to_thread``.
This is the only module in the package that imports it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ValidationError

from scout_careers.common.config import Settings
from scout_careers.common.logging import get_logger
from scout_careers.llm.base import (
    Alias,
    LLMConfigError,
    LLMProviderUnavailable,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    SchemaViolation,
    T,
    Usage,
)
from scout_careers.llm.router import ModelRouter

if TYPE_CHECKING:  # pragma: no cover - import-time only for the checker
    from types import TracebackType

log = get_logger(__name__)

#: The tool the model is forced to call. One tool, one name, every family: the
#: schema differs per call, the mechanism never does.
TOOL_NAME: Final = "emit"

#: Bedrock's ``stopReason`` when the generation hit ``maxTokens``. Treated as a
#: schema failure rather than a successful call: see :meth:`BedrockClient.structured`.
_TRUNCATED: Final = "max_tokens"

#: Botocore error codes that mean "slow down". Retryable with backoff.
_THROTTLING: Final[frozenset[str]] = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "RequestLimitExceeded"}
)

#: Codes that mean the provider is temporarily unhappy. Retryable.
_TRANSIENT: Final[frozenset[str]] = frozenset(
    {"ServiceUnavailableException", "InternalServerException", "ModelNotReadyException"}
)

#: Codes that will not improve by asking again. Not retryable, and the message
#: has to name the remedy, because every one of these is a console step.
_FATAL: Final[Mapping[str, str]] = {
    "AccessDeniedException": (
        "Bedrock refused the credential. Check the IAM policy allows "
        "bedrock:InvokeModel and bedrock:Converse for this model."
    ),
    "ValidationException": (
        "Bedrock rejected the request shape. Usually a model ID that does not "
        "exist in this region, or one the account has no access to."
    ),
    "ResourceNotFoundException": (
        "No such model in this region. Model access is granted per region: a "
        "model enabled in us-east-1 is not enabled in ap-south-1."
    ),
    "UnrecognizedClientException": "The AWS credentials were not recognised.",
}


#: Appended to every configuration failure. AWS's own message is the thing that
#: actually names the broken field, and it is deliberately NOT put in this
#: exception: botocore quotes the request, and the request carries
#: job-description text (§11.2). The probe asks the same questions with a
#: two-word prompt, so it can print the message in full without that risk.
#:
#: The first real run failed five times with no cause, which was a diagnosability
#: hole worth closing — but closing it by relaxing the leak rule would have
#: traded a permanent invariant for one debugging session. Pointing at the safe
#: place to look costs nothing and holds both.
_PROBE_HINT: Final = (
    "Run `bash scripts/llm-probe.sh fast` for AWS's own message — its requests "
    "carry no posting text, so it can show the error in full."
)


#: Keys a model reaches for when it wraps the object instead of emitting it.
#: ``parameters`` and ``arguments`` are the OpenAI function-calling envelope;
#: the rest are what a model invents when the tool description sounds like it is
#: asking for a container.
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {"parameters", "arguments", "input", "properties", "value", "result", "response", "output"}
)


#: Keywords whose values are maps of *names* to schemas, not schemas themselves.
#: Recursion has to change gear here: a key inside one of these is a field name
#: the caller chose, not a JSON Schema keyword.
_NAME_KEYED: Final[frozenset[str]] = frozenset(
    {"properties", "patternProperties", "$defs", "definitions"}
)


def _tool_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Return the JSON schema for the tool, without its ``title`` keywords.

    Args:
        schema: The Pydantic model.

    Returns:
        The JSON schema with the ``title`` *keyword* removed at every level.

    Pydantic emits ``"title": "ExtractionResult"`` at the root, and the first
    live run had models call the tool with ``{"ExtractionResult": {...}}`` — the
    title read as the name of a key to nest under. Titles are documentation with
    no validation meaning, and ``$ref`` resolves through ``$defs`` keys rather
    than titles, so removing them costs nothing.

    ``title`` is also a perfectly ordinary *field name*, and a schema with one
    must keep it. So the walk tracks which position it is in: inside
    ``properties`` or ``$defs`` the keys are names the caller chose, and only
    their values are schemas. Stripping by key name alone would silently delete
    a field from the contract — which is what the first version of this did, and
    what a test caught.
    """

    def strip(node: Any) -> Any:
        if isinstance(node, list):
            return [strip(item) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title":
                continue
            if key in _NAME_KEYED and isinstance(value, dict):
                out[key] = {name: strip(child) for name, child in value.items()}
            else:
                out[key] = strip(value)
        return out

    stripped: dict[str, Any] = strip(schema.model_json_schema())
    return stripped


def _unwrap_envelope(payload: dict[str, Any], schema: type[BaseModel]) -> dict[str, Any]:
    """Undo a wrapper the model put around the object.

    Args:
        payload: The tool input as the model sent it.
        schema: The expected model.

    Returns:
        The inner object when ``payload`` is an envelope, otherwise ``payload``
        unchanged.

    **This is provider normalisation, not coercion.** ``llm/enforce.py`` refuses
    to repair a bad answer — no defaulting a missing enum, no mapping "mostly
    met" onto ``partial`` — because those invent an answer nobody gave. Removing
    a container invents nothing: every field, and every value, is exactly what
    the model wrote. Which is also why it lives here, behind the provider
    boundary, alongside the other facts about how this provider's models package
    a tool call.

    The guard that keeps it narrow: a single key that is **a real field of the
    schema** is never unwrapped. ``ExtractionResult`` has one required-ish field
    and defaults for the rest, so ``{"requirements": [...]}`` is a perfectly
    valid answer that happens to be a one-key dict — and unwrapping it would
    turn a good response into a mystery.

    Logged every time. If the rate is anything but tiny the tool description is
    wrong, and the fix belongs there rather than here.
    """
    if len(payload) != 1:
        return payload
    ((key, value),) = payload.items()
    if not isinstance(value, dict):
        return payload
    if key in schema.model_fields:
        # A legitimate one-field answer, not an envelope.
        return payload
    if key != schema.__name__ and key.lower() not in _ENVELOPE_KEYS:
        return payload

    log.warning(
        "llm_tool_input_unwrapped",
        schema=schema.__name__,
        envelope_key=key,
        note="the model wrapped the object; the tool description should stop it",
    )
    unwrapped: dict[str, Any] = value
    return unwrapped


class BedrockClient:
    """The default provider.

    Args:
        settings: Supplies the region, credentials, model IDs and timeouts.
        client: Injectable ``bedrock-runtime`` client, for tests. When omitted
            one is built lazily on first use, so constructing this object never
            touches the network or requires credentials to exist.
    """

    name = "bedrock"

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._router = ModelRouter(settings)
        self._client = client
        self._prompt_cache = settings.bedrock_prompt_cache_enabled
        #: Aliases already warned about, so the notice is one line per process
        #: rather than one per posting in a 7,000-call backfill.
        self._warned_no_temperature: set[str] = set()

    # -- construction -------------------------------------------------------

    def _build_client(self) -> Any:
        """Build the boto3 client.

        Returns:
            A ``bedrock-runtime`` client.

        Raises:
            LLMConfigError: When boto3 is absent, or no credential resolves.

        Explicit keys are used when configured; otherwise boto3 walks its own
        chain — environment, instance role, ``~/.aws/credentials``. That is why
        ``.env.prod`` leaves the keys blank: on a server with a role, supplying
        keys would be a downgrade.
        """
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMConfigError(
                "boto3 is not installed; it is required for LLM_PROVIDER=bedrock"
            ) from exc

        kwargs: dict[str, Any] = {"region_name": self._settings.aws_region}
        key = self._settings.aws_access_key_id
        secret = self._settings.aws_secret_access_key
        if key is not None and secret is not None:
            kwargs["aws_access_key_id"] = key.get_secret_value()
            kwargs["aws_secret_access_key"] = secret.get_secret_value()
        if self._settings.bedrock_endpoint_url:
            kwargs["endpoint_url"] = self._settings.bedrock_endpoint_url

        config = Config(
            # The extraction stage fans out; the default pool of 10 would
            # serialise what concurrency was meant to parallelise.
            max_pool_connections=max(10, self._settings.llm_max_concurrency * 2),
            read_timeout=int(self._settings.source_timeout_s),
            connect_timeout=10,
            # One adaptive retry inside botocore, on top of our own bounded
            # loop. Not more: two retry layers multiply, and the outer one is
            # the one with the budget and the jitter.
            retries={"max_attempts": 1, "mode": "adaptive"},
        )
        return boto3.client("bedrock-runtime", config=config, **kwargs)

    @property
    def client(self) -> Any:
        """The boto3 client, built on first use."""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # -- the interface ------------------------------------------------------

    async def structured(
        self,
        *,
        model: Alias,
        system: str,
        user: str,
        schema: type[T],
        temperature: float,
        max_output_tokens: int,
        timeout_s: float,
        cache_system: bool = False,
    ) -> LLMResponse[T]:
        """Make one Converse call and validate the result locally.

        Args:
            model: ``fast`` or ``strong``.
            system: The trusted system prompt.
            user: The rendered user message, untrusted parts already enveloped.
            schema: The Pydantic model the answer must satisfy.
            temperature: Sampling temperature.
            max_output_tokens: Ceiling on the response.
            timeout_s: Deadline for this attempt.
            cache_system: Ask Bedrock to cache the system block.

        Returns:
            The validated response.

        Raises:
            SchemaViolation: The model answered without calling the tool, or the
                payload failed validation.
            LLMTimeout, LLMRateLimited, LLMProviderUnavailable, LLMConfigError.
        """
        model_id = self._router.resolve(model)
        request = self._build_request(
            model_id=model_id,
            system=system,
            user=user,
            schema=schema,
            temperature=temperature if self._temperature_supported(model) else None,
            max_output_tokens=max_output_tokens,
            cache_system=cache_system and self._prompt_cache,
        )

        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                response = await asyncio.to_thread(self._converse, request)
        except TimeoutError as exc:
            raise LLMTimeout(f"bedrock call exceeded {timeout_s}s") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        stop_reason = str(response.get("stopReason", ""))
        if stop_reason == _TRUNCATED:
            # A truncated answer is not an answer, even when it validates.
            #
            # The first real backfill hit this on four of five postings, and one
            # of them stored *zero* requirements — the generation was cut off
            # early enough that the list came back empty. `ExtractionResult`
            # permits an empty list on purpose, so that a model looking at a
            # benefits page says "nothing here" rather than inventing something;
            # that choice turns a truncation into a confident, wrong, silently
            # stored "this posting asks for nothing". Refusing here is what
            # keeps the empty list meaning what it was meant to mean.
            raise SchemaViolation(
                errors=[
                    {
                        "msg": (
                            "the response was cut off at the output limit, so it is "
                            "incomplete. Answer again, more concisely: keep each "
                            "requirement to one short line."
                        ),
                        "type": "max_tokens",
                    }
                ],
                raw="",
            )

        payload = self._extract_tool_input(response, schema)
        try:
            value = schema.model_validate(payload)
        except ValidationError as exc:
            # The payload is not attached to the log line anywhere; it can
            # contain job-description text. Only the error paths travel.
            raise SchemaViolation(
                errors=[dict(error) for error in exc.errors()],
                raw=json.dumps(payload, default=str),
            ) from None

        return LLMResponse(
            value=value,
            raw_text=json.dumps(payload, default=str),
            model_id=model_id,
            prompt_version="",  # filled in by the enforcement layer, which knows it
            usage=self._usage_of(response),
            latency_ms=latency_ms,
            attempts=1,
            stop_reason=str(response.get("stopReason", "")),
        )

    def stream_text(
        self,
        *,
        model: Alias,
        system: str,
        user: str,
        temperature: float,
        max_output_tokens: int,
        timeout_s: float,
    ) -> AsyncIterator[str]:
        """Not implemented in this phase.

        Raises:
            NotImplementedError: Streaming exists for operator-initiated
                interactive generation, which arrives with the UI. Raising is
                better than a silent non-streaming fallback that would make the
                UI look broken rather than absent.
        """
        # Named and discarded rather than silenced: the signature has to match
        # the Protocol, and `del` says "deliberately unused" in a way a `noqa`
        # would not. Same pattern as the stage-4 predicates.
        del model, system, user, temperature, max_output_tokens, timeout_s
        raise NotImplementedError("stream_text arrives with the interactive UI")

    async def healthcheck(self) -> bool:
        """Report whether Bedrock is reachable and the model is permitted.

        Returns:
            True when a minimal call succeeds.

        Deliberately a real, tiny Converse call rather than a credential check:
        the failure this needs to catch is "model access not granted in this
        region", which no amount of credential validation reveals.
        """
        try:
            await self.structured(
                model="fast",
                system="Reply by calling the tool.",
                user="Return ok=true.",
                schema=_HealthProbe,
                temperature=0.0,
                max_output_tokens=64,
                timeout_s=self._settings.source_probe_timeout_s,
            )
        # Broad on purpose: a health probe reports, it never raises. Any
        # failure at all is the answer `False`.
        except Exception as exc:
            log.warning("bedrock_healthcheck_failed", error_type=type(exc).__name__)
            return False
        return True

    async def aclose(self) -> None:
        """Release the client. botocore holds pooled sockets."""
        client, self._client = self._client, None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    async def __aenter__(self) -> BedrockClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- internals ----------------------------------------------------------

    def _temperature_supported(self, alias: Alias) -> bool:
        """Whether this alias's model still accepts ``temperature``.

        Args:
            alias: ``fast`` or ``strong``.

        Returns:
            The configured answer for that alias.

        Warns once per process when it is false. Dropping the parameter is the
        only way to call these models at all, but it also drops the guarantee
        that ``TEMPERATURE["requirement_extraction"] = 0.0`` was buying — two
        runs over one posting are no longer pinned to the same sampling — and
        an eval that compares prompt versions needs to know that its baseline
        moves on its own.
        """
        supported = (
            self._settings.llm_temperature_supported_fast
            if alias == "fast"
            else self._settings.llm_temperature_supported_strong
        )
        if not supported and alias not in self._warned_no_temperature:
            self._warned_no_temperature.add(alias)
            log.warning(
                "llm_temperature_unsupported",
                alias=alias,
                model_id=self._router.resolve(alias),
                effect="sampling is the model default; extraction is no longer pinned to 0.0",
            )
        return supported

    def _build_request(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        temperature: float | None,
        max_output_tokens: int,
        cache_system: bool,
    ) -> dict[str, Any]:
        """Assemble the Converse request.

        The system block optionally carries a ``cachePoint``. Everything before
        that marker is cached and billed at a fraction on subsequent calls
        within the cache window — which is exactly the shape of the extraction
        stage, where one long static rubric precedes 1,500 different job
        descriptions. The user message is never cached: it is different every
        time, so a cache point there would be pure write cost.

        ``temperature`` of ``None`` omits the field rather than sending a
        default. Newer Anthropic models reject the parameter outright — the
        request fails, it is not ignored — so "omit" and "send 0.0" are
        different requests, not different spellings of one.
        """
        system_blocks: list[dict[str, Any]] = [{"text": system}]
        if cache_system:
            system_blocks.append({"cachePoint": {"type": "default"}})

        inference: dict[str, Any] = {"maxTokens": max_output_tokens}
        if temperature is not None:
            inference["temperature"] = temperature

        return {
            "modelId": model_id,
            "system": system_blocks,
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": TOOL_NAME,
                            # Names the top-level fields and forbids nesting.
                            # The first live run had three of five postings fail
                            # because the model called the tool with
                            # `{"ExtractionResult": {...}}` or
                            # `{"parameters": {...}}` — the schema's own title,
                            # and the OpenAI function-calling envelope, used as
                            # a wrapper key. Saying "a single X object" invited
                            # exactly that.
                            "description": (
                                "Call this tool with the result. The fields listed in the "
                                "input schema go at the top level: "
                                f"{', '.join(schema.model_fields)}. Do not nest them "
                                "inside another key."
                            ),
                            "inputSchema": {"json": _tool_schema(schema)},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": TOOL_NAME}},
            },
            "inferenceConfig": inference,
        }

    def _converse(self, request: dict[str, Any]) -> Mapping[str, Any]:
        """Call Bedrock, translating botocore errors into this layer's own.

        Raises:
            LLMRateLimited, LLMProviderUnavailable, LLMConfigError.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            result: Mapping[str, Any] = self.client.converse(**request)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _THROTTLING:
                raise LLMRateLimited(f"bedrock throttled the request ({code})") from None
            if code in _TRANSIENT:
                raise LLMProviderUnavailable(f"bedrock is unavailable ({code})") from None
            if code in _FATAL:
                raise LLMConfigError(f"{_FATAL[code]} {_PROBE_HINT}") from None
            # `from None` throughout: botocore's message quotes the request,
            # which carries job-description text.
            raise LLMProviderUnavailable(f"bedrock returned {code or 'an error'}") from None
        except BotoCoreError as exc:
            raise LLMProviderUnavailable(
                f"bedrock transport failure ({type(exc).__name__})"
            ) from None
        return result

    @staticmethod
    def _extract_tool_input(
        response: Mapping[str, Any], schema: type[BaseModel] | None = None
    ) -> dict[str, Any]:
        """Pull the tool input out of a Converse response.

        Args:
            response: The Converse response.
            schema: The expected model, used to recognise an envelope. Optional
                so the unwrap is opt-in rather than something a caller gets by
                accident.

        Returns:
            The tool input, unwrapped if the model put it in an envelope.

        Raises:
            SchemaViolation: When the model answered with prose instead of
                calling the tool. That is a schema failure, not a transport one:
                retrying the same request identically is unlikely to help, and
                the repair loop is what handles it.
        """
        content = response.get("output", {}).get("message", {}).get("content", [])
        for block in content:
            tool_use = block.get("toolUse")
            if tool_use and tool_use.get("name") == TOOL_NAME:
                payload: dict[str, Any] = tool_use.get("input") or {}
                return _unwrap_envelope(payload, schema) if schema else payload
        raise SchemaViolation(
            errors=[{"msg": "model did not call the required tool", "type": "missing_tool_use"}],
            raw="",
        )

    @staticmethod
    def _usage_of(response: Mapping[str, Any]) -> Usage:
        """Read the token counts.

        Cache fields are absent on models or regions without prompt caching, and
        default to zero rather than raising: an unsupported cache is a missing
        saving, not a failed call.
        """
        usage = response.get("usage", {}) or {}
        return Usage(
            input_tokens=int(usage.get("inputTokens", 0) or 0),
            output_tokens=int(usage.get("outputTokens", 0) or 0),
            cached_input_tokens=int(usage.get("cacheReadInputTokens", 0) or 0),
            cache_write_tokens=int(usage.get("cacheWriteInputTokens", 0) or 0),
        )


class _HealthProbe(BaseModel):
    """The smallest possible structured answer, for :meth:`healthcheck`."""

    ok: bool


__all__ = ["TOOL_NAME", "BedrockClient"]
