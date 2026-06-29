"""Tests for money_pit.compute.sizing."""
import pytest

from money_pit.compute.sizing import _kelly_derivative
from money_pit.compute.sizing import apply_haircuts
from money_pit.compute.sizing import compute_ev
from money_pit.compute.sizing import size_position
from money_pit.compute.sizing import solve_kelly
from money_pit.config import Config


_STUB_CONFIG = Config(alpaca_service="stub", alpaca_username="stub")
_TOTAL_ACCOUNT_VALUE: float = 100_000.0
_LARGE_HEADROOM: float = 1_000_000.0


@pytest.mark.parametrize(
    ("scenarios", "expected"),
    [
        ([(1.0, 0.10)], 0.10),
        ([(0.6, 0.15), (0.4, -0.10)], 0.05),
        ([], 0.0),
    ],
)
def test_compute_ev_with_standard_inputs(scenarios: list[tuple[float, float]], expected: float) -> None:
    assert compute_ev(scenarios) == pytest.approx(expected)


def test_solve_kelly_with_negative_ev() -> None:
    assert solve_kelly([(0.6, 0.0), (0.4, -0.10)]) == 0.0


@pytest.mark.parametrize("r_bear", [-0.1, -0.3, -0.5, -0.7, -0.9])
def test_solve_kelly_self_limiting_as_ruin_approaches(r_bear: float) -> None:
    p_bear: float = 0.4
    p_bull: float = 0.6
    r_bull: float = (0.10 - p_bear * r_bear) / p_bull
    f: float = solve_kelly([(p_bull, r_bull), (p_bear, r_bear)])
    assert 0.0 <= f <= 1.0


def test_solve_kelly_self_limiting_decreases_as_loss_deepens() -> None:
    p_bear: float = 0.4
    p_bull: float = 0.6
    target_ev: float = 0.10
    bear_losses: list[float] = [-0.1, -0.3, -0.5, -0.7, -0.9]
    kelly_values: list[float] = []
    for r_bear in bear_losses:
        r_bull: float = (target_ev - p_bear * r_bear) / p_bull
        kelly_values.append(solve_kelly([(p_bull, r_bull), (p_bear, r_bear)]))
    assert all(kelly_values[i] > kelly_values[i + 1] for i in range(len(kelly_values) - 1))


def test_solve_kelly_derivative_near_zero_at_optimum() -> None:
    # Analytical f* = 5/6 ≈ 0.833, which is within [0, 1.0] so bisection converges to it
    scenarios: list[tuple[float, float]] = [(0.5, 0.30), (0.5, -0.20)]
    f_kelly: float = solve_kelly(scenarios)
    assert abs(_kelly_derivative(f_kelly, scenarios)) < 1e-6


def test_solve_kelly_monotonic_in_ev() -> None:
    # r_bear=-0.20, r_bull=0.30 fixed; increasing p_bull raises EV
    # p=0.5: EV=0.05, analytical f*=5/6≈0.833 (within [0,1])
    # p=0.7: EV=0.15, analytical f*=2.5 → hits upper_bound=1.0
    low_ev_scenarios: list[tuple[float, float]] = [(0.5, 0.30), (0.5, -0.20)]
    high_ev_scenarios: list[tuple[float, float]] = [(0.7, 0.30), (0.3, -0.20)]
    assert solve_kelly(low_ev_scenarios) < solve_kelly(high_ev_scenarios)


