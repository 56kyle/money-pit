"""Module containing execution authority contracts for the money_pit package."""

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


class ExecutionPolicy(BaseModel):
    """Versioned environment and authority policy."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(min_length=1)
    broker_environment: BrokerEnvironment
    execution_mode: ExecutionMode = ExecutionMode.APPROVAL_REQUIRED
    allowed_asset_classes: tuple[str, ...] = ("us_equity",)
    maximum_order_notional: float = Field(gt=0)
    maximum_daily_turnover: float = Field(ge=0, le=1)
