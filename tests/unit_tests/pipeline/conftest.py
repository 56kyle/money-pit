"""Shared fixtures for pipeline unit tests."""

from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple

import pytest
from loguru import logger

if TYPE_CHECKING:
    from loguru import Message


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
