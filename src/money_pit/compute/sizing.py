"""Module containing expected-value, EV-gate, constraint-extraction, position-sizing, and clamp logic for the money_pit package."""

from money_pit.config import Config


def compute_ev(scenarios: list[tuple[float, float]]) -> float:
    """Return the probability-weighted expected return across all scenarios."""
    return sum(p * r for p, r in scenarios)


_MAX_KELLY_FRACTION: float = 1.0


def _kelly_derivative(f: float, scenarios: list[tuple[float, float]]) -> float:
    return sum(p * r / (1.0 + f * r) for p, r in scenarios)


def solve_kelly(
    scenarios: list[tuple[float, float]],
    tol: float = 1e-8,
    max_iter: int = 100,
) -> float:
    """Return the Kelly-optimal fraction via bisection on the log-growth derivative."""
    if compute_ev(scenarios) <= 0:
        return 0.0

    loss_ruin_points: list[float] = [-1.0 / r for _, r in scenarios if r < 0]
    if loss_ruin_points:
        upper_bound: float = min(min(loss_ruin_points) - tol, _MAX_KELLY_FRACTION)
    else:
        upper_bound = _MAX_KELLY_FRACTION

    lo: float = 0.0
    hi: float = upper_bound

    for _ in range(max_iter):
        if hi - lo < tol:
            break
        mid: float = (lo + hi) / 2.0
        if _kelly_derivative(mid, scenarios) > 0:
            lo = mid
        else:
            hi = mid

    return (lo + hi) / 2.0


def apply_haircuts(
    f_kelly: float,
    kelly_fraction: float,
    haircut_unverified: float,
    haircut_uncertain: float,
    verified: bool,
    regime_uncertain: bool,
) -> float:
    """Return the haircutted Kelly weight after applying verification and regime multipliers."""
    haircut_verification: float = 1.0 if verified else haircut_unverified
    haircut_regime: float = 1.0 if not regime_uncertain else haircut_uncertain
    return kelly_fraction * haircut_verification * haircut_regime * f_kelly


def size_position(
    scenarios: list[tuple[float, float]],
    total_account_value: float,
    config: Config,
    verified: bool,
    regime_uncertain: bool,
    sector_headroom: float,
    cash_headroom: float,
    overlap_headroom: float,
) -> float | None:
    """Return dollars to allocate to the position, or None if any gate or clamp eliminates it."""
    if compute_ev(scenarios) < config.ev_gate:
        return None

    f_kelly: float = solve_kelly(scenarios)
    w: float = apply_haircuts(
        f_kelly,
        config.kelly_fraction,
        config.haircut_unverified,
        config.haircut_uncertain,
        verified,
        regime_uncertain,
    )
    w = min(w, config.max_position_weight)
    dollars: float = w * total_account_value
    dollars = min(dollars, sector_headroom, cash_headroom, overlap_headroom)
    if dollars <= 0:
        return None
    return dollars
