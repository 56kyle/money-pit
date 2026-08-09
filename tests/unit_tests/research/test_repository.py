from __future__ import annotations

import hashlib
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from money_pit.portfolio.theses import ThesisRepository
from money_pit.research.errors import ResearchBudgetExceededError
from money_pit.research.errors import ResearchStageRecoveryIncompleteError
from money_pit.research.repository import ResearchRepository
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.research import CandidateThesisResearchScope
from money_pit.schemas.research import ResearchDiscoveryBatch
from money_pit.schemas.research import ResearchDiscoveryResult
from money_pit.schemas.research import ResearchFetch
from money_pit.schemas.research import ResearchFetchStatus
from money_pit.schemas.research import ResearchQuery
from money_pit.schemas.research import ResearchSession
from money_pit.schemas.research import ResearchSessionStatus
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.research import ResearchTask
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.research_semantics import canonical_research_payload


if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue


NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
_TASK_COLLISION_MESSAGE = "Research task identity collision"
_RESULT_COLLISION_MESSAGE = "Research result identity collision"
_FETCH_COLLISION_MESSAGE = "Research fetch identity collision"


def _database_and_repository(tmp_path: Path) -> tuple[Database, ResearchRepository]:
    database = Database(tmp_path / "research.sqlite3")
    database.initialize()
    with database.transaction() as connection:
        _ = connection.execute(
            """
                INSERT INTO runs (
                    run_id, requested_as_of, started_at, known_at, through_stage,
                    source_config_hash, intelligence_config_hash, manifest_json
                ) VALUES (?, ?, ?, ?, 'A3', ?, ?, '{}')
                """,
            (RUN_ID, NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), "a" * 64, "b" * 64),
        )
    ThesisRepository(database).append_candidate(
        CandidateThesis(
            candidate_thesis_id="candidate-1",
            subject="Candidate",
            direction=ThesisDirection.LONG,
            instrument_reference="CANDIDATE",
            horizon_class=HorizonClass.TACTICAL,
            discovery_basis=DiscoveryBasis(source_claim_keys=("claim-1",)),
            created_at=NOW,
            known_at=NOW,
        ),
    )
    return database, ResearchRepository(database)


def _repository(tmp_path: Path) -> ResearchRepository:
    return _database_and_repository(tmp_path)[1]


def _session(
    *,
    session_id: str = "session-1",
    maximum_queries: int = 2,
    maximum_fetches: int = 2,
) -> ResearchSession:
    return ResearchSession(
        session_id=session_id,
        run_id=RUN_ID,
        scope=CandidateThesisResearchScope(candidate_thesis_id="candidate-1"),
        started_at=NOW,
        deadline_at=NOW + timedelta(hours=1),
        maximum_queries=maximum_queries,
        maximum_fetches=maximum_fetches,
    )


def _query(query_text: str = "query") -> ResearchQuery:
    return ResearchQuery(
        provider="brave",
        query_text=query_text,
        candidate_thesis_id="candidate-1",
        purpose="verification",
        requested_at=NOW,
        max_results=2,
    )


def _task(
    *,
    task_id: str = "task-1",
    session_id: str = "session-1",
    query_text: str = "query",
    created_at: datetime = NOW,
) -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        session_id=session_id,
        round_number=1,
        query=_query(query_text),
        created_at=created_at,
    )


def _result(*, result_id: str = "result-1") -> ResearchDiscoveryResult:
    return ResearchDiscoveryResult(
        result_id=result_id,
        provider="brave",
        canonical_uri=f"https://example.test/{result_id}",
        discovered_at=NOW,
        provenance_group="publisher",
    )


