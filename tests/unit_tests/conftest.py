"""Fixtures used in unit tests."""

from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend


class InMemoryKeyring(KeyringBackend):
    """A real KeyringBackend whose secrets live in a dict, so credential resolution runs mock-free."""

    priority = 1  # pyright: ignore[reportAssignmentType]

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self._store[(service, username)]


@pytest.fixture
def in_memory_keyring() -> Iterator[InMemoryKeyring]:
    """Install an in-memory keyring backend for the test, restoring the prior backend on teardown."""
    previous: KeyringBackend = keyring.get_keyring()
    backend: InMemoryKeyring = InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
