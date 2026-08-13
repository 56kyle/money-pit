import json
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit.agents.inference import InferenceCallRecord
from money_pit.agents.inference import InferenceCorrelation
from money_pit.agents.inference import InferenceUsage
from money_pit.composition import _reconcile_holding_discovery_units  # pyright: ignore[reportPrivateUsage]
from money_pit.composition import _reconcile_universe_discovery_units  # pyright: ignore[reportPrivateUsage]
from money_pit.composition import intelligence_work_status
from money_pit.config import ConfigurationScope
from money_pit.config import load_application_config
from money_pit.pipeline.research import _recover_wave_checkpoint  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStatePosition
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.intelligence_work import DiscoveryUnitKind
from money_pit.storage.intelligence_work import DiscoveryUnitRecord
from money_pit.storage.intelligence_work import IncrementalResearchAdmissionRecord
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import InterpretationBundleRecord
from money_pit.storage.intelligence_work import InterpretationChunkSpec
from money_pit.storage.intelligence_work import ResearchCheckpointRecord
from money_pit.storage.intelligence_work import ResearchJobRecord
from money_pit.storage.intelligence_work import ResearchUriAdmissionRecord
from money_pit.storage.intelligence_work import ResearchWaveResultRecord
from money_pit.storage.intelligence_work import SynthesisOutputRecord
from money_pit.storage.intelligence_work import SynthesisUnitRecord
from money_pit.storage.intelligence_work import UriDisposition
from money_pit.storage.intelligence_work import WorkTransitionError


if TYPE_CHECKING:
    import sqlite3


_NOW = datetime(2026, 8, 12, 20, tzinfo=UTC)
_FINGERPRINT = "a" * 64


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _seed_runs_and_candidates(database)
    return database


@pytest.fixture
def repository(database: Database) -> IntelligenceWorkRepository:
    return IntelligenceWorkRepository(database)


def _seed_runs_and_candidates(database: Database) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        for run_id in ("run-1", "run-2"):
            _ = connection.execute(
                """INSERT INTO runs (
                    run_id, requested_as_of, started_at, known_at, through_stage,
                    source_config_hash, intelligence_config_hash, manifest_json
                ) VALUES (?, ?, ?, ?, 'A4', 'sources', 'intelligence', '{}')""",
                (run_id, _NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat()),
            )
        for index in range(1, 5):
            _ = connection.execute(
                """INSERT INTO candidate_theses (
                    candidate_thesis_id, status, created_at, known_at, candidate_json
                ) VALUES (?, 'open', ?, ?, '{}')""",
                (f"candidate-{index}", _NOW.isoformat(), _NOW.isoformat()),
            )
        _ = connection.execute(
            """INSERT INTO evidence_assets (asset_id, content_hash, local_path, metadata_json)
            VALUES (?, ?, 'bb/test-asset', '{}')""",
            ("b" * 64, "b" * 64),
        )
        _ = connection.execute(
            """INSERT INTO source_definition_revisions (
                definition_hash, source_id, registry_version, provenance_group,
                definition_json, registered_at
            ) VALUES ('definition', 'source-a', 'test', 'commentary', '{}', ?)""",
            (_NOW.isoformat(),),
        )
        _ = connection.execute(
            """INSERT INTO source_items (
                source_item_id, content_version, source_id, source_definition_hash,
                canonical_uri, discovered_at
            ) VALUES ('video-1', 'version-1', 'source-a', 'definition',
                'https://video.test/1', ?)""",
            (_NOW.isoformat(),),
        )


def _discovery_unit(identifier: str, *, source_id: str) -> DiscoveryUnitRecord:
    return DiscoveryUnitRecord(
        unit_id=identifier,
        kind=DiscoveryUnitKind.SOURCE_BUNDLE,
        subject_id=f"subject-{identifier}",
        input_fingerprint=_FINGERPRINT,
        source_id=source_id,
        created_at=_NOW,
        payload={"observation_ids": [f"observation-{identifier}"]},
    )


