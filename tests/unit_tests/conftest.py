"""Fixtures used in unit tests."""

import os
from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from pytest import MonkeyPatch

from money_pit.config import ENV_PREFIX


@pytest.fixture(autouse=True)
def _clear_money_pit_env(monkeypatch: MonkeyPatch) -> None:
    """Strip ambient MONEY_PIT__* vars so config-constructing unit tests assert code defaults, not a real environment.

    Config is a BaseSettings, so any Config(...) built in a unit test would otherwise read a developer's live
    MONEY_PIT__* env for the fields it does not pass explicitly. Clearing them pins tests to the single-source defaults.
    """
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)


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
