"""Module containing durable bounded-research state transitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import cast

from money_pit.research.errors import ResearchBudgetExceededError
from money_pit.research.errors import ResearchFetchCollisionError
from money_pit.research.errors import ResearchResultCollisionError
from money_pit.research.errors import ResearchSessionOwnershipError
from money_pit.research.errors import ResearchSessionStoppedError
from money_pit.research.errors import ResearchStageAlreadyAdmittedError
from money_pit.research.errors import ResearchStageRecoveryIncompleteError
from money_pit.research.errors import ResearchTaskCollisionError
from money_pit.schemas.research import CandidateThesisResearchScope
from money_pit.schemas.research import CanonicalClaimResearchScope
from money_pit.schemas.research import RecoveredResearchStage
from money_pit.schemas.research import ResearchDiscoveryBatch
from money_pit.schemas.research import ResearchDiscoveryResult
from money_pit.schemas.research import ResearchFetch
from money_pit.schemas.research import ResearchScope
from money_pit.schemas.research import ResearchSession
from money_pit.schemas.research import ResearchSessionStatus
from money_pit.schemas.research import ResearchStageAdmission
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.research import ResearchTask
from money_pit.schemas.research import ResearchTaskStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.research_semantics import canonical_research_context
from money_pit.storage.research_semantics import canonical_research_payload


if TYPE_CHECKING:
    import sqlite3

    from pydantic import JsonValue


@dataclass(frozen=True)
class ResearchBudgetState:
    """Current durable counters for one bounded research session."""

    session: ResearchSession
    query_count: int
    fetch_count: int


@dataclass(frozen=True)
class DurableResearchRoundState:
    """Complete normalized journal projection for one research wave."""

    session_id: str
    round_number: int
    tasks: tuple[ResearchTask, ...]
    results: tuple[ResearchDiscoveryResult, ...]
    fetches: tuple[ResearchFetch, ...]
    failure_ids: tuple[str, ...]
    source_item_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    fragment_ids: tuple[str, ...]
    interpretation_attempt_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]


class ResearchRepository:
    """Persist sessions, tasks, searches, fetches, and stop decisions."""

    def __init__(self, database: Database) -> None:
        """Bind research persistence to an initialized database."""
        self._database: Database = database

    def create_session(self, session: ResearchSession) -> bool:
        """Persist one exact active session, returning whether it was new."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT session_json FROM research_sessions WHERE session_id = ?",
                    (session.session_id,),
                ).fetchone(),
            )
            session_json: str = session.model_dump_json()
            if existing is not None:
                if str(_column(existing, "session_json")) != session_json:
                    raise ValueError("Research session identity collision")
                return False
            _ = connection.execute(
                """
                INSERT INTO research_sessions (
                    session_id, run_id, scope_kind, scope_subject_id, started_at, deadline_at,
                    maximum_rounds, maximum_queries, maximum_fetches,
                    status, stop_reason, session_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.run_id,
                    session.scope.kind,
                    _scope_subject_id(session.scope),
                    _utc_text(session.started_at),
                    _utc_text(session.deadline_at),
                    session.maximum_rounds,
                    session.maximum_queries,
                    session.maximum_fetches,
                    session.status,
                    session.stop_reason,
                    session_json,
                ),
            )
        return True

    def sessions(self) -> tuple[ResearchSession, ...]:
        """Return every session with current durable status in start order."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    "SELECT session_id FROM research_sessions ORDER BY started_at, session_id",
                ).fetchall(),
            )
        return tuple(self.budget_state(str(_column(row, "session_id"))).session for row in rows)

    def session(self, session_id: str) -> ResearchSession | None:
        """Return one session with current durable status."""
        try:
            return self.budget_state(session_id).session
        except KeyError:
            return None

    def budget_state(self, session_id: str) -> ResearchBudgetState:
        """Return an exact session and its transactionally maintained counters."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT session_json, query_count, fetch_count, status, stop_reason
                    FROM research_sessions WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone(),
            )
        if row is None:
            raise KeyError(session_id)
        session: ResearchSession = ResearchSession.model_validate_json(
            str(_column(row, "session_json")),
        )
        session = ResearchSession.model_validate(
            {
                **session.model_dump(),
                "status": ResearchSessionStatus(str(_column(row, "status"))),
                "stop_reason": (
                    ResearchStopReason(str(value)) if (value := _column(row, "stop_reason")) is not None else None
                ),
            },
        )
        return ResearchBudgetState(
            session=session,
            query_count=int(str(_column(row, "query_count"))),
            fetch_count=int(str(_column(row, "fetch_count"))),
        )

    def active_session_for_scope(self, scope: ResearchScope) -> ResearchSession | None:
        """Return the newest active session for one exact research subject."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """
                    SELECT session_json
                    FROM research_sessions
                    WHERE scope_kind = ? AND scope_subject_id = ? AND status = 'active'
                    ORDER BY started_at DESC, session_id DESC
                    LIMIT 1
                    """,
                    (scope.kind, _scope_subject_id(scope)),
                ).fetchone(),
            )
        if row is None:
            return None
        return ResearchSession.model_validate_json(str(_column(row, "session_json")))

    def adopt_interrupted_session(
        self,
        session: ResearchSession,
        *,
        run_id: str,
        reclaim_before: datetime,
        deadline: datetime,
    ) -> ResearchSession:
        """Move interrupted work to a retry run with a fresh bounded deadline."""
        adopted = session.model_copy(update={"run_id": run_id, "deadline_at": deadline})
        with self._database.transaction(TransactionMode.WRITE) as connection:
            cursor = connection.execute(
                """UPDATE research_sessions SET run_id = ?, deadline_at = ?, session_json = ?
                WHERE session_id = ? AND status = 'active'
                  AND (started_at <= ? OR EXISTS (
                    SELECT 1 FROM run_terminal_events WHERE run_id = research_sessions.run_id))""",
                (
                    run_id,
                    _utc_text(deadline),
                    adopted.model_dump_json(),
                    session.session_id,
                    _utc_text(reclaim_before),
                ),
            )
            if cursor.rowcount != 1:
                raise ResearchSessionOwnershipError("Active research session is still owned by a live run")
        return adopted

    def pending_tasks(self, session_id: str) -> tuple[ResearchTask, ...]:
        """Return pending tasks in deterministic round and creation order."""
        with self._database.transaction() as connection:
            rows: list[sqlite3.Row] = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT task_json
                    FROM research_tasks
                    WHERE session_id = ? AND status = 'pending'
                    ORDER BY round_number, created_at, task_id
                    """,
                    (session_id,),
                ).fetchall(),
            )
        return tuple(ResearchTask.model_validate_json(str(_column(row, "task_json"))) for row in rows)

    def research_round_state(self, session_id: str, round_number: int) -> DurableResearchRoundState:
        """Project every committed task/result/fetch/interpretation for one wave."""
        if round_number < 1:
            raise ValueError("Research round number must be positive")
        with self._database.transaction() as connection:
            task_rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT task_json, status FROM research_tasks
                WHERE session_id = ? AND round_number = ? ORDER BY created_at, task_id""",
                    (session_id, round_number),
                ).fetchall(),
            )
            tasks = tuple(
                ResearchTask.model_validate_json(str(_column(row, "task_json"))).model_copy(
                    update={"status": ResearchTaskStatus(str(_column(row, "status")))}
                )
                for row in task_rows
            )
            task_ids = tuple(task.task_id for task in tasks)
            encoded_ids = json.dumps(task_ids)
            result_rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT result_json FROM research_results
                WHERE task_id IN (SELECT value FROM json_each(?))
                ORDER BY discovered_at, result_id""",
                    (encoded_ids,),
                ).fetchall(),
            )
            fetch_rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT fetch_json FROM research_fetches
                WHERE task_id IN (SELECT value FROM json_each(?))
                ORDER BY attempted_at, fetch_id""",
                    (encoded_ids,),
                ).fetchall(),
            )
            failures = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT failure_id FROM research_failures
                WHERE session_id = ? AND (task_id IS NULL OR task_id IN (SELECT value FROM json_each(?)))
                ORDER BY occurred_at, failure_id""",
                    (session_id, encoded_ids),
                ).fetchall(),
            )
            descendants = _round_descendant_ids(connection, task_ids)
        return DurableResearchRoundState(
            session_id=session_id,
            round_number=round_number,
            tasks=tasks,
            results=tuple(
                ResearchDiscoveryResult.model_validate_json(str(_column(row, "result_json"))) for row in result_rows
            ),
            fetches=tuple(ResearchFetch.model_validate_json(str(_column(row, "fetch_json"))) for row in fetch_rows),
            failure_ids=tuple(str(_column(row, "failure_id")) for row in failures),
            source_item_ids=descendants[0],
            asset_ids=descendants[1],
            fragment_ids=descendants[2],
            interpretation_attempt_ids=descendants[3],
            observation_ids=descendants[4],
        )

    def latest_research_round_state(self, session_id: str) -> DurableResearchRoundState | None:
        """Return the newest materialized wave for an active durable session."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT max(round_number) AS round_number FROM research_tasks WHERE session_id = ?",
                    (session_id,),
                ).fetchone(),
            )
        round_number_value: object | None = None if row is None else _column(row, "round_number")
        if round_number_value is None:
            return None
        return self.research_round_state(session_id, int(str(round_number_value)))

    def tasks_for_round(self, session_id: str, round_number: int) -> tuple[ResearchTask, ...]:
        """Return every durable task with its current status for one wave."""
        return self.research_round_state(session_id, round_number).tasks

    def results_for_task(self, task_id: str) -> tuple[ResearchDiscoveryResult, ...]:
        """Return all committed discoveries for one task."""
        with self._database.transaction() as connection:
            rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT result_json FROM research_results
                WHERE task_id = ? ORDER BY discovered_at, result_id""",
                    (task_id,),
                ).fetchall(),
            )
        return tuple(ResearchDiscoveryResult.model_validate_json(str(_column(row, "result_json"))) for row in rows)

    def fetches_for_task(self, task_id: str) -> tuple[ResearchFetch, ...]:
        """Return every terminal fetch disposition for one task."""
        with self._database.transaction() as connection:
            rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT fetch_json FROM research_fetches
                WHERE task_id = ? ORDER BY attempted_at, fetch_id""",
                    (task_id,),
                ).fetchall(),
            )
        return tuple(ResearchFetch.model_validate_json(str(_column(row, "fetch_json"))) for row in rows)

    def fetch_for_result(self, task_id: str, result_id: str) -> ResearchFetch | None:
        """Return the durable terminal fetch disposition for one task result."""
        with self._database.transaction() as connection:
            row = cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT fetch_json FROM research_fetches
                WHERE task_id = ? AND result_id = ? ORDER BY attempted_at DESC LIMIT 1""",
                    (task_id, result_id),
                ).fetchone(),
            )
        return None if row is None else ResearchFetch.model_validate_json(str(_column(row, "fetch_json")))

    def task_status(self, task_id: str) -> ResearchTaskStatus | None:
        """Return one durable task status."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT status FROM research_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone(),
            )
        return ResearchTaskStatus(str(_column(row, "status"))) if row is not None else None

    def add_task(self, task: ResearchTask) -> bool:
        """Persist a unique provider/query pair within one session."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _require_active_session(
                connection,
                task.session_id,
                round_number=task.round_number,
                at=task.created_at,
            )
            cursor = connection.execute(
                """
                INSERT INTO research_tasks (
                    task_id, session_id, round_number, provider, query_text,
                    status, created_at, task_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    task.task_id,
                    task.session_id,
                    task.round_number,
                    task.query.provider,
                    task.query.query_text,
                    task.status,
                    _utc_text(task.created_at),
                    task.model_dump_json(),
                ),
            )
            if cursor.rowcount == 0:
                existing: sqlite3.Row | None = cast(
                    "sqlite3.Row | None",
                    connection.execute(
                        "SELECT task_json FROM research_tasks WHERE task_id = ?",
                        (task.task_id,),
                    ).fetchone(),
                )
                if existing is None or str(_column(existing, "task_json")) != task.model_dump_json():
                    raise ResearchTaskCollisionError("Research task identity collision")
        return cursor.rowcount > 0

    def reserve_query(self, task: ResearchTask, *, attempted_at: datetime) -> None:
        """Reserve one query atomically before provider I/O."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _consume_query_budget(connection, task.session_id, attempted_at=attempted_at)

    def reserve_fetch(self, session_id: str, *, attempted_at: datetime) -> None:
        """Reserve one fetch atomically before provider I/O."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _consume_fetch_budget(connection, session_id, attempted_at=attempted_at)

    def record_search(self, task: ResearchTask, batch: ResearchDiscoveryBatch) -> int:
        """Persist one query attempt and its discovery-only metadata atomically."""
        if task.query != batch.query:
            raise ValueError("Research batch does not belong to its task")
        with self._database.transaction(TransactionMode.WRITE) as connection:
            inserted_count: int = 0
            for result in batch.results:
                cursor = connection.execute(
                    """
                    INSERT INTO research_results (
                        result_id, task_id, provider, canonical_uri,
                        provenance_group, discovered_at, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        result.result_id,
                        task.task_id,
                        result.provider,
                        result.canonical_uri,
                        result.provenance_group,
                        _utc_text(result.discovered_at),
                        result.model_dump_json(),
                    ),
                )
                if cursor.rowcount == 0:
                    existing: sqlite3.Row | None = cast(
                        "sqlite3.Row | None",
                        connection.execute(
                            "SELECT task_id, result_json FROM research_results WHERE result_id = ?",
                            (result.result_id,),
                        ).fetchone(),
                    )
                    if (
                        existing is None
                        or str(_column(existing, "task_id")) != task.task_id
                        or str(_column(existing, "result_json")) != result.model_dump_json()
                    ):
                        raise ResearchResultCollisionError("Research result identity collision")
                inserted_count += int(cursor.rowcount > 0)
            _set_task_status(connection, task.task_id, ResearchTaskStatus.SEARCHED)
        return inserted_count

    def record_search_failure(self, task: ResearchTask) -> None:
        """Consume one attempted query and mark its durable task failed."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _set_task_status(connection, task.task_id, ResearchTaskStatus.FAILED)

    def record_fetch(self, fetch: ResearchFetch) -> bool:
        """Persist one fetch outcome and consume budget exactly once."""
        with self._database.transaction(TransactionMode.WRITE) as connection:
            existing: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT fetch_json FROM research_fetches WHERE fetch_id = ?",
                    (fetch.fetch_id,),
                ).fetchone(),
            )
            fetch_json: str = fetch.model_dump_json()
            if existing is not None:
                if str(_column(existing, "fetch_json")) != fetch_json:
                    raise ResearchFetchCollisionError("Research fetch identity collision")
                return False
            _ = connection.execute(
                """
                INSERT INTO research_fetches (
                    fetch_id, task_id, result_id, source_item_id, asset_id,
                    status, failure_kind, attempted_at, fetch_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetch.fetch_id,
                    fetch.task_id,
                    fetch.result_id,
                    fetch.source_item_id,
                    fetch.asset_id,
                    fetch.status,
                    fetch.failure_kind,
                    _utc_text(fetch.attempted_at),
                    fetch_json,
                ),
            )
        return True

    def complete_task(self, task_id: str, *, failed: bool = False) -> None:
        """Set the terminal task state after its selected fetches finish."""
        status: ResearchTaskStatus = ResearchTaskStatus.FAILED if failed else ResearchTaskStatus.COMPLETED
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _set_task_status(connection, task_id, status)

    def finalize_session(
        self,
        session_id: str,
        *,
        run_id: str,
        reason: ResearchStopReason,
        stopped_at: datetime,
        known_at: datetime,
        summary_payload: JsonValue,
    ) -> str:
        """Atomically stop one session and persist its exact recoverable semantic summary."""
        encoded = _canonical_json(summary_payload)
        summary_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        summary_id = f"research-summary:{summary_hash}"
        stop_event_id = f"{session_id}:{reason.value}"
        with self._database.transaction(TransactionMode.WRITE) as connection:
            session = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT run_id, status FROM research_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone(),
            )
            if session is None or str(_column(session, "run_id")) != run_id:
                raise KeyError(session_id)
            existing = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT summary_id, summary_hash, summary_json FROM research_candidate_summaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone(),
            )
            if existing is not None:
                if (
                    str(_column(existing, "summary_id")) != summary_id
                    or str(_column(existing, "summary_hash")) != summary_hash
                    or str(_column(existing, "summary_json")) != encoded
                ):
                    raise ValueError("Research session summary identity collision")
                return summary_id
            if str(_column(session, "status")) != ResearchSessionStatus.ACTIVE:
                raise ResearchStageRecoveryIncompleteError(
                    "Terminal research session has no recoverable semantic summary"
                )
            _ = connection.execute(
                "UPDATE research_sessions SET status = ?, stop_reason = ? WHERE session_id = ?",
                (ResearchSessionStatus.STOPPED, reason, session_id),
            )
            _ = connection.execute(
                """INSERT INTO research_stop_events (
                       stop_event_id, session_id, reason, stopped_at, detail_json
                   ) VALUES (?, ?, ?, ?, '{}')""",
                (stop_event_id, session_id, reason, _utc_text(stopped_at)),
            )
            _ = connection.execute(
                """INSERT INTO research_candidate_summaries (
                       summary_id, session_id, run_id, known_at, summary_hash, summary_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (summary_id, session_id, run_id, _utc_text(known_at), summary_hash, encoded),
            )
        return summary_id

    def recover_stage_admission(
        self,
        run_id: str,
        *,
        known_at: datetime,
    ) -> RecoveredResearchStage | None:
        """Recover terminal unadmitted A3 state without invoking providers or models."""
        with self._database.transaction() as connection:
            admitted: sqlite3.Row | None = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT 1 FROM research_stage_admissions WHERE run_id = ?",
                    (run_id,),
                ).fetchone(),
            )
            if admitted is not None:
                raise ResearchStageAlreadyAdmittedError("Research stage is already admitted")
            rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    "SELECT session_id, status FROM research_sessions WHERE run_id = ? ORDER BY session_id",
                    (run_id,),
                ).fetchall(),
            )
            if not rows:
                return None
            if any(str(_column(row, "status")) == ResearchSessionStatus.ACTIVE for row in rows):
                raise ResearchStageRecoveryIncompleteError("Research run still contains an active session")
            session_ids = tuple(str(_column(row, "session_id")) for row in rows)
            count_row: sqlite3.Row = cast(
                "sqlite3.Row",
                connection.execute(
                    "SELECT COUNT(*) FROM research_candidate_summaries WHERE run_id = ?",
                    (run_id,),
                ).fetchone(),
            )
            summary_count = int(str(cast("object", count_row[0])))
            if summary_count != len(session_ids):
                raise ResearchStageRecoveryIncompleteError(
                    "Terminal research run is missing a recoverable session summary"
                )
        admission = self.stage_admission(run_id=run_id, session_ids=session_ids, known_at=known_at)
        with self._database.transaction() as connection:
            payload = canonical_research_payload(_summary_json_values(connection, session_ids))
        return RecoveredResearchStage(admission=admission, payload=payload)

    def stage_admission(
        self,
        *,
        run_id: str,
        session_ids: tuple[str, ...],
        known_at: datetime,
    ) -> ResearchStageAdmission:
        """Reconstruct exact terminal A3 descendants from durable session ownership."""
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("Research admission session identities must be unique")
        encoded_sessions = json.dumps(session_ids, separators=(",", ":"))
        with self._database.transaction() as connection:
            sessions = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """
                    SELECT session_id, scope_kind, scope_subject_id, status
                    FROM research_sessions
                    WHERE run_id = ? AND session_id IN (SELECT value FROM json_each(?))
                    ORDER BY session_id
                    """,
                    (run_id, encoded_sessions),
                ).fetchall(),
            )
            if len(sessions) != len(session_ids) or any(str(_column(row, "status")) == "active" for row in sessions):
                raise ValueError("Research stage admission requires exact terminal run-owned sessions")
            rows = _research_stage_descendants(connection, encoded_sessions, run_id)
            summary_rows = cast(
                "list[sqlite3.Row]",
                connection.execute(
                    """SELECT summary_id FROM research_candidate_summaries
                       WHERE run_id = ? AND session_id IN (SELECT value FROM json_each(?))
                       ORDER BY session_id""",
                    (run_id, encoded_sessions),
                ).fetchall(),
            )
            if len(summary_rows) != len(session_ids):
                raise ResearchStageRecoveryIncompleteError(
                    "Research stage admission requires one durable summary per session"
                )
            payload = canonical_research_payload(_summary_json_values(connection, session_ids))
        payload_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        admission = ResearchStageAdmission(
            run_id=run_id,
            known_at=known_at,
            payload_hash=payload_hash,
            semantic_context_hash="0" * 64,
            candidate_thesis_ids=tuple(
                str(_column(row, "scope_subject_id"))
                for row in sessions
                if str(_column(row, "scope_kind")) == "candidate_thesis"
            ),
            session_ids=tuple(str(_column(row, "session_id")) for row in sessions),
            summary_ids=tuple(str(_column(row, "summary_id")) for row in summary_rows),
            planned_task_ids=rows["planned"],
            task_ids=rows["tasks"],
            result_ids=rows["results"],
            fetch_ids=rows["fetches"],
            failure_ids=rows["failures"],
            source_item_ids=rows["source_items"],
            asset_ids=rows["assets"],
            document_ids=rows["assets"],
            fragment_ids=rows["fragments"],
            interpretation_attempt_ids=rows["attempts"],
            observation_ids=rows["observations"],
            stop_event_ids=rows["stops"],
        )
        with self._database.transaction() as connection:
            semantic_context_hash = hashlib.sha256(
                canonical_research_context(connection, admission).encode("utf-8")
            ).hexdigest()
        return admission.model_copy(update={"semantic_context_hash": semantic_context_hash})


def _require_active_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    round_number: int,
    at: datetime,
) -> None:
    row: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(
            """
            SELECT status, maximum_rounds, deadline_at
            FROM research_sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone(),
    )
    if row is None:
        raise KeyError(session_id)
    if str(_column(row, "status")) != ResearchSessionStatus.ACTIVE:
        raise ResearchSessionStoppedError(f"Research session is not active: {session_id}")
    if round_number > int(str(_column(row, "maximum_rounds"))):
        raise ResearchBudgetExceededError("Research round budget exceeded")
    if at.astimezone(timezone.utc) >= datetime.fromisoformat(
        str(_column(row, "deadline_at")),
    ):
        raise ResearchBudgetExceededError("Research elapsed-time budget expired")


