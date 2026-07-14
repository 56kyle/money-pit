"""Module containing the ReconciledOrder and PriorRunReconciliation models of the prior-run recovery contract for the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.enums import RecoveryDecision


class ReconciledOrder(BaseModel):
    """One prior-run leg re-observed at the broker during recovery, classified open or settled."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    client_order_id: str
    observed_status: str
    phase: ExecutionPhase


class PriorRunReconciliation(BaseModel):
    """The recovery verdict for the most recent prior run, decided before the new run plans."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision: RecoveryDecision
    prior_slug: str | None
    prior_outcome: ExecutionOutcome | None
    open_orders: list[ReconciledOrder]
    settled_orders: list[ReconciledOrder]
