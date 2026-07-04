"""Tests for money_pit.compute.signal_flags."""

from collections.abc import Callable

import pytest

from money_pit.compute.signal_flags import (
    count_by_tier,
    has_actionable_content,
    normalize_ticker,
    requires_validation,
)
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.signals import Claim


@pytest.mark.parametrize(
    "tier,expected",
    [
        (SignalTier.HIGH, True),
        (SignalTier.MEDIUM, True),
        (SignalTier.LOW, False),
        (SignalTier.PORTFOLIO, False),
    ],
)
def test_requires_validation_with_all_tiers(tier: SignalTier, expected: bool) -> None:
    assert requires_validation(tier) is expected


def test_has_actionable_content_with_empty_list() -> None:
    assert has_actionable_content([]) is False


def test_has_actionable_content_with_one_high_claim(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    assert has_actionable_content([make_claim("c1", SignalTier.HIGH)]) is True


def test_has_actionable_content_with_only_low_claims(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    claims = [make_claim("c1", SignalTier.LOW), make_claim("c2", SignalTier.LOW)]
    assert has_actionable_content(claims) is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("aapl", "AAPL"),
        ("NVDA ", "NVDA"),
        ("BRK.B", "BRK.B"),
        ("  ", None),
        ("", None),
        ("SPY!", "SPY"),
    ],
)
def test_normalize_ticker_with_various_inputs(raw: str, expected: str | None) -> None:
    assert normalize_ticker(raw) == expected


def test_count_by_tier_with_empty_list() -> None:
    result = count_by_tier([])
    assert all(v == 0 for v in result.values())
    assert set(result.keys()) == set(SignalTier)


def test_count_by_tier_with_mixed_claims(
    make_claim: Callable[[str, SignalTier], Claim],
) -> None:
    claims = [
        make_claim("c1", SignalTier.HIGH),
        make_claim("c2", SignalTier.HIGH),
        make_claim("c3", SignalTier.LOW),
    ]
    result = count_by_tier(claims)
    assert result[SignalTier.HIGH] == 2
    assert result[SignalTier.LOW] == 1
    assert result[SignalTier.MEDIUM] == 0
    assert result[SignalTier.PORTFOLIO] == 0