def _research_stage_descendants(
    connection: sqlite3.Connection,
    encoded_sessions: str,
    run_id: str,
) -> dict[str, tuple[str, ...]]:
    """Return exact IDs only after all session-owned tasks are terminal."""
    nonterminal: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(
            """
        SELECT 1 FROM research_tasks
        WHERE session_id IN (SELECT value FROM json_each(?))
          AND status NOT IN ('completed', 'failed')
        LIMIT 1
        """,
            (encoded_sessions,),
        ).fetchone(),
    )
    incomplete_plan: sqlite3.Row | None = cast(
        "sqlite3.Row | None",
        connection.execute(
            """
        SELECT 1 FROM planned_research_tasks
        WHERE materialized_session_id IN (SELECT value FROM json_each(?))
          AND status != 'completed'
        LIMIT 1
        """,
            (encoded_sessions,),
        ).fetchone(),
    )
    if nonterminal is not None or incomplete_plan is not None:
        raise ValueError("Research stage admission requires terminal tasks and completed plans")

    def identifiers(sql: str, parameters: tuple[object, ...] = ()) -> tuple[str, ...]:
        rows: list[sqlite3.Row] = cast(
            "list[sqlite3.Row]",
            connection.execute(sql, (encoded_sessions, *parameters)).fetchall(),
        )
        return tuple(str(cast("object", row[0])) for row in rows)

    return {
        "planned": identifiers(
            """SELECT task_id FROM planned_research_tasks
               WHERE materialized_session_id IN (SELECT value FROM json_each(?))
               ORDER BY task_id"""
        ),
        "tasks": identifiers(
            """SELECT task_id FROM research_tasks
               WHERE session_id IN (SELECT value FROM json_each(?)) ORDER BY task_id"""
        ),
        "results": identifiers(
            """SELECT result.result_id FROM research_results AS result
               JOIN research_tasks AS task ON task.task_id = result.task_id
               WHERE task.session_id IN (SELECT value FROM json_each(?)) ORDER BY result.result_id"""
        ),
        "fetches": identifiers(
            """SELECT fetch.fetch_id FROM research_fetches AS fetch
               JOIN research_tasks AS task ON task.task_id = fetch.task_id
               WHERE task.session_id IN (SELECT value FROM json_each(?)) ORDER BY fetch.fetch_id"""
        ),
        "failures": identifiers(
            """SELECT failure_id FROM research_failures
               WHERE session_id IN (SELECT value FROM json_each(?)) ORDER BY failure_id"""
        ),
        "source_items": identifiers(
            """SELECT DISTINCT fetch.source_item_id FROM research_fetches AS fetch
               JOIN research_tasks AS task ON task.task_id = fetch.task_id
               WHERE task.session_id IN (SELECT value FROM json_each(?))
                 AND fetch.status = 'succeeded' ORDER BY fetch.source_item_id"""
        ),
        "assets": identifiers(
            """SELECT DISTINCT fetch.asset_id FROM research_fetches AS fetch
               JOIN research_tasks AS task ON task.task_id = fetch.task_id
               WHERE task.session_id IN (SELECT value FROM json_each(?))
                 AND fetch.status = 'succeeded' ORDER BY fetch.asset_id"""
        ),
        "fragments": identifiers(
            """SELECT DISTINCT fragment.fragment_id FROM evidence_fragments AS fragment
               JOIN research_fetches AS fetch ON fetch.asset_id = fragment.asset_id
               JOIN research_tasks AS task ON task.task_id = fetch.task_id
               WHERE task.session_id IN (SELECT value FROM json_each(?)) ORDER BY fragment.fragment_id"""
        ),
        "attempts": identifiers(
            """SELECT DISTINCT attempt.attempt_id FROM claim_interpretation_attempts AS attempt
               JOIN research_fetches AS fetch
                 ON fetch.source_item_id = attempt.source_item_id AND fetch.asset_id = attempt.asset_id
               JOIN research_tasks AS task ON task.task_id = fetch.task_id
               WHERE task.session_id IN (SELECT value FROM json_each(?))
                 AND attempt.run_id = ? ORDER BY attempt.attempt_id""",
            (run_id,),
        ),
        "observations": identifiers(
            """SELECT DISTINCT observation.value FROM claim_interpretation_attempts AS attempt
               JOIN research_fetches AS fetch
                 ON fetch.source_item_id = attempt.source_item_id AND fetch.asset_id = attempt.asset_id
               JOIN research_tasks AS task ON task.task_id = fetch.task_id
               JOIN json_each(attempt.observation_ids_json) AS observation
               WHERE task.session_id IN (SELECT value FROM json_each(?))
                 AND attempt.run_id = ? ORDER BY observation.value""",
            (run_id,),
        ),
        "stops": identifiers(
            """SELECT stop_event_id FROM research_stop_events
               WHERE session_id IN (SELECT value FROM json_each(?)) ORDER BY stop_event_id"""
        ),
    }


