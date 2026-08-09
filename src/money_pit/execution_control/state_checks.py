"""Module containing production read-only current-state checks for A6."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from math import isclose
from typing import Protocol

from money_pit.execution_control.errors import StateCheckUnavailableError
from money_pit.execution_control.gateway import CurrentStateChecks
from money_pit.execution_control.models import ConfirmedFill
from money_pit.portfolio.providers import MarketStateProvider
from money_pit.portfolio.providers import PortfolioStateProvider
from money_pit.portfolio.providers import TaxLotStateProvider
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.snapshots import DecisionSnapshot
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.storage.errors import StorageError


class DecisionSnapshotReader(Protocol):
    """Read one immutable decision snapshot by identifier."""

    def get(self, decision_snapshot_id: str) -> DecisionSnapshot | None:
        """Return the decision snapshot or None."""
        ...


class EvidenceFreshnessReader(Protocol):
    """Re-evaluate plan evidence after applying confirmed fill progress."""

    def is_fresh(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Return whether every plan-bound evidence gate remains current."""
        ...


class TaxStateValidator(Protocol):
    """Compare current tax state with the decision snapshot and confirmed fills."""

    def matches(
        self,
        plan: PortfolioPlan,
        bound: TaxLotSnapshot,
        current: TaxLotSnapshot,
        confirmed_fills: tuple[ConfirmedFill, ...],
    ) -> bool:
        """Return whether current tax state matches expected post-fill state."""
        ...


