import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit import intelligence_cli
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import ProviderResearchWaveRecord
from money_pit.storage.intelligence_work import ResearchJobRecord
from money_pit.storage.recovery_audit import RecoveryAuditBlockedError
from money_pit.storage.recovery_audit import RecoveryAuditDisposition
from money_pit.storage.recovery_audit import RecoveryAuditor
from money_pit.storage.recovery_audit import RecoveryAuditStatus
from money_pit.storage.semantic_intelligence import SemanticIntelligenceRepository
from money_pit.storage.semantic_intelligence import reconcile_unbound_research_sessions


if TYPE_CHECKING:
    import sqlite3


_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-1",
        subject="GDX operating leverage",
        direction=ThesisDirection.LONG,
        instrument_reference="VanEck Gold Miners ETF",
        instrument="GDX",
        theme="Gold miners",
        horizon_class=HorizonClass.MEDIUM_TERM,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim:gold",)),
        causal_mechanisms=("Operating leverage",),
        regime_assumptions=("Stable funding",),
        created_at=_NOW,
        known_at=_NOW,
    )
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
            VALUES ('candidate-1', 'researching', ?, ?, ?)""",
            (_NOW.isoformat(), _NOW.isoformat(), candidate.model_dump_json()),
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
    semantic = SemanticIntelligenceRepository(database)
    reconciliation = semantic.reconcile_candidate(candidate, recorded_at=_NOW)
    _ = semantic.ensure_research_job_semantics(
        job_id="job-1",
        hypothesis_id=reconciliation.membership.group_id,
        scope_fingerprint="b" * 64,
        semantic_premise_fingerprint="a" * 64,
        task_bindings=(),
        recorded_at=_NOW,
    )
    return database


def _seed_unbound_job_session(
    database: Database,
    *,
    candidate_id: str = "candidate-1",
    job_id: str = "job-1",
) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO research_sessions
            (session_id, run_id, scope_kind, scope_subject_id, started_at, deadline_at,
             maximum_rounds, maximum_queries, maximum_fetches, status, session_json)
            VALUES ('session-crash', 'retry-run', 'candidate_thesis', ?, ?, ?,
            3, 6, 12, 'active', '{}')""",
            (
                f"{candidate_id}|research-job:{job_id}",
                _NOW.isoformat(),
                _NOW.isoformat(),
            ),
        )


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


def test_reconcile_unbound_research_sessions_repairs_valid_crash_gap_provider_free(tmp_path: Path) -> None:
    database = _database(tmp_path / "session-recovery.sqlite3")
    _seed_unbound_job_session(database)

    bound = reconcile_unbound_research_sessions(database)

    with database.read_only_transaction() as connection:
        owner_row = cast(
            "sqlite3.Row",
            connection.execute(
                "SELECT job_id FROM research_job_sessions WHERE session_id = 'session-crash'"
            ).fetchone(),
        )
        owner = cast("str", owner_row[0])
    assert (bound, owner, RecoveryAuditor(database).audit().blocked) == (1, "job-1", False)


@pytest.mark.parametrize("invalid_owner", ["candidate_mismatch", "noncurrent_job", "wrong_source_scope"])
def test_reconcile_unbound_research_sessions_leaves_invalid_ownership_blocked(
    tmp_path: Path,
    invalid_owner: str,
) -> None:
    database = _database(tmp_path / f"invalid-session-{invalid_owner}.sqlite3")
    semantic = SemanticIntelligenceRepository(database)
    if invalid_owner == "candidate_mismatch":
        _seed_unbound_job_session(database, candidate_id="candidate-other")
    elif invalid_owner == "noncurrent_job":
        work = IntelligenceWorkRepository(database)
        work.ensure_research_job(
            ResearchJobRecord(
                job_id="job-2",
                candidate_thesis_id="candidate-1",
                premise_fingerprint="c" * 64,
                created_at=_NOW,
                payload={},
            )
        )
        hypothesis_id = semantic.hypothesis_id_for_candidate("candidate-1")
        assert hypothesis_id is not None
        _ = semantic.ensure_research_job_semantics(
            job_id="job-2",
            hypothesis_id=hypothesis_id,
            scope_fingerprint="b" * 64,
            semantic_premise_fingerprint="c" * 64,
            task_bindings=(),
            recorded_at=_NOW,
        )
        _seed_unbound_job_session(database)
    else:
        _seed_unbound_job_session(database)

    bound = reconcile_unbound_research_sessions(
        database,
        source_id="source-a" if invalid_owner == "wrong_source_scope" else None,
    )

    with database.read_only_transaction() as connection:
        binding_row = cast(
            "sqlite3.Row",
            connection.execute(
                "SELECT count(*) FROM research_job_sessions WHERE session_id = 'session-crash'"
            ).fetchone(),
        )
        binding_count = cast("int", binding_row[0])
    report = RecoveryAuditor(database).audit()
    assert (
        bound,
        binding_count,
        report.status,
        any(finding.code == "research_session_without_job_binding" for finding in report.findings),
    ) == (0, 0, RecoveryAuditStatus.BLOCKED, True)


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


