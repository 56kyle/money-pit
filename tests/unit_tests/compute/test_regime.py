"""Tests for money_pit.compute.regime."""

import pytest

from money_pit.compute.regime import classify_regime, discretize
from money_pit.config import Config
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.macro import MacroIndicators

@pytest.fixture
def stub_config() -> Config:
    return Config(alpaca_service="stub", alpaca_username="stub")


def _good_macro() -> MacroIndicators:
    return MacroIndicators(
        yield_curve=0.8,
        credit_spreads=2.0,
        pmi=52.0,
        earnings_revisions=0.6,
        inflation=2.0,
        as_of="2026-01-01",
    )


# --- discretize ---


def test_discretize_with_none_value() -> None:
    assert discretize(None, 0.0, 1, 0.5) is None


def test_discretize_with_value_above_threshold_positive_orientation() -> None:
    assert discretize(1.0, 0.0, 1, 0.5) == 1


def test_discretize_with_value_below_threshold_positive_orientation() -> None:
    assert discretize(-1.0, 0.0, 1, 0.5) == -1


def test_discretize_with_value_in_band() -> None:
    assert discretize(0.3, 0.0, 1, 0.5) == 0


def test_discretize_with_negative_orientation_flips_result() -> None:
    assert discretize(1.0, 0.0, -1, 0.5) == -1


def test_discretize_with_value_at_threshold() -> None:
    assert discretize(0.0, 0.0, 1, 0.5) == 0


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.6, 1),
        (0.5, 0),
    ],
)
def test_discretize_with_band_boundary(value: float, expected: int) -> None:
    assert discretize(value, 0.0, 1, 0.5) == expected


# --- classify_regime ---


def test_classify_regime_with_all_none_indicators(stub_config: Config) -> None:
    indicators = MacroIndicators(
        yield_curve=None,
        credit_spreads=None,
        pmi=None,
        earnings_revisions=None,
        inflation=None,
        as_of=None,
    )
    assert classify_regime(indicators, stub_config) == RegimeTag.UNCERTAIN


@pytest.mark.parametrize(
    "field",
    ["yield_curve", "credit_spreads", "pmi", "earnings_revisions", "inflation"],
)
def test_classify_regime_with_one_none_returns_uncertain(field: str, stub_config: Config) -> None:
    base = MacroIndicators(
        yield_curve=0.8,
        credit_spreads=2.0,
        pmi=52.0,
        earnings_revisions=0.6,
        inflation=2.0,
        as_of="2026-01-01",
    )
    indicators = base.model_copy(update={field: None})
    assert classify_regime(indicators, stub_config) == RegimeTag.UNCERTAIN


def test_classify_regime_with_late_cycle_stress_signals(stub_config: Config) -> None:
    # curve=-1, credit=-1, pmi=-1, earnings=-1 → Rule 1
    indicators = MacroIndicators(
        yield_curve=-0.8,
        credit_spreads=5.0,
        pmi=48.0,
        earnings_revisions=-0.6,
        inflation=2.5,
        as_of="2026-01-01",
    )
    assert classify_regime(indicators, stub_config) == RegimeTag.LATE_CYCLE_STRESS


def test_classify_regime_with_stagflation_signals(stub_config: Config) -> None:
    # curve=0, credit=-1, pmi=-1, earnings=-1, inflation=-1 → Rule 2
    indicators = MacroIndicators(
        yield_curve=0.2,
        credit_spreads=3.8,
        pmi=48.0,
        earnings_revisions=-0.6,
        inflation=3.2,
        as_of="2026-01-01",
    )
    assert classify_regime(indicators, stub_config) == RegimeTag.STAGFLATION


def test_classify_regime_with_growth_accelerating_signals(stub_config: Config) -> None:
    # curve=+1, credit=+1, pmi=+1, earnings=+1, inflation=0 → Rule 3
    assert classify_regime(_good_macro(), stub_config) == RegimeTag.GROWTH_ACCELERATING


def test_classify_regime_with_growth_decelerating_signals(stub_config: Config) -> None:
    # curve=0, credit=+1, pmi=-1, earnings=-1, inflation=0 → Rule 5
    indicators = MacroIndicators(
        yield_curve=0.2,
        credit_spreads=2.0,
        pmi=48.0,
        earnings_revisions=-0.6,
        inflation=2.3,
        as_of="2026-01-01",
    )
    assert classify_regime(indicators, stub_config) == RegimeTag.GROWTH_DECELERATING


def test_classify_regime_with_conflict_guard_strong_financial_weak_growth(stub_config: Config) -> None:
    # curve=+1, credit=+1, pmi=-1, earnings=-1, inflation=-1
    # Rules 1-5 all fail; conflict guard: (1+1)>=1 and growth=-2<=-1
    indicators = MacroIndicators(
        yield_curve=0.8,
        credit_spreads=2.0,
        pmi=48.0,
        earnings_revisions=-0.6,
        inflation=3.2,
        as_of="2026-01-01",
    )
    assert classify_regime(indicators, stub_config) == RegimeTag.UNCERTAIN


def test_classify_regime_with_conflict_guard_weak_financial_strong_growth(stub_config: Config) -> None:
    # curve=-1, credit=-1, pmi=+1, earnings=+1, inflation=0
    # Rules 1-5 all fail; conflict guard: (-1-1)<=-1 and growth=+2>=1
    indicators = MacroIndicators(
        yield_curve=-0.8,
        credit_spreads=5.0,
        pmi=52.0,
        earnings_revisions=0.6,
        inflation=2.5,
        as_of="2026-01-01",
    )
    assert classify_regime(indicators, stub_config) == RegimeTag.UNCERTAIN


def test_classify_regime_with_signed_sum_fallback_uncertain(stub_config: Config) -> None:
    # curve=0, credit=0, pmi=0, earnings=+1, inflation=0
    # Rules 1-5 fail; no conflict; total=1 → not >1 → UNCERTAIN
    indicators = MacroIndicators(
        yield_curve=0.2,
        credit_spreads=3.0,
        pmi=50.3,
        earnings_revisions=0.6,
        inflation=2.5,
        as_of="2026-01-01",
    )
    assert classify_regime(indicators, stub_config) == RegimeTag.UNCERTAIN


def test_classify_regime_recovery_is_unreachable(stub_config: Config) -> None:
    _yc_vals: list[float] = [-0.8, 0.2, 0.8]
    _cs_vals: list[float] = [5.0, 3.0, 2.0]
    _pmi_vals: list[float] = [48.0, 50.3, 52.0]
    _er_vals: list[float] = [-0.6, 0.3, 0.6]
    _inf_vals: list[float] = [3.2, 2.5, 1.9]
    results: set[RegimeTag] = set()
    for yc in _yc_vals:
        for cs in _cs_vals:
            for p in _pmi_vals:
                for er in _er_vals:
                    for inf in _inf_vals:
                        indicators = MacroIndicators(
                            yield_curve=yc,
                            credit_spreads=cs,
                            pmi=p,
                            earnings_revisions=er,
                            inflation=inf,
                            as_of="2026-01-01",
                        )
                        results.add(classify_regime(indicators, stub_config))
    assert RegimeTag.RECOVERY not in results
