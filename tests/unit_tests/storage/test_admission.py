import hashlib
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest
from pydantic import JsonValue
from pydantic import TypeAdapter

from money_pit.claims.repository import ClaimNotFoundError
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import ClaimResolutionKind
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.schemas.research import ResearchStageAdmission
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import StageArtifactRecord
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.storage.admission import IntelligenceAdmissionError
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.admission import InterpretationAdmission
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.research_semantics import ResearchSemanticPayloadError
from money_pit.storage.research_semantics import canonical_research_context
from money_pit.storage.research_semantics import canonical_research_payload
from money_pit.storage.runs import RunRepository
from money_pit.storage.sources import SourceRepository


if TYPE_CHECKING:
    import sqlite3


_NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
_RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"


def _run(stage: str) -> RunRecord:
    return RunRecord(
        run_id=_RUN_ID,
        requested_as_of=_NOW,
        started_at=_NOW,
        known_at=_NOW,
        through_stage=stage,
        source_config_hash="a" * 64,
        intelligence_config_hash=None if stage == "A1" else "b" * 64,
    )


def _artifact(
    stage: str,
    output_ids: tuple[str, ...],
    *,
    input_ids: tuple[str, ...] = (),
    payload: JsonValue | None = None,
) -> StageArtifactRecord:
    return StageArtifactRecord.from_payload(
        run_id=_RUN_ID,
        stage=stage,
        requested_as_of=_NOW,
        started_at=_NOW,
        decision_at=_NOW,
        known_at=_NOW,
        input_ids=input_ids,
        output_ids=output_ids,
        implementation_version="test-1",
        payload={} if payload is None else payload,
    )


def _observation(
    observation_id: str,
    *,
    source_item_id: str = "source-item",
    evidence_fragment_ids: tuple[str, ...] = ("fragment-1",),
) -> ClaimObservation:
    return ClaimObservation(
        observation_id=observation_id,
        claim_text=f"Claim {observation_id}",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.FUNDAMENTAL,
        source_item_id=source_item_id,
        evidence_fragment_ids=evidence_fragment_ids,
        asserted_at=_NOW,
        known_at=_NOW,
        effective_from=_NOW,
        horizon_class=HorizonClass.TACTICAL,
    )


def _prepare_a1(database: Database) -> None:
    RunRepository(database).append_run(_run("A1"))
    definition = SourceDefinition(
        source_id="source",
        adapter_name="manual",
        locator="manual:source",
        provenance_group="source-group",
        allowed_uses=(AllowedUse.INTERPRETATION,),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.COMMENTARY,
            ),
        ),
    )
    sources = SourceRepository(database)
    _ = sources.register_definition(definition, registry_version="0.0.2", registered_at=_NOW)
    definition_hash = sources.definition_hash(definition)
    _ = sources.persist_discovery(
        "source",
        (
            SourceItem(
                source_item_id="source-item",
                source_id="source",
                source_definition_hash=definition_hash,
                canonical_uri="manual:source-item",
                discovered_at=_NOW,
                content_version="content-1",
            ),
        ),
        next_cursor=None,
        updated_at=_NOW,
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "INSERT INTO evidence_assets VALUES (?, ?, ?, '{}')",
            ("a" * 64, "a" * 64, "assets/a"),
        )
        _ = connection.execute(
            """
            INSERT INTO evidence_asset_acquisitions VALUES (
                'acquisition-1', ?, 'source-item', 'content-1', ?, ?, 'text/plain'
            )
            """,
            ("a" * 64, definition_hash, _NOW.isoformat()),
        )
        _ = connection.execute(
            """
            INSERT INTO evidence_fragments (
                fragment_id, asset_id, fragment_kind, locator_json, extraction_method
            ) VALUES ('fragment-1', ?, 'web_span', '{}', 'manual')
            """,
            ("a" * 64,),
        )
        _ = connection.execute(
            """
            INSERT INTO claim_interpretation_attempts (
                attempt_id, source_item_id, content_version, asset_id, run_id,
                interpreter_version, started_at, outcome, observation_ids_json
            ) VALUES ('attempt-1', 'source-item', 'content-1', ?, ?, 'agent-1', ?, 'pending', '[]')
            """,
            ("a" * 64, _RUN_ID, _NOW.isoformat()),
        )


