from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from money_pit.graph.state import PipelineState
from money_pit.graph.state import completed_with
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.orchestration import HarnessNodes
from money_pit.pipeline.orchestration import IntelligenceNodes
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.pipeline.orchestration import run_intelligence_update
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.schemas.runs import RunRecord
from money_pit.storage.database import Database
from money_pit.storage.intelligence_work import DiscoveryUnitKind
from money_pit.storage.intelligence_work import DiscoveryUnitRecord
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import InterpretationBundleRecord
from money_pit.storage.intelligence_work import InterpretationChunkSpec
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


class _CompleteStage:
    def __init__(self, stage: Stage) -> None:
        self._stage: Stage = stage

    def __call__(self, state: PipelineState) -> PipelineState:
        return {"completed_stages": completed_with(state, self._stage.value)}


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


def test_run_intelligence_update_reports_exact_completed_work_delta(tmp_path: Path) -> None:
    database = Database(tmp_path / "incremental.sqlite3")
    database.initialize()
    repository = RunRepository(database)
    work = IntelligenceWorkRepository(database)
    record = _record(through=Stage.A1)
    repository.append_run(record)
    with database.transaction() as connection:
        _ = connection.execute(
            """INSERT INTO source_definition_revisions (
                definition_hash, source_id, registry_version, provenance_group,
                definition_json, registered_at
            ) VALUES ('definition', 'source-a', 'test', 'commentary', '{}', ?)""",
            (_STARTED_AT.isoformat(),),
        )
        _ = connection.execute(
            """INSERT INTO source_items (
                source_item_id, content_version, source_id, source_definition_hash,
                canonical_uri, discovered_at
            ) VALUES ('item-1', 'version-1', 'source-a', 'definition',
                'https://source.test/item-1', ?)""",
            (_STARTED_AT.isoformat(),),
        )
    work.ensure_interpretation_bundle(
        InterpretationBundleRecord(
            bundle_id="bundle-1",
            source_item_id="item-1",
            content_version="version-1",
            interpreter_version="test",
            input_fingerprint="a" * 64,
            created_at=_STARTED_AT,
            payload={},
        ),
        (
            InterpretationChunkSpec(
                chunk_id="chunk-1",
                chunk_number=0,
                input_fingerprint="b" * 64,
                payload={},
            ),
        ),
    )
    work.append_discovery_unit(
        DiscoveryUnitRecord(
            unit_id="completed-by-run",
            kind=DiscoveryUnitKind.UNIVERSE_ENTRY,
            subject_id="AAA",
            input_fingerprint="c" * 64,
            source_id=None,
            created_at=_STARTED_AT,
            payload={"instrument": "AAA"},
        ),
        (),
    )
    batch = work.claim_discovery_batch(
        batch_id="batch-1",
        run_id=record.run_id,
        created_at=_STARTED_AT,
        reclaim_before=_STARTED_AT - timedelta(minutes=1),
        maximum_units=1,
    )
    assert batch is not None
    work.complete_discovery_batch(
        batch_id=batch.batch_id,
        run_id=record.run_id,
        completed_at=_STARTED_AT,
        result_fingerprint="d" * 64,
        output_candidate_ids=(),
    )
    work.append_discovery_unit(
        DiscoveryUnitRecord(
            unit_id="concurrently-queued",
            kind=DiscoveryUnitKind.UNIVERSE_ENTRY,
            subject_id="BBB",
            input_fingerprint="e" * 64,
            source_id=None,
            created_at=_STARTED_AT,
            payload={"instrument": "BBB"},
        ),
        (),
    )

    report = run_intelligence_update(
        _state(tmp_path),
        nodes=IntelligenceNodes(a1=_CompleteStage(Stage.A1)),
        through=IntelligenceStage.INTERPRETATION,
        run_record=record,
        work=work,
    )

    assert report.completed.interpretation_chunks == 0
    assert report.completed.discovery_units == 1
    assert report.remaining.pending_interpretation_chunks == 1
    assert report.remaining.pending_discovery_units == 1