def test_audit_reports_pending_hypothesis_review_as_unavailable_not_corrupt(tmp_path: Path) -> None:
    database = _database(tmp_path / "review.sqlite3")
    candidate = CandidateThesis(
        candidate_thesis_id="candidate-2",
        subject="Precious-metal equities",
        direction=ThesisDirection.LONG,
        instrument_reference="VanEck Gold Miners ETF",
        instrument="GDX",
        theme="Precious-metal equities",
        horizon_class=HorizonClass.MEDIUM_TERM,
        discovery_basis=DiscoveryBasis(source_claim_keys=("claim:gold",)),
        causal_mechanisms=("Operating leverage",),
        regime_assumptions=("Stable funding",),
        created_at=_NOW,
        known_at=_NOW,
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """INSERT INTO candidate_theses
            (candidate_thesis_id, status, created_at, known_at, candidate_json)
            VALUES (?, 'open', ?, ?, ?)""",
            (candidate.candidate_thesis_id, _NOW.isoformat(), _NOW.isoformat(), candidate.model_dump_json()),
        )
    _ = SemanticIntelligenceRepository(database).reconcile_candidate(candidate, recorded_at=_NOW)

    report = RecoveryAuditor(database).audit()
    finding = next(item for item in report.findings if item.code == "hypothesis_review_required")

    assert (report.status, finding.disposition) == (
        RecoveryAuditStatus.RESUMABLE,
        RecoveryAuditDisposition.UNAVAILABLE,
    )


def test_audit_blocks_malformed_hypothesis_group_supersession(tmp_path: Path) -> None:
    database = _database(tmp_path / "malformed-group.sqlite3")
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute("UPDATE canonical_hypothesis_groups SET status = 'superseded'")

    report = RecoveryAuditor(database).audit()

    assert report.status is RecoveryAuditStatus.BLOCKED
    assert any(item.code == "malformed_hypothesis_group_supersession" for item in report.findings)


def test_audit_blocks_terminal_research_that_bypassed_material_admission(tmp_path: Path) -> None:
    database = _database(tmp_path / "terminal-without-material.sqlite3")
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """UPDATE research_jobs SET status = 'terminal', completed_at = ?, stop_reason = 'legacy_bypass'
            WHERE job_id = 'job-1'""",
            (_NOW.isoformat(),),
        )

    report = RecoveryAuditor(database).audit()
    finding = next(item for item in report.findings if item.code == "terminal_research_without_material_assessment")

    assert (report.status, finding.disposition, finding.durable_ids) == (
        RecoveryAuditStatus.BLOCKED,
        RecoveryAuditDisposition.BLOCKED,
        ("job-1",),
    )


def test_audit_marks_checkpointed_terminal_research_without_material_as_resumable(tmp_path: Path) -> None:
    database = _database(tmp_path / "terminal-awaiting-material.sqlite3")
    digest = json.dumps(
        {
            "candidate": {
                "candidate_thesis_id": "candidate-1",
                "session_id": "session-1",
                "rounds": [],
                "stop_reason": "unresolved",
            },
            "contexts": [],
        }
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """UPDATE research_jobs SET status = 'terminal', completed_at = ?, stop_reason = 'checkpointed'
            WHERE job_id = 'job-1'""",
            (_NOW.isoformat(),),
        )
        _ = connection.execute(
            """INSERT INTO research_job_checkpoints
            (checkpoint_id, job_id, run_id, wave_number, search_count,
             accepted_fetch_count, recorded_at, digest_json)
            VALUES ('checkpoint-1', 'job-1', 'origin-run', 0, 0, 0, ?, ?)""",
            (_NOW.isoformat(), digest),
        )

    report = RecoveryAuditor(database).audit()
    finding = next(item for item in report.findings if item.code == "terminal_research_awaiting_materialization")

    assert (report.status, finding.disposition, finding.durable_ids) == (
        RecoveryAuditStatus.RESUMABLE,
        RecoveryAuditDisposition.RESUMABLE,
        ("job-1",),
    )


def test_audit_blocks_terminal_research_with_malformed_material_checkpoint(tmp_path: Path) -> None:
    database = _database(tmp_path / "terminal-malformed-material.sqlite3")
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """UPDATE research_jobs SET status = 'terminal', completed_at = ?, stop_reason = 'checkpointed'
            WHERE job_id = 'job-1'""",
            (_NOW.isoformat(),),
        )
        _ = connection.execute(
            """INSERT INTO research_job_checkpoints
            (checkpoint_id, job_id, run_id, wave_number, search_count,
             accepted_fetch_count, recorded_at, digest_json)
            VALUES ('checkpoint-1', 'job-1', 'origin-run', 0, 0, 0, ?, '{}')""",
            (_NOW.isoformat(),),
        )

    report = RecoveryAuditor(database).audit()
    finding = next(
        item for item in report.findings if item.code == "terminal_research_with_malformed_material_checkpoint"
    )

    assert (report.status, finding.disposition, finding.durable_ids) == (
        RecoveryAuditStatus.BLOCKED,
        RecoveryAuditDisposition.BLOCKED,
        ("job-1",),
    )