def _prepare_second_asset_attempt(database: Database) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        definition_row = cast(
            "sqlite3.Row | None",
            connection.execute(
                """SELECT source_definition_hash FROM source_items
                WHERE source_item_id = 'source-item' AND content_version = 'content-1'"""
            ).fetchone(),
        )
        if definition_row is None:
            raise AssertionError("A1 source item fixture was not prepared.")
        definition_hash = str(cast("object", definition_row[0]))
        _ = connection.execute(
            "INSERT INTO evidence_assets VALUES (?, ?, ?, '{}')",
            ("b" * 64, "b" * 64, "assets/b"),
        )
        _ = connection.execute(
            """
            INSERT INTO evidence_asset_acquisitions VALUES (
                'acquisition-2', ?, 'source-item', 'content-1', ?, ?, 'text/plain'
            )
            """,
            ("b" * 64, definition_hash, _NOW.isoformat()),
        )
        _ = connection.execute(
            """
            INSERT INTO evidence_fragments (
                fragment_id, asset_id, fragment_kind, locator_json, extraction_method
            ) VALUES ('fragment-2', ?, 'web_span', '{}', 'manual')
            """,
            ("b" * 64,),
        )
        _ = connection.execute(
            """
            INSERT INTO claim_interpretation_attempts (
                attempt_id, source_item_id, content_version, asset_id, run_id,
                interpreter_version, started_at, outcome, observation_ids_json
            ) VALUES ('attempt-2', 'source-item', 'content-1', ?, ?, 'agent-1', ?, 'pending', '[]')
            """,
            ("b" * 64, _RUN_ID, _NOW.isoformat()),
        )


def test_admit_interpretation_rolls_back_all_records_on_late_failure(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _prepare_a1(database)
    first = _observation("observation-1")
    invalid = _observation("observation-2", source_item_id="another-source")

    with pytest.raises(IntelligenceAdmissionError):
        IntelligenceAdmissionRepository(database).admit_interpretation(
            (InterpretationAdmission(attempt_id="attempt-1", observations=(first, invalid)),),
            completed_at=_NOW,
            known_at=_NOW,
            artifact=_artifact(
                "A1",
                (
                    bind_artifact_record(ArtifactRecordKind.INTERPRETATION_ATTEMPT, "attempt-1"),
                    bind_artifact_record(ArtifactRecordKind.OBSERVATION, first.observation_id),
                    bind_artifact_record(ArtifactRecordKind.OBSERVATION, invalid.observation_id),
                ),
            ),
        )

    with database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claim_observations").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT outcome FROM claim_interpretation_attempts WHERE attempt_id = 'attempt-1'"
            ).fetchone()[0]
            == "pending"
        )
        assert connection.execute("SELECT COUNT(*) FROM stage_artifacts").fetchone()[0] == 0


def test_admit_research_interpretation_commits_without_stage_artifact(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _prepare_a1(database)
    observation = _observation("observation-1")

    IntelligenceAdmissionRepository(database).admit_research_interpretation(
        InterpretationAdmission(attempt_id="attempt-1", observations=(observation,)),
        completed_at=_NOW,
        known_at=_NOW,
        run_id=_RUN_ID,
    )

    with database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claim_observations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT outcome FROM claim_interpretation_attempts WHERE attempt_id = 'attempt-1'"
            ).fetchone()[0]
            == "succeeded"
        )
        assert connection.execute("SELECT COUNT(*) FROM stage_artifacts").fetchone()[0] == 0


