"""The models and the migrations must describe the same database.

They are written by hand, separately, and nothing but this file makes them
agree. The failure mode is quiet and expensive: SQLAlchemy emits a SELECT naming
every column on the model, so one column that exists in ``models.py`` and not in
the migration takes out every query against that table — and it does so at
runtime, against a database that migrated cleanly, with an error naming a column
the operator never heard of.

This is a static comparison. It parses the migration's ``op.create_table`` calls
rather than connecting to Postgres, so it runs in the offline unit gate where it
will actually be seen, instead of in the integration tests that are skipped on
every laptop without a database.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scout_careers.db.base import Base

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _migration_tables() -> dict[str, set[str]]:
    """Return ``table -> column names`` as the migrations create them.

    Reads the AST rather than importing: a migration module imports ``alembic.op``
    bound to a live context, and executing one outside a migration run is not a
    thing it supports.
    """
    tables: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "create_table"):
                continue
            if not (node.args and isinstance(node.args[0], ast.Constant)):
                continue
            table = str(node.args[0].value)
            columns: set[str] = set()
            for arg in node.args[1:]:
                if not isinstance(arg, ast.Call):
                    continue
                func = arg.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                # sa.Column("x", ...) directly, or a local helper whose first
                # positional argument is the column name (this revision has one
                # for timestamps).
                if name in {"Column", "_timestamptz"} and arg.args:
                    first = arg.args[0]
                    if isinstance(first, ast.Constant):
                        columns.add(str(first.value))
            tables[table] = columns
    return tables


@pytest.fixture(scope="module")
def migration_tables() -> dict[str, set[str]]:
    return _migration_tables()


def test_the_migrations_were_actually_parsed(migration_tables) -> None:
    """A parser that silently matches nothing would make every test below pass."""
    assert "company" in migration_tables
    assert "job_posting" in migration_tables
    assert {"resume_variant", "requirement", "match_score"} <= set(migration_tables)
    assert len(migration_tables["company"]) > 10


def test_every_model_table_is_created_by_a_migration(migration_tables) -> None:
    missing = sorted(set(Base.metadata.tables) - set(migration_tables))
    assert missing == [], f"models with no CREATE TABLE: {missing}"


@pytest.mark.parametrize(
    "table_name",
    sorted(Base.metadata.tables),
)
def test_model_columns_match_the_migration(table_name: str, migration_tables) -> None:
    """Both directions.

    A model column absent from the migration breaks every SELECT on the table.
    A migration column absent from the model is dead weight the ORM will never
    read or write, which is how a documented field quietly never gets populated.
    """
    model_columns = {column.name for column in Base.metadata.tables[table_name].columns}
    migration_columns = migration_tables[table_name]

    assert model_columns - migration_columns == set(), (
        f"{table_name}: on the model, never created — every query on this table "
        f"would fail: {sorted(model_columns - migration_columns)}"
    )
    assert migration_columns - model_columns == set(), (
        f"{table_name}: created, but no model reads it: {sorted(migration_columns - model_columns)}"
    )


def test_the_requirement_text_column_keeps_its_documented_name() -> None:
    """The attribute is ``text_``; the column must still be ``text``.

    ``text`` is SQLAlchemy's own function, imported at module scope in
    ``models.py``, so a class attribute of that name would shadow it. The
    workaround is invisible from the database's side and must stay that way —
    DATA_MODEL.md §4.2 calls the column ``text``.
    """
    columns = {column.name for column in Base.metadata.tables["requirement"].columns}
    assert "text" in columns
    assert "text_" not in columns
