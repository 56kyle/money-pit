"""Tests for reachable operational paths in the portfolio-intelligence CLI."""

from pathlib import Path

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

from money_pit import __main__
from money_pit.config import Config
from money_pit.runs.paths import RepositoryPaths
from money_pit.scheduler.runner import RunLatestOutcome
from money_pit.scheduler.runner import RunLatestResult
from money_pit.schemas.execution_policy import ExecutionMode


def _config() -> Config:
    return Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=True,
        execution_mode=ExecutionMode.OBSERVE,
    )


def test_ingest_command_delegates_to_provenance_pipeline(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config().model_copy(update={"ingest_cache_dir": tmp_path / "cache"})
    expected_path = tmp_path / "cache" / "signals" / "yt_evidence.json"
    monkeypatch.setattr(__main__, "load_config", lambda: config)
    monkeypatch.setattr(__main__, "production_seams", lambda _config: object())
    monkeypatch.setattr(__main__, "make_video_llm_agent", lambda _config: object())
    monkeypatch.setattr(
        __main__,
        "_ingest_to_signal_file",
        lambda *_args, **_kwargs: expected_path,
    )

    result = CliRunner().invoke(
        __main__.app,
        ["ingest", "https://youtu.be/evidence"],
    )

    assert result.exit_code == 0
    assert str(expected_path) in result.output


def test__execution_authority_repository_initializes_database(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    initialized: list[Path] = []
    sentinel = object()

    class FakeDatabase:
        def __init__(self, path: Path) -> None:
            self.path = path

        def initialize(self) -> None:
            initialized.append(self.path)

    paths = RepositoryPaths.from_data_root(tmp_path)
    monkeypatch.setattr(__main__.RepositoryPaths, "from_data_root", lambda: paths)
    monkeypatch.setattr(__main__, "Database", FakeDatabase)
    monkeypatch.setattr(
        __main__,
        "SqliteExecutionAuthorityRepository",
        lambda _database: sentinel,
    )

    result = __main__._execution_authority_repository()

    assert result is sentinel
    assert initialized == [paths.database_path]


def test_run_latest_with_scheduler_error_exits_one(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(__main__, "load_config", _config)

    def fail(*_args: object, **_kwargs: object) -> RunLatestResult:
        raise __main__.SchedulerConfigError("channel absent")

    monkeypatch.setattr(__main__, "run_latest_once", fail)

    result = CliRunner().invoke(__main__.app, ["run-latest"])

    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (RunLatestOutcome.SKIPPED, "No new episode"),
        (RunLatestOutcome.RAN, "Processed"),
    ],
)
def test_run_latest_reports_outcome_and_exercises_live_seams(
    monkeypatch: MonkeyPatch,
    outcome: RunLatestOutcome,
    expected: str,
) -> None:
    monkeypatch.setattr(__main__, "load_config", _config)
    monkeypatch.setattr(__main__, "make_requests_http_get", lambda: object())
    monkeypatch.setattr(
        __main__,
        "fetch_latest_video_id",
        lambda channel_id, _http_get: f"video-{channel_id}",
    )
    monkeypatch.setattr(
        __main__,
        "_run_url",
        lambda _url, _config, *, through: {"slug": "run-1"} if through is None else {},
    )

    def run_once(
        _config_value: Config,
        *,
        read_latest: object,
        run_url: object,
        ledger_path: Path,
        now: object,
    ) -> RunLatestResult:
        _ = ledger_path
        video_id: str = read_latest("channel")  # type: ignore[operator]
        _ = run_url(f"https://youtu.be/{video_id}")  # type: ignore[operator]
        _ = now()  # type: ignore[operator]
        return RunLatestResult(outcome, f"yt:{video_id}", "run-1")

    monkeypatch.setattr(__main__, "run_latest_once", run_once)

    result = CliRunner().invoke(__main__.app, ["run-latest"])

    assert result.exit_code == 0
    assert expected in result.output
