"""requires_validation, has_actionable_content, ticker normalization, signal counts."""

import re

from money_pit.schemas.enums import SignalTier
from money_pit.schemas.signals import Claim


def requires_validation(tier: SignalTier) -> bool:
    return tier in (SignalTier.HIGH, SignalTier.MEDIUM)


def has_actionable_content(claims: list[Claim]) -> bool:
    return any(requires_validation(c.tier) for c in claims)


_TICKER_STRIP: re.Pattern[str] = re.compile(r"[^A-Z0-9.\-/]")


def normalize_ticker(raw: str) -> str | None:
    cleaned = _TICKER_STRIP.sub("", raw.upper())
    return cleaned if cleaned else None


def count_by_tier(claims: list[Claim]) -> dict[SignalTier, int]:
    counts: dict[SignalTier, int] = dict.fromkeys(SignalTier, 0)
    for claim in claims:
        counts[claim.tier] += 1
    return counts
