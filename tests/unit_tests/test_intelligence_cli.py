import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest
from typer.testing import CliRunner

from money_pit import intelligence_cli
from money_pit.__main__ import app
from money_pit.agents.inference import InferenceUsage
from money_pit.config import ApplicationConfig
from money_pit.config import ConfigurationScope
from money_pit.config import load_application_config
from money_pit.intelligence_cli import intelligence_app
from money_pit.pipeline.orchestration import IntelligenceStage
from money_pit.pipeline.orchestration import IntelligenceUpdateReport
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.universe import DiscoveryBasis
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.intelligence_work import IntelligenceWorkCompletionCounts
from money_pit.storage.intelligence_work import IntelligenceWorkStatus
from money_pit.storage.intelligence_work import RunInferenceUsage
from money_pit.storage.runs import RunRepository
from money_pit.storage.semantic_intelligence import SemanticIntelligenceRepository


if TYPE_CHECKING:
    import sqlite3


_NOW = datetime(2026, 8, 12, 20, tzinfo=UTC)


def _database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def _semantic_review_database(path: Path) -> tuple[Database, str]:
    database = _database(path)
    repository = SemanticIntelligenceRepository(database)
    for candidate_id, theme in (
        ("candidate:1", "Gold miners"),
        ("candidate:2", "Precious-metal equities"),
    ):
        candidate = CandidateThesis(
            candidate_thesis_id=candidate_id,
            subject=theme,
            direction=ThesisDirection.LONG,
            instrument_reference="VanEck Gold Miners ETF",
            instrument="GDX",
            theme=theme,
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
                (candidate_id, _NOW.isoformat(), _NOW.isoformat(), candidate.model_dump_json()),
            )
        reconciliation = repository.reconcile_candidate(candidate, recorded_at=_NOW)
    return database, reconciliation.reviews[0].review_id


def _reject_configuration_load(
    *,
    sources_path: Path | None = None,
    strategy_path: Path | None = None,
    execution_path: Path | None = None,
    scope: ConfigurationScope = ConfigurationScope.EXECUTION,
) -> ApplicationConfig:
    raise AssertionError((sources_path, strategy_path, execution_path, scope))


def _update_report(run_id: str, *, durable_transition_count: int) -> IntelligenceUpdateReport:
    return IntelligenceUpdateReport(
        run_id=run_id,
        through=IntelligenceStage.SYNTHESIS,
        completed_stages=(),
        completed=IntelligenceWorkCompletionCounts(
            interpretation_bundles=0,
            interpretation_chunks=0,
            discovery_units=0,
            research_jobs=0,
            synthesis_units=0,
        ),
        usage=RunInferenceUsage(
            run_id=run_id,
            logical_call_count=0,
            failed_call_count=0,
            unavailable_usage_call_count=0,
            usage=InferenceUsage(),
        ),
        remaining=IntelligenceWorkStatus(
            pending_interpretation_bundles=0,
            active_interpretation_bundles=0,
            pending_interpretation_chunks=0,
            active_interpretation_chunks=0,
            pending_discovery_units=0,
            active_discovery_units=0,
            pending_research_jobs=0,
            active_research_jobs=0,
            pending_synthesis_units=0,
            active_synthesis_units=0,
        ),
        durable_transition_count=durable_transition_count,
    )


def test_intelligence_status_loads_only_nonsecret_intelligence_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "status.sqlite3")
    config = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    observed_scopes: list[ConfigurationScope] = []

    def load_intelligence_config(**kwargs: object) -> ApplicationConfig:
        scope = kwargs.get("scope")
        assert isinstance(scope, ConfigurationScope)
        observed_scopes.append(scope)
        return config

    monkeypatch.setattr(intelligence_cli, "_read_database", lambda: database)
    monkeypatch.setattr(
        intelligence_cli,
        "load_application_config",
        load_intelligence_config,
    )

    result = CliRunner().invoke(intelligence_app, ["status"])

    assert result.exit_code == 0
    assert observed_scopes == [ConfigurationScope.INTELLIGENCE]


def test_intelligence_audit_is_provider_and_configuration_free_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "audit.sqlite3")
    monkeypatch.setattr(intelligence_cli, "_read_database", lambda: database)
    monkeypatch.setattr(intelligence_cli, "load_application_config", _reject_configuration_load)

    result = CliRunner().invoke(app, ["--json", "intelligence", "audit"])

    assert result.exit_code == 0
    assert '"status":"healthy"' in result.stdout
    assert result.stderr == ""


def test_intelligence_reviews_is_provider_free_and_does_not_mutate_review_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, review_id = _semantic_review_database(tmp_path / "reviews.sqlite3")
    monkeypatch.setattr(intelligence_cli, "_read_database", lambda: database)
    monkeypatch.setattr(intelligence_cli, "load_application_config", _reject_configuration_load)
    with database.read_only_transaction() as connection:
        before_row = cast(
            "sqlite3.Row",
            connection.execute("SELECT count(*) FROM hypothesis_reviews").fetchone(),
        )

    result = CliRunner().invoke(app, ["--json", "intelligence", "reviews"])

    with database.read_only_transaction() as connection:
        after_row = cast(
            "sqlite3.Row",
            connection.execute("SELECT count(*) FROM hypothesis_reviews").fetchone(),
        )
    payload = cast("list[dict[str, object]]", json.loads(result.stdout))
    assert (result.exit_code, result.stderr, before_row[0], after_row[0], payload[0]["review_id"]) == (
        0,
        "",
        1,
        1,
        review_id,
    )


