"""Module containing deterministic hashed portfolio-plan contracts."""

import hashlib
import json
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator


class ProposedTrade(BaseModel):
    """A deterministic trade proposed to move toward target weights."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: float = Field(gt=0)
    estimated_notional: float = Field(gt=0)
    tax_cost_known: bool


class RejectedCandidate(BaseModel):
    """A universe candidate excluded from a plan with explicit reasons."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)


class PortfolioPlanPayload(BaseModel):
    """All capital-sensitive inputs and outputs covered by a plan hash."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    portfolio_snapshot_id: str = Field(min_length=1)
    market_snapshot_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    target_weights: dict[str, float]
    proposed_trades: tuple[ProposedTrade, ...]
    rejected_candidates: tuple[RejectedCandidate, ...] = ()
    expected_risk_change: float | None = None
    expected_return_change: float | None = None
    turnover_estimate: float = Field(ge=0)
    tax_estimates: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_gate_results: dict[str, bool] = Field(default_factory=dict)
    constraint_results: dict[str, bool] = Field(default_factory=dict)

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
