from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from money_pit.schemas.runs import RunFailureDetail
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import RunTerminalEvent
from money_pit.schemas.runs import RunTerminalStatus
from money_pit.schemas.runs import StageArtifactRecord
from money_pit.storage.database import Database
from money_pit.storage.runs import ImmutableRunCollisionError
from money_pit.storage.runs import InvalidRunTerminalEventError
from money_pit.storage.runs import InvalidStageArtifactError
from money_pit.storage.runs import RunRepository


_RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
_STARTED_AT = datetime(2026, 8, 9, 14, tzinfo=UTC)


def _run() -> RunRecord:
    return RunRecord(
        run_id=_RUN_ID,
        requested_as_of=_STARTED_AT,
        started_at=_STARTED_AT,
        known_at=_STARTED_AT,
        through_stage="A4",
        source_config_hash="a" * 64,
        intelligence_config_hash="b" * 64,
    )


def _repository(tmp_path: Path) -> RunRepository:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    repository = RunRepository(database)
    repository.append_run(_run())
    return repository


def _completed_event(*, completed_at: datetime | None = None) -> RunTerminalEvent:
    actual = completed_at or _STARTED_AT + timedelta(minutes=1)
    return RunTerminalEvent(
        run_id=_RUN_ID,
        status=RunTerminalStatus.COMPLETED,
        completed_at=actual,
        known_at=actual,
    )


def _artifact(
    *,
    requested_as_of: datetime = _STARTED_AT,
    started_at: datetime = _STARTED_AT,
    stage: str = "A4",
) -> StageArtifactRecord:
    return StageArtifactRecord.from_payload(
        run_id=_RUN_ID,
        stage=stage,
        requested_as_of=requested_as_of,
        started_at=started_at,
        decision_at=started_at,
        known_at=started_at,
        input_ids=(),
        output_ids=(),
        implementation_version="test-1",
        payload={},
    )


def test_append_terminal_event_is_exact_and_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    event = _completed_event()

    repository.append_terminal_event(event)
    repository.append_terminal_event(event)

    assert repository.terminal_event_for_run(_RUN_ID) == event


def test_recent_intelligence_runs_rehydrates_complete_run_record(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    recent = repository.recent_intelligence_runs(limit=1)

    assert len(recent) == 1
    assert recent[0].run_id == _RUN_ID


def test_append_terminal_event_rejects_different_second_outcome(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append_terminal_event(_completed_event())
    different = _completed_event(completed_at=_STARTED_AT + timedelta(minutes=2))

    with pytest.raises(ImmutableRunCollisionError):
        repository.append_terminal_event(different)


def test_append_terminal_event_rejects_completion_before_start(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(InvalidRunTerminalEventError):
        repository.append_terminal_event(_completed_event(completed_at=_STARTED_AT - timedelta(seconds=1)))


@pytest.mark.parametrize(
    "artifact",
    [
        _artifact(requested_as_of=_STARTED_AT - timedelta(days=1)),
        _artifact(started_at=_STARTED_AT - timedelta(seconds=1)),
        _artifact(stage="A5"),
    ],
)
def test_append_stage_artifact_rejects_run_boundary_mismatch(
    tmp_path: Path,
    artifact: StageArtifactRecord,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(InvalidStageArtifactError):
        repository.append_stage_artifact(artifact)


def test_append_stage_artifact_rejects_terminal_run(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append_terminal_event(_completed_event())

    with pytest.raises(InvalidStageArtifactError):
        repository.append_stage_artifact(_artifact())


def test_run_terminal_event_requires_bounded_structured_failure_data() -> None:
    failed_at = _STARTED_AT + timedelta(minutes=1)
    event = RunTerminalEvent(
        run_id=_RUN_ID,
        status=RunTerminalStatus.FAILED,
        completed_at=failed_at,
        known_at=failed_at,
        failure_kind="ResearchBudgetExpired",
        failure_detail=RunFailureDetail(
            stage="A3",
            durable_record_ids=("session-1", "task-2"),
            retryable=True,
        ),
    )

    assert event.failure_detail is not None
    assert event.failure_detail.durable_record_ids == ("session-1", "task-2")


def test_run_terminal_event_rejects_missing_failure_detail() -> None:
    failed_at = _STARTED_AT + timedelta(minutes=1)

    with pytest.raises(ValidationError):
        _ = RunTerminalEvent(
            run_id=_RUN_ID,
            status=RunTerminalStatus.FAILED,
            completed_at=failed_at,
            known_at=failed_at,
            failure_kind="ResearchBudgetExpired",
        )


@pytest.mark.parametrize(
    ("through_stage", "intelligence", "portfolio", "execution"),
    [
        ("A1", None, None, None),
        ("A2", "b" * 64, None, None),
        ("A4", "b" * 64, None, None),
        ("A5", "b" * 64, "c" * 64, None),
        ("A6", "b" * 64, "c" * 64, "d" * 64),
    ],
)
def test_run_record_accepts_exact_stage_applicable_config_bindings(
    through_stage: str,
    intelligence: str | None,
    portfolio: str | None,
    execution: str | None,
) -> None:
    values = _run().model_dump()
    values.update(
        through_stage=through_stage,
        intelligence_config_hash=intelligence,
        portfolio_config_hash=portfolio,
        execution_config_hash=execution,
    )

    record = RunRecord.model_validate(values)

    assert record.through_stage == through_stage


@pytest.mark.parametrize(
    ("through_stage", "intelligence", "portfolio", "execution"),
    [
        ("A1", "b" * 64, None, None),
        ("A2", None, None, None),
        ("A4", "b" * 64, "c" * 64, None),
        ("A5", "b" * 64, None, None),
        ("A5", "b" * 64, "c" * 64, "d" * 64),
        ("A6", "b" * 64, "c" * 64, None),
    ],
)
def test_run_record_rejects_missing_or_later_stage_config_bindings(
    through_stage: str,
    intelligence: str | None,
    portfolio: str | None,
    execution: str | None,
) -> None:
    values = _run().model_dump()
    values.update(
        through_stage=through_stage,
        intelligence_config_hash=intelligence,
        portfolio_config_hash=portfolio,
        execution_config_hash=execution,
    )

    with pytest.raises(ValidationError):
        _ = RunRecord.model_validate(values)
