"""CLI tests for portfolio-intelligence reports, plans, execution controls, and replay."""

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

from money_pit import __main__
from money_pit.config import Config
from money_pit.execution_control.models import ExecutionControlState
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.plans.lifecycle import PlanDecision
from money_pit.reports.portfolio import PortfolioReview
from money_pit.runs.paths import RepositoryPaths
from money_pit.runs.repository import RunNotFoundError
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload
from money_pit.schemas.portfolio_plan import ProposedTrade


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


@pytest.fixture
def portfolio_plan(now: datetime) -> PortfolioPlan:
    return PortfolioPlan.from_payload(
        PortfolioPlanPayload(
            plan_id="plan-cli",
            created_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(days=365),
            portfolio_snapshot_id="portfolio-1",
            market_snapshot_id="market-1",
            policy_version="policy-1",
            target_weights={"SPY": 0.8},
            proposed_trades=(
                ProposedTrade(
                    instrument="SPY",
                    side="buy",
                    quantity=1.0,
                    estimated_notional=600.0,
                    tax_cost_known=True,
                ),
            ),
            turnover_estimate=0.01,
            evidence_gate_results={"independent_support": True},
            constraint_results={"position_limit": True},
        )
    )


@pytest.fixture
def enabled_state(now: datetime) -> ExecutionControlState:
    return ExecutionControlState(disabled=False, changed_at=now, actor="system")


@dataclass
class _AuthorityRepository:
    plan: PortfolioPlan | None
    state: ExecutionControlState
    decision: PlanDecision | None = None

    def get(self, plan_id: str) -> PortfolioPlan | None:
        if self.plan is not None and self.plan.payload.plan_id == plan_id:
            return self.plan
        return None

    def append(self, decision: PlanDecision) -> None:
        self.decision = decision

    def latest_for(self, plan_id: str) -> PlanDecision | None:
        _ = plan_id
        return self.decision

    def get_control_state(self) -> ExecutionControlState:
        return self.state

    def disable(self, state: ExecutionControlState) -> None:
        self.state = state

    def enable(self, state: ExecutionControlState) -> None:
        self.state = state


@dataclass
class _PlanWriteRepository:
    imported_plan: PortfolioPlan | None = None

    def append(self, plan: PortfolioPlan) -> None:
        self.imported_plan = plan


@pytest.fixture
def authority_repository(
    portfolio_plan: PortfolioPlan,
    enabled_state: ExecutionControlState,
) -> _AuthorityRepository:
    return _AuthorityRepository(plan=portfolio_plan, state=enabled_state)


@pytest.fixture
def use_authority_repository(
    monkeypatch: MonkeyPatch,
    authority_repository: _AuthorityRepository,
) -> None:
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: authority_repository)


def _config(execution_mode: ExecutionMode, *, complete_policy: bool = True) -> Config:
    return Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=True,
        execution_mode=execution_mode,
        execution_policy_version="policy-1" if complete_policy else None,
        maximum_order_notional=1_000.0 if complete_policy else None,
        maximum_daily_turnover=0.2 if complete_policy else None,
    )


def test_portfolio_intelligence_command_groups_are_registered() -> None:
    names = {group.name for group in __main__.app.registered_groups}

    assert {"portfolio", "plan", "execution", "source", "claims"} <= names


