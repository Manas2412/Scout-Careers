"""The ``scout-careers`` entry point.

One Typer app, five groups: ``db``, ``company``, ``seed``, ``run`` and ``runs``.
Nothing here holds logic — each group's module does — so that adding a command
never means editing the place the entry point is declared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from scout_careers.cli import company as company_cli
from scout_careers.cli import runs as runs_cli
from scout_careers.cli import seed as seed_cli
from scout_careers.cli.output import echo, error
from scout_careers.common.config import get_settings
from scout_careers.common.logging import configure_logging
from scout_careers.sources.registry import verify_registry

#: ``backend/`` in a source checkout: the directory holding ``alembic.ini``.
BACKEND_ROOT = Path(__file__).resolve().parents[3]

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Scout Careers — a personal job-search agent. Discovery, registry, runs.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")

app.add_typer(db_app, name="db")
app.add_typer(company_cli.app, name="company")
app.add_typer(seed_cli.app, name="seed")
app.add_typer(runs_cli.run_app, name="run")
app.add_typer(runs_cli.runs_app, name="runs")


def alembic_config(config_path: Path | None = None) -> AlembicConfig:
    """Build the Alembic configuration.

    ``script_location`` is set to an absolute path rather than left to the
    ``alembic.ini`` value, which is relative and therefore depends on the
    current working directory — a migration that only runs from one directory is
    a migration that does not run in a container.

    Args:
        config_path: An explicit ``alembic.ini``; the checkout's when omitted.

    Returns:
        A configured ``AlembicConfig``.

    Raises:
        FileNotFoundError: When no ``alembic.ini`` can be found.
    """
    resolved = config_path or (BACKEND_ROOT / "alembic.ini")
    if not resolved.exists():
        raise FileNotFoundError(
            f"no alembic.ini at {resolved}; pass --config from a source checkout"
        )
    config = AlembicConfig(str(resolved))
    config.set_main_option("script_location", str(resolved.parent / "migrations"))
    return config


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Option(help="Target revision.")] = "head",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to alembic.ini.")
    ] = None,
) -> None:
    """Apply migrations up to a revision (``head`` by default)."""
    settings = get_settings()
    configure_logging(settings)
    try:
        config = alembic_config(config_path)
    except FileNotFoundError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    alembic_command.upgrade(config, revision)
    echo(f"Database is at {revision}.")


@db_app.command("downgrade")
def db_downgrade(
    revision: Annotated[str, typer.Argument(help="Target revision, e.g. base or -1.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to alembic.ini.")
    ] = None,
) -> None:
    """Roll migrations back. Written for development; forward-only in operation."""
    settings = get_settings()
    configure_logging(settings)
    try:
        config = alembic_config(config_path)
    except FileNotFoundError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    alembic_command.downgrade(config, revision)
    echo(f"Database is at {revision}.")


@app.command("check")
def check() -> None:
    """Verify configuration and the adapter registry without touching the network.

    The boot assertion, on demand: a missing adapter is a boot failure, not a
    runtime 500 (``sources/registry.py``).
    """
    settings = get_settings()
    verify_registry()
    echo(f"Configuration valid for {settings.scout_env}; every Phase 1 adapter is registered.")
    if not settings.user_agent_is_attributable:
        echo(
            "Warning: SOURCE_USER_AGENT still carries the .env.example placeholder. "
            "Replace it with a real contact address before running against employers."
        )


def main() -> None:
    """Console-script entry point."""
    app()


__all__ = ["BACKEND_ROOT", "alembic_config", "app", "main"]
