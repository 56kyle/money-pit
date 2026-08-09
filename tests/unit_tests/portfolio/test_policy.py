"""Tests for complete deterministic portfolio policy validation."""

import pytest
from pydantic import ValidationError

from money_pit.portfolio.policy import PortfolioPolicy


@pytest.mark.parametrize(
    ("update", "expected_message"),
    [
        pytest.param({"maximum_sector_weights": {}}, "must not be empty", id="empty-sectors"),
        pytest.param({"maximum_sector_weights": {"technology": 0.0}}, "greater than zero", id="zero-sector"),
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
        _ = PortfolioPolicy.model_validate(values)


def test_validate_weight_limits_accepts_complete_policy(portfolio_policy: PortfolioPolicy) -> None:
    assert PortfolioPolicy.model_validate(portfolio_policy.model_dump()) == portfolio_policy


@pytest.mark.parametrize("obsolete_field", ["minimum_core_weights", "maximum_satellite_weight"])
def test_validate_weight_limits_rejects_obsolete_allocation_contracts(
    portfolio_policy: PortfolioPolicy,
    obsolete_field: str,
) -> None:
    values = {**portfolio_policy.model_dump(), obsolete_field: {"SPY": 0.4}}

    with pytest.raises(ValidationError):
        _ = PortfolioPolicy.model_validate(values)
