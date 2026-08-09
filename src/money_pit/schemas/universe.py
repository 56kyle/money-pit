"""Module containing typed investment-universe origins."""

from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class UniverseLayer(StrEnum):
    """Origins from which an investment candidate can enter the universe."""

    HOLDING = "holding"
    WATCHLIST = "watchlist"
    SOURCE_MENTION = "source_mention"
    BENCHMARK = "benchmark"
    QUANTITATIVE_SCREEN = "quantitative_screen"
    EXPLICIT_PROXY = "explicit_proxy"
    PORTFOLIO_GAP = "portfolio_gap"


class DiscoveryBasis(BaseModel):
    """Exact sourced-claim or authorized universe origin for a candidate."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_claim_keys: tuple[str, ...] = ()
    universe_layer: UniverseLayer | None = None
    universe_reference: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        """Require claims or one complete authorized universe origin."""
        has_claims = bool(self.source_claim_keys)
        has_universe = self.universe_layer is not None or self.universe_reference is not None
        if has_claims == has_universe:
            raise ValueError("discovery basis requires claim keys or one universe layer/reference, exclusively")
        if has_universe and (self.universe_layer is None or self.universe_reference is None):
            raise ValueError("universe discovery requires both layer and reference")
        return self
