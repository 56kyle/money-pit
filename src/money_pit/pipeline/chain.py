"""Module containing the linear planning chain and the stages runnable standalone within it for the money_pit package."""

from enum import Enum
from typing import Literal


RECOVERY_NODE: Literal["recovery"] = "recovery"
EXECUTION_NODE: Literal["execution"] = "execution"

_UNCHAINED_STAGE_MESSAGE: str = "Stage '{stage}' is absent from the linear planning chain, so no successor to it exists."
_TERMINAL_STAGE_MESSAGE: str = "Stage '{stage}' ends the linear planning chain, so no successor to it exists."


class PlanningChainError(Exception):
    """Raised when a stage has no successor in the linear planning chain and so cannot bound a run."""


class Stage(str, Enum):
    """A pipeline node that may be run standalone against an existing run directory.

    The set is deliberately confined to nodes that neither move capital nor contact the
    owner: `recovery` and `notification` send email and `execution` places orders, so
    those stay reachable only through a full run (see ADR 0036).
    """

    SNAPSHOT = "snapshot"
    AGGREGATOR = "aggregator"
    QUESTIONS = "questions"
    RETRIEVAL = "retrieval"
    ANALYSIS = "analysis"
    VALIDATOR = "validator"
    DETERMINATION = "determination"


PLANNING_CHAIN: tuple[str, ...] = (
    RECOVERY_NODE,
    Stage.SNAPSHOT.value,
    Stage.AGGREGATOR.value,
    Stage.QUESTIONS.value,
    Stage.RETRIEVAL.value,
    Stage.ANALYSIS.value,
    Stage.VALIDATOR.value,
    Stage.DETERMINATION.value,
    EXECUTION_NODE,
)


def interrupt_before_successor_of(through: Stage | None) -> list[str]:
    """Return the nodes to interrupt before so a run stops after `through`, empty when `through` is None, raising PlanningChainError when `through` has no successor.

    The list holds exactly the one node that follows `through` in the linear planning
    chain, never every downstream node: interrupting only the successor leaves the
    notification and terminal branches reachable, which is what preserves the
    full-fidelity email behavior of ADR 0035. `PLANNING_CHAIN` is written out as an
    independent literal rather than derived from `Stage`, so a node inserted into the
    graph forces a conscious decision about where in the chain it sits.
    """
    if through is None:
        return []
    if through.value not in PLANNING_CHAIN:
        raise PlanningChainError(_UNCHAINED_STAGE_MESSAGE.format(stage=through.value))
    successor_index: int = PLANNING_CHAIN.index(through.value) + 1
    if successor_index >= len(PLANNING_CHAIN):
        raise PlanningChainError(_TERMINAL_STAGE_MESSAGE.format(stage=through.value))
    return [PLANNING_CHAIN[successor_index]]
