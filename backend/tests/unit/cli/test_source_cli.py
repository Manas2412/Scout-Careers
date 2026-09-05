"""``scout-careers source …`` — the confirmation, and what each command calls.

The service tests prove what a retire and a delete do to the rows. These prove
the thing only the command layer can get wrong: that ``rm`` states the number of
postings it is about to destroy *before* asking, that declining leaves the
registry untouched, and that ``--yes`` is the only way past the prompt.

Nothing here opens a session, a socket or a Redis connection: every service
function the module imported is replaced, so the test exercises the command's
own logic and nothing beneath it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from typer.testing import CliRunner

from scout_careers.cli import source as source_cli
from scout_careers.common.errors import DeniedByPolicy
from scout_careers.common.types import AtsType
from scout_careers.registry.service import SourceNotFound, SourceProbe
from scout_careers.sources.base import ProbeResult
from tests.conftest import make_settings
from tests.unit.registry.conftest import make_company, make_source

runner = CliRunner()

SESSION = object()


class Calls:
    """Every service call the command layer made, in order."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    def record(self, name: str, **kwargs: Any) -> None:
        self.names.append(name)
        self.kwargs.append(kwargs)

    def kwargs_for(self, name: str) -> dict[str, Any]:
        return self.kwargs[self.names.index(name)]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Replace the CLI module's service surface with recorders."""
    recorded = Calls()
    source = make_source(7, consecutive_failures=5)
    company = make_company(1, slug="anysphere")

    @asynccontextmanager
    async def fake_scope(*_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
        yield SESSION

    async def require_source(_session: Any, source_id: int) -> Any:
        recorded.record("require_source", source_id=source_id)
        if source_id != 7:
            raise SourceNotFound(f"source {source_id} does not exist")
        return source

    async def companies_by_id(_session: Any, ids: Sequence[int]) -> dict[int, Any]:
        del ids
        return {1: company}

    async def count_source_postings(_session: Any, source_id: int) -> int:
        recorded.record("count_source_postings", source_id=source_id)
        return 214

    async def open_posting_counts(
        _session: Any, ids: Sequence[int] | None = None
    ) -> dict[int, int]:
        del ids
        return {7: 200}

    async def delete_source(_session: Any, source_id: int) -> Any:
        recorded.record("delete_source", source_id=source_id)
        return source_cli.SourceDeletion(
            source_id=source_id, company_id=1, adapter=AtsType.ASHBY, postings_deleted=214
        )

    async def retire_source(_session: Any, source_id: int) -> Any:
        recorded.record("retire_source", source_id=source_id)
        return source

    async def set_source_enabled(_session: Any, source_id: int, enabled: bool) -> Any:
        recorded.record("set_source_enabled", source_id=source_id, enabled=enabled)
        return source

    async def list_sources(_session: Any, **kwargs: Any) -> list[Any]:
        recorded.record("list_sources", **kwargs)
        return [source]

    async def resolve_company(_session: Any, reference: str) -> Any:
        recorded.record("resolve_company", reference=reference)
        return company

    async def probe_source(_session: Any, source_id: int, **_kwargs: Any) -> SourceProbe:
        recorded.record("probe_source", source_id=source_id)
        return SourceProbe(
            source_id=source_id,
            adapter=AtsType.ASHBY,
            describe="Ashby · cursor",
            url="https://api.ashbyhq.com/posting-api/job-board/cursor",
            probe=ProbeResult(reachable=True, sample_count=31, latency_ms=120, http_status=200),
        )

    async def no_redis(_settings: Any) -> None:
        return None

    monkeypatch.setattr(source_cli, "session_scope", fake_scope)
    monkeypatch.setattr(source_cli, "get_settings", make_settings)
    monkeypatch.setattr(source_cli, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(source_cli, "_redis_if_reachable", no_redis)
    monkeypatch.setattr(source_cli, "require_source", require_source)
    monkeypatch.setattr(source_cli, "companies_by_id", companies_by_id)
    monkeypatch.setattr(source_cli, "count_source_postings", count_source_postings)
    monkeypatch.setattr(source_cli, "open_posting_counts", open_posting_counts)
    monkeypatch.setattr(source_cli, "delete_source", delete_source)
    monkeypatch.setattr(source_cli, "retire_source", retire_source)
    monkeypatch.setattr(source_cli, "set_source_enabled", set_source_enabled)
    monkeypatch.setattr(source_cli, "list_sources", list_sources)
    monkeypatch.setattr(source_cli, "resolve_company", resolve_company)
    monkeypatch.setattr(source_cli, "probe_source", probe_source)
    return recorded


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def test_rm_without_yes_declines_and_deletes_nothing(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["rm", "7"], input="n\n")

    assert result.exit_code == 1
    assert "Nothing was deleted." in result.output
    assert "delete_source" not in calls.names


def test_rm_states_the_consequence_before_asking(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["rm", "7"], input="n\n")

    before_prompt = result.output.split("Delete the source")[0]
    assert "214 posting(s)" in before_prompt
    assert "cannot be undone" in before_prompt
    assert "retire" in before_prompt
    assert "count_source_postings" in calls.names


def test_rm_with_no_answer_at_all_aborts(calls: Calls) -> None:
    # EOF on stdin is not consent. The prompt is the only gate other than --yes.
    result = runner.invoke(source_cli.app, ["rm", "7"], input="")

    assert result.exit_code != 0
    assert "delete_source" not in calls.names


def test_rm_yes_deletes_and_reports_the_count(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["rm", "7", "--yes"])

    assert result.exit_code == 0
    assert calls.kwargs_for("delete_source") == {"source_id": 7}
    assert "destroying 214 posting(s)" in result.output


def test_rm_on_a_source_that_does_not_exist_deletes_nothing(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["rm", "404", "--yes"])

    assert result.exit_code == 1
    assert "delete_source" not in calls.names


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------


def test_retire_without_yes_declines_and_changes_nothing(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["retire", "7"], input="n\n")

    assert result.exit_code == 1
    assert "Nothing was changed." in result.output
    assert "retire_source" not in calls.names


def test_retire_keeps_the_postings_and_says_so(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["retire", "7", "--yes"])

    assert result.exit_code == 0
    assert calls.kwargs_for("retire_source") == {"source_id": 7}
    assert "delete_source" not in calls.names
    assert "214 posting(s) are kept" in result.output
    assert "nothing deleted" in result.output


# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------


def test_enable_clears_the_failure_count(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["enable", "7"])

    assert result.exit_code == 0
    assert calls.kwargs_for("set_source_enabled") == {"source_id": 7, "enabled": True}
    assert "failure count is reset" in result.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_failing_asks_for_the_failing_ones(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["list", "--failing"])

    assert result.exit_code == 0
    assert calls.kwargs_for("list_sources")["failing"] is True
    assert calls.kwargs_for("list_sources")["enabled"] is None


def test_list_disabled_asks_only_for_disabled_sources(calls: Calls) -> None:
    runner.invoke(source_cli.app, ["list", "--disabled"])
    assert calls.kwargs_for("list_sources")["enabled"] is False


def test_list_defaults_to_every_source(calls: Calls) -> None:
    runner.invoke(source_cli.app, ["list"])
    assert calls.kwargs_for("list_sources") == {
        "company_id": None,
        "adapter": None,
        "last_status": None,
        "failing": False,
        "enabled": None,
    }


def test_list_resolves_a_company_slug(calls: Calls) -> None:
    runner.invoke(source_cli.app, ["list", "--company", "anysphere"])
    assert calls.kwargs_for("resolve_company") == {"reference": "anysphere"}
    assert calls.kwargs_for("list_sources")["company_id"] == 1


def test_list_shows_the_identifying_config_value_and_the_open_count(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["list"])
    assert "board_name=anysphere" in result.output
    assert "200" in result.output


def test_list_json_carries_the_open_count(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["list", "--json"])
    assert '"open_postings": 200' in result.output
    assert '"company_slug": "anysphere"' in result.output


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def test_test_prints_the_probe(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["test", "7"])

    assert result.exit_code == 0
    assert calls.kwargs_for("probe_source") == {"source_id": 7}
    assert "Ashby · cursor" in result.output
    assert "31" in result.output


def test_test_on_a_denied_host_reports_the_refusal(
    calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def denied(_session: Any, source_id: int, **_kwargs: Any) -> SourceProbe:
        del source_id
        raise DeniedByPolicy("www.linkedin.com")

    monkeypatch.setattr(source_cli, "probe_source", denied)

    result = runner.invoke(source_cli.app, ["test", "9"])

    assert result.exit_code == 2
    assert "never-fetch list" in result.output
    assert "not probed" in result.output


def test_an_unreachable_board_exits_non_zero(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreachable(_session: Any, source_id: int, **_kwargs: Any) -> SourceProbe:
        return SourceProbe(
            source_id=source_id,
            adapter=AtsType.ASHBY,
            describe="Ashby · anysphere",
            url="https://api.ashbyhq.com/posting-api/job-board/anysphere",
            probe=ProbeResult(
                reachable=False,
                latency_ms=90,
                http_status=404,
                detail="Board token not found",
            ),
        )

    monkeypatch.setattr(source_cli, "probe_source", unreachable)

    result = runner.invoke(source_cli.app, ["test", "7"])

    assert result.exit_code == 1
    assert "Board token not found" in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_the_config_and_the_last_error(calls: Calls) -> None:
    result = runner.invoke(source_cli.app, ["show", "7"])

    assert result.exit_code == 0
    assert "board_name" in result.output
    assert "Board token not found" in result.output
    assert "214" in result.output


def test_show_on_a_missing_source_exits_non_zero(calls: Calls) -> None:
    assert runner.invoke(source_cli.app, ["show", "404"]).exit_code == 1
