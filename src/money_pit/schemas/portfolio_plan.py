"""Module containing deterministic hashed portfolio-plan contracts."""

import hashlib
import json
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.tax import LotSelectionPolicy
from money_pit.schemas.tax import WashSaleStatus


class PlannedTaxLot(BaseModel):
    """One exact acquisition lot quantity committed by a sell trade."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    lot_id: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    unit_cost: float = Field(ge=0)
    estimated_gain: float
    acquired_at: AwareDatetime
    long_term: bool
    estimated_tax_cost: float | None = Field(default=None, ge=0)


class PlannedLotSelection(BaseModel):
    """Typed tax-lot choice and completeness covered by the plan hash."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    policy: LotSelectionPolicy
    tax_snapshot_id: str = Field(min_length=1)
    lots: tuple[PlannedTaxLot, ...] = Field(min_length=1)
    estimated_gain: float
    tax_cost_known: bool
    wash_sale_status: WashSaleStatus


class UnresolvedLotSelection(BaseModel):
    """Typed non-executable sell when exact configured tax lots are unavailable."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    policy: LotSelectionPolicy
    tax_snapshot_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    tax_cost_known: bool = False


class ProposedTrade(BaseModel):
    """A deterministic trade proposed to move toward target weights."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    asset_class: TradableAssetClass
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: float = Field(gt=0)
    estimated_notional: float = Field(gt=0)
    tax_cost_known: bool
    lot_selection: PlannedLotSelection | UnresolvedLotSelection | None = None

    @model_validator(mode="after")
    def require_sell_lots(self) -> Self:
        """Require exact lots for sells and forbid irrelevant lots on buys."""
        if self.side == "sell" and self.lot_selection is None:
            raise ValueError("sell trades require a typed tax-lot selection state")
        if self.side == "buy" and self.lot_selection is not None:
            raise ValueError("buy trades cannot select tax lots")
        if self.lot_selection is not None and self.tax_cost_known != self.lot_selection.tax_cost_known:
            raise ValueError("trade and lot-selection tax certainty disagree")
        return self


class RejectedCandidate(BaseModel):
    """A universe candidate excluded from a plan with explicit reasons."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)


class PlanTaxEstimate(BaseModel):
    """Exact selected-lot tax estimate in account currency."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    estimated_cost: float | None = Field(default=None, ge=0)
    known: bool

    @model_validator(mode="after")
    def require_known_value(self) -> Self:
        """Keep certainty aligned with the presence of an exact cost."""
        if self.known != (self.estimated_cost is not None):
            raise ValueError("known tax estimate requires an exact currency cost")
        return self


class PortfolioPlanPayload(BaseModel):
    """All capital-sensitive inputs and outputs covered by a plan hash."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    portfolio_snapshot_id: str = Field(min_length=1)
    market_snapshot_id: str = Field(min_length=1)
    decision_snapshot_id: str = Field(min_length=1)
    decision_snapshot_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1)
    broker_environment: BrokerEnvironment
    execution_config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_policy_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_policy_version: str | None = Field(default=None, min_length=1)
    policy_version: str = Field(min_length=1)
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    target_weights: dict[str, float]
    proposed_trades: tuple[ProposedTrade, ...]
    rejected_candidates: tuple[RejectedCandidate, ...] = ()
    expected_risk_change: float | None = None
    expected_return_change: float | None = None
    turnover_estimate: float = Field(ge=0)
    tax_estimate: PlanTaxEstimate
    evidence_gate_results: dict[str, bool] = Field(default_factory=dict)
    constraint_results: dict[str, bool] = Field(default_factory=dict)
    historical_non_executable: bool = False

    @model_validator(mode="after")
    def require_complete_execution_policy_binding(self) -> Self:
        """Reject partial optional policy bindings."""
        values = (self.execution_config_hash, self.execution_policy_hash, self.execution_policy_version)
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError("execution-policy binding must be wholly present or absent")
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize the payload deterministically for hashing and approval."""
        encoded: str = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return encoded.encode("utf-8")

    def sha256(self) -> str:
        """Return the lowercase SHA-256 digest of the canonical payload."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class PortfolioPlan(BaseModel):
    """A portfolio plan whose digest detects any payload mutation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    payload: PortfolioPlanPayload
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_payload(cls, payload: PortfolioPlanPayload) -> Self:
        """Create a plan carrying the canonical digest of its payload."""
        return cls(payload=payload, plan_hash=payload.sha256())

    @model_validator(mode="after")
    def validate_plan_hash(self) -> Self:
        """Reject plans whose persisted digest does not match their payload."""
        if self.plan_hash != self.payload.sha256():
            raise ValueError("plan_hash does not match the canonical payload")
        return self
