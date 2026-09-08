#!/usr/bin/env bash
#
# Why a Bedrock call is failing. Read-only, and it sends no job-description
# text — every request here is a fixed two-word prompt.
#
# `ValidationException` means "Bedrock rejected the request shape", and the
# request has four independently suspect parts: the model ID, the tool config,
# the cache point, and the JSON schema of the tool's input. When all four go out
# together a rejection names none of them.
#
# So this sends them one at a time, adding one part per step, and prints AWS's
# own error for each. The first step that fails is the answer.
#
# Usage:  bash scripts/llm-probe.sh [fast|strong]

set -euo pipefail
cd "$(dirname "$0")/.."

ALIAS="${1:-fast}" exec python - <<'PY'
import json
import os

from scout_careers.common.config import get_settings
from scout_careers.extract.schema import ExtractionResult
from scout_careers.llm.bedrock import TOOL_NAME, BedrockClient
from scout_careers.llm.router import ModelRouter

FLAT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


def tool_config(schema: dict[str, object]) -> dict[str, object]:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": TOOL_NAME,
                    "description": "Return the result as a single object.",
                    "inputSchema": {"json": schema},
                }
            }
        ],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


def main() -> None:
    settings = get_settings()
    alias = os.environ.get("ALIAS", "fast")
    model_id = ModelRouter(settings).resolve(alias)
    client = BedrockClient(settings).client

    print()
    print(f"  Region    {settings.aws_region}")
    print(f"  Alias     {alias}")
    print(f"  Model ID  {model_id}")
    print()

    # --- what the account can actually call -------------------------------
    # A model ID is only real if it is in one of these lists. Printing them
    # turns "does not exist in this region, or one the account has no access
    # to" from a guess into something checkable.
    import boto3

    control = boto3.client("bedrock", region_name=settings.aws_region)
    try:
        profiles = [
            p["inferenceProfileId"]
            for p in control.list_inference_profiles().get("inferenceProfileSummaries", [])
            if "claude" in p["inferenceProfileId"]
        ]
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not fail
        profiles = []
        print(f"  (could not list inference profiles: {type(exc).__name__}: {exc})")

    if model_id in profiles:
        print("  The model ID is a known inference profile in this region.")
    else:
        print("  !! The model ID is NOT in this region's inference profile list.")
        if profiles:
            print("  Claude profiles this account can see here:")
            for name in sorted(profiles):
                print(f"    {name}")
    print()

    # --- one part at a time -----------------------------------------------
    cached_system = [
        {"text": "You answer with the tool."},
        {"cachePoint": {"type": "default"}},
    ]
    steps: list[tuple[str, dict[str, object]]] = [
        (
            "1. plain text, no tools, no cache, no temperature",
            {},
        ),
        (
            "2. + temperature",
            {"inferenceConfig": {"temperature": 0.0, "maxTokens": 64}},
        ),
        (
            "3. + toolConfig with a flat schema",
            {"toolConfig": tool_config(FLAT_SCHEMA)},
        ),
        (
            "4. + cachePoint on the system block",
            {"toolConfig": tool_config(FLAT_SCHEMA), "system": cached_system},
        ),
        (
            "5. + the real ExtractionResult schema",
            {
                "toolConfig": tool_config(ExtractionResult.model_json_schema()),
                "system": cached_system,
            },
        ),
    ]

    from botocore.exceptions import ClientError

    # Every step runs, even after one fails. Stopping at the first rejection
    # answers "what is broken now" and nothing else — and a request with four
    # independently suspect parts usually has to be fixed more than once, so
    # the useful output is the whole column at once.
    failures: list[str] = []
    for caption, extra in steps:
        request: dict[str, object] = {
            "modelId": model_id,
            "system": [{"text": "You answer with the tool."}],
            "messages": [{"role": "user", "content": [{"text": "Say ok."}]}],
            "inferenceConfig": {"maxTokens": 64},
            **extra,
        }
        try:
            response = client.converse(**request)
            usage = response.get("usage", {})
            print(f"  ok  {caption}")
            print(
                f"        in={usage.get('inputTokens')} out={usage.get('outputTokens')} "
                f"cache_read={usage.get('cacheReadInputTokens', 0)} "
                f"cache_write={usage.get('cacheWriteInputTokens', 0)}"
            )
        except ClientError as exc:
            error = exc.response.get("Error", {})
            failures.append(caption)
            print(f"  !!  {caption}")
            print(f"        {error.get('Code')}: {error.get('Message')}")
            if caption.startswith("5"):
                print()
                print("  The schema it refused:")
                print(json.dumps(ExtractionResult.model_json_schema(), indent=2)[:1500])

    print()
    if not failures:
        print("  Every part of the request is accepted.")
    else:
        print(f"  {len(failures)} of {len(steps)} steps failed:")
        for caption in failures:
            print(f"    {caption}")
        print()
        print("  Step 2 failing on its own means the model has deprecated")
        print("  `temperature`. Set LLM_TEMPERATURE_SUPPORTED_FAST=false (or")
        print("  _STRONG) and the client omits the field. Note what that costs:")
        print("  extraction is no longer pinned to temperature 0, so two runs")
        print("  over one posting may differ.")
    print()


main()
PY