def _summary_json_values(
    connection: sqlite3.Connection,
    session_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Return canonical session summaries in deterministic artifact order."""
    rows: list[sqlite3.Row] = cast(
        "list[sqlite3.Row]",
        connection.execute(
            """SELECT summary_json FROM research_candidate_summaries
               WHERE session_id IN (SELECT value FROM json_each(?)) ORDER BY session_id""",
            (json.dumps(session_ids, separators=(",", ":")),),
        ).fetchall(),
    )
    return tuple(str(_column(row, "summary_json")) for row in rows)


def _round_descendant_ids(
    connection: sqlite3.Connection,
    task_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    encoded = json.dumps(task_ids)

    def identifiers(query: str) -> tuple[str, ...]:
        rows = cast("list[sqlite3.Row]", connection.execute(query, (encoded,)).fetchall())
        return tuple(str(_column(row, "identifier")) for row in rows)

    source_items = identifiers(
        """SELECT DISTINCT source_item_id AS identifier FROM research_fetches
        WHERE task_id IN (SELECT value FROM json_each(?)) AND status = 'succeeded'
        ORDER BY source_item_id"""
    )
    assets = identifiers(
        """SELECT DISTINCT asset_id AS identifier FROM research_fetches
        WHERE task_id IN (SELECT value FROM json_each(?)) AND status = 'succeeded'
        ORDER BY asset_id"""
    )
    fragments = identifiers(
        """SELECT DISTINCT fragment.fragment_id AS identifier FROM evidence_fragments AS fragment
        JOIN research_fetches AS fetch ON fetch.asset_id = fragment.asset_id
        WHERE fetch.task_id IN (SELECT value FROM json_each(?)) ORDER BY fragment.fragment_id"""
    )
    attempts = identifiers(
        """SELECT DISTINCT attempt.attempt_id AS identifier FROM claim_interpretation_attempts AS attempt
        JOIN research_fetches AS fetch ON fetch.asset_id = attempt.asset_id
         AND fetch.source_item_id = attempt.source_item_id
        WHERE fetch.task_id IN (SELECT value FROM json_each(?)) AND attempt.outcome = 'succeeded'
        ORDER BY attempt.attempt_id"""
    )
    observations = identifiers(
        """SELECT DISTINCT observation.value AS identifier FROM claim_interpretation_attempts AS attempt
        JOIN research_fetches AS fetch ON fetch.asset_id = attempt.asset_id
         AND fetch.source_item_id = attempt.source_item_id
        JOIN json_each(attempt.observation_ids_json) AS observation
        WHERE fetch.task_id IN (SELECT value FROM json_each(?)) AND attempt.outcome = 'succeeded'
        ORDER BY observation.value"""
    )
    return source_items, assets, fragments, attempts, observations


def _canonical_json(value: JsonValue) -> str:
    """Return deterministic JSON for a value already constrained to durable semantics."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _consume_query_budget(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    attempted_at: datetime,
) -> None:
    cursor = connection.execute(
        """
        UPDATE research_sessions
        SET query_count = query_count + 1
        WHERE session_id = ?
          AND status = 'active'
          AND query_count < maximum_queries
          AND deadline_at > ?
        """,
        (session_id, _utc_text(attempted_at)),
    )
    if cursor.rowcount != 1:
        raise ResearchBudgetExceededError("Research query budget exceeded")