def test_admit_interpretation_atomically_succeeds_for_distinct_assets_of_one_source_item(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    _prepare_a1(database)
    _prepare_second_asset_attempt(database)
    observations = (
        _observation("observation-1"),
        _observation("observation-2", evidence_fragment_ids=("fragment-2",)),
    )
    admissions = tuple(
        InterpretationAdmission(attempt_id=f"attempt-{index}", observations=(observation,))
        for index, observation in enumerate(observations, start=1)
    )

    IntelligenceAdmissionRepository(database).admit_interpretation(
        admissions,
        completed_at=_NOW,
        known_at=_NOW,
        artifact=_artifact(
            "A1",
            (
                *(
                    bind_artifact_record(ArtifactRecordKind.INTERPRETATION_ATTEMPT, item.attempt_id)
                    for item in admissions
                ),
                *(bind_artifact_record(ArtifactRecordKind.OBSERVATION, item.observation_id) for item in observations),
            ),
        ),
    )

    with database.transaction() as connection:
        attempt_rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT asset_id, outcome, observation_ids_json
                FROM claim_interpretation_attempts ORDER BY asset_id"""
            ).fetchall(),
        )
        observation_rows = cast(
            "list[sqlite3.Row]",
            connection.execute("SELECT observation_id FROM claim_observations ORDER BY observation_id").fetchall(),
        )
    assert (
        tuple(
            (
                str(cast("object", row[0])),
                str(cast("object", row[1])),
                str(cast("object", row[2])),
            )
            for row in attempt_rows
        ),
        tuple(str(cast("object", row[0])) for row in observation_rows),
    ) == (
        (
            ("a" * 64, "succeeded", '["observation-1"]'),
            ("b" * 64, "succeeded", '["observation-2"]'),
        ),
        ("observation-1", "observation-2"),
    )


def test_admit_synthesis_rolls_back_resolution_when_later_verification_fails(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    RunRepository(database).append_run(_run("A4"))
    observation = _observation("observation-1")
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """
            INSERT INTO claim_observations (
                observation_id, claim_text, source_item_id, asserted_at, known_at,
                effective_from, horizon_class, observation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.observation_id,
                observation.claim_text,
                observation.source_item_id,
                _NOW.isoformat(),
                _NOW.isoformat(),
                _NOW.isoformat(),
                observation.horizon_class.value,
                observation.model_dump_json(),
            ),
        )
    resolution = ClaimResolutionDecision(
        decision_id="resolution-1",
        subject_observation_id=observation.observation_id,
        relation=ClaimResolutionKind.DISTINCT,
        decided_at=_NOW,
        known_at=_NOW,
        resolver_version="resolver-1",
        rationale="No candidate represented the same statement.",
    )
    invalid_verification = VerificationResult(
        verification_id="verification-1",
        observation_id="missing-observation",
        status=VerificationStatus.UNRESOLVED,
        checked_at=_NOW,
        known_at=_NOW,
        verifier_version="verifier-1",
    )

    with pytest.raises(ClaimNotFoundError):
        IntelligenceAdmissionRepository(database).admit_synthesis(
            resolutions=(resolution,),
            verifications=(invalid_verification,),
            revisions=(),
            contributions=(),
            artifact=_artifact(
                "A4",
                (
                    bind_artifact_record(ArtifactRecordKind.CLAIM_RESOLUTION, resolution.decision_id),
                    bind_artifact_record(
                        ArtifactRecordKind.VERIFICATION,
                        invalid_verification.verification_id,
                    ),
                ),
            ),
        )

    with database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claim_resolution_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM stage_artifacts").fetchone()[0] == 0


