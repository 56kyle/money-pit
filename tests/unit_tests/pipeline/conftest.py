"""Shared fixtures for pipeline unit tests."""

from collections.abc import Iterator

import pytest
from loguru import logger


@pytest.fixture
def loguru_warnings() -> Iterator[list[str]]:
    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)
