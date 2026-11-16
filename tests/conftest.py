"""Shared pytest configuration.

The suite must run in a restricted, offline environment: no network, no
browser/CDN runtime, and no database or cache services. The autouse guard
below fails any test that tries to open a socket connection, so a stray
network call shows up as a test failure instead of a hang or a silent
dependency on connectivity.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest


class NetworkAccessError(RuntimeError):
    """Raised when a test attempts a network connection."""


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    """Refuse every outbound connection attempt.

    Raises:
        NetworkAccessError: Always.
    """
    raise NetworkAccessError("tests must not touch the network")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block socket connections and DNS lookups for every test.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Iterator yielding once while the guard is active.
    """
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    yield
