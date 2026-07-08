"""Module containing the deterministic-fetch result union used across the money_pit package.

A deterministic fetch has three distinct outcomes that must never be conflated: a concrete
value, a legitimately-empty upstream response, and a failure to retrieve at all. Collapsing
the latter two into a single ``None`` hides outages behind ordinary absence, so the fetch
protocol returns this closed union and callers dispatch on the variant.
"""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict


class FetchValue(BaseModel):
    """A deterministic fetch that returned a concrete numeric observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    value: float


class NoData(BaseModel):
    """A deterministic fetch that completed cleanly but the series or ticker held no observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class FetchError(BaseModel):
    """A deterministic fetch that failed against an expected upstream error; the datum is unknown, not absent."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    reason: str


FetchResult = FetchValue | NoData | FetchError
