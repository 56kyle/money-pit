"""Fixtures used in unit tests."""

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING
from typing import NamedTuple

import keyring
import pytest
from keyring.backend import KeyringBackend
from loguru import logger
from pytest import MonkeyPatch
from typing_extensions import override

from money_pit.config import ENV_PREFIX


if TYPE_CHECKING:
    from loguru import Message


@pytest.fixture(autouse=True)
def _clear_money_pit_env(monkeypatch: MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # Pytest discovers this autouse fixture dynamically.
    """Strip ambient MONEY_PIT__* vars so config-constructing unit tests assert code defaults, not a real environment.

    Config is a BaseSettings, so any Config(...) built in a unit test would otherwise read a developer's live
    MONEY_PIT__* env for the fields it does not pass explicitly. Clearing them pins tests to the single-source defaults.
    """
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)


class InMemoryKeyring(KeyringBackend):
    """A real KeyringBackend whose secrets live in a dict, so credential resolution runs mock-free."""

    priority: float = 1  # pyright: ignore[reportIncompatibleVariableOverride]  # keyring types incorrectly expose this numeric setting as a classproperty.

    def __init__(self) -> None:
        """Create isolated in-memory checkpoint storage for one test."""
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    @override
    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    @override
    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    @override
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


@pytest.fixture
def loguru_warnings() -> Iterator[list[str]]:
    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


class CapturedLog(NamedTuple):
    level: str
    message: str


@pytest.fixture
def loguru_records() -> Iterator[list[CapturedLog]]:
    captured: list[CapturedLog] = []

    def sink(message: "Message") -> None:
        record = message.record
        captured.append(CapturedLog(level=record["level"].name, message=record["message"]))

    sink_id = logger.add(sink, level="DEBUG", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)
