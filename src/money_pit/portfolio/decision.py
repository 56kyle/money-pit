"""Module binding exact decision snapshots to executable portfolio plans."""

from datetime import datetime
from typing import ClassVar
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import model_validator

from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.snapshots import DecisionSnapshot


class PlannedPortfolioDecision(BaseModel):
    """One exact decision snapshot bound into one exact executable plan."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision: DecisionSnapshot
    plan: PortfolioPlan

    @model_validator(mode="after")
    def require_plan_binding(self) -> Self:
        """Require plan authority to match every shared snapshot binding."""
        decision = self.decision
        decision_payload = decision.payload
        plan_payload = self.plan.payload
        if plan_payload.decision_snapshot_id != decision.decision_snapshot_id:
            raise ValueError("portfolio plan uses a different decision snapshot")
        if plan_payload.decision_snapshot_hash != decision.decision_hash:
            raise ValueError("portfolio plan is not bound to the exact decision hash")
        if plan_payload.portfolio_snapshot_id != decision_payload.portfolio_snapshot.snapshot_id:
            raise ValueError("portfolio plan uses a different portfolio snapshot")
        if plan_payload.market_snapshot_id != decision_payload.market_snapshot.snapshot_id:
            raise ValueError("portfolio plan uses a different market snapshot")
        if plan_payload.policy_version != decision_payload.policy_version:
            raise ValueError("portfolio plan uses a different policy")
        return self


class PointInTimeReplayError(Exception):
    """Raised when a portfolio decision cannot be replayed at an exact cutoff."""


def replay_decision_at(
    decision: PlannedPortfolioDecision,
    *,
    as_of: datetime,
) -> PlannedPortfolioDecision:
    """Validate and return the immutable decision for its exact historical cutoff."""
    validated: PlannedPortfolioDecision = PlannedPortfolioDecision.model_validate(decision.model_dump())
    payload = validated.decision.payload
    if payload.requested_as_of != as_of:
        raise PointInTimeReplayError("a decision can only be replayed at its recorded as-of time")
    captured_at_values: tuple[datetime, ...] = (
        payload.portfolio_snapshot.captured_at,
        payload.market_snapshot.captured_at,
        payload.risk_snapshot.captured_at,
        payload.liquidity_snapshot.captured_at,
        payload.tax_snapshot.captured_at,
    )
    if any(captured_at > payload.decision_at for captured_at in captured_at_values):
        raise PointInTimeReplayError("decision contains a snapshot unavailable at the replay cutoff")
    return validated