def _research_job(index: int, *, source_unit_id: str | None = None) -> ResearchJobRecord:
    return ResearchJobRecord(
        job_id=f"job-{index}",
        candidate_thesis_id=f"candidate-{index}",
        premise_fingerprint=f"{index}" * 64,
        source_discovery_unit_id=source_unit_id,
        created_at=_NOW + timedelta(seconds=index),
        payload={"premises": [f"premise-{index}"]},
    )


def _seed_research_session(database: Database, *, session_id: str = "session-1") -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO research_sessions (
                session_id, run_id, scope_kind, scope_subject_id, started_at,
                deadline_at, maximum_rounds, maximum_queries, maximum_fetches,
                status, session_json
            ) VALUES (?, 'run-1', 'candidate_thesis', 'candidate-1', ?, ?, 3, 6, 12,
                'active', '{}')""",
            (session_id, _NOW.isoformat(), (_NOW + timedelta(minutes=10)).isoformat()),
        )


def test_interpretation_bundle_resumes_at_first_unfinished_prompt_chunk(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_interpretation_bundle(
        InterpretationBundleRecord(
            bundle_id="bundle-1",
            source_item_id="video-1",
            content_version="version-1",
            interpreter_version="interpretation-v2",
            input_fingerprint=_FINGERPRINT,
            created_at=_NOW,
            payload={"asset_ids": [f"asset-{index}" for index in range(25)]},
        ),
        (
            InterpretationChunkSpec(
                chunk_id="chunk-1",
                chunk_number=0,
                input_fingerprint="1" * 64,
                payload={"asset_count": 20},
            ),
            InterpretationChunkSpec(
                chunk_id="chunk-2",
                chunk_number=1,
                input_fingerprint="2" * 64,
                payload={"asset_count": 5},
            ),
        ),
    )
    first = repository.claim_interpretation_chunk(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
        source_id="source-a",
    )
    assert first is not None
    repository.complete_interpretation_chunk(
        chunk_id=first.chunk_id,
        run_id="run-1",
        completed_at=_NOW,
        output_attempt_ids=("attempt-1",),
    )

    resumed = repository.claim_interpretation_chunk(
        run_id="run-2",
        claimed_at=_NOW + timedelta(minutes=1),
        reclaim_before=_NOW + timedelta(seconds=30),
        source_id="source-a",
    )

    assert resumed is not None
    assert (first.chunk_id, resumed.chunk_id) == ("chunk-1", "chunk-2")


def test_complete_discovery_batch_with_empty_output_prevents_replay(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.append_discovery_unit(_discovery_unit("unit-a", source_id="source-a"), ())
    batch = repository.claim_discovery_batch(
        batch_id="batch-a",
        run_id="run-1",
        created_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
        maximum_units=32,
        source_id="source-a",
    )
    assert batch is not None
    repository.complete_discovery_batch(
        batch_id=batch.batch_id,
        run_id="run-1",
        completed_at=_NOW,
        result_fingerprint=_FINGERPRINT,
        output_candidate_ids=(),
    )

    replay = repository.claim_discovery_batch(
        batch_id="batch-replay",
        run_id="run-2",
        created_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
        maximum_units=32,
        source_id="source-a",
    )

    assert replay is None


def test_claim_discovery_batch_with_source_excludes_global_backlog(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.append_discovery_unit(_discovery_unit("unit-a", source_id="source-a"), ())
    repository.append_discovery_unit(_discovery_unit("unit-b", source_id="source-b"), ())

    batch = repository.claim_discovery_batch(
        batch_id="batch-a",
        run_id="run-1",
        created_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
        maximum_units=32,
        source_id="source-a",
    )

    assert batch is not None
    assert batch.unit_ids == ("unit-a",)


def test_claim_research_jobs_reclaims_exact_bounded_jobs_before_new_work(
    repository: IntelligenceWorkRepository,
) -> None:
    for index in range(1, 4):
        repository.ensure_research_job(_research_job(index))
    first_claim = repository.claim_research_jobs(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )

    resumed = repository.claim_research_jobs(
        run_id="run-2",
        claimed_at=_NOW + timedelta(minutes=1),
        reclaim_before=_NOW + timedelta(seconds=30),
    )

    assert (
        tuple(item.job_id for item in first_claim),
        tuple(item.job_id for item in resumed),
        tuple(item.claimed_run_id for item in resumed),
    ) == (("job-1", "job-2"), ("job-1", "job-2"), ("run-2", "run-2"))


def test_append_research_checkpoint_preserves_lifetime_counters_across_resume(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    _ = repository.claim_research_jobs(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )
    repository.append_research_checkpoint(
        ResearchCheckpointRecord(
            checkpoint_id="checkpoint-1",
            job_id="job-1",
            run_id="run-1",
            wave_number=1,
            search_count=2,
            accepted_fetch_count=4,
            recorded_at=_NOW,
            digest={"remaining_work": ["premise-2"]},
        )
    )

    resumed = repository.claim_research_jobs(
        run_id="run-2",
        claimed_at=_NOW + timedelta(minutes=1),
        reclaim_before=_NOW + timedelta(seconds=30),
    )

    assert (resumed[0].wave_count, resumed[0].search_count, resumed[0].accepted_fetch_count) == (1, 2, 4)


def test__recover_wave_checkpoint_reconciles_durable_outbox_without_replaying_provider_work(
    database: Database,
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    _seed_research_session(database)
    claimed = repository.claim_research_jobs(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )[0]
    repository.record_research_wave_result(
        ResearchWaveResultRecord(
            wave_result_id="wave-1",
            job_id=claimed.job_id,
            session_id="session-1",
            run_id="run-1",
            wave_number=1,
            recorded_at=_NOW,
            execution={
                "task_count": 1,
                "query_count": 2,
                "fetch_count": 3,
                "independent_provenance_groups": ["issuer-primary"],
                "failure_kinds": [],
            },
            context=None,
            search_count_delta=2,
            accepted_fetch_count_delta=3,
        )
    )
    resumed = repository.claim_research_jobs(
        run_id="run-2",
        claimed_at=_NOW + timedelta(minutes=1),
        reclaim_before=_NOW + timedelta(seconds=30),
    )[0]

    _recover_wave_checkpoint(repository, resumed)
    _recover_wave_checkpoint(repository, resumed)

    checkpoint = repository.latest_research_checkpoint("job-1")
    assert checkpoint is not None
    assert (
        checkpoint.run_id,
        checkpoint.wave_number,
        checkpoint.search_count,
        checkpoint.accepted_fetch_count,
        repository.uncheckpointed_wave("job-1"),
    ) == ("run-1", 1, 2, 3, None)


def test_admit_incremental_research_preserves_prior_run_child_ownership(
    database: Database,
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    _seed_research_session(database)
    _ = repository.claim_research_jobs(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )
    repository.record_research_wave_result(
        ResearchWaveResultRecord(
            wave_result_id="wave-1",
            job_id="job-1",
            session_id="session-1",
            run_id="run-1",
            wave_number=1,
            recorded_at=_NOW,
            execution={"task_count": 0, "query_count": 0, "fetch_count": 0},
            context=None,
            search_count_delta=0,
            accepted_fetch_count_delta=0,
        )
    )
    repository.checkpoint_research_wave(
        wave_result_id="wave-1",
        checkpoint=ResearchCheckpointRecord(
            checkpoint_id="checkpoint-1",
            job_id="job-1",
            run_id="run-1",
            wave_number=1,
            search_count=0,
            accepted_fetch_count=0,
            recorded_at=_NOW,
            digest={},
        ),
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO stage_artifacts (
                artifact_id, run_id, stage, requested_as_of, started_at, decision_at,
                known_at, input_ids_json, output_ids_json, implementation_version,
                payload_hash, payload_json
            ) VALUES ('artifact-a3', 'run-2', 'A3', ?, ?, ?, ?, '[]', '[]',
                'test', ?, '{}')""",
            (
                _NOW.isoformat(),
                _NOW.isoformat(),
                _NOW.isoformat(),
                _NOW.isoformat(),
                _FINGERPRINT,
            ),
        )

    repository.admit_incremental_research(
        IncrementalResearchAdmissionRecord(
            admission_id="admission-1",
            run_id="run-2",
            artifact_id="artifact-a3",
            known_at=_NOW,
            input_job_ids=("job-1",),
            input_wave_result_ids=("wave-1",),
            input_checkpoint_ids=("checkpoint-1",),
            output_record_ids=(),
            payload={},
        )
    )

    with database.transaction() as connection:
        ownership = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT job.claimed_run_id, wave.run_id, checkpoint.run_id, admission.run_id
                FROM research_jobs AS job
                JOIN research_wave_results AS wave USING (job_id)
                JOIN research_job_checkpoints AS checkpoint USING (job_id)
                JOIN incremental_research_admissions AS admission
                    ON admission.admission_id = 'admission-1'
                WHERE job.job_id = 'job-1'"""
            ).fetchone(),
        )
    assert tuple(ownership) == ("run-1", "run-1", "run-1", "run-2")


def test__recover_wave_checkpoint_does_not_count_failed_fetch_as_accepted(
    database: Database,
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    _seed_research_session(database)
    claimed = repository.claim_research_jobs(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )[0]
    repository.record_research_wave_result(
        ResearchWaveResultRecord(
            wave_result_id="failed-fetch-wave",
            job_id=claimed.job_id,
            session_id="session-1",
            run_id="run-1",
            wave_number=1,
            recorded_at=_NOW,
            execution={
                "task_count": 1,
                "query_count": 1,
                "fetch_count": 1,
                "failure_kinds": ["ProviderFetchError"],
            },
            context=None,
            search_count_delta=1,
            accepted_fetch_count_delta=0,
        )
    )

    _recover_wave_checkpoint(repository, claimed)

    checkpoint = repository.latest_research_checkpoint("job-1")
    assert checkpoint is not None
    assert checkpoint.accepted_fetch_count == 0


def test_claim_research_jobs_selects_fresh_job_when_material_premise_changes(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    _ = repository.claim_research_jobs(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )
    repository.finalize_research_job(
        job_id="job-1",
        completed_at=_NOW,
        stop_reason="evidence_standard_satisfied",
    )
    repository.ensure_research_job(
        ResearchJobRecord(
            job_id="job-1-changed-premise",
            candidate_thesis_id="candidate-1",
            premise_fingerprint="f" * 64,
            created_at=_NOW + timedelta(minutes=1),
            payload={"premises": ["materially changed premise"]},
        )
    )

    claimed = repository.claim_research_jobs(
        run_id="run-2",
        claimed_at=_NOW + timedelta(minutes=1),
        reclaim_before=_NOW,
    )

    assert tuple((item.job_id, item.premise_fingerprint, item.wave_count) for item in claimed) == (
        ("job-1-changed-premise", "f" * 64, 0),
    )


def test_claim_research_jobs_reactivates_only_terminal_jobs_due_for_review(
    repository: IntelligenceWorkRepository,
    database: Database,
) -> None:
    repository.append_discovery_unit(_discovery_unit("unit-a", source_id="source-a"), ())
    immutable_tasks = [
        {
            "provider": "edgar",
            "query": "issuer backlog",
            "purpose": "verify material premise",
            "maximum_results": 2,
        }
    ]
    repository.ensure_research_job(
        _research_job(1, source_unit_id="unit-a").model_copy(
            update={"payload": {"premises": ["premise-1"], "research_tasks": immutable_tasks}}
        ),
        ("unit-a",),
    )
    repository.ensure_research_job(_research_job(2))
    _ = repository.claim_research_jobs(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
        maximum_jobs=2,
    )
    repository.append_research_checkpoint(
        ResearchCheckpointRecord(
            checkpoint_id="checkpoint-due-parent",
            job_id="job-1",
            run_id="run-1",
            wave_number=1,
            search_count=2,
            accepted_fetch_count=4,
            recorded_at=_NOW,
            digest={"remaining_work": []},
        )
    )
    review_trigger_at = _NOW + timedelta(minutes=1)
    repository.finalize_research_job(
        job_id="job-1",
        completed_at=_NOW,
        stop_reason="review_scheduled",
        next_review_at=review_trigger_at,
    )
    repository.finalize_research_job(
        job_id="job-2",
        completed_at=_NOW,
        stop_reason="review_scheduled",
        next_review_at=_NOW + timedelta(hours=1),
    )
    due_status = repository.status("source-a")

    claimed = repository.claim_research_jobs(
        run_id="run-2",
        claimed_at=_NOW + timedelta(minutes=2),
        reclaim_before=_NOW + timedelta(minutes=1),
        maximum_jobs=2,
        source_id="source-a",
    )
    reclaimed = repository.claim_research_jobs(
        run_id="run-2",
        claimed_at=_NOW + timedelta(minutes=2),
        reclaim_before=_NOW + timedelta(minutes=1),
        maximum_jobs=2,
        source_id="source-a",
    )
    with database.transaction() as connection:
        parent = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT status, wave_count, search_count, accepted_fetch_count, next_review_at
                FROM research_jobs WHERE job_id = 'job-1'"""
            ).fetchone(),
        )
        future = cast(
            "sqlite3.Row",
            connection.execute("SELECT status, next_review_at FROM research_jobs WHERE job_id = 'job-2'").fetchone(),
        )

    successor = claimed[0]
    assert (
        successor.job_id != "job-1",
        successor.parent_job_id,
        successor.cycle_number,
        successor.review_trigger_at,
        successor.source_discovery_unit_id,
        successor.wave_count,
        successor.search_count,
        successor.accepted_fetch_count,
        successor.payload,
        due_status.due_research_reviews,
        tuple(item.job_id for item in reclaimed),
        tuple(parent),
        tuple(future),
    ) == (
        True,
        "job-1",
        1,
        review_trigger_at,
        "unit-a",
        0,
        0,
        0,
        {"premises": ["premise-1"], "research_tasks": immutable_tasks},
        1,
        (successor.job_id,),
        ("terminal", 1, 2, 4, None),
        ("terminal", (_NOW + timedelta(hours=1)).isoformat()),
    )


