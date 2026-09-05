"""Every CLI entry point runs its coroutine through one helper.

`scout-careers source retire` fetched a preview in one `asyncio.run`, prompted
for confirmation synchronously, then performed the update in a second
`asyncio.run`. Both halves were correct. What was not correct is that
`_default_engine` is `lru_cache`d, so the second loop inherited a pool whose
asyncpg connections belonged to the first, already-closed loop:

    RuntimeError: Task ... got Future ... attached to a different loop

The fix disposes the engine inside the loop that created it. These tests pin
both halves: the helper disposes, and no CLI module reaches for `asyncio.run`
directly again — because the next command to do so would reintroduce the bug
silently, in a code path nobody re-tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scout_careers.cli import _async


def test_the_helper_returns_the_coroutine_result() -> None:
    async def _answer() -> int:
        return 42

    assert _async.run(_answer()) == 42


def test_the_engine_is_disposed_after_every_run(monkeypatch: pytest.MonkeyPatch) -> None:
    disposals: list[str] = []

    async def _fake_dispose() -> None:
        disposals.append("disposed")

    monkeypatch.setattr(_async, "dispose_engine", _fake_dispose)

    async def _work() -> str:
        return "done"

    assert _async.run(_work()) == "done"
    assert disposals == ["disposed"]


def test_the_engine_is_disposed_even_when_the_coroutine_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path is the one that strands a pool across loops."""
    disposals: list[str] = []

    async def _fake_dispose() -> None:
        disposals.append("disposed")

    monkeypatch.setattr(_async, "dispose_engine", _fake_dispose)

    async def _boom() -> None:
        raise ValueError("upstream said no")

    with pytest.raises(ValueError, match="upstream said no"):
        _async.run(_boom())

    assert disposals == ["disposed"]


def test_two_sequential_runs_each_get_a_fresh_loop() -> None:
    """The shape of `source retire`: run, prompt, run again.

    The loop objects are kept alive deliberately. Comparing `id()` would be
    wrong: CPython reuses the address once the first loop is collected, so the
    ids compare equal and the test passes for the wrong reason.
    """
    loops: list[asyncio.AbstractEventLoop] = []

    async def _record() -> None:
        loops.append(asyncio.get_running_loop())

    _async.run(_record())
    _async.run(_record())

    assert len(loops) == 2
    assert loops[0] is not loops[1], "the second run must not reuse the first loop"
    # And the property that actually caused the bug: the first loop is gone,
    # so any pool still bound to it would be unusable.
    assert loops[0].is_closed()


def test_no_cli_module_calls_asyncio_run_directly() -> None:
    """The rule, enforced rather than documented.

    A new command that reaches for `asyncio.run` reintroduces the bug in a path
    that nobody thinks to re-test, so the ban is a test rather than a comment.
    """
    cli_dir = Path(_async.__file__).parent
    offenders = [
        path.name
        for path in sorted(cli_dir.glob("*.py"))
        if path.name != "_async.py" and "asyncio.run(" in path.read_text()
    ]
    assert offenders == [], (
        f"{offenders} call asyncio.run directly; use "
        "`from scout_careers.cli._async import run as run_async` instead"
    )
