"""Unit tests for money_pit.scheduler.runner — the idempotent latest-episode orchestration core.

Pins run_latest_once against injected fake seams (read_latest / run_url / now) and a real tmp-dir ledger:
the fail-closed SchedulerConfigError when no channel is set, the ledger-driven SKIPPED path that never
touches run_url, the RAN path that records the returned slug, and record-only-on-success under a raising
run_url. Fakes are plain call-recording callables (no mocking framework); assertions target enum identity
and exception TYPES.
"""

from pathlib import Path

import pytest
from pytest import FixtureRequest

from money_pit.config import Config
from money_pit.graph.state import PipelineState
from money_pit.scheduler.ledger import ProcessedEpisode
from money_pit.scheduler.ledger import _load_records
from money_pit.scheduler.ledger import load_processed_ids
from money_pit.scheduler.ledger import record_processed
from money_pit.scheduler.runner import RunLatestOutcome
from money_pit.scheduler.runner import RunLatestResult
from money_pit.scheduler.runner import SchedulerConfigError
from money_pit.scheduler.runner import run_latest_once


_CHANNEL_ID: str = "UCtest123"
_NOW: str = "2026-06-18T14:30:05Z"


class RecordingReader:
    """A LatestReader stand-in recording the channel ids it is called with and returning a fixed video id."""

    def __init__(self, video_id: str) -> None:
        self.video_id: str = video_id
        self.calls: list[str] = []

    def __call__(self, channel_id: str) -> str:
        self.calls.append(channel_id)
        return self.video_id


class RecordingRunner:
    """A UrlRunner stand-in recording the urls it is called with and returning a fixed PipelineState."""

    def __init__(self, state: PipelineState) -> None:
        self.state: PipelineState = state
        self.calls: list[str] = []

    def __call__(self, url: str) -> PipelineState:
        self.calls.append(url)
        return self.state


class RaisingRunner:
    """A UrlRunner stand-in recording its calls then raising, to pin record-only-on-success."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> PipelineState:
        self.calls.append(url)
        raise RuntimeError("pipeline blew up")


@pytest.fixture
def config__youtube_channel_id(request: FixtureRequest) -> str | None:
    return getattr(request, "param", _CHANNEL_ID)


@pytest.fixture
def config(request: FixtureRequest, config__youtube_channel_id: str | None) -> Config:
    return getattr(
        request,
        "param",
        Config(
            alpaca_service="alpaca-paper",
            alpaca_username="alpaca-api-key",
            alpaca_paper=True,
            youtube_channel_id=config__youtube_channel_id,
        ),
    )


@pytest.fixture
def ledger_path(request: FixtureRequest, tmp_path: Path) -> Path:
    return getattr(request, "param", tmp_path / "scheduler" / "ledger.json")


@pytest.mark.parametrize("config__youtube_channel_id", [None], indirect=True)
def test_run_latest_once_with_channel_unset(config: Config, ledger_path: Path) -> None:
    read_latest: RecordingReader = RecordingReader("vid123")
    run_url: RecordingRunner = RecordingRunner({"slug": "unused"})

    with pytest.raises(SchedulerConfigError):
        _ = run_latest_once(
            config, read_latest=read_latest, run_url=run_url, ledger_path=ledger_path, now=lambda: _NOW
        )

    assert read_latest.calls == []
    assert run_url.calls == []


def test_run_latest_once_with_already_processed(config: Config, ledger_path: Path) -> None:
    record_processed(ledger_path, "yt:vid123", "slug", processed_at="t")
    read_latest: RecordingReader = RecordingReader("vid123")
    run_url: RecordingRunner = RecordingRunner({"slug": "unused"})

    result: RunLatestResult = run_latest_once(
        config, read_latest=read_latest, run_url=run_url, ledger_path=ledger_path, now=lambda: _NOW
    )

    assert result.outcome is RunLatestOutcome.SKIPPED
    assert result.source_id == "yt:vid123"
    assert result.slug is None
    assert run_url.calls == []


def test_run_latest_once_with_new_episode(config: Config, ledger_path: Path) -> None:
    read_latest: RecordingReader = RecordingReader("vidNEW")
    run_url: RecordingRunner = RecordingRunner({"slug": "2026-06-18_14-30-00"})

    result: RunLatestResult = run_latest_once(
        config, read_latest=read_latest, run_url=run_url, ledger_path=ledger_path, now=lambda: _NOW
    )

    assert run_url.calls == ["https://www.youtube.com/watch?v=vidNEW"]
    assert result.outcome is RunLatestOutcome.RAN
    assert result.source_id == "yt:vidNEW"
    assert result.slug == "2026-06-18_14-30-00"

    assert load_processed_ids(ledger_path) == {"yt:vidNEW"}
    recorded: ProcessedEpisode = _sole_record(ledger_path)
    assert recorded.source_id == "yt:vidNEW"
    assert recorded.slug == "2026-06-18_14-30-00"
    assert recorded.processed_at == _NOW


def test_run_latest_once_with_raising_run_url(config: Config, ledger_path: Path) -> None:
    read_latest: RecordingReader = RecordingReader("vidNEW")
    run_url: RaisingRunner = RaisingRunner()

    with pytest.raises(RuntimeError):
        _ = run_latest_once(
            config, read_latest=read_latest, run_url=run_url, ledger_path=ledger_path, now=lambda: _NOW
        )

    assert run_url.calls == ["https://www.youtube.com/watch?v=vidNEW"]
    assert load_processed_ids(ledger_path) == set()


def _sole_record(path: Path) -> ProcessedEpisode:
    """Return the single ledger record at `path`, asserting exactly one was written."""
    records: list[ProcessedEpisode] = _load_records(path)
    assert len(records) == 1
    return records[0]
