"""Tests for portfolio-plan and execution-authority CLI commands."""

from pytest import MonkeyPatch
from typer.testing import CliRunner

from money_pit import __main__
from money_pit.config import Config
from money_pit.execution_control.repository import SqliteExecutionAuthorityRepository
from money_pit.plans.lifecycle import ApprovalRecord
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.portfolio_plan import PortfolioPlan


def test_plan_commands_are_registered() -> None:
    names: set[str | None] = {command.name for command in __main__.plan_app.registered_commands}
    assert {"show", "approve", "reject"} <= names


def test_execution_commands_are_registered() -> None:
    names: set[str | None] = {command.name for command in __main__.execution_app.registered_commands}
    assert {"status", "disable"} <= names


def test_execute_command_is_registered() -> None:
    assert any(command.name == "execute" for command in __main__.app.registered_commands)


def test_plan_show_outputs_exact_plan_hash(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: execution_repository)

    result = CliRunner().invoke(__main__.app, ["plan", "show", persisted_portfolio_plan.payload.plan_id])

    assert persisted_portfolio_plan.plan_hash in result.output


def test_plan_approve_persists_exact_hash(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: execution_repository)

    _ = CliRunner().invoke(__main__.app, ["plan", "approve", persisted_portfolio_plan.payload.plan_id])

    decision = execution_repository.latest_for(persisted_portfolio_plan.payload.plan_id)
    assert isinstance(decision, ApprovalRecord)
    assert decision.plan_hash == persisted_portfolio_plan.plan_hash


def test_execution_disable_persists_global_kill_switch(
    execution_repository: SqliteExecutionAuthorityRepository,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: execution_repository)

    _ = CliRunner().invoke(__main__.app, ["execution", "disable", "manual halt"])

    assert execution_repository.get_control_state().disabled


def test_execute_with_disabled_kill_switch_fails_before_broker_writer(
    execution_repository: SqliteExecutionAuthorityRepository,
    persisted_portfolio_plan: PortfolioPlan,
    monkeypatch: MonkeyPatch,
) -> None:
    config = Config(
        alpaca_service="service",
        alpaca_username="user",
        alpaca_paper=True,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        execution_policy_version="policy-1",
        maximum_order_notional=1_000.0,
        maximum_daily_turnover=0.2,
    )
    monkeypatch.setattr(__main__, "load_config", lambda: config)
    monkeypatch.setattr(__main__, "_execution_authority_repository", lambda: execution_repository)

    result = CliRunner().invoke(__main__.app, ["execute", persisted_portfolio_plan.payload.plan_id])

    assert (result.exit_code, "kill switch" in result.output) == (1, True)
