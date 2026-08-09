"""Module containing deterministic thesis-return calibration."""

import math
from typing import ClassVar
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class ScenarioEstimate(BaseModel):
    """One mutually exclusive outcome used to estimate thesis return."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)
    expected_return: float

    @model_validator(mode="after")
    def require_finite_return(self) -> Self:
        """Reject non-finite scenario returns."""
        if not math.isfinite(self.expected_return):
            raise ValueError("scenario expected return must be finite")
        return self


class ScenarioDistribution(BaseModel):
    """A normalized, uniquely named scenario distribution."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    scenarios: tuple[ScenarioEstimate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_normalized_unique_scenarios(self) -> Self:
        """Require probabilities to form one complete distribution."""
        names: tuple[str, ...] = tuple(item.name for item in self.scenarios)
        if len(names) != len(set(names)):
            raise ValueError("scenario names must be unique")
        probability: float = sum(item.probability for item in self.scenarios)
        if not math.isclose(probability, 1.0, abs_tol=1e-9):
            raise ValueError("scenario probabilities must sum to one")
        return self

    def expected_return(self) -> float:
        """Return the probability-weighted scenario return."""
        return sum(item.probability * item.expected_return for item in self.scenarios)


class ReturnCalibration(BaseModel):
    """Versioned deterministic multipliers applied after scenario synthesis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    calibration_version: str = Field(min_length=1)
    thesis_confidence: float = Field(ge=0, le=1)
    verification_multiplier: float = Field(ge=0, le=1)
    freshness_multiplier: float = Field(ge=0, le=1)
    uncertainty_multiplier: float = Field(ge=0, le=1)


class CalibratedExpectedReturn(BaseModel):
    """Auditable components of one deterministic expected return."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    scenario_expected_return: float
    calibrated_expected_return: float
    calibration: ReturnCalibration


def calibrate_expected_return(
    distribution: ScenarioDistribution,
    calibration: ReturnCalibration,
) -> CalibratedExpectedReturn:
    """Apply confidence, verification, freshness, and uncertainty exactly once."""
    scenario_return: float = distribution.expected_return()
    calibrated: float = (
        scenario_return
        * calibration.thesis_confidence
        * calibration.verification_multiplier
        * calibration.freshness_multiplier
        * calibration.uncertainty_multiplier
    )
    return CalibratedExpectedReturn(
        scenario_expected_return=scenario_return,
        calibrated_expected_return=calibrated,
        calibration=calibration,
    )
