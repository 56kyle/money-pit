"""Tests for production A6 read-only state checks."""

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.execution_control.models import ConfirmedFill
from money_pit.execution_control.state_checks import ProductionStateCheckFactory
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.snapshots import MarketQuote
from money_pit.portfolio.snapshots import MarketStatePayload
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.snapshots import DecisionSnapshot
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.storage.database import Database


@dataclass(frozen=True)
class _PortfolioProvider:
    state: PortfolioStateSnapshot

    def snapshot(self) -> PortfolioStateSnapshot:
        return self.state


@dataclass(frozen=True)
class _MarketProvider:
    state: MarketStateSnapshot

    def snapshot(self, instruments: tuple[str, ...]) -> MarketStateSnapshot:
        assert instruments == ("AAPL",)
        return self.state


class _Decisions:
    def get(self, decision_snapshot_id: str) -> DecisionSnapshot | None:
        del decision_snapshot_id
        return None


class _TaxLots:
    def snapshot(self) -> TaxLotSnapshot:
        raise AssertionError("tax state is outside these checks")


class _Evidence:
    def is_fresh(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        del plan, confirmed_fills
        raise AssertionError("evidence state is outside these checks")


class _TaxValidator:
    def matches(
        self,
        plan: PortfolioPlan,
        bound: TaxLotSnapshot,
        current: TaxLotSnapshot,
        confirmed_fills: tuple[ConfirmedFill, ...],
    ) -> bool:
        del plan, bound, current, confirmed_fills
        raise AssertionError("tax state is outside these checks")


def _factory(
    database: Database,
    portfolio: PortfolioStateSnapshot,
    market: MarketStateSnapshot,
    *,
    now: datetime,
) -> ProductionStateCheckFactory:
    return ProductionStateCheckFactory(
        decisions=_Decisions(),
        snapshots=SnapshotRepository(database),
        portfolio=_PortfolioProvider(portfolio),
        market=_MarketProvider(market),
        tax_lots=_TaxLots(),
        evidence=_Evidence(),
        tax_validator=_TaxValidator(),
        maximum_market_drift_fraction=0.01,
        maximum_quote_age=timedelta(minutes=5),
        clock=lambda: now,
    )


def _portfolio(
    plan: PortfolioPlan,
    now: datetime,
    **updates: object,
) -> PortfolioStateSnapshot:
    payload = PortfolioStatePayload(
        account_id=plan.payload.account_id,
        broker_environment=plan.payload.broker_environment,
        account_status="ACTIVE",
        trading_blocked=False,
        captured_at=now,
        available_cash=1_000.0,
        positions=(),
        open_order_ids=(),
    ).model_copy(update=updates)
    return PortfolioStateSnapshot.from_payload(payload)


def _market(now: datetime, *, observed_at: datetime) -> MarketStateSnapshot:
    return MarketStateSnapshot.from_payload(
        MarketStatePayload(
            captured_at=now,
            quotes=(
                MarketQuote(
                    instrument="AAPL",
                    price=200.0,
                    observed_at=observed_at,
                    source="test-provider",
                ),
            ),
        )
    )


@pytest.mark.parametrize(
    "updates",
    [
        pytest.param({"account_status": "SUSPENDED"}, id="inactive"),
        pytest.param({"trading_blocked": True}, id="trading-blocked"),
        pytest.param({"account_id": "other-account"}, id="wrong-account"),
        pytest.param({"broker_environment": BrokerEnvironment.LIVE}, id="wrong-environment"),
    ],
)
def test_account_ready_with_unauthorized_broker_state_fails_closed(
    database: Database,
    portfolio_plan: PortfolioPlan,
    now: datetime,
    updates: dict[str, object],
) -> None:
    portfolio = _portfolio(portfolio_plan, now, **updates)
    market = _market(now, observed_at=now)

    ready = _factory(database, portfolio, market, now=now).account_ready(portfolio_plan, ())

    assert not ready


@pytest.mark.parametrize(
    ("observed_at_offset", "expected_fresh"),
    [
        pytest.param(timedelta(minutes=-4), True, id="recent"),
        pytest.param(timedelta(minutes=-6), False, id="stale"),
        pytest.param(timedelta(seconds=1), False, id="future-dated"),
    ],
)
def test_market_fresh_uses_provider_quote_timestamp(
    database: Database,
    portfolio_plan: PortfolioPlan,
    now: datetime,
    observed_at_offset: timedelta,
    expected_fresh: bool,
) -> None:
    portfolio = _portfolio(portfolio_plan, now)
    market = _market(now, observed_at=now + observed_at_offset)

    fresh = _factory(database, portfolio, market, now=now).market_fresh(portfolio_plan, ())

    assert fresh is expected_fresh
