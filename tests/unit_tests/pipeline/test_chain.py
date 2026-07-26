"""Unit tests for the linear planning chain: the stage-to-successor mapping that bounds a `--through` run.

The expected interrupt targets are written out as an explicit literal table rather than derived from
`PLANNING_CHAIN`, for the same reason `PLANNING_CHAIN` is itself a literal rather than derived from `Stage`
(ADR 0038): an expectation computed from the thing under test absorbs any change to it and catches nothing.
Reordering the chain must fail here.

`PlanningChainError`'s two branches are unreachable through the real `Stage` type — every member sits in the
chain and the chain's terminal element (`execution`) is deliberately not a `Stage`. The guard is the code in
`chain.py`; what protects it here are the invariant tests below, which fail at test time on the only reachable
way either branch could arise (a `Stage` added without a chain position) rather than contriving a stand-in
`Stage` to reach a branch the type system and ADR 0036 both rule out.
"""

import pytest

from money_pit.pipeline.chain import EXECUTION_NODE
from money_pit.pipeline.chain import PLANNING_CHAIN
from money_pit.pipeline.chain import RECOVERY_NODE
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.chain import interrupt_before_successor_of


_EXPECTED_CHAIN: tuple[str, ...] = (
    "recovery",
    "snapshot",
    "aggregator",
    "questions",
    "retrieval",
    "analysis",
    "validator",
    "determination",
    "execution",
)

_EXPECTED_INTERRUPTS: dict[Stage, list[str]] = {
    Stage.SNAPSHOT: ["aggregator"],
    Stage.AGGREGATOR: ["questions"],
    Stage.QUESTIONS: ["retrieval"],
    Stage.RETRIEVAL: ["analysis"],
    Stage.ANALYSIS: ["validator"],
    Stage.VALIDATOR: ["determination"],
    Stage.DETERMINATION: ["execution"],
}


def test_planning_chain_holds_the_declared_order() -> None:
    """The chain is the single source of the `--through` semantics, so its order is pinned literally."""
    assert PLANNING_CHAIN == _EXPECTED_CHAIN


def test_planning_chain_starts_at_recovery() -> None:
    assert PLANNING_CHAIN[0] == RECOVERY_NODE


def test_planning_chain_ends_at_execution() -> None:
    """Execution terminates the chain, which is why no `--through` can name it and reach past it."""
    assert PLANNING_CHAIN[-1] == EXECUTION_NODE


def test_every_stage_appears_in_the_planning_chain() -> None:
    """A Stage added without a chain position would raise PlanningChainError in front of an operator."""
    assert {stage.value for stage in Stage} <= set(PLANNING_CHAIN)


def test_every_stage_is_covered_by_the_interrupt_table() -> None:
    assert set(_EXPECTED_INTERRUPTS) == set(Stage)


@pytest.mark.parametrize(
    ("through", "expected"),
    [pytest.param(stage, expected, id=stage.value) for stage, expected in _EXPECTED_INTERRUPTS.items()],
)
def test_interrupt_before_successor_of_with_a_stage(through: Stage, expected: list[str]) -> None:
    assert interrupt_before_successor_of(through) == expected


def test_interrupt_before_successor_of_with_determination_stops_before_execution() -> None:
    """The plan-only invocation ADR 0035's behavior now rests on: exactly one node, and it is execution.

    Interrupting only the successor rather than every downstream node is what leaves the notification and
    terminal branches reachable, so a plan-only run still emails with full fidelity.
    """
    assert interrupt_before_successor_of(Stage.DETERMINATION) == ["execution"]


def test_interrupt_before_successor_of_with_none() -> None:
    """No bound means no interrupt list at all, which is what lets a full run compile without a checkpointer."""
    assert interrupt_before_successor_of(None) == []
