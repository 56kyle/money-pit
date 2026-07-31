"""Tests for point-in-time portfolio and market snapshot authority."""

from datetime import UTC
from datetime import datetime

import pytest
from pydantic import ValidationError

from money_pit.portfolio.repository import MalformedSnapshotRecordError
from money_pit.portfolio.repository import SnapshotNotFoundError
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.snapshots import MarketQuote
from money_pit.portfolio.snapshots import MarketStatePayload
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStatePosition
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


@pytest.fixture
def captured_at() -> datetime:
    return datetime(2026, 7, 29, 15, 0, tzinfo=UTC)


@pytest.fixture
def portfolio_state_payload(captured_at: datetime) -> PortfolioStatePayload:
    return PortfolioStatePayload(
        account_id="account-1",
        captured_at=captured_at,
        available_cash=1_000.0,
        positions=(
            PortfolioStatePosition(
                instrument="AAPL",
                quantity=2.0,
                market_price=200.0,
                market_value=400.0,
            ),
            PortfolioStatePosition(
                instrument="SPY",
                quantity=4.0,
                market_price=500.0,
                market_value=2_000.0,
            ),
        ),
        open_order_ids=("order-1", "order-2"),
    )


@pytest.fixture
def market_state_payload(captured_at: datetime) -> MarketStatePayload:
    return MarketStatePayload(
        captured_at=captured_at,
        quotes=(
            MarketQuote(
                instrument="AAPL",
                price=200.0,
                bid=199.9,
                ask=200.1,
                volume=1_000_000.0,
                observed_at=captured_at,
                source="market-data",
            ),
            MarketQuote(
                instrument="SPY",
                price=500.0,
                observed_at=captured_at,
                source="market-data",
            ),
        ),
    )


@pytest.fixture
def snapshot_repository(database: Database) -> SnapshotRepository:
    return SnapshotRepository(database)


def test_portfolio_state_snapshot_from_payload_uses_content_fingerprint(
    portfolio_state_payload: PortfolioStatePayload,
) -> None:
    snapshot: PortfolioStateSnapshot = PortfolioStateSnapshot.from_payload(portfolio_state_payload)
    assert snapshot.snapshot_id == portfolio_state_payload.fingerprint()


def test_market_state_snapshot_from_payload_uses_content_fingerprint(
    market_state_payload: MarketStatePayload,
) -> None:
    snapshot: MarketStateSnapshot = MarketStateSnapshot.from_payload(market_state_payload)
    assert snapshot.snapshot_id == market_state_payload.fingerprint()


def test_portfolio_state_payload_with_unsorted_positions_rejects_value(
    portfolio_state_payload: PortfolioStatePayload,
) -> None:
    with pytest.raises(ValidationError):
        PortfolioStatePayload(
            **portfolio_state_payload.model_dump(exclude={"positions"}),
            positions=tuple(reversed(portfolio_state_payload.positions)),
        )


def test_market_state_payload_with_duplicate_quotes_rejects_value(
    market_state_payload: MarketStatePayload,
) -> None:
    with pytest.raises(ValidationError):
        MarketStatePayload(
            captured_at=market_state_payload.captured_at,
            quotes=(market_state_payload.quotes[0], market_state_payload.quotes[0]),
        )


def test_snapshot_repository_round_trips_portfolio(
    snapshot_repository: SnapshotRepository,
    portfolio_state_payload: PortfolioStatePayload,
) -> None:
    snapshot: PortfolioStateSnapshot = PortfolioStateSnapshot.from_payload(portfolio_state_payload)
    snapshot_repository.append_portfolio(snapshot)
    assert snapshot_repository.get_portfolio(snapshot.snapshot_id) == snapshot


def test_snapshot_repository_round_trips_market(
    snapshot_repository: SnapshotRepository,
    market_state_payload: MarketStatePayload,
) -> None:
    snapshot: MarketStateSnapshot = MarketStateSnapshot.from_payload(market_state_payload)
    snapshot_repository.append_market(snapshot)
    assert snapshot_repository.get_market(snapshot.snapshot_id) == snapshot


def test_snapshot_repository_append_is_idempotent(
    snapshot_repository: SnapshotRepository,
    portfolio_state_payload: PortfolioStatePayload,
) -> None:
    snapshot: PortfolioStateSnapshot = PortfolioStateSnapshot.from_payload(portfolio_state_payload)
    snapshot_repository.append_portfolio(snapshot)
    snapshot_repository.append_portfolio(snapshot)
    assert snapshot_repository.get_portfolio(snapshot.snapshot_id) == snapshot


def test_snapshot_repository_rejects_identifier_collision(
    snapshot_repository: SnapshotRepository,
    portfolio_state_payload: PortfolioStatePayload,
) -> None:
    original: PortfolioStateSnapshot = PortfolioStateSnapshot.from_payload(portfolio_state_payload)
    changed_payload: PortfolioStatePayload = portfolio_state_payload.model_copy(
        update={"available_cash": portfolio_state_payload.available_cash + 1.0}
    )
    collision: PortfolioStateSnapshot = PortfolioStateSnapshot.model_construct(
        snapshot_id=original.snapshot_id,
        payload=changed_payload,
    )
    snapshot_repository.append_portfolio(original)

    with pytest.raises(MalformedSnapshotRecordError):
        snapshot_repository.append_portfolio(collision)


def test_snapshot_repository_with_missing_snapshot_raises(
    snapshot_repository: SnapshotRepository,
) -> None:
    with pytest.raises(SnapshotNotFoundError):
        snapshot_repository.get_market("missing")


def test_snapshot_repository_with_tampered_json_fails_closed(
    database: Database,
    snapshot_repository: SnapshotRepository,
    portfolio_state_payload: PortfolioStatePayload,
) -> None:
    snapshot: PortfolioStateSnapshot = PortfolioStateSnapshot.from_payload(portfolio_state_payload)
    snapshot_repository.append_portfolio(snapshot)
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            """UPDATE portfolio_state_snapshots SET payload_json = ?
            WHERE snapshot_id = ?""",
            ("{}", snapshot.snapshot_id),
        )

    with pytest.raises(MalformedSnapshotRecordError):
        snapshot_repository.get_portfolio(snapshot.snapshot_id)
