"""The suite must stay offline: the autouse guard in conftest blocks sockets."""

import socket

import pytest
from conftest import NetworkAccessError


def test_outbound_connections_are_blocked() -> None:
    """Any socket connection attempt fails fast instead of reaching out."""
    with pytest.raises(NetworkAccessError):
        socket.create_connection(("example.invalid", 80))
    with socket.socket() as sock, pytest.raises(NetworkAccessError):
        sock.connect(("127.0.0.1", 9))


def test_dns_lookups_are_blocked() -> None:
    """Name resolution is refused too, so no lookup leaks out."""
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo("example.invalid", 443)
