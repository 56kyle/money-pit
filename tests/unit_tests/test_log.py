"""Tests for money_pit.log file-sink configuration."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from money_pit import log


@pytest.fixture(autouse=True)
def _clear_log_caches() -> Iterator[None]:
    log.log_path.cache_clear()
    log.configure_file_logging.cache_clear()
    yield
    log.log_path.cache_clear()
    log.configure_file_logging.cache_clear()


@pytest.fixture
def tmp_log_folder(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    monkeypatch.setattr(log, "user_log_folder", lambda: tmp_path)
    return tmp_path


def test_log_path(tmp_log_folder: Path) -> None:
    path = log.log_path()
    assert path.parent == tmp_log_folder
    assert log.log_path() is path


def test_configure_file_logging_registers_sink_once(tmp_log_folder: Path, monkeypatch: MonkeyPatch) -> None:
    added_sinks: list[Path] = []
    monkeypatch.setattr(log.logger, "add", lambda sink, **_: added_sinks.append(sink) or len(added_sinks))
    first = log.configure_file_logging()
    second = log.configure_file_logging()
    assert first == second
    assert first.parent == tmp_log_folder
    assert len(added_sinks) == 1