def _consume_fetch_budget(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    attempted_at: datetime,
) -> None:
    cursor = connection.execute(
        """
        UPDATE research_sessions
        SET fetch_count = fetch_count + 1
        WHERE session_id = ?
          AND status = 'active'
          AND fetch_count < maximum_fetches
          AND deadline_at > ?
        """,
        (session_id, _utc_text(attempted_at)),
    )
    if cursor.rowcount != 1:
        raise ResearchBudgetExceededError("Research fetch budget exceeded")


def _set_task_status(
    connection: sqlite3.Connection,
    task_id: str,
    status: ResearchTaskStatus,
) -> None:
    cursor = connection.execute(
        "UPDATE research_tasks SET status = ? WHERE task_id = ?",
        (status, task_id),
    )
    if cursor.rowcount != 1:
        raise KeyError(task_id)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Research timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _scope_subject_id(scope: ResearchScope) -> str:
    if isinstance(scope, CandidateThesisResearchScope):
        if scope.research_job_id is None:
            return scope.candidate_thesis_id
        return f"{scope.candidate_thesis_id}|research-job:{scope.research_job_id}"
    if isinstance(scope, CanonicalClaimResearchScope):
        return scope.canonical_claim_key
    return scope.observation_id


def _column(row: sqlite3.Row, name: str) -> object:
    """Return one SQLite value at the repository's validation boundary."""
    return cast("object", row[name])
