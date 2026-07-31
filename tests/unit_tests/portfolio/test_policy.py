"""Tests for complete deterministic portfolio policy validation."""

import pytest
from pydantic import ValidationError

from money_pit.portfolio.policy import PortfolioPolicy


@pytest.mark.parametrize(
    ("update", "expected_message"),
    [
        pytest.param({"maximum_sector_weights": {}}, "must not be empty", id="empty-sectors"),
        pytest.param({"minimum_core_weights": {"SPY": -0.1}}, "between zero and one", id="negative-core"),
        pytest.param({"maximum_sector_weights": {"technology": 0.0}}, "greater than zero", id="zero-sector"),
        pytest.param({"minimum_core_weights": {"SPY": 0.95}}, "insufficient required cash", id="core-cash"),
        pytest.param({"minimum_trade_weight": 0.3}, "exceeds maximum position change", id="trade-change"),
    ],
)
def test_validate_weight_limits_rejects_contradictory_policy(
    portfolio_policy: PortfolioPolicy,
    update: dict[str, object],
    expected_message: str,
) -> None:
    values = {**portfolio_policy.model_dump(), **update}

    with pytest.raises(ValidationError, match=expected_message):
        PortfolioPolicy.model_validate(values)


def test_validate_weight_limits_accepts_complete_policy(portfolio_policy: PortfolioPolicy) -> None:
    assert PortfolioPolicy.model_validate(portfolio_policy.model_dump()) == portfolio_policy