def test_admit_research_stage_atomically_binds_terminal_staged_records(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    RunRepository(database).append_run(_run("A3"))
    candidate_summary: dict[str, object] = {
        "candidate_thesis_id": "candidate-1",
        "session_id": "session-1",
        "rounds": [],
        "stop_reason": "unresolved",
        "planner_requests": [],
        "planner_responses": [],
    }
    stored_summary: dict[str, object] = {
        "candidate": candidate_summary,
        "contexts": [
            {
                "context_marker": "round-1",
                "evidence": [{"alias": "E000001", "text": "evidence"}],
                "alias_bindings": [{"alias": "E000001", "fragment_ids": ["fragment-1"]}],
            }
        ],
        "alias_bindings": [{"alias": "E000001", "fragment_ids": ["fragment-1"]}],
    }
    summary_json = json.dumps(stored_summary, sort_keys=True, separators=(",", ":"))
    summary_hash = hashlib.sha256(summary_json.encode()).hexdigest()
    research_payload = canonical_research_payload((summary_json,))
    assert research_payload["contexts"] == [
        {
            "context_marker": "round-1",
            "evidence": [{"alias": "E000001", "text": "evidence"}],
        }
    ]
    payload_json = json.dumps(research_payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO candidate_theses VALUES
            ('candidate-1', 'open', ?, ?, '{}')""",
            (_NOW.isoformat(), _NOW.isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO research_sessions (
                session_id, run_id, scope_kind, scope_subject_id, started_at, deadline_at,
                maximum_rounds, maximum_queries, maximum_fetches, status, stop_reason,
                session_json
            ) VALUES ('session-1', ?, 'candidate_thesis', 'candidate-1', ?, ?,
                      3, 12, 24, 'stopped', 'unresolved', '{}')""",
            (_RUN_ID, _NOW.isoformat(), (_NOW + timedelta(minutes=10)).isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO research_tasks VALUES
            ('task-1', 'session-1', 1, 'provider', 'query', 'completed', ?, '{}')""",
            (_NOW.isoformat(),),
        )
        _ = connection.execute(
            """INSERT INTO planned_research_tasks (
                task_id, candidate_thesis_id, status, created_at, known_at,
                materialized_session_id, materialized_task_id, completed_at, run_id, task_json
            ) VALUES ('planned-1', 'candidate-1', 'completed', ?, ?, 'session-1',
                      'task-1', ?, ?, '{}')""",
            (_NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat(), _RUN_ID),
        )
        _ = connection.execute(
            """INSERT INTO research_stop_events VALUES
            ('stop-1', 'session-1', 'unresolved', ?, '{}')""",
            (_NOW.isoformat(),),
        )
        _ = connection.execute(
            """INSERT INTO research_candidate_summaries VALUES
            ('summary-1', 'session-1', ?, ?, ?, ?)""",
            (_RUN_ID, _NOW.isoformat(), summary_hash, summary_json),
        )
    provisional = ResearchStageAdmission(
        run_id=_RUN_ID,
        known_at=_NOW,
        payload_hash=payload_hash,
        semantic_context_hash="0" * 64,
        candidate_thesis_ids=("candidate-1",),
        session_ids=("session-1",),
        summary_ids=("summary-1",),
        planned_task_ids=("planned-1",),
        task_ids=("task-1",),
        stop_event_ids=("stop-1",),
    )
    with database.transaction() as connection:
        semantic_hash = hashlib.sha256(canonical_research_context(connection, provisional).encode()).hexdigest()
    admission = provisional.model_copy(update={"semantic_context_hash": semantic_hash})
    artifact = _artifact(
        "A3",
        admission.output_ids(),
        input_ids=admission.input_ids(),
        payload=research_payload,
    )

    repository = IntelligenceAdmissionRepository(database)
    repository.admit_research_stage(
        admission,
        artifact=artifact,
    )

    assert repository.research_stage_admission(_RUN_ID) == admission
    with database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM stage_artifacts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_stage_admissions").fetchone()[0] == 1


def test_canonical_research_payload_reindexes_aliases_across_candidate_summaries() -> None:
    def summary(candidate_id: str, fragment_id: str) -> str:
        return json.dumps(
            {
                "candidate": {"candidate_thesis_id": candidate_id},
                "contexts": [{"evidence": [{"alias": "E010001", "text": candidate_id}]}],
                "alias_bindings": [
                    {
                        "alias": "E010001",
                        "fragment_ids": [fragment_id],
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    payload = canonical_research_payload(
        (summary("candidate-1", "fragment-1"), summary("candidate-2", "fragment-2")),
    )

    typed_payload = TypeAdapter(dict[str, JsonValue]).validate_python(payload)
    contexts = TypeAdapter(list[dict[str, JsonValue]]).validate_python(typed_payload["contexts"])
    bindings = TypeAdapter(list[dict[str, JsonValue]]).validate_python(typed_payload["alias_bindings"])
    context_aliases = tuple(
        TypeAdapter(list[dict[str, JsonValue]]).validate_python(context["evidence"])[0]["alias"] for context in contexts
    )

    assert context_aliases == ("E000001", "E000002")
    assert [binding["alias"] for binding in bindings] == ["E000001", "E000002"]


def test_canonical_research_payload_rejects_an_unowned_alias_binding() -> None:
    encoded = json.dumps(
        {
            "candidate": {"candidate_thesis_id": "candidate-1"},
            "contexts": [{"evidence": []}],
            "alias_bindings": [{"alias": "E010001", "fragment_ids": ["fragment-1"]}],
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(ResearchSemanticPayloadError):
        _ = canonical_research_payload((encoded,))
