"""Tests for money_pit.schemas.portfolio.PortfolioSnapshot — the etf_holdings default and its serialization round-trip."""

import json

from money_pit.schemas.portfolio import PortfolioSnapshot


def _minimal_snapshot(**overrides: object) -> PortfolioSnapshot:
    fields: dict[str, object] = {
        "slug": "2026-01-01_00-00-00",
        "as_of": "2026-01-01T00:00:00Z",
        "total_account_value": 0.0,
        "available_cash": 0.0,
        "positions": [],
        "sector_weights": {},
        "correlated_overlaps": [],
    }
    fields.update(overrides)
    return PortfolioSnapshot(**fields)  # type: ignore[arg-type]


def test_portfolio_snapshot_etf_holdings_defaults_to_empty() -> None:
    snapshot = _minimal_snapshot()

    assert snapshot.etf_holdings == {}


def test_portfolio_snapshot_etf_holdings_round_trips() -> None:
    snapshot = _minimal_snapshot(etf_holdings={"SMH": ["NVDA", "AVGO"]})

    restored = PortfolioSnapshot.model_validate_json(snapshot.model_dump_json())

    assert snapshot.etf_holdings == {"SMH": ["NVDA", "AVGO"]}
    assert restored == snapshot


def test_portfolio_snapshot_etf_holdings_defaults_when_omitted_from_json() -> None:
    payload = {
        "slug": "2026-01-01_00-00-00",
        "as_of": "2026-01-01T00:00:00Z",
        "total_account_value": 0.0,
        "available_cash": 0.0,
        "positions": [],
        "sector_weights": {},
        "correlated_overlaps": [],
    }

    snapshot = PortfolioSnapshot.model_validate_json(json.dumps(payload))

    assert snapshot.etf_holdings == {}
