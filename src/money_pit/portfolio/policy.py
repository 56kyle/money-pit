"""Module containing complete versioned portfolio-policy contracts."""

from typing import ClassVar
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class PortfolioPolicy(BaseModel):
    """All deterministic limits and objective weights required for optimization."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(min_length=1)
    risk_aversion: float = Field(gt=0)
    turnover_penalty: float = Field(ge=0)
    tax_penalty: float = Field(ge=0)
    minimum_cash_weight: float = Field(ge=0, lt=1)
    maximum_position_weight: float = Field(gt=0, le=1)
    maximum_satellite_weight: float = Field(ge=0, le=1)
    maximum_turnover: float = Field(ge=0, le=1)
    minimum_trade_weight: float = Field(ge=0, le=1)
    maximum_position_change: float = Field(gt=0, le=1)
    minimum_core_weights: dict[str, float]
    maximum_sector_weights: dict[str, float]
    maximum_factor_exposures: dict[str, float] = Field(default_factory=dict)
    maximum_correlated_group_weights: dict[str, float] = Field(default_factory=dict)
    accept_optimal_inaccurate: bool
    feasibility_tolerance: float = Field(gt=0, le=0.01)

    @model_validator(mode="after")
    def validate_weight_limits(self) -> Self:
        """Reject incomplete or contradictory weight constraints."""
        if not self.maximum_sector_weights:
            raise ValueError("maximum_sector_weights must not be empty")
        if any(weight < 0 or weight > 1 for weight in self.minimum_core_weights.values()):
            raise ValueError("minimum_core_weights values must be between zero and one")
        if any(weight <= 0 or weight > 1 for weight in self.maximum_sector_weights.values()):
            raise ValueError("maximum_sector_weights values must be greater than zero and at most one")
        if any(limit <= 0 for limit in self.maximum_factor_exposures.values()):
            raise ValueError("maximum_factor_exposures values must be positive")
        if any(weight <= 0 or weight > 1 for weight in self.maximum_correlated_group_weights.values()):
            raise ValueError("maximum_correlated_group_weights values must be greater than zero and at most one")
        if sum(self.minimum_core_weights.values()) > 1 - self.minimum_cash_weight:
            raise ValueError("minimum core weights leave insufficient required cash")
        if self.minimum_trade_weight > self.maximum_position_change:
            raise ValueError("minimum trade weight exceeds maximum position change")
        return self
