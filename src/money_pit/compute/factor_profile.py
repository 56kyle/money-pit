"""Module containing factor-profile aggregation from per-position factor_tags for the money_pit package."""

from money_pit.schemas.enums import FactorTag
from money_pit.schemas.portfolio import Position


def aggregate_factor_profile(positions: list[Position]) -> dict[FactorTag, float]:
    total_value: float = sum(p.current_value for p in positions)
    result: dict[FactorTag, float] = dict.fromkeys(FactorTag, 0.0)
    if total_value == 0.0:
        return result
    for pos in positions:
        if not pos.factor_tags:
            continue
        weight_per_tag: float = pos.current_value / (total_value * len(pos.factor_tags))
        for tag in pos.factor_tags:
            result[tag] += weight_per_tag
    return result
