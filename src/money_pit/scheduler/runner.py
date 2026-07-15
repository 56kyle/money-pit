"""Module containing the idempotent latest-episode orchestration core for the money_pit package."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from money_pit.config import Config
from money_pit.graph.state import PipelineState
from money_pit.scheduler.ledger import load_processed_ids
from money_pit.scheduler.ledger import record_processed


_WATCH_URL_TEMPLATE: str = "https://www.youtube.com/watch?v={video_id}"
_SOURCE_ID_TEMPLATE: str = "yt:{video_id}"


class SchedulerConfigError(Exception):
    """Raised when youtube_channel_id is not configured, so the scheduler fails closed instead of running against an unknown channel."""


class RunLatestOutcome(str, Enum):
    SKIPPED = "SKIPPED"
    RAN = "RAN"


@dataclass(frozen=True)
class RunLatestResult:
    """The outcome of a single latest-episode poll, reported by the CLI."""

    outcome: RunLatestOutcome
    source_id: str
    slug: str | None


LatestReader: TypeAlias = Callable[[str], str]
UrlRunner: TypeAlias = Callable[[str], PipelineState]
Clock: TypeAlias = Callable[[], str]


def run_latest_once(
    config: Config,
    *,
    read_latest: LatestReader,
    run_url: UrlRunner,
    ledger_path: Path,
    now: Clock,
) -> RunLatestResult:
    """Process the channel's latest episode once, skipping when the ledger already records it.

    The ledger is written only after run_url returns, so a raising run_url propagates and leaves the
    episode unrecorded for the next poll to retry.
    """
    channel_id: str | None = config.youtube_channel_id
    if channel_id is None:
        raise SchedulerConfigError("youtube_channel_id is not set (MONEY_PIT__YOUTUBE_CHANNEL_ID)")

    video_id: str = read_latest(channel_id)
    source_id: str = _SOURCE_ID_TEMPLATE.format(video_id=video_id)

    if source_id in load_processed_ids(ledger_path):
        return RunLatestResult(RunLatestOutcome.SKIPPED, source_id, None)

    url: str = _WATCH_URL_TEMPLATE.format(video_id=video_id)
    state: PipelineState = run_url(url)
    slug: str | None = state.get("slug")
    record_processed(ledger_path, source_id, slug or "", processed_at=now())
    return RunLatestResult(RunLatestOutcome.RAN, source_id, slug)
