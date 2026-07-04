"""Tests for money_pit.pipeline.execution — honest, crash-survivable independent-path node.

Pins wave S1 / ADR 0003: outcome derivation, per-leg fail-and-continue, incremental
crash-survivable journaling, and the fail-closed atomic-group terminus.
"""

from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pytest import FixtureRequest

from money_pit.pipeline.execution import (
    AtomicGroupNotSupportedError,
    OrderSubmissionError,
    _derive_outcome,
    make_execution_node,
)
from money_pit.schemas.action_steps import ActionStep, ExecutionParameters
from money_pit.schemas.analysis_draft import Scenario, ScenarioTable
from money_pit.schemas.enums import (
    ActionType,
    ConvictionLevel,
    ExecutionOutcome,
    ExecutionPhase,
    RegimeTag,
)
from money_pit.schemas.journal import ExecutionJournal, ExecutionJournalEntry

_SLUG = "test-run"
_ACTION_STEPS_ADAPTER: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


def _make_action_step(step_id: str, *, group_id: str | None = None, symbol: str = "NVDA") -> ActionStep:
    scenario = Scenario(
        probability=33,
        return_pct=0.1,
        timeframe=None,
        confirming_metric=None,
        mechanism=None,
        max_drawdown=None,
    )
    return ActionStep(
        step_id=step_id,
        instrument=symbol,
        action_type=ActionType.BUY,
        description="Establish a starter position.",
        group_id=group_id,
        execution_parameters=ExecutionParameters(
            symbol=symbol,
            notional=1500.0,
            quantity=None,
            side="buy",
            type="market",
            time_in_force="day",
            client_order_id=f"{_SLUG}:{step_id}",
        ),
        one_sentence_thesis="The thesis still holds on current data.",
        regime_tag=RegimeTag.UNCERTAIN,
        expected_value=1.0,
        scenario_table=ScenarioTable(bull=scenario, base=scenario, bear=scenario),
        invalidation_conditions=[],
        sizing_rationale="Sized to conviction and account risk budget.",
        conviction=ConvictionLevel.MEDIUM,
    )


def _make_entry(phase: ExecutionPhase, *, step_id: str = "A001") -> ExecutionJournalEntry:
    submitted = phase == ExecutionPhase.SUBMITTED
    return ExecutionJournalEntry(
        step_id=step_id,
        group_id=None,
        client_order_id=f"{_SLUG}:{step_id}",
        phase=phase,
        intended={},
        broker_order_id="broker-x" if submitted else None,
        status=None,
        filled_qty=None,
        filled_avg_price=None,
        realized_notional=None,
        compensation_of=None,
        error=None if submitted else "broker rejected the order",
        timestamp="2026-07-01T00:00:00Z",
    )


class _StubPlaceOrder:
    """Injected place_order stub: returns a deterministic broker id, records calls, and raises on request.

    failures maps a zero-based call index to the exception raised on that call, so a single
    real callable models the happy path, a mid-loop broker rejection, and an unexpected crash.
    """

    def __init__(self, failures: dict[int, Exception] | None = None) -> None:
        self._failures = failures or {}
        self.calls: list[ExecutionParameters] = []

    def __call__(self, params: ExecutionParameters) -> str:
        index = len(self.calls)
        self.calls.append(params)
        if index in self._failures:
            raise self._failures[index]
        return f"broker-{index}"


@pytest.mark.parametrize(
    ("phases", "expected"),
    [
        (
            [ExecutionPhase.SUBMITTED, ExecutionPhase.SUBMITTED, ExecutionPhase.SUBMITTED],
            ExecutionOutcome.EXECUTED_CLEAN,
        ),
        (
            [ExecutionPhase.SUBMITTED, ExecutionPhase.FAILED, ExecutionPhase.SUBMITTED],
            ExecutionOutcome.EXECUTION_FAILED,
        ),
        ([], ExecutionOutcome.EXECUTED_CLEAN),
    ],
)
def test__derive_outcome(phases: list[ExecutionPhase], expected: ExecutionOutcome) -> None:
    entries = [_make_entry(phase, step_id=f"A00{index}") for index, phase in enumerate(phases)]
    assert _derive_outcome(entries) == expected


@pytest.fixture
def execution_working_dir__steps(request: FixtureRequest) -> list[ActionStep]:
    return getattr(
        request,
        "param",
        [_make_action_step("A001"), _make_action_step("A002"), _make_action_step("A003")],
    )


@pytest.fixture
def execution_working_dir(tmp_path: Path, execution_working_dir__steps: list[ActionStep]) -> Path:
    _ = (tmp_path / "action_steps.json").write_text(
        _ACTION_STEPS_ADAPTER.dump_json(execution_working_dir__steps, indent=2).decode("utf-8"),
        encoding="utf-8",
    )
    return tmp_path


