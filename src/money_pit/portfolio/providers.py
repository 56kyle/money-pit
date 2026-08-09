"""Module containing read-only point-in-time portfolio data boundaries."""

import hashlib
import json
from typing import ClassVar
from typing import Protocol
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.tax import TaxLotSnapshot


class InstrumentRisk(BaseModel):
    """Point-in-time risk classification for one instrument."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    instrument: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    factor_loadings: dict[str, float]
    covariance: dict[str, float]


class RiskSnapshotPayload(BaseModel):
    """Every risk input consumed by a portfolio decision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    captured_at: AwareDatetime
    observations: tuple[InstrumentRisk, ...]

    @model_validator(mode="after")
    def require_canonical_instruments(self) -> Self:
        """Require unique sorted observations and a complete covariance matrix."""
        instruments: tuple[str, ...] = tuple(item.instrument for item in self.observations)
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("risk observations must be unique and sorted by instrument")
        required: set[str] = set(instruments)
        if any(set(item.covariance) != required for item in self.observations):
            raise ValueError("each risk covariance row must cover every instrument")
        return self

    def fingerprint(self) -> str:
        """Return a deterministic digest of the complete risk payload."""
        return _model_fingerprint(self)


class RiskSnapshot(BaseModel):
    """Content-addressed risk state."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: RiskSnapshotPayload

    @classmethod
    def from_payload(cls, payload: RiskSnapshotPayload) -> Self:
        """Create a fingerprinted risk snapshot."""
        return cls(snapshot_id=payload.fingerprint(), payload=payload)

    @model_validator(mode="after")
    def require_fingerprint(self) -> Self:
        """Reject tampered risk state."""
        if self.snapshot_id != self.payload.fingerprint():
            raise ValueError("risk snapshot ID does not match its payload")
        return self


class InstrumentLiquidity(BaseModel):
    """Point-in-time execution capacity for one instrument."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    instrument: str = Field(min_length=1)
    average_daily_notional: float = Field(gt=0)
    maximum_participation_rate: float = Field(gt=0, le=1)
    estimated_slippage_bps: float = Field(ge=0)
    tradable: bool


class LiquiditySnapshotPayload(BaseModel):
    """Every liquidity input consumed by a portfolio decision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    captured_at: AwareDatetime
    observations: tuple[InstrumentLiquidity, ...]

    @model_validator(mode="after")
    def require_canonical_instruments(self) -> Self:
        """Require unique sorted liquidity observations."""
        instruments: tuple[str, ...] = tuple(item.instrument for item in self.observations)
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("liquidity observations must be unique and sorted by instrument")
        return self

    def fingerprint(self) -> str:
        """Return a deterministic digest of the complete liquidity payload."""
        return _model_fingerprint(self)


class LiquiditySnapshot(BaseModel):
    """Content-addressed liquidity and tradability state."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: LiquiditySnapshotPayload

    @classmethod
    def from_payload(cls, payload: LiquiditySnapshotPayload) -> Self:
        """Create a fingerprinted liquidity snapshot."""
        return cls(snapshot_id=payload.fingerprint(), payload=payload)

    @model_validator(mode="after")
    def require_fingerprint(self) -> Self:
        """Reject tampered liquidity state."""
        if self.snapshot_id != self.payload.fingerprint():
            raise ValueError("liquidity snapshot ID does not match its payload")
        return self


class PortfolioStateProvider(Protocol):
    """Read-only provider of authoritative portfolio state."""

    def snapshot(self) -> PortfolioStateSnapshot:
        """Return current immutable portfolio state."""
        ...


class MarketStateProvider(Protocol):
    """Read-only provider of authoritative market state."""

    def snapshot(self, instruments: tuple[str, ...]) -> MarketStateSnapshot:
        """Return current immutable quotes for the exact requested instruments."""
        ...


class RiskStateProvider(Protocol):
    """Read-only provider of portfolio risk inputs."""

    def snapshot(self, instruments: tuple[str, ...]) -> RiskSnapshot:
        """Return current immutable risk inputs for the requested universe."""
        ...


class LiquidityStateProvider(Protocol):
    """Read-only provider of liquidity and tradability inputs."""

    def snapshot(self, instruments: tuple[str, ...]) -> LiquiditySnapshot:
        """Return current immutable liquidity state for the requested universe."""
        ...


class TaxLotStateProvider(Protocol):
    """Read-only provider of point-in-time tax lots."""

    def snapshot(self) -> TaxLotSnapshot:
        """Return current immutable tax-lot state."""
        ...


def _model_fingerprint(model: BaseModel) -> str:
    serialized: str = json.dumps(
        model.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