def test_size_position_with_ev_below_gate() -> None:
    scenarios: list[tuple[float, float]] = [(0.6, 0.01), (0.4, -0.005)]
    result = size_position(scenarios, _TOTAL_ACCOUNT_VALUE, _STUB_CONFIG, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    assert result is None


def test_size_position_with_zero_kelly_fraction() -> None:
    config = Config(alpaca_service="stub", alpaca_username="stub", kelly_fraction=0.0)
    scenarios: list[tuple[float, float]] = [(0.9, 0.50), (0.1, -0.05)]
    result = size_position(scenarios, _TOTAL_ACCOUNT_VALUE, config, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    assert result is None


def test_size_position_respects_max_position_weight() -> None:
    scenarios: list[tuple[float, float]] = [(0.9, 1.0), (0.1, -0.05)]
    result = size_position(scenarios, _TOTAL_ACCOUNT_VALUE, _STUB_CONFIG, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    assert result is not None
    assert result <= _STUB_CONFIG.max_position_weight * _TOTAL_ACCOUNT_VALUE


def test_size_position_haircut_reduces_unverified() -> None:
    # Analytical f*=2/3: verified w=0.25*(2/3)=0.167>cap → $10k; unverified w=0.25*0.5*(2/3)=0.083<cap → $8333
    scenarios: list[tuple[float, float]] = [(0.8, 0.15), (0.2, -0.40)]
    verified_result = size_position(scenarios, _TOTAL_ACCOUNT_VALUE, _STUB_CONFIG, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    unverified_result = size_position(scenarios, _TOTAL_ACCOUNT_VALUE, _STUB_CONFIG, False, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    assert verified_result is not None
    assert unverified_result is not None
    assert unverified_result < verified_result


def test_size_position_clamped_by_sector_headroom() -> None:
    scenarios: list[tuple[float, float]] = [(0.7, 0.20), (0.3, -0.05)]
    result = size_position(scenarios, _TOTAL_ACCOUNT_VALUE, _STUB_CONFIG, True, False, 100.0, _LARGE_HEADROOM, _LARGE_HEADROOM)
    assert result == pytest.approx(100.0)


def test_size_position_monotonic_in_ev() -> None:
    # max_position_weight=1.0 prevents both scenarios from hitting the same cap
    config = Config(alpaca_service="stub", alpaca_username="stub", max_position_weight=1.0)
    low_ev_scenarios: list[tuple[float, float]] = [(0.5, 0.30), (0.5, -0.20)]
    high_ev_scenarios: list[tuple[float, float]] = [(0.7, 0.30), (0.3, -0.20)]
    low_result = size_position(low_ev_scenarios, _TOTAL_ACCOUNT_VALUE, config, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    high_result = size_position(high_ev_scenarios, _TOTAL_ACCOUNT_VALUE, config, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    assert low_result is not None
    assert high_result is not None
    assert low_result < high_result


def test_size_position_monotonic_decreasing_in_variance() -> None:
    # Same EV=0.05, p_bull=0.6, p_bear=0.4; increasing |r_bear| raises variance
    # low variance: r_bear=-0.30 → Kelly caps at max_position_weight → larger position
    # high variance: r_bear=-0.60 → Kelly~0.172 → smaller position, below cap
    p_bull: float = 0.6
    p_bear: float = 0.4
    target_ev: float = 0.05

    r_bear_low: float = -0.30
    r_bull_low: float = (target_ev - p_bear * r_bear_low) / p_bull
    low_var_scenarios: list[tuple[float, float]] = [(p_bull, r_bull_low), (p_bear, r_bear_low)]

    r_bear_high: float = -0.60
    r_bull_high: float = (target_ev - p_bear * r_bear_high) / p_bull
    high_var_scenarios: list[tuple[float, float]] = [(p_bull, r_bull_high), (p_bear, r_bear_high)]

    low_var_result = size_position(low_var_scenarios, _TOTAL_ACCOUNT_VALUE, _STUB_CONFIG, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)
    high_var_result = size_position(high_var_scenarios, _TOTAL_ACCOUNT_VALUE, _STUB_CONFIG, True, False, _LARGE_HEADROOM, _LARGE_HEADROOM, _LARGE_HEADROOM)

    assert low_var_result is not None
    assert high_var_result is not None
    assert low_var_result > high_var_result


def test_apply_haircuts_with_verified_no_uncertainty() -> None:
    result: float = apply_haircuts(0.5, 0.25, 0.5, 0.75, verified=True, regime_uncertain=False)
    assert result == pytest.approx(0.25 * 1.0 * 1.0 * 0.5)


def test_apply_haircuts_with_unverified_uncertain() -> None:
    result: float = apply_haircuts(0.5, 0.25, 0.5, 0.75, verified=False, regime_uncertain=True)
    assert result == pytest.approx(0.25 * 0.5 * 0.75 * 0.5)
