"""Module containing execution authority contracts for the money_pit package."""

import hashlib
import json
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class BrokerEnvironment(StrEnum):
    """Broker account environment."""

    PAPER = "paper"
    LIVE = "live"


class ExecutionMode(StrEnum):
    """Authority granted to the execution subsystem."""

    OBSERVE = "observe"
    APPROVAL_REQUIRED = "approval_required"
    AUTONOMOUS = "autonomous"


class TradableAssetClass(StrEnum):
    """Asset classes eligible for release 0.0.2 capital actions."""

    US_EQUITY = "us_equity"
    US_ETF = "us_etf"


class ExecutionPolicy(BaseModel):
    """Versioned environment and authority policy."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(min_length=1)
    broker_environment: BrokerEnvironment
    execution_mode: ExecutionMode = ExecutionMode.APPROVAL_REQUIRED
    allowed_asset_classes: tuple[TradableAssetClass, ...] = (TradableAssetClass.US_EQUITY,)
    maximum_order_notional: float = Field(gt=0)
    maximum_daily_turnover: float = Field(ge=0, le=1)

    def fingerprint(self) -> str:
        """Return the exact active execution-policy binding."""
        payload = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(payload).hexdigest()