def test_portfolio_review_with_valid_writes_all_formats(
    runner: CliRunner,
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    review = PortfolioReview(
        title="Review",
        plan=portfolio_plan,
        current_weights={"SPY": 0.75},
        risk_contributions={},
        sector_exposures={},
        factor_exposures={},
        thesis_statuses={},
        conflicting_claims=(),
        source_reliability={},
        evidence=(),
        scenarios=(),
        sensitivities=(),
        approval_state="pending",
        execution_state="observe",
    )
    review_input = tmp_path / "review.json"
    _ = review_input.write_text(review.model_dump_json(), encoding="utf-8")
    reports = tmp_path / "reports"

    result = runner.invoke(
        __main__.app,
        ["portfolio", "review", "--input", str(review_input), "--report-directory", str(reports)],
    )

    assert result.exit_code == 0
    assert {path.suffix for path in reports.iterdir()} == {".json", ".md", ".html"}


def test_portfolio_review_with_missing_input_exits_one(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        __main__.app,
        ["portfolio", "review", "--input", str(tmp_path / "missing.json")],
    )

    assert result.exit_code == 1


def test_plan_import_persists_validated_hash_bound_plan(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
    portfolio_plan: PortfolioPlan,
    tmp_path: Path,
) -> None:
    repository = _PlanWriteRepository()
    input_path: Path = tmp_path / "plan.json"
    _ = input_path.write_text(portfolio_plan.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(__main__, "_portfolio_plan_repository", lambda: repository)

    result = runner.invoke(__main__.app, ["plan", "import", str(input_path)])

    assert result.exit_code == 0
    assert repository.imported_plan == portfolio_plan


def test_plan_show_with_existing_plan_prints_hash(
    runner: CliRunner,
    use_authority_repository: None,
    portfolio_plan: PortfolioPlan,
) -> None:
    result = runner.invoke(__main__.app, ["plan", "show", portfolio_plan.payload.plan_id])

    assert result.exit_code == 0
    assert portfolio_plan.plan_hash in result.output


def test_plan_show_with_missing_plan_exits_one(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
    enabled_state: ExecutionControlState,
) -> None:
    repository = _AuthorityRepository(plan=None, state=enabled_state)
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: repository)

    result = runner.invoke(__main__.app, ["plan", "show", "missing"])

    assert result.exit_code == 1


def test_plan_approve_persists_exact_hash(
    runner: CliRunner,
    use_authority_repository: None,
    authority_repository: _AuthorityRepository,
    portfolio_plan: PortfolioPlan,
) -> None:
    result = runner.invoke(
        __main__.app,
        ["plan", "approve", portfolio_plan.payload.plan_id, "--actor", "alice"],
    )

    assert result.exit_code == 0
    assert isinstance(authority_repository.decision, ApprovalRecord)
    assert authority_repository.decision.plan_hash == portfolio_plan.plan_hash


def test_plan_reject_persists_reason(
    runner: CliRunner,
    use_authority_repository: None,
    authority_repository: _AuthorityRepository,
    portfolio_plan: PortfolioPlan,
) -> None:
    result = runner.invoke(
        __main__.app,
        ["plan", "reject", portfolio_plan.payload.plan_id, "stale evidence", "--actor", "alice"],
    )

    assert result.exit_code == 0
    assert authority_repository.decision is not None
    assert getattr(authority_repository.decision, "reason", None) == "stale evidence"


def test_execution_disable_persists_disabled_state(
    runner: CliRunner,
    use_authority_repository: None,
    authority_repository: _AuthorityRepository,
) -> None:
    result = runner.invoke(__main__.app, ["execution", "disable", "incident", "--actor", "alice"])

    assert result.exit_code == 0
    assert authority_repository.state.disabled is True


def test_execution_enable_does_not_create_plan_authority(
    runner: CliRunner,
    use_authority_repository: None,
    authority_repository: _AuthorityRepository,
) -> None:
    authority_repository.state = authority_repository.state.model_copy(update={"disabled": True})

    result = runner.invoke(__main__.app, ["execution", "enable", "reviewed", "--actor", "alice"])

    assert result.exit_code == 0
    assert authority_repository.state.disabled is False
    assert authority_repository.decision is None


def test_execution_status_reports_kill_switch(
    runner: CliRunner,
    use_authority_repository: None,
) -> None:
    result = runner.invoke(__main__.app, ["execution", "status"])

    assert result.exit_code == 0
    assert '"disabled": false' in result.output


def test_execute_plan_with_kill_switch_exits_before_policy_check(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
    authority_repository: _AuthorityRepository,
) -> None:
    authority_repository.state = authority_repository.state.model_copy(update={"disabled": True})
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: authority_repository)
    monkeypatch.setattr(__main__, "load_config", lambda: _config(ExecutionMode.AUTONOMOUS))

    result = runner.invoke(__main__.app, ["execute", "plan-cli"])

    assert result.exit_code == 1
    assert "kill switch" in result.output


def test_execute_plan_with_observe_mode_exits_before_writer_creation(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
    authority_repository: _AuthorityRepository,
) -> None:
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: authority_repository)
    monkeypatch.setattr(__main__, "load_config", lambda: _config(ExecutionMode.OBSERVE))

    result = runner.invoke(__main__.app, ["execute", "plan-cli"])

    assert result.exit_code == 1
    assert "Observe mode" in result.output


def test_execute_plan_with_incomplete_policy_exits_before_approval_check(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
    authority_repository: _AuthorityRepository,
) -> None:
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: authority_repository)
    monkeypatch.setattr(
        __main__,
        "load_config",
        lambda: _config(ExecutionMode.APPROVAL_REQUIRED, complete_policy=False),
    )

    result = runner.invoke(__main__.app, ["execute", "plan-cli"])

    assert result.exit_code == 1
    assert "policy is incomplete" in result.output


def test_execute_plan_with_missing_exact_approval_exits_one(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
    authority_repository: _AuthorityRepository,
) -> None:
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: authority_repository)
    monkeypatch.setattr(__main__, "load_config", lambda: _config(ExecutionMode.APPROVAL_REQUIRED))

    result = runner.invoke(__main__.app, ["execute", "plan-cli"])

    assert result.exit_code == 1
    assert "lacks current operator approval" in result.output


def test_execute_plan_with_exact_approval_still_fails_closed_without_live_context(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
    authority_repository: _AuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    authority_repository.decision = ApprovalRecord(
        decision_id="approval-1",
        plan_id=portfolio_plan.payload.plan_id,
        plan_hash=portfolio_plan.plan_hash,
        decided_at=now,
        decided_by="alice",
    )
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: authority_repository)
    monkeypatch.setattr(__main__, "load_config", lambda: _config(ExecutionMode.APPROVAL_REQUIRED))

    result = runner.invoke(__main__.app, ["execute", "plan-cli"])

    assert result.exit_code == 1
    assert "No trusted live AuthorizationContext" in result.output


def test_replay_with_resolution_failure_exits_one(
    runner: CliRunner,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail(_run_id: str, _paths: RepositoryPaths) -> object:
        raise RunNotFoundError("missing")

    monkeypatch.setattr(__main__, "_resolve_replay_snapshot", fail)

    result = runner.invoke(__main__.app, ["replay", "missing"])

    assert result.exit_code == 1


def test__resolve_replay_snapshot_with_empty_repository_raises(
    tmp_path: Path,
) -> None:
    paths = RepositoryPaths.from_data_root(
        tmp_path / "data",
        legacy_daily_show_root=tmp_path / "legacy",
    )

    with pytest.raises(RunNotFoundError):
        _ = __main__._resolve_replay_snapshot("missing", paths)
