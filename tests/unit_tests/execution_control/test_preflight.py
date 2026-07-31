"""Tests for deterministic pre-execution validation."""

from datetime import datetime

from money_pit.execution_control.errors import StateCheckUnavailableError
from money_pit.execution_control.models import PreflightDenialCode
from money_pit.execution_control.preflight import validate_pre_execution
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import ExecutionPolicy
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.portfolio_plan import PortfolioPlanPayload


def _policy(mode: ExecutionMode) -> ExecutionPolicy:
    return ExecutionPolicy(
        policy_version="policy-1",
        broker_environment=BrokerEnvironment.PAPER,
        execution_mode=mode,
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )


def _unchanged(_plan: PortfolioPlan) -> bool:
    return True


def _unavailable(_plan: PortfolioPlan) -> bool:
    raise StateCheckUnavailableError("upstream unavailable")


def test_validate_pre_execution_autonomous_without_coverage_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    result = validate_pre_execution(
        portfolio_plan,
        _policy(ExecutionMode.AUTONOMOUS),
        execution_repository,
        execution_repository,
        now=now,
        portfolio_unchanged=_unchanged,
        market_unchanged=_unchanged,
    )

    assert PreflightDenialCode.AUTONOMOUS_POLICY_NOT_COVERED in {denial.code for denial in result.denials}


def test_validate_pre_execution_with_missing_evidence_gates_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    payload: PortfolioPlanPayload = portfolio_plan.payload.model_copy(update={"evidence_gate_results": {}})
    plan: PortfolioPlan = PortfolioPlan.from_payload(payload)

    result = validate_pre_execution(
        plan,
        _policy(ExecutionMode.APPROVAL_REQUIRED),
        execution_repository,
        execution_repository,
        now=now,
        portfolio_unchanged=_unchanged,
        market_unchanged=_unchanged,
    )

    assert PreflightDenialCode.EVIDENCE_GATE_FAILED in {denial.code for denial in result.denials}


def test_validate_pre_execution_with_unavailable_portfolio_state_fails_closed(
    execution_repository: SqliteExecutionAuthorityRepository,
    portfolio_plan: PortfolioPlan,
    now: datetime,
) -> None:
    result = validate_pre_execution(
        portfolio_plan,
        _policy(ExecutionMode.APPROVAL_REQUIRED),
        execution_repository,
        execution_repository,
        now=now,
        portfolio_unchanged=_unavailable,
        market_unchanged=_unchanged,
    )

    assert PreflightDenialCode.PORTFOLIO_STATE_DRIFT in {denial.code for denial in result.denials}
