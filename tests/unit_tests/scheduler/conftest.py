"""Fixtures shared across the scheduler unit tests (channel new-episode detection)."""

from pathlib import Path

import pytest
from pytest import FixtureRequest


_SCHEDULER_DATA_SUBPATH: tuple[str, ...] = ("scheduler",)
_CHANNEL_FEED_NAME: str = "channel_feed.xml"


@pytest.fixture
def scheduler_data_folder(request: FixtureRequest, data_folder: Path) -> Path:
    return getattr(request, "param", data_folder.joinpath(*_SCHEDULER_DATA_SUBPATH))


@pytest.fixture
def channel_feed_path(request: FixtureRequest, scheduler_data_folder: Path) -> Path:
    return getattr(request, "param", scheduler_data_folder / _CHANNEL_FEED_NAME)


@pytest.fixture
def channel_feed_xml(request: FixtureRequest, channel_feed_path: Path) -> str:
    return getattr(request, "param", channel_feed_path.read_text(encoding="utf-8"))
