from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from money_pit import intelligence_cli
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import ProviderResearchWaveRecord
from money_pit.storage.intelligence_work import ResearchJobRecord
from money_pit.storage.recovery_audit import RecoveryAuditBlockedError
from money_pit.storage.recovery_audit import RecoveryAuditor
from money_pit.storage.recovery_audit import RecoveryAuditStatus


_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    with database.transaction(TransactionMode.WRITE) as connection:
        for run_id in ("origin-run", "retry-run"):
            _ = connection.execute(
                """INSERT INTO runs (run_id, requested_as_of, started_at, known_at, through_stage,
                source_config_hash, intelligence_config_hash, manifest_json)
                VALUES (?, ?, ?, ?, 'A3', 'sources', 'intelligence', '{}')""",
                (run_id, _NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat()),
            )
        _ = connection.execute(
            """INSERT INTO candidate_theses
            (candidate_thesis_id, status, created_at, known_at, candidate_json)
            VALUES ('candidate-1', 'researching', ?, ?, '{}')""",
            (_NOW.isoformat(), _NOW.isoformat()),
        )
        _ = connection.execute(
            """INSERT INTO research_sessions
            (session_id, run_id, scope_kind, scope_subject_id, started_at, deadline_at,
             maximum_rounds, maximum_queries, maximum_fetches, query_count, fetch_count,
             status, session_json)
            VALUES ('session-1', 'origin-run', 'candidate_thesis', 'candidate-1', ?, ?,
            3, 6, 12, 1, 0, 'active', '{}')""",
            (_NOW.isoformat(), _NOW.isoformat()),
        )
    work = IntelligenceWorkRepository(database)
    work.ensure_research_job(
        ResearchJobRecord(
            job_id="job-1",
            candidate_thesis_id="candidate-1",
            premise_fingerprint="a" * 64,
            created_at=_NOW,
            payload={},
        )
    )
    return database


def test_audit_classifies_provider_completed_work_as_safely_resumable(tmp_path: Path) -> None:
    database = _database(tmp_path / "resumable.sqlite3")
    IntelligenceWorkRepository(database).record_provider_wave(
        ProviderResearchWaveRecord(
            wave_result_id="wave-1",
            job_id="job-1",
            session_id="session-1",
            origin_run_id="origin-run",
            wave_number=1,
            recorded_at=_NOW,
            provider_result={"task_count": 0},
            search_count_delta=1,
            accepted_fetch_count_delta=0,
        )
    )

    report = RecoveryAuditor(database).audit()

    assert report.status is RecoveryAuditStatus.RESUMABLE
    assert report.findings[0].code == "provider_wave_awaiting_interpretation"
    assert report.findings[0].durable_ids == ("wave-1",)


def test_audit_blocks_malformed_execution_transition_ownership(tmp_path: Path) -> None:
    database = _database(tmp_path / "blocked.sqlite3")
    work = IntelligenceWorkRepository(database)
    work.record_provider_wave(
        ProviderResearchWaveRecord(
            wave_result_id="wave-1",
            job_id="job-1",
            session_id="session-1",
            origin_run_id="origin-run",
            wave_number=1,
            recorded_at=_NOW,
            provider_result={},
            search_count_delta=1,
            accepted_fetch_count_delta=0,
        )
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "UPDATE research_wave_results SET phase = 'execution_completed' WHERE wave_result_id = 'wave-1'"
        )

    report = RecoveryAuditor(database).audit()

    assert report.status is RecoveryAuditStatus.BLOCKED
    assert any(item.code == "malformed_research_wave" for item in report.findings)


def test_update_preflight_blocks_before_provider_backed_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "blocked-update.sqlite3")
    work = IntelligenceWorkRepository(database)
    work.record_provider_wave(
        ProviderResearchWaveRecord(
            wave_result_id="wave-1",
            job_id="job-1",
            session_id="session-1",
            origin_run_id="origin-run",
            wave_number=1,
            recorded_at=_NOW,
            provider_result={},
            search_count_delta=1,
            accepted_fetch_count_delta=0,
        )
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "UPDATE research_wave_results SET phase = 'execution_completed' WHERE wave_result_id = 'wave-1'"
        )
    monkeypatch.setattr(intelligence_cli, "_database", lambda: database)

    def reject_provider_execution(**_kwargs: object) -> None:
        raise AssertionError("provider-backed execution must not be constructed")

    monkeypatch.setattr(intelligence_cli, "execute_intelligence_update", reject_provider_execution)

    with pytest.raises(RecoveryAuditBlockedError):
        _ = intelligence_cli._run_intelligence_updates(  # pyright: ignore[reportPrivateUsage]
            source=None,
            through=IntelligenceStage.RESEARCH,
            iterations=1,
        )