def _read_journal(working_dir: Path) -> ExecutionJournal:
    return ExecutionJournal.model_validate_json((working_dir / "execution_journal.json").read_text(encoding="utf-8"))


@pytest.fixture
def leg_failure_place_order() -> _StubPlaceOrder:
    return _StubPlaceOrder(failures={1: OrderSubmissionError("broker rejected leg 2")})


@pytest.fixture
def leg_failure_journal(execution_working_dir: Path, leg_failure_place_order: _StubPlaceOrder) -> ExecutionJournal:
    node = make_execution_node(leg_failure_place_order)
    _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    return _read_journal(execution_working_dir)


def test_make_execution_node_with_leg_failure_completes(leg_failure_journal: ExecutionJournal) -> None:
    assert len(leg_failure_journal.entries) == 3


def test_make_execution_node_with_leg_failure_phases(leg_failure_journal: ExecutionJournal) -> None:
    assert [entry.phase for entry in leg_failure_journal.entries] == [
        ExecutionPhase.SUBMITTED,
        ExecutionPhase.FAILED,
        ExecutionPhase.SUBMITTED,
    ]


def test_make_execution_node_with_leg_failure_outcome(leg_failure_journal: ExecutionJournal) -> None:
    assert leg_failure_journal.outcome == ExecutionOutcome.EXECUTION_FAILED


def test_make_execution_node_with_leg_failure_failed_entry(leg_failure_journal: ExecutionJournal) -> None:
    failed_entry = leg_failure_journal.entries[1]
    assert failed_entry.broker_order_id is None
    assert failed_entry.error is not None


def test_make_execution_node_with_unexpected_error_propagates(execution_working_dir: Path) -> None:
    place_order = _StubPlaceOrder(failures={1: RuntimeError("unexpected broker client bug")})
    node = make_execution_node(place_order)
    with pytest.raises(RuntimeError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})


@pytest.fixture
def crashed_working_dir(execution_working_dir: Path) -> Path:
    place_order = _StubPlaceOrder(failures={1: RuntimeError("unexpected broker client bug")})
    node = make_execution_node(place_order)
    with pytest.raises(RuntimeError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    return execution_working_dir


def test_make_execution_node_with_unexpected_error_writes_partial_journal(crashed_working_dir: Path) -> None:
    journal_path = crashed_working_dir / "execution_journal.json"
    assert journal_path.exists()


def test_make_execution_node_with_unexpected_error_journal_holds_first_entry(crashed_working_dir: Path) -> None:
    journal = _read_journal(crashed_working_dir)
    assert [entry.phase for entry in journal.entries] == [ExecutionPhase.SUBMITTED]


def test_make_execution_node_with_unexpected_error_journal_has_no_terminal_outcome(crashed_working_dir: Path) -> None:
    journal = _read_journal(crashed_working_dir)
    assert journal.outcome is None


@pytest.fixture
def atomic_place_order() -> _StubPlaceOrder:
    return _StubPlaceOrder()


@pytest.mark.parametrize(
    "execution_working_dir__steps",
    [[_make_action_step("A001"), _make_action_step("A002", group_id="G1")]],
    indirect=True,
)
def test_make_execution_node_with_group_id_raises(
    execution_working_dir: Path, atomic_place_order: _StubPlaceOrder
) -> None:
    node = make_execution_node(atomic_place_order)
    with pytest.raises(AtomicGroupNotSupportedError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})


@pytest.mark.parametrize(
    "execution_working_dir__steps",
    [[_make_action_step("A001"), _make_action_step("A002", group_id="G1")]],
    indirect=True,
)
def test_make_execution_node_with_group_id_places_no_orders(
    execution_working_dir: Path, atomic_place_order: _StubPlaceOrder
) -> None:
    node = make_execution_node(atomic_place_order)
    with pytest.raises(AtomicGroupNotSupportedError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    assert len(atomic_place_order.calls) == 0


@pytest.fixture
def happy_journal(execution_working_dir: Path) -> ExecutionJournal:
    node = make_execution_node(_StubPlaceOrder())
    _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    return _read_journal(execution_working_dir)


def test_make_execution_node_with_all_success_outcome(happy_journal: ExecutionJournal) -> None:
    assert happy_journal.outcome == ExecutionOutcome.EXECUTED_CLEAN


def test_make_execution_node_with_all_success_phases(happy_journal: ExecutionJournal) -> None:
    assert all(entry.phase == ExecutionPhase.SUBMITTED for entry in happy_journal.entries)


def test_make_execution_node_with_all_success_fill_markers_none(happy_journal: ExecutionJournal) -> None:
    assert all(
        entry.filled_qty is None and entry.filled_avg_price is None and entry.realized_notional is None
        for entry in happy_journal.entries
    )
