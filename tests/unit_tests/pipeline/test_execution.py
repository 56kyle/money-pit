"""Tests for money_pit.pipeline.execution — honest, crash-survivable independent-path node.

Pins ADR 0003 plus the Wave C fill-observability contract: per-leg fail-and-continue,
incremental crash-survivable journaling, the fail-closed atomic-group terminus, and the
poll-until-terminal fill observation that never fabricates a fill. The poll clock and sleep
are injected so every observation path is deterministic without real time.
"""

from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pytest import FixtureRequest

from money_pit.alpaca_orders import FillObservationError
from money_pit.alpaca_orders import OrderNotYetVisibleError
from money_pit.compute.fills import build_fill_observation
from money_pit.graph.state import PipelineNode
from money_pit.pipeline.execution import AtomicGroupNotSupportedError
from money_pit.pipeline.execution import OrderSubmissionError
from money_pit.pipeline.execution import _apply_fill
from money_pit.pipeline.execution import _poll_fill
from money_pit.pipeline.execution import _reject_atomic_groups
from money_pit.pipeline.execution import _submit_step
from money_pit.pipeline.execution import _unobserved_fill
from money_pit.pipeline.execution import make_execution_node
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.analysis_draft import Scenario
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.enums import ActionType
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.fills import FillObservation
from money_pit.schemas.journal import ExecutionJournal
from money_pit.schemas.journal import ExecutionJournalEntry


_SLUG = "test-run"
_POLL_INTERVAL = 1.0
_POLL_TIMEOUT = 30.0
_PAST_DEADLINE = _POLL_TIMEOUT + 1.0
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
            notional="1500.00",
            qty=None,
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


class _ScriptedObserver:
    """Injected observe_fill: replays a scripted sequence of observations/errors, recording each client_order_id.

    One entry is consumed per call and the final entry repeats once the script is exhausted, so
    a test can script accepted->accepted->filled or a never-resolving error. An Exception entry is
    raised (modelling a retryable 404 or a read failure); a FillObservation entry is returned.
    A single non-list script is that one item repeating forever.
    """

    def __init__(self, script: FillObservation | Exception | list[FillObservation | Exception]) -> None:
        self._script: list[FillObservation | Exception] = script if isinstance(script, list) else [script]
        self.calls: list[str] = []

    def __call__(self, client_order_id: str) -> FillObservation:
        item = self._script[min(len(self.calls), len(self._script) - 1)]
        self.calls.append(client_order_id)
        if isinstance(item, Exception):
            raise item
        return item