def test_reusable_uri_admission_reuses_asset_across_research_jobs(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    repository.ensure_research_job(_research_job(2))
    repository.append_uri_admission(
        ResearchUriAdmissionRecord(
            admission_id="uri-1",
            job_id="job-1",
            canonical_uri="https://issuer.test/filing",
            disposition=UriDisposition.ACCEPTED,
            provenance_group="issuer-primary",
            consumes_fetch_capacity=True,
            admitted_at=_NOW,
            source_item_id="issuer-filing-v1",
            asset_id="b" * 64,
            content_hash="b" * 64,
            content_version="content-v1",
            source_definition_hash="definition-v1",
            processor_name="web-text",
            processor_version="processor-v1",
            interpretation_model="model-v1",
            interpretation_prompt_version="prompt-v1",
            payload={"context": {"observation_ids": ["observation-1"]}},
        )
    )

    reusable = repository.reusable_uri_admission(
        "https://issuer.test/filing",
        content_hash="b" * 64,
        content_version="content-v1",
        source_definition_hash="definition-v1",
        processor_name="web-text",
        processor_version="processor-v1",
        interpretation_model="model-v1",
        interpretation_prompt_version="prompt-v1",
    )
    wrong_prompt = repository.reusable_uri_admission(
        "https://issuer.test/filing",
        content_hash="b" * 64,
        content_version="content-v1",
        source_definition_hash="definition-v1",
        processor_name="web-text",
        processor_version="processor-v1",
        interpretation_model="model-v1",
        interpretation_prompt_version="prompt-v2",
    )
    changed_bytes = repository.reusable_uri_admission(
        "https://issuer.test/filing",
        content_hash="c" * 64,
        content_version="content-v2",
        source_definition_hash="definition-v1",
        processor_name="web-text",
        processor_version="processor-v1",
        interpretation_model="model-v1",
        interpretation_prompt_version="prompt-v1",
    )
    repository.append_uri_admission(
        ResearchUriAdmissionRecord(
            admission_id="uri-2",
            job_id="job-2",
            canonical_uri="https://issuer.test/filing",
            disposition=UriDisposition.REUSED,
            provenance_group="issuer-primary",
            consumes_fetch_capacity=False,
            admitted_at=_NOW + timedelta(seconds=1),
            source_item_id=reusable.source_item_id if reusable is not None else None,
            asset_id=reusable.asset_id if reusable is not None else None,
            content_hash=reusable.content_hash if reusable is not None else None,
            content_version="content-v1",
            source_definition_hash="definition-v1",
            processor_name="web-text",
            processor_version="processor-v1",
            interpretation_model="model-v1",
            interpretation_prompt_version="prompt-v1",
            payload=reusable.payload if reusable is not None else None,
        )
    )
    reused_for_job = repository.uri_admission_for_job("job-2", "https://issuer.test/filing")

    assert reusable is not None
    assert reused_for_job is not None
    assert (
        reusable.job_id,
        reusable.source_item_id,
        reusable.asset_id,
        reusable.payload,
        reused_for_job.source_item_id,
        wrong_prompt,
        changed_bytes,
    ) == (
        "job-1",
        "issuer-filing-v1",
        "b" * 64,
        {"context": {"observation_ids": ["observation-1"]}},
        "issuer-filing-v1",
        None,
        None,
    )


def test_append_uri_admission_rejects_capacity_charge_for_prefetch_rejection(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    admission = ResearchUriAdmissionRecord(
        admission_id="uri-rejected",
        job_id="job-1",
        canonical_uri="https://unknown.test/article",
        disposition=UriDisposition.REJECTED,
        consumes_fetch_capacity=True,
        admitted_at=_NOW,
        reason="publisher_not_authorized",
        payload={},
    )

    with pytest.raises(WorkTransitionError):
        repository.append_uri_admission(admission)


def test_append_uri_admission_allows_same_job_uri_under_new_processing_identity(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.ensure_research_job(_research_job(1))
    for version in ("content-v1", "content-v2"):
        repository.append_uri_admission(
            ResearchUriAdmissionRecord(
                admission_id=f"uri-{version}",
                job_id="job-1",
                canonical_uri="https://issuer.test/filing",
                disposition=UriDisposition.ACCEPTED,
                provenance_group="issuer-primary",
                consumes_fetch_capacity=True,
                admitted_at=_NOW,
                source_item_id=f"issuer-{version}",
                asset_id="b" * 64,
                content_hash="b" * 64,
                content_version=version,
                source_definition_hash="definition-v1",
                processor_name="web-text",
                processor_version="processor-v1",
                interpretation_model="model-v1",
                interpretation_prompt_version="prompt-v1",
                payload={"content_version": version},
            )
        )

    reusable = repository.reusable_uri_admission(
        "https://issuer.test/filing",
        content_hash="b" * 64,
        content_version="content-v2",
        source_definition_hash="definition-v1",
        processor_name="web-text",
        processor_version="processor-v1",
        interpretation_model="model-v1",
        interpretation_prompt_version="prompt-v1",
    )

    assert reusable is not None
    assert (reusable.source_item_id, reusable.payload) == (
        "issuer-content-v2",
        {"content_version": "content-v2"},
    )


def test_complete_synthesis_unit_checkpoints_one_job_without_replaying_it(
    repository: IntelligenceWorkRepository,
) -> None:
    for index in (1, 2):
        repository.ensure_research_job(_research_job(index))
        repository.ensure_synthesis_unit(
            SynthesisUnitRecord(
                unit_id=f"synthesis-{index}",
                research_job_id=f"job-{index}",
                input_fingerprint=f"{index}" * 64,
                created_at=_NOW + timedelta(seconds=index),
                payload={},
            )
        )
    first = repository.claim_synthesis_unit(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )
    assert first is not None
    repository.complete_synthesis_unit(
        unit_id=first.unit_id,
        run_id="run-1",
        completed_at=_NOW,
        output=SynthesisOutputRecord(
            output_id="output-1",
            output_fingerprint=_FINGERPRINT,
            created_at=_NOW,
            output_record_ids=("revision-1",),
            payload={},
        ),
    )

    second = repository.claim_synthesis_unit(
        run_id="run-2",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )

    assert second is not None
    assert (first.unit_id, second.unit_id) == ("synthesis-1", "synthesis-2")


def test_complete_synthesis_unit_rejects_dropped_required_research_context(
    repository: IntelligenceWorkRepository,
    database: Database,
) -> None:
    repository.ensure_research_job(_research_job(1))
    repository.ensure_synthesis_unit(
        SynthesisUnitRecord(
            unit_id="synthesis-semantic-1",
            research_job_id="job-1",
            input_fingerprint=_FINGERPRINT,
            created_at=_NOW,
            payload={"candidate_id": "candidate-1", "context": {"contexts": [{"required": True}]}},
        )
    )
    claimed = repository.claim_synthesis_unit(
        run_id="run-1",
        claimed_at=_NOW,
        reclaim_before=_NOW - timedelta(minutes=1),
    )
    assert claimed is not None
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "UPDATE synthesis_units SET unit_json = ? WHERE unit_id = ?",
            ('{"candidate_id":"candidate-1","context":{"contexts":[]}}', claimed.unit_id),
        )

    with pytest.raises(WorkTransitionError):
        repository.complete_synthesis_unit(
            unit_id=claimed.unit_id,
            run_id="run-1",
            completed_at=_NOW,
            output=SynthesisOutputRecord(
                output_id="synthesis-output-semantic-1",
                output_fingerprint=_FINGERPRINT,
                created_at=_NOW,
                output_record_ids=("revision-1",),
                payload={},
            ),
        )


def test_usage_for_run_aggregates_cache_retry_and_sanitized_failure(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.record_inference_call(
        InferenceCallRecord(
            stage="A1",
            purpose="interpret_evidence",
            model="test-model",
            request_hash=_FINGERPRINT,
            correlation=InferenceCorrelation(run_id="run-1", work_unit_id="chunk-1"),
            started_at=_NOW,
            completed_at=_NOW + timedelta(seconds=1),
            elapsed_milliseconds=1_000,
            status="succeeded",
            usage=InferenceUsage(
                input_tokens=100,
                cache_write_tokens=20,
                cache_read_tokens=30,
                output_tokens=40,
                request_count=2,
            ),
        )
    )
    repository.record_inference_call(
        InferenceCallRecord(
            stage="A1",
            purpose="interpret_frame",
            model="test-model",
            request_hash="b" * 64,
            correlation=InferenceCorrelation(run_id="run-1", work_unit_id="chunk-1"),
            started_at=_NOW,
            completed_at=_NOW + timedelta(seconds=1),
            elapsed_milliseconds=1_000,
            status="failed",
            usage=InferenceUsage(input_tokens=50, output_tokens=5, request_count=1),
            failure_kind="ValidationError",
        )
    )

    usage = repository.usage_for_run("run-1")

    assert (
        usage.logical_call_count,
        usage.failed_call_count,
        usage.usage,
    ) == (
        2,
        1,
        InferenceUsage(
            input_tokens=150,
            cache_write_tokens=20,
            cache_read_tokens=30,
            output_tokens=45,
            request_count=3,
        ),
    )


def test_usage_for_run_counts_unavailable_provider_usage_without_inventing_tokens(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "unavailable-usage.sqlite3")
    database.initialize()
    _seed_runs_and_candidates(database)
    repository = IntelligenceWorkRepository(database)
    repository.record_inference_call(
        InferenceCallRecord(
            stage="A3",
            purpose="plan_research",
            model="test-model",
            request_hash=_FINGERPRINT,
            correlation=InferenceCorrelation(run_id="run-1", work_unit_id="job-1"),
            status="failed",
            usage=None,
            started_at=_NOW,
            completed_at=_NOW + timedelta(seconds=1),
            elapsed_milliseconds=1_000,
            failure_kind="ModelHTTPError",
        )
    )

    aggregate = repository.usage_for_run("run-1")
    with database.transaction() as connection:
        counters = cast(
            "sqlite3.Row | None",
            connection.execute(
                """SELECT usage_available, input_tokens, cache_read_tokens,
                cache_write_tokens, output_tokens FROM inference_calls"""
            ).fetchone(),
        )

    assert counters is not None
    assert tuple(counters) == (0, None, None, None, None)
    assert (aggregate.unavailable_usage_call_count, aggregate.usage) == (1, InferenceUsage())


def test_status_with_source_reports_only_matching_backlog(
    repository: IntelligenceWorkRepository,
) -> None:
    repository.append_discovery_unit(_discovery_unit("unit-a", source_id="source-a"), ())
    repository.append_discovery_unit(_discovery_unit("unit-b", source_id="source-b"), ())

    status = repository.status("source-a")

    assert (status.source_id, status.pending_discovery_units, status.active_discovery_units) == (
        "source-a",
        1,
        0,
    )


def test__reconcile_universe_discovery_units_queues_each_instrument_once_per_strategy_version(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "universe.sqlite3")
    database.initialize()
    repository = IntelligenceWorkRepository(database)
    config = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    instruments = {
        *config.intelligence.watchlist,
        *config.intelligence.benchmark_constituents,
        *config.intelligence.explicit_proxies,
        *config.intelligence.explicit_proxies.values(),
    }
    _reconcile_universe_discovery_units(config, work=repository, created_at=_NOW)
    _reconcile_universe_discovery_units(config, work=repository, created_at=_NOW)
    revised = replace(
        config,
        intelligence=config.intelligence.model_copy(update={"version": f"{config.intelligence.version}-revised"}),
    )
    _reconcile_universe_discovery_units(revised, work=repository, created_at=_NOW)

    with database.transaction() as connection:
        count_row = cast(
            "sqlite3.Row",
            connection.execute("SELECT count(*) FROM discovery_units WHERE unit_kind = 'universe_entry'").fetchone(),
        )

    assert count_row is not None
    assert cast("int", count_row[0]) == len(instruments) * 2


def test_intelligence_work_status_projects_unmaterialized_configured_universe(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "projected-status.sqlite3")
    database.initialize()
    config = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    instruments = {
        *config.intelligence.watchlist,
        *config.intelligence.benchmark_constituents,
        *config.intelligence.explicit_proxies,
        *config.intelligence.explicit_proxies.values(),
    }

    before = intelligence_work_status(
        database=database,
        config=config,
        source_id=None,
        implementation_version="status-test",
        as_of=_NOW,
    )
    _reconcile_universe_discovery_units(config, work=IntelligenceWorkRepository(database), created_at=_NOW)
    after = intelligence_work_status(
        database=database,
        config=config,
        source_id=None,
        implementation_version="status-test",
        as_of=_NOW,
    )

    assert (before.unmaterialized_discovery_units, after.pending_discovery_units) == (
        len(instruments),
        len(instruments),
    )
    assert after.unmaterialized_discovery_units == 0


def test__reconcile_holding_discovery_units_queues_verified_broker_holdings(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "holdings-universe.sqlite3")
    database.initialize()
    config = load_application_config(scope=ConfigurationScope.CAPITAL)
    snapshot = PortfolioStateSnapshot.from_payload(
        PortfolioStatePayload(
            account_id="paper-account",
            broker_environment=config.require_strategy().portfolio_environment,
            captured_at=_NOW,
            available_cash=1_000,
            open_order_ids=(),
            positions=(
                PortfolioStatePosition(
                    instrument="GOOG",
                    quantity=1,
                    market_price=200,
                    market_value=200,
                ),
            ),
        )
    )
    SnapshotRepository(database).append_portfolio(snapshot)
    repository = IntelligenceWorkRepository(database)

    _reconcile_holding_discovery_units(
        database,
        config=config,
        work=repository,
        created_at=_NOW,
    )
    _reconcile_holding_discovery_units(
        database,
        config=config,
        work=repository,
        created_at=_NOW + timedelta(minutes=1),
    )

    with database.transaction() as connection:
        row = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT subject_id, unit_json,
                    (SELECT count(*) FROM discovery_units
                     WHERE unit_kind = 'universe_entry') AS unit_count
                FROM discovery_units
                WHERE unit_kind = 'universe_entry'"""
            ).fetchone(),
        )
    assert (row["subject_id"], json.loads(cast("str", row["unit_json"])), row["unit_count"]) == (
        "GOOG",
        {
            "holding_state": {
                "account_id": "paper-account",
                "broker_environment": "paper",
                "instrument": "GOOG",
                "universe_layer": "holding",
            },
            "observation_ids": [],
        },
        1,
    )