def test_reserve_query_consumes_budget_before_provider_io(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session = _session(maximum_queries=1)
    task = _task()
    _ = repository.create_session(session)
    _ = repository.add_task(task)

    repository.reserve_query(task, attempted_at=NOW)

    assert repository.budget_state(session.session_id).query_count == 1


def test_reserve_query_rejects_an_attempt_beyond_the_budget(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session = _session(maximum_queries=1)
    task = _task()
    _ = repository.create_session(session)
    _ = repository.add_task(task)
    repository.reserve_query(task, attempted_at=NOW)

    with pytest.raises(ResearchBudgetExceededError):
        repository.reserve_query(task, attempted_at=NOW)


def test_reserve_fetch_consumes_budget_independently_of_recording(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session = _session(maximum_fetches=1)
    _ = repository.create_session(session)

    repository.reserve_fetch(session.session_id, attempted_at=NOW)

    assert repository.budget_state(session.session_id).fetch_count == 1


def test_add_task_rejects_a_task_identity_collision(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _ = repository.create_session(_session())
    _ = repository.add_task(_task())

    with pytest.raises(ValueError, match=_TASK_COLLISION_MESSAGE):
        _ = repository.add_task(_task(query_text="different query"))


def test_record_search_rejects_a_result_identity_collision(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _ = repository.create_session(_session())
    first_task = _task()
    second_task = _task(task_id="task-2", query_text="other", created_at=NOW + timedelta(seconds=1))
    _ = repository.add_task(first_task)
    _ = repository.add_task(second_task)
    result = _result()
    _ = repository.record_search(
        first_task,
        ResearchDiscoveryBatch(query=first_task.query, results=(result,), searched_at=NOW),
    )

    with pytest.raises(ValueError, match=_RESULT_COLLISION_MESSAGE):
        _ = repository.record_search(
            second_task,
            ResearchDiscoveryBatch(query=second_task.query, results=(result,), searched_at=NOW),
        )


def test_pending_tasks_restores_only_unsearched_work_in_deterministic_order(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _ = repository.create_session(_session())
    later = _task(task_id="task-later", query_text="later", created_at=NOW + timedelta(seconds=1))
    searched = _task(task_id="task-searched", query_text="searched")
    earlier = _task(task_id="task-earlier", query_text="earlier")
    _ = repository.add_task(later)
    _ = repository.add_task(searched)
    _ = repository.add_task(earlier)
    _ = repository.record_search(
        searched,
        ResearchDiscoveryBatch(query=searched.query, results=(), searched_at=NOW),
    )

    pending = repository.pending_tasks("session-1")

    assert tuple(task.task_id for task in pending) == ("task-earlier", "task-later")


def test_record_fetch_is_idempotent_but_rejects_identity_collision(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _ = repository.create_session(_session())
    task = _task()
    _ = repository.add_task(task)
    result = _result()
    _ = repository.record_search(
        task,
        ResearchDiscoveryBatch(query=task.query, results=(result,), searched_at=NOW),
    )
    fetch = ResearchFetch(
        fetch_id="fetch-1",
        task_id=task.task_id,
        result_id=result.result_id,
        status=ResearchFetchStatus.FAILED,
        failure_kind="transport",
        attempted_at=NOW,
    )

    assert repository.record_fetch(fetch) is True
    assert repository.record_fetch(fetch) is False
    with pytest.raises(ValueError, match=_FETCH_COLLISION_MESSAGE):
        _ = repository.record_fetch(fetch.model_copy(update={"failure_kind": "different"}))


def test_finalize_session_recovers_the_exact_canonical_stage_payload(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session = _session()
    _ = repository.create_session(session)
    summary: dict[str, JsonValue] = {
        "candidate": {"candidate_thesis_id": "candidate-1", "session_id": session.session_id},
        "contexts": [],
        "alias_bindings": [],
    }

    summary_id = repository.finalize_session(
        session.session_id,
        run_id=RUN_ID,
        reason=ResearchStopReason.UNRESOLVED,
        stopped_at=NOW,
        known_at=NOW,
        summary_payload=summary,
    )
    recovered = repository.recover_stage_admission(RUN_ID, known_at=NOW)

    assert recovered is not None
    expected_payload = canonical_research_payload(
        (json.dumps(summary, sort_keys=True, separators=(",", ":")),),
    )
    expected_payload_hash = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    assert recovered.payload == expected_payload
    assert recovered.admission.summary_ids == (summary_id,)
    assert recovered.admission.payload_hash == expected_payload_hash
    assert len(recovered.admission.semantic_context_hash) == 64


def test_recover_stage_admission_rejects_an_active_session(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _ = repository.create_session(_session())

    with pytest.raises(ResearchStageRecoveryIncompleteError):
        _ = repository.recover_stage_admission(RUN_ID, known_at=NOW)


def test_recover_stage_admission_rejects_a_terminal_session_without_summary(
    tmp_path: Path,
) -> None:
    database, repository = _database_and_repository(tmp_path)
    session = _session()
    _ = repository.create_session(session)
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "UPDATE research_sessions SET status = ?, stop_reason = ? WHERE session_id = ?",
            (ResearchSessionStatus.STOPPED, ResearchStopReason.UNRESOLVED, session.session_id),
        )

    with pytest.raises(ResearchStageRecoveryIncompleteError):
        _ = repository.recover_stage_admission(RUN_ID, known_at=NOW)
