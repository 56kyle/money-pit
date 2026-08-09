"""Module containing content-fingerprinted point-in-time portfolio state."""

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


class PortfolioStatePosition(BaseModel):
    """One position included in an authoritative portfolio fingerprint."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    quantity: float
    market_price: float = Field(ge=0)
    market_value: float


class PortfolioStatePayload(BaseModel):
    """All broker state that can change a portfolio plan."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1)
    broker_environment: BrokerEnvironment = BrokerEnvironment.PAPER
    account_status: str = Field(default="ACTIVE", min_length=1)
    trading_blocked: bool = False
    captured_at: AwareDatetime
    available_cash: float
    positions: tuple[PortfolioStatePosition, ...]
    open_order_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_canonical_order(self) -> Self:
        """Require stable unique position and order identities."""
        instruments: tuple[str, ...] = tuple(position.instrument for position in self.positions)
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("positions must be unique and sorted by instrument")
        if self.open_order_ids != tuple(sorted(set(self.open_order_ids))):
            raise ValueError("open_order_ids must be unique and sorted")
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize every state-bearing field deterministically."""
        serialized: str = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return serialized.encode("utf-8")

    def fingerprint(self) -> str:
        """Return the content fingerprint used as portfolio snapshot ID."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class PortfolioStateSnapshot(BaseModel):
    """A portfolio state whose identifier is derived from its content."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: PortfolioStatePayload

    @classmethod
    def from_payload(cls, payload: PortfolioStatePayload) -> Self:
        """Create a snapshot with its canonical content fingerprint."""
        return cls(snapshot_id=payload.fingerprint(), payload=payload)

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        """Reject an identifier that is not the payload fingerprint."""
        if self.snapshot_id != self.payload.fingerprint():
            raise ValueError("portfolio snapshot ID does not match its payload")
        return self


class MarketQuote(BaseModel):
    """One point-in-time quote included in market-state authority."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    price: float = Field(gt=0)
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    observed_at: AwareDatetime
    source: str = Field(min_length=1)


class MarketStatePayload(BaseModel):
    """All point-in-time market data used to produce a plan."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    captured_at: AwareDatetime
    quotes: tuple[MarketQuote, ...]

    @model_validator(mode="after")
    def validate_canonical_order(self) -> Self:
        """Require stable unique quote identities."""
        instruments: tuple[str, ...] = tuple(quote.instrument for quote in self.quotes)
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("quotes must be unique and sorted by instrument")
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize every market input deterministically."""
        serialized: str = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return serialized.encode("utf-8")

    def fingerprint(self) -> str:
        """Return the content fingerprint used as market snapshot ID."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class MarketStateSnapshot(BaseModel):
    """Market state whose identifier is derived from its content."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: MarketStatePayload

    @classmethod
    def from_payload(cls, payload: MarketStatePayload) -> Self:
        """Create a snapshot with its canonical content fingerprint."""
        return cls(snapshot_id=payload.fingerprint(), payload=payload)

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        """Reject an identifier that is not the payload fingerprint."""
        if self.snapshot_id != self.payload.fingerprint():
            raise ValueError("market snapshot ID does not match its payload")
        return self
