"""Tests for money_pit.compute.factor_profile."""

import pytest

from money_pit.compute.factor_profile import aggregate_factor_profile
from money_pit.schemas.enums import FactorTag
from money_pit.schemas.portfolio import Position


def _position(ticker: str, current_value: float, factor_tags: list[FactorTag]) -> Position:
    return Position(
        ticker=ticker,
        quantity=1.0,
        cost_basis=current_value,
        current_value=current_value,
        unrealized_pl=0.0,
        sector="Tech",
        factor_tags=factor_tags,
    )


def test_aggregate_factor_profile_with_empty_positions() -> None:
    result = aggregate_factor_profile([])
    assert set(result.keys()) == set(FactorTag)
    assert all(v == 0.0 for v in result.values())


def test_aggregate_factor_profile_with_single_position_two_tags() -> None:
    pos = _position("AAPL", 100.0, [FactorTag.GROWTH, FactorTag.QUALITY])
    result = aggregate_factor_profile([pos])
    assert result[FactorTag.GROWTH] == pytest.approx(0.5)
    assert result[FactorTag.QUALITY] == pytest.approx(0.5)
    for tag in (FactorTag.VALUE, FactorTag.MOMENTUM, FactorTag.LOW_VOL):
        assert result[tag] == pytest.approx(0.0)


def test_aggregate_factor_profile_with_two_positions_non_overlapping_tags() -> None:
    pos1 = _position("AAPL", 100.0, [FactorTag.GROWTH])
    pos2 = _position("BRK.B", 100.0, [FactorTag.VALUE])
    result = aggregate_factor_profile([pos1, pos2])
    assert result[FactorTag.GROWTH] == pytest.approx(0.5)
    assert result[FactorTag.VALUE] == pytest.approx(0.5)
    for tag in (FactorTag.MOMENTUM, FactorTag.QUALITY, FactorTag.LOW_VOL):
        assert result[tag] == pytest.approx(0.0)
