"""Module containing scheduled investment-outcome contracts."""

import hashlib
import json
import math
from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator


class OutcomeBoundary(StrEnum):
    """Boundaries at which an investment outcome is evaluated."""

    EVENT = "event"
    REVIEW = "review"
    HORIZON = "horizon"


class OutcomeSchedule(BaseModel):
    """A version-bound future outcome observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    schedule_id: str = Field(min_length=1)
    thesis_revision_id: str = Field(min_length=1)
    plan_id: str | None = Field(default=None, min_length=1)
    plan_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    benchmark_snapshot_id: str = Field(min_length=1)
    benchmark_id: str = Field(min_length=1)
    benchmark_provider: str = Field(min_length=1)
    benchmark_weights: dict[str, float] = Field(min_length=1)
    benchmark_composition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark_input_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    portfolio_input_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    portfolio_observation_provider: str = Field(min_length=1)
    portfolio_observation_query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark_observation_query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    portfolio_baseline_observed_at: AwareDatetime
    benchmark_baseline_observed_at: AwareDatetime
    portfolio_baseline_value: float = Field(gt=0)
    benchmark_baseline_value: float = Field(gt=0)
    scenario_distribution_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_versions: dict[str, str] = Field(min_length=1)
    model_versions: dict[str, str] = Field(min_length=1)
    boundary: OutcomeBoundary
    observe_at: AwareDatetime
    schedule_details: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_exact_benchmark_composition(self) -> Self:
        """Require the persisted benchmark weights and identity to match their hash."""
        if any(weight <= 0 or weight > 1 for weight in self.benchmark_weights.values()) or not math.isclose(
            sum(self.benchmark_weights.values()), 1.0, abs_tol=1e-9
        ):
            raise ValueError("benchmark weights must be positive and sum to one")
        payload = {
            "benchmark_id": self.benchmark_id,
            "provider": self.benchmark_provider,
            "weights": self.benchmark_weights,
        }
        expected = hashlib.sha256(
            json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.benchmark_composition_hash != expected:
            raise ValueError("benchmark composition hash does not match persisted weights")
        return self


class OutcomeMetric(BaseModel):
    """Immutable measured outcomes for one scheduled observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    metric_id: str = Field(min_length=1)
    schedule_id: str = Field(min_length=1)
    thesis_revision_id: str = Field(min_length=1)
    plan_id: str | None = Field(default=None, min_length=1)
    plan_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    benchmark_snapshot_id: str = Field(min_length=1)
    evaluated_at: AwareDatetime
    metrics: dict[str, JsonValue] = Field(min_length=1)
