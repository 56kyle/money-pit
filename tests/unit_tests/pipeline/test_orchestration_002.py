from datetime import UTC
from datetime import datetime
from pathlib import Path

from money_pit.graph.state import PipelineState
from money_pit.graph.state import completed_with
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.orchestration import HarnessNodes
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.schemas.runs import RunRecord
from money_pit.storage.database import Database
from money_pit.storage.runs import RunRepository


_RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
_STARTED_AT = datetime(2026, 8, 9, 14, tzinfo=UTC)


def _record(*, through: Stage) -> RunRecord:
    return RunRecord(
        run_id=_RUN_ID,
        requested_as_of=_STARTED_AT,
        started_at=_STARTED_AT,
        known_at=_STARTED_AT,
        through_stage=through.value,
        source_config_hash="a" * 64,
        intelligence_config_hash=None if through is Stage.A1 else "b" * 64,
        portfolio_config_hash="c" * 64 if through in {Stage.A5, Stage.A6} else None,
        execution_config_hash="d" * 64 if through is Stage.A6 else None,
    )


def _state(tmp_path: Path) -> PipelineState:
    return {
        "run_id": _RUN_ID,
        "run_dir": str(tmp_path / "runs" / _RUN_ID),
        "requested_as_of": _STARTED_AT,
        "run_started_at": _STARTED_AT,
        "completed_stages": (),
    }


class _CompleteA1:
    def __call__(self, state: PipelineState) -> PipelineState:
        return {"completed_stages": completed_with(state, Stage.A1.value)}


class _MustNotRun:
    def __call__(self, state: PipelineState) -> PipelineState:
        del state
        raise AssertionError("A non-replay stage received capability during replay")


class _ReplayLoader:
    def __call__(self, state: PipelineState) -> PipelineState:
        del state
        return {"replay": True, "artifact_ids": ("artifact-1",)}


def test_run_pipeline_uses_the_exact_registered_invocation_record(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    repository = RunRepository(database)
    record = _record(through=Stage.A1)
    repository.append_run(record)

    _ = run_pipeline(
        _state(tmp_path),
        nodes=HarnessNodes(
            a1=_CompleteA1(),
            a2=_MustNotRun(),
            a3=_MustNotRun(),
            a4=_MustNotRun(),
            a5=None,
            a6=None,
        ),
        through=Stage.A1,
        run_record=record,
    )

    assert repository.get_run(record.run_id) == record


def test_run_pipeline_replay_invokes_only_the_read_only_loader(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    repository = RunRepository(database)
    record = _record(through=Stage.A5)
    repository.append_run(record)

    result = run_pipeline(
        _state(tmp_path),
        nodes=HarnessNodes(
            a1=_MustNotRun(),
            a2=_MustNotRun(),
            a3=_MustNotRun(),
            a4=_MustNotRun(),
            a5=_MustNotRun(),
            a6=None,
            replay=_ReplayLoader(),
        ),
        through=Stage.A5,
        replay=True,
        run_record=record,
    )

    assert (result.get("replay"), result.get("artifact_ids")) == (True, ("artifact-1",))