class _NoOpSleep:
    """Injected sleep: records the interval of every call without pausing, so poll timing is inspectable but instant."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, interval: float) -> None:
        self.calls.append(interval)


class _FakeClock:
    """Injected monotonic: emits successive values from ticks (final value repeating), recording read count.

    A single-tick clock never advances, so a poll loop stops only on a terminal observation; a
    clock whose later tick exceeds the deadline drives _poll_fill to its timeout branch.
    """

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = ticks
        self.reads = 0

    def __call__(self) -> float:
        value = self._ticks[min(self.reads, len(self._ticks) - 1)]
        self.reads += 1
        return value


def _build_node(
    place_order: _StubPlaceOrder,
    observer: _ScriptedObserver,
    *,
    sleep: _NoOpSleep | None = None,
    clock: _FakeClock | None = None,
) -> PipelineNode:
    return make_execution_node(
        place_order,
        observer,
        poll_interval=_POLL_INTERVAL,
        poll_timeout=_POLL_TIMEOUT,
        sleep=sleep if sleep is not None else _NoOpSleep(),
        monotonic=clock if clock is not None else _FakeClock([0.0]),
    )


def _read_journal(working_dir: Path) -> ExecutionJournal:
    return ExecutionJournal.model_validate_json((working_dir / "execution_journal.json").read_text(encoding="utf-8"))


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


@pytest.fixture
def single_step_working_dir(tmp_path: Path) -> Path:
    steps = [_make_action_step("A001")]
    _ = (tmp_path / "action_steps.json").write_text(
        _ACTION_STEPS_ADAPTER.dump_json(steps, indent=2).decode("utf-8"),
        encoding="utf-8",
    )
    return tmp_path


# --- All filled -> EXECUTED_CLEAN ---------------------------------------------------------------


@pytest.fixture
def all_filled_observer() -> _ScriptedObserver:
    return _ScriptedObserver(build_fill_observation("filled", 8.0, 100.0))


@pytest.fixture
def all_filled_journal(execution_working_dir: Path, all_filled_observer: _ScriptedObserver) -> ExecutionJournal:
    node = _build_node(_StubPlaceOrder(), all_filled_observer)
    _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    return _read_journal(execution_working_dir)


def test_make_execution_node_with_all_filled_phases(all_filled_journal: ExecutionJournal) -> None:
    assert all(entry.phase == ExecutionPhase.FILLED for entry in all_filled_journal.entries)


def test_make_execution_node_with_all_filled_fills(all_filled_journal: ExecutionJournal) -> None:
    assert all(
        (entry.filled_qty, entry.filled_avg_price, entry.realized_notional) == (8.0, 100.0, 800.0)
        for entry in all_filled_journal.entries
    )


def test_make_execution_node_with_all_filled_outcome(all_filled_journal: ExecutionJournal) -> None:
    assert all_filled_journal.outcome == ExecutionOutcome.EXECUTED_CLEAN


def test_make_execution_node_with_all_filled_polls_each_step(
    all_filled_observer: _ScriptedObserver, all_filled_journal: ExecutionJournal
) -> None:
    assert all_filled_observer.calls == [f"{_SLUG}:A001", f"{_SLUG}:A002", f"{_SLUG}:A003"]


# --- Rejected -> EXECUTION_FAILED ---------------------------------------------------------------


@pytest.fixture
def rejected_journal(execution_working_dir: Path) -> ExecutionJournal:
    observer = _ScriptedObserver(build_fill_observation("rejected", None, None))
    node = _build_node(_StubPlaceOrder(), observer)
    _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    return _read_journal(execution_working_dir)


def test_make_execution_node_with_rejected_fill_phase(rejected_journal: ExecutionJournal) -> None:
    assert all(entry.phase == ExecutionPhase.REJECTED for entry in rejected_journal.entries)


def test_make_execution_node_with_rejected_fill_outcome(rejected_journal: ExecutionJournal) -> None:
    assert rejected_journal.outcome == ExecutionOutcome.EXECUTION_FAILED


# --- Terminal partial -> EXECUTED_INCOMPLETE ----------------------------------------------------


@pytest.fixture
def partial_journal(execution_working_dir: Path) -> ExecutionJournal:
    observer = _ScriptedObserver(build_fill_observation("done_for_day", 3.0, 100.0))
    node = _build_node(_StubPlaceOrder(), observer)
    _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    return _read_journal(execution_working_dir)


def test_make_execution_node_with_terminal_partial_phase(partial_journal: ExecutionJournal) -> None:
    assert all(entry.phase == ExecutionPhase.PARTIALLY_FILLED for entry in partial_journal.entries)


def test_make_execution_node_with_terminal_partial_outcome(partial_journal: ExecutionJournal) -> None:
    assert partial_journal.outcome == ExecutionOutcome.EXECUTED_INCOMPLETE


# --- Open at timeout -> last non-terminal -> EXECUTED_INCOMPLETE --------------------------------


@pytest.fixture
def open_at_timeout_journal(single_step_working_dir: Path) -> ExecutionJournal:
    observer = _ScriptedObserver(build_fill_observation("accepted", None, None))
    node = _build_node(_StubPlaceOrder(), observer, clock=_FakeClock([0.0, _PAST_DEADLINE]))
    _ = node({"slug": _SLUG, "working_dir": str(single_step_working_dir)})
    return _read_journal(single_step_working_dir)


def test_make_execution_node_with_open_at_timeout_phase(open_at_timeout_journal: ExecutionJournal) -> None:
    assert open_at_timeout_journal.entries[0].phase == ExecutionPhase.SUBMITTED


def test_make_execution_node_with_open_at_timeout_status(open_at_timeout_journal: ExecutionJournal) -> None:
    assert open_at_timeout_journal.entries[0].status == "accepted"


def test_make_execution_node_with_open_at_timeout_outcome(open_at_timeout_journal: ExecutionJournal) -> None:
    assert open_at_timeout_journal.outcome == ExecutionOutcome.EXECUTED_INCOMPLETE


# --- Terminal after retries ---------------------------------------------------------------------


@pytest.fixture
def retry_observer() -> _ScriptedObserver:
    accepted = build_fill_observation("accepted", None, None)
    filled = build_fill_observation("filled", 8.0, 100.0)
    return _ScriptedObserver([accepted, accepted, filled])


@pytest.fixture
def retry_journal(single_step_working_dir: Path, retry_observer: _ScriptedObserver) -> ExecutionJournal:
    node = _build_node(_StubPlaceOrder(), retry_observer, clock=_FakeClock([0.0]))
    _ = node({"slug": _SLUG, "working_dir": str(single_step_working_dir)})
    return _read_journal(single_step_working_dir)


def test_make_execution_node_with_retries_final_filled(retry_journal: ExecutionJournal) -> None:
    assert retry_journal.entries[0].phase == ExecutionPhase.FILLED


def test_make_execution_node_with_retries_polls_until_terminal(
    retry_observer: _ScriptedObserver, retry_journal: ExecutionJournal
) -> None:
    assert len(retry_observer.calls) == 3


# --- 404 (OrderNotYetVisibleError) then filled --------------------------------------------------


@pytest.fixture
def not_yet_visible_observer() -> _ScriptedObserver:
    return _ScriptedObserver([OrderNotYetVisibleError("not yet indexed"), build_fill_observation("filled", 8.0, 100.0)])


@pytest.fixture
def not_yet_visible_journal(
    single_step_working_dir: Path, not_yet_visible_observer: _ScriptedObserver
) -> ExecutionJournal:
    node = _build_node(_StubPlaceOrder(), not_yet_visible_observer, clock=_FakeClock([0.0]))
    _ = node({"slug": _SLUG, "working_dir": str(single_step_working_dir)})
    return _read_journal(single_step_working_dir)


def test_make_execution_node_with_not_yet_visible_retried(
    not_yet_visible_observer: _ScriptedObserver, not_yet_visible_journal: ExecutionJournal
) -> None:
    assert len(not_yet_visible_observer.calls) == 2


def test_make_execution_node_with_not_yet_visible_final_filled(not_yet_visible_journal: ExecutionJournal) -> None:
    assert not_yet_visible_journal.entries[0].phase == ExecutionPhase.FILLED


# --- FillObservationError never resolves -> unobserved SUBMITTED -> EXECUTED_INCOMPLETE ---------


@pytest.fixture
def never_observed_journal(single_step_working_dir: Path) -> ExecutionJournal:
    observer = _ScriptedObserver(FillObservationError("observer transport down"))
    node = _build_node(_StubPlaceOrder(), observer, clock=_FakeClock([0.0, _PAST_DEADLINE]))
    _ = node({"slug": _SLUG, "working_dir": str(single_step_working_dir)})
    return _read_journal(single_step_working_dir)


def test_make_execution_node_with_never_observed_phase(never_observed_journal: ExecutionJournal) -> None:
    assert never_observed_journal.entries[0].phase == ExecutionPhase.SUBMITTED


def test_make_execution_node_with_never_observed_marker_status(never_observed_journal: ExecutionJournal) -> None:
    assert never_observed_journal.entries[0].status == "unobserved"


def test_make_execution_node_with_never_observed_fills_none(never_observed_journal: ExecutionJournal) -> None:
    entry = never_observed_journal.entries[0]
    assert (entry.filled_qty, entry.filled_avg_price, entry.realized_notional) == (None, None, None)


def test_make_execution_node_with_never_observed_outcome(never_observed_journal: ExecutionJournal) -> None:
    assert never_observed_journal.outcome == ExecutionOutcome.EXECUTED_INCOMPLETE


# --- Submit failure -> FAILED, no poll ----------------------------------------------------------


@pytest.fixture
def submit_failure_place_order() -> _StubPlaceOrder:
    return _StubPlaceOrder(failures={1: OrderSubmissionError("broker rejected leg 2")})


@pytest.fixture
def submit_failure_observer() -> _ScriptedObserver:
    return _ScriptedObserver(build_fill_observation("filled", 8.0, 100.0))


@pytest.fixture
def submit_failure_journal(
    execution_working_dir: Path,
    submit_failure_place_order: _StubPlaceOrder,
    submit_failure_observer: _ScriptedObserver,
) -> ExecutionJournal:
    node = _build_node(submit_failure_place_order, submit_failure_observer)
    _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    return _read_journal(execution_working_dir)


def test_make_execution_node_with_submit_failure_completes(submit_failure_journal: ExecutionJournal) -> None:
    assert len(submit_failure_journal.entries) == 3


def test_make_execution_node_with_submit_failure_phases(submit_failure_journal: ExecutionJournal) -> None:
    assert [entry.phase for entry in submit_failure_journal.entries] == [
        ExecutionPhase.FILLED,
        ExecutionPhase.FAILED,
        ExecutionPhase.FILLED,
    ]


def test_make_execution_node_with_submit_failure_outcome(submit_failure_journal: ExecutionJournal) -> None:
    assert submit_failure_journal.outcome == ExecutionOutcome.EXECUTION_FAILED


def test_make_execution_node_with_submit_failure_failed_entry(submit_failure_journal: ExecutionJournal) -> None:
    failed_entry = submit_failure_journal.entries[1]
    assert failed_entry.broker_order_id is None
    assert failed_entry.error is not None


def test_make_execution_node_with_submit_failure_skips_poll_for_failed_step(
    submit_failure_observer: _ScriptedObserver, submit_failure_journal: ExecutionJournal
) -> None:
    assert submit_failure_observer.calls == [f"{_SLUG}:A001", f"{_SLUG}:A003"]


# --- Unexpected place_order error propagates, leaving a truthful partial journal ----------------


def test_make_execution_node_with_unexpected_error_propagates(execution_working_dir: Path) -> None:
    place_order = _StubPlaceOrder(failures={1: RuntimeError("unexpected broker client bug")})
    node = _build_node(place_order, _ScriptedObserver(build_fill_observation("filled", 8.0, 100.0)))
    with pytest.raises(RuntimeError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})


# --- Crash mid-poll leaves the incremental SUBMITTED entry with no terminal outcome -------------


@pytest.fixture
def crashed_working_dir(single_step_working_dir: Path) -> Path:
    observer = _ScriptedObserver(KeyError("uncaught observer bug"))
    node = _build_node(_StubPlaceOrder(), observer, clock=_FakeClock([0.0]))
    with pytest.raises(KeyError):
        _ = node({"slug": _SLUG, "working_dir": str(single_step_working_dir)})
    return single_step_working_dir


def test_make_execution_node_with_mid_poll_crash_writes_partial_journal(crashed_working_dir: Path) -> None:
    assert (crashed_working_dir / "execution_journal.json").exists()


def test_make_execution_node_with_mid_poll_crash_journal_holds_submitted_entry(crashed_working_dir: Path) -> None:
    journal = _read_journal(crashed_working_dir)
    assert [entry.phase for entry in journal.entries] == [ExecutionPhase.SUBMITTED]


def test_make_execution_node_with_mid_poll_crash_journal_has_no_terminal_outcome(crashed_working_dir: Path) -> None:
    journal = _read_journal(crashed_working_dir)
    assert journal.outcome is None


# --- Atomic group rejected before any submission ------------------------------------------------


@pytest.fixture
def atomic_place_order() -> _StubPlaceOrder:
    return _StubPlaceOrder()


@pytest.fixture
def atomic_observer() -> _ScriptedObserver:
    return _ScriptedObserver(build_fill_observation("filled", 8.0, 100.0))


@pytest.mark.parametrize(
    "execution_working_dir__steps",
    [[_make_action_step("A001"), _make_action_step("A002", group_id="G1")]],
    indirect=True,
)
def test_make_execution_node_with_group_id_raises(
    execution_working_dir: Path, atomic_place_order: _StubPlaceOrder, atomic_observer: _ScriptedObserver
) -> None:
    node = _build_node(atomic_place_order, atomic_observer)
    with pytest.raises(AtomicGroupNotSupportedError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})


@pytest.mark.parametrize(
    "execution_working_dir__steps",
    [[_make_action_step("A001"), _make_action_step("A002", group_id="G1")]],
    indirect=True,
)
def test_make_execution_node_with_group_id_places_no_orders(
    execution_working_dir: Path, atomic_place_order: _StubPlaceOrder, atomic_observer: _ScriptedObserver
) -> None:
    node = _build_node(atomic_place_order, atomic_observer)
    with pytest.raises(AtomicGroupNotSupportedError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    assert len(atomic_place_order.calls) == 0


@pytest.mark.parametrize(
    "execution_working_dir__steps",
    [[_make_action_step("A001"), _make_action_step("A002", group_id="G1")]],
    indirect=True,
)
def test_make_execution_node_with_group_id_observes_no_fills(
    execution_working_dir: Path, atomic_place_order: _StubPlaceOrder, atomic_observer: _ScriptedObserver
) -> None:
    node = _build_node(atomic_place_order, atomic_observer)
    with pytest.raises(AtomicGroupNotSupportedError):
        _ = node({"slug": _SLUG, "working_dir": str(execution_working_dir)})
    assert atomic_observer.calls == []


# --- _submit_step -------------------------------------------------------------------------------


def test__submit_step_with_success_phase() -> None:
    entry = _submit_step(_make_action_step("A001"), _StubPlaceOrder())
    assert entry.phase == ExecutionPhase.SUBMITTED


def test__submit_step_with_success_broker_order_id() -> None:
    entry = _submit_step(_make_action_step("A001"), _StubPlaceOrder())
    assert entry.broker_order_id is not None


def test__submit_step_with_success_error() -> None:
    entry = _submit_step(_make_action_step("A001"), _StubPlaceOrder())
    assert entry.error is None


def test__submit_step_with_rejection_phase() -> None:
    place_order = _StubPlaceOrder(failures={0: OrderSubmissionError("rejected")})
    entry = _submit_step(_make_action_step("A001"), place_order)
    assert entry.phase == ExecutionPhase.FAILED


def test__submit_step_with_rejection_broker_order_id() -> None:
    place_order = _StubPlaceOrder(failures={0: OrderSubmissionError("rejected")})
    entry = _submit_step(_make_action_step("A001"), place_order)
    assert entry.broker_order_id is None


def test__submit_step_with_rejection_error() -> None:
    place_order = _StubPlaceOrder(failures={0: OrderSubmissionError("rejected")})
    entry = _submit_step(_make_action_step("A001"), place_order)
    assert entry.error is not None


def test__submit_step_with_unexpected_error_propagates() -> None:
    place_order = _StubPlaceOrder(failures={0: RuntimeError("bug")})
    with pytest.raises(RuntimeError):
        _ = _submit_step(_make_action_step("A001"), place_order)


# --- _reject_atomic_groups ----------------------------------------------------------------------


def test__reject_atomic_groups_with_group_id_raises() -> None:
    with pytest.raises(AtomicGroupNotSupportedError):
        _reject_atomic_groups([_make_action_step("A001", group_id="G1")])


def test__reject_atomic_groups_with_no_group_ids_returns_none() -> None:
    assert _reject_atomic_groups([_make_action_step("A001")]) is None


# --- _poll_fill ---------------------------------------------------------------------------------


def test__poll_fill_with_terminal_first_returns_terminal() -> None:
    filled = build_fill_observation("filled", 8.0, 100.0)
    result = _poll_fill(
        _ScriptedObserver(filled),
        f"{_SLUG}:A001",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
        sleep=_NoOpSleep(),
        monotonic=_FakeClock([0.0]),
    )
    assert result == filled


def test__poll_fill_with_terminal_first_polls_once() -> None:
    observer = _ScriptedObserver(build_fill_observation("filled", 8.0, 100.0))
    _ = _poll_fill(
        observer,
        f"{_SLUG}:A001",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
        sleep=_NoOpSleep(),
        monotonic=_FakeClock([0.0]),
    )
    assert len(observer.calls) == 1


def test__poll_fill_with_open_at_timeout_returns_last_non_terminal() -> None:
    accepted = build_fill_observation("accepted", None, None)
    result = _poll_fill(
        _ScriptedObserver(accepted),
        f"{_SLUG}:A001",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
        sleep=_NoOpSleep(),
        monotonic=_FakeClock([0.0, _PAST_DEADLINE]),
    )
    assert result == accepted


def test__poll_fill_with_never_observed_returns_unobserved_marker() -> None:
    result = _poll_fill(
        _ScriptedObserver(FillObservationError("transport down")),
        f"{_SLUG}:A001",
        interval=_POLL_INTERVAL,
        timeout=_POLL_TIMEOUT,
        sleep=_NoOpSleep(),
        monotonic=_FakeClock([0.0, _PAST_DEADLINE]),
    )
    assert result == _unobserved_fill()


# --- _apply_fill --------------------------------------------------------------------------------


def test__apply_fill_copies_phase_and_status() -> None:
    updated = _apply_fill(_make_entry(ExecutionPhase.SUBMITTED), build_fill_observation("filled", 8.0, 100.0))
    assert (updated.phase, updated.status) == (ExecutionPhase.FILLED, "filled")


def test__apply_fill_copies_fill_fields() -> None:
    updated = _apply_fill(_make_entry(ExecutionPhase.SUBMITTED), build_fill_observation("filled", 8.0, 100.0))
    assert (updated.filled_qty, updated.filled_avg_price, updated.realized_notional) == (8.0, 100.0, 800.0)


def test__apply_fill_preserves_identity_fields() -> None:
    entry = _make_entry(ExecutionPhase.SUBMITTED)
    updated = _apply_fill(entry, build_fill_observation("filled", 8.0, 100.0))
    assert (updated.step_id, updated.client_order_id, updated.broker_order_id) == (
        entry.step_id,
        entry.client_order_id,
        entry.broker_order_id,
    )
