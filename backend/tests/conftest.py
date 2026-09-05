"""Test harness guarantees.

Two of them, both autouse:

- **No test may touch the network.** ``socket.connect`` / ``create_connection``
  / ``getaddrinfo`` are patched to raise. ``respx`` intercepts at the httpx
  transport layer, well above this, so mocked requests still work — but a call
  that escapes the mock fails loudly instead of quietly reaching an employer.
- **No test may read the operator's ``.env``.** ``make_settings`` passes
  ``_env_file=None``, so a stray ``.env`` on a developer's machine cannot change
  a test result.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from scout_careers.common.config import Settings


class NetworkAccessDenied(RuntimeError):
    """Raised when a test tries to open a real socket."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def _deny(*_args: object, **_kwargs: object) -> None:
        raise NetworkAccessDenied("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(socket, "getaddrinfo", _deny)
    yield


BASE_ENV: dict[str, object] = {
    "postgres_password": "test-password",
    "database_url": "postgresql+asyncpg://scout:scout@localhost:5432/scout",
    "redis_url": "redis://localhost:6379/0",
    "_env_file": None,
}


def make_settings(**overrides: object) -> Settings:
    """Build a Settings object from an explicit dict, ignoring any .env on disk.

    Args:
        **overrides: Field values to override.

    Returns:
        A validated Settings instance.
    """
    values = {**BASE_ENV, **overrides}
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    """Default Phase 1 settings for a test."""
    return make_settings()