def test_intelligence_resolve_records_append_only_decision_without_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, review_id = _semantic_review_database(tmp_path / "resolve.sqlite3")
    monkeypatch.setattr(intelligence_cli, "_read_database", lambda: database)
    monkeypatch.setattr(intelligence_cli, "load_application_config", _reject_configuration_load)

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "intelligence",
            "resolve",
            review_id,
            "--decision",
            "distinct",
            "--actor",
            "operator",
            "--reason",
            "Different horizon intent.",
        ],
    )

    with database.read_only_transaction() as connection:
        durable = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT review.review_id, resolution.decision, resolution.actor, resolution.reason
                FROM hypothesis_reviews review
                JOIN hypothesis_review_resolutions resolution USING (review_id)"""
            ).fetchone(),
        )
    payload = cast("dict[str, object]", json.loads(result.stdout))
    assert (result.exit_code, result.stderr, tuple(durable), payload["status"]) == (
        0,
        "",
        (review_id, "distinct", "operator", "Different horizon intent."),
        "distinct",
    )


def test_intelligence_lineage_is_provider_free_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _review_id = _semantic_review_database(tmp_path / "lineage.sqlite3")
    monkeypatch.setattr(intelligence_cli, "_read_database", lambda: database)
    monkeypatch.setattr(intelligence_cli, "load_application_config", _reject_configuration_load)
    with database.read_only_transaction() as connection:
        before_row = cast(
            "sqlite3.Row",
            connection.execute("SELECT count(*) FROM candidate_hypothesis_memberships").fetchone(),
        )

    result = CliRunner().invoke(
        app,
        ["--json", "intelligence", "lineage", "candidate:1"],
    )

    with database.read_only_transaction() as connection:
        after_row = cast(
            "sqlite3.Row",
            connection.execute("SELECT count(*) FROM candidate_hypothesis_memberships").fetchone(),
        )
    payload = cast("dict[str, object]", json.loads(result.stdout))
    assert (result.exit_code, result.stderr, before_row[0], after_row[0], payload["candidate_ids"]) == (
        0,
        "",
        2,
        2,
        ["candidate:1"],
    )


def test_intelligence_show_reports_durable_usage_without_loading_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "show.sqlite3")
    run_id = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
    RunRepository(database).append_run(
        RunRecord(
            run_id=run_id,
            requested_as_of=_NOW,
            started_at=_NOW,
            known_at=_NOW,
            through_stage="A4",
            source_config_hash="a" * 64,
            intelligence_config_hash="b" * 64,
        )
    )
    monkeypatch.setattr(intelligence_cli, "_read_database", lambda: database)
    monkeypatch.setattr(
        intelligence_cli,
        "load_application_config",
        _reject_configuration_load,
    )

    result = CliRunner().invoke(app, ["--json", "intelligence", "show", run_id])

    assert result.exit_code == 0
    assert f'"run_id":"{run_id}"' in result.stdout
    assert '"logical_call_count":0' in result.stdout


def test_root_cli_has_no_ambiguous_run_command() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "\n run " not in result.stdout.lower()


def test__run_intelligence_updates_aggregates_separate_runs_and_stops_on_no_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "iterations.sqlite3")
    reports = iter(
        (
            _update_report("run-1", durable_transition_count=2),
            _update_report("run-2", durable_transition_count=1),
            _update_report("run-3", durable_transition_count=0),
        )
    )
    calls: list[str] = []

    def execute(**_kwargs: object) -> IntelligenceUpdateReport:
        report = next(reports)
        calls.append(report.run_id)
        return report

    monkeypatch.setattr(intelligence_cli, "_database", lambda: database)
    monkeypatch.setattr(intelligence_cli, "execute_intelligence_update", execute)

    result = intelligence_cli._run_intelligence_updates(  # pyright: ignore[reportPrivateUsage]
        source="news",
        through=IntelligenceStage.SYNTHESIS,
        iterations=5,
    )

    assert (result.requested_iterations, result.performed_iterations) == (5, 3)
    assert result.stopped_because_no_transition is True
    assert tuple(report.run_id for report in result.runs) == ("run-1", "run-2", "run-3")
    assert result.run_ids == ("run-1", "run-2", "run-3")
    assert result.durable_transition_count == 3
    assert result.completed == IntelligenceWorkCompletionCounts(
        interpretation_bundles=0,
        interpretation_chunks=0,
        discovery_units=0,
        research_jobs=0,
        synthesis_units=0,
    )
    assert result.usage == InferenceUsage()
    assert calls == ["run-1", "run-2", "run-3"]


def test__run_intelligence_updates_does_not_erase_a_completed_run_when_a_later_iteration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "partial.sqlite3")
    durable_runs = RunRepository(database)
    calls = 0
    completed_run_id = "4fa85f64-5717-4562-b3fc-2c963f66afa6"

    def execute(**_kwargs: object) -> IntelligenceUpdateReport:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second durable run failed")
        durable_runs.append_run(
            RunRecord(
                run_id=completed_run_id,
                requested_as_of=_NOW,
                started_at=_NOW,
                known_at=_NOW,
                through_stage="A4",
                source_config_hash="a" * 64,
                intelligence_config_hash="b" * 64,
            )
        )
        return _update_report(completed_run_id, durable_transition_count=1)

    monkeypatch.setattr(intelligence_cli, "_database", lambda: database)
    monkeypatch.setattr(intelligence_cli, "execute_intelligence_update", execute)

    with pytest.raises(RuntimeError):
        _ = intelligence_cli._run_intelligence_updates(  # pyright: ignore[reportPrivateUsage]
            source=None,
            through=IntelligenceStage.SYNTHESIS,
            iterations=2,
        )

    assert durable_runs.get_run(completed_run_id).run_id == completed_run_id