class ProductionStateCheckFactory:
    """Build checks backed by current providers and immutable decision inputs."""

    def __init__(
        self,
        *,
        decisions: DecisionSnapshotReader,
        snapshots: SnapshotRepository,
        portfolio: PortfolioStateProvider,
        market: MarketStateProvider,
        tax_lots: TaxLotStateProvider,
        evidence: EvidenceFreshnessReader,
        tax_validator: TaxStateValidator,
        maximum_market_drift_fraction: float,
        maximum_quote_age: timedelta | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        """Bind real read-only providers and a configured market-drift limit."""
        if not 0 <= maximum_market_drift_fraction < 1:
            raise ValueError("maximum_market_drift_fraction must be in [0, 1).")
        self._decisions: DecisionSnapshotReader = decisions
        self._snapshots: SnapshotRepository = snapshots
        self._portfolio: PortfolioStateProvider = portfolio
        self._market: MarketStateProvider = market
        self._tax_lots: TaxLotStateProvider = tax_lots
        self._evidence: EvidenceFreshnessReader = evidence
        self._tax_validator: TaxStateValidator = tax_validator
        self._maximum_market_drift_fraction: float = maximum_market_drift_fraction
        resolved_quote_age = timedelta(minutes=5) if maximum_quote_age is None else maximum_quote_age
        if resolved_quote_age <= timedelta(0):
            raise ValueError("maximum_quote_age must be positive")
        self._maximum_quote_age: timedelta = resolved_quote_age
        self._clock: Callable[[], datetime] = clock

    def build(self) -> CurrentStateChecks:
        """Return six checks that fail closed through their provider boundaries."""
        return CurrentStateChecks(
            portfolio_unchanged=self.portfolio_unchanged,
            market_unchanged=self.market_unchanged,
            cash_unchanged=self.cash_unchanged,
            open_orders_unchanged=self.open_orders_unchanged,
            evidence_fresh=self._evidence.is_fresh,
            tax_state_unchanged=self.tax_state_unchanged,
            account_ready=self.account_ready,
            market_fresh=self.market_fresh,
        )

    def _bound(self, plan: PortfolioPlan) -> tuple[DecisionSnapshot, PortfolioStateSnapshot, MarketStateSnapshot]:
        try:
            return self._load_bound(plan)
        except StorageError as error:
            raise StateCheckUnavailableError("Plan-bound storage could not be revalidated.") from error

    def _load_bound(self, plan: PortfolioPlan) -> tuple[DecisionSnapshot, PortfolioStateSnapshot, MarketStateSnapshot]:
        decision: DecisionSnapshot | None = self._decisions.get(plan.payload.decision_snapshot_id)
        if decision is None or decision.decision_hash != plan.payload.decision_snapshot_hash:
            raise StateCheckUnavailableError("The plan's decision snapshot is absent or has a different hash.")
        payload = decision.payload
        if (
            payload.portfolio_snapshot.snapshot_id != plan.payload.portfolio_snapshot_id
            or payload.market_snapshot.snapshot_id != plan.payload.market_snapshot_id
        ):
            raise StateCheckUnavailableError("The plan and decision snapshot bind different current-state inputs.")
        return (
            decision,
            self._snapshots.get_portfolio(payload.portfolio_snapshot.snapshot_id),
            self._snapshots.get_market(payload.market_snapshot.snapshot_id),
        )

    def portfolio_unchanged(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Compare quantities with deterministic expected post-fill positions."""
        _, bound, _ = self._bound(plan)
        current: PortfolioStateSnapshot = self._portfolio.snapshot()
        expected: dict[str, float] = {item.instrument: item.quantity for item in bound.payload.positions}
        for fill in confirmed_fills:
            direction: float = 1.0 if fill.side == "buy" else -1.0
            expected[fill.instrument] = expected.get(fill.instrument, 0.0) + direction * fill.filled_qty
        expected = {instrument: quantity for instrument, quantity in expected.items() if not isclose(quantity, 0.0)}
        actual: dict[str, float] = {item.instrument: item.quantity for item in current.payload.positions}
        return (
            current.payload.account_id == bound.payload.account_id
            and current.payload.account_id == plan.payload.account_id
            and current.payload.broker_environment is plan.payload.broker_environment
            and expected.keys() == actual.keys()
            and all(
                isclose(quantity, actual[instrument], rel_tol=1e-9, abs_tol=1e-9)
                for instrument, quantity in expected.items()
            )
        )

    def account_ready(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Require the exact active, unblocked account and environment."""
        del confirmed_fills
        current = self._portfolio.snapshot().payload
        return (
            current.account_id == plan.payload.account_id
            and current.broker_environment is plan.payload.broker_environment
            and current.account_status.upper() == "ACTIVE"
            and not current.trading_blocked
        )

    def market_fresh(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Require every current quote to carry a recent provider timestamp."""
        del confirmed_fills
        instruments = tuple(trade.instrument for trade in plan.payload.proposed_trades)
        current = self._market.snapshot(tuple(sorted(set(instruments))))
        now = self._clock()
        return all(
            quote.observed_at <= now and now - quote.observed_at <= self._maximum_quote_age
            for quote in current.payload.quotes
        )

    def cash_unchanged(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Compare cash with deterministic expected post-fill cash."""
        _, bound, _ = self._bound(plan)
        expected: float = bound.payload.available_cash
        for fill in confirmed_fills:
            expected += fill.realized_notional if fill.side == "sell" else -fill.realized_notional
        return isclose(self._portfolio.snapshot().payload.available_cash, expected, abs_tol=0.01)

    def open_orders_unchanged(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Require no unplanned open order after excluding confirmed terminal fills."""
        del confirmed_fills
        _, bound, _ = self._bound(plan)
        return self._portfolio.snapshot().payload.open_order_ids == bound.payload.open_order_ids

    def market_unchanged(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Require each current quote to remain within configured plan-bound drift."""
        del confirmed_fills
        _, _, bound = self._bound(plan)
        instruments: tuple[str, ...] = tuple(quote.instrument for quote in bound.payload.quotes)
        current: MarketStateSnapshot = self._market.snapshot(instruments)
        bound_prices: dict[str, float] = {quote.instrument: quote.price for quote in bound.payload.quotes}
        current_prices: dict[str, float] = {quote.instrument: quote.price for quote in current.payload.quotes}
        if bound_prices.keys() != current_prices.keys():
            return False
        return all(
            abs(current_prices[instrument] / price - 1) <= self._maximum_market_drift_fraction
            for instrument, price in bound_prices.items()
        )

    def tax_state_unchanged(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Delegate tax-lot comparison to the configured capital-sensitive validator."""
        decision, _, _ = self._bound(plan)
        bound: TaxLotSnapshot = self._snapshots.get_tax(decision.payload.tax_snapshot.snapshot_id)
        return self._tax_validator.matches(plan, bound, self._tax_lots.snapshot(), confirmed_fills)
