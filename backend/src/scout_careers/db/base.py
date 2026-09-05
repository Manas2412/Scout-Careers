"""Declarative base and shared column mixins.

The naming convention matters more than it looks: Alembic autogenerate can only
emit a correct ``DROP CONSTRAINT`` if the constraint has a name it can predict,
and an unnamed constraint created by one Postgres version is not necessarily
named the same by the next.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import MetaData, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    """Declarative base for every Scout Careers model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at`` / ``updated_at``, both server-side defaults.

    ``updated_at`` is given a server default here and is maintained by an
    ``ON UPDATE`` trigger in the migration, so a raw SQL repair statement
    cannot leave a stale timestamp behind (DATA_MODEL.md §1).
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["NAMING_CONVENTION", "Base", "TimestampMixin"]
