"""Tests for immutable point-in-time tax-lot boundary contracts."""

from datetime import UTC
from datetime import datetime

import pytest
from pydantic import ValidationError

from money_pit.schemas.tax import LotSelectionPolicy
from money_pit.schemas.tax import TaxLot
from money_pit.schemas.tax import TaxLotSnapshot


def test_tax_lot_snapshot_preserves_incomplete_external_account_state() -> None:
    lot = TaxLot(
        lot_id="lot-1",
        account_id="taxable-1",
        instrument="AAPL",
        quantity=3.0,
        unit_cost=150.0,
        acquired_at=datetime(2025, 1, 2, tzinfo=UTC),
    )

    snapshot = TaxLotSnapshot(
        snapshot_id="tax-snapshot-1",
        captured_at=datetime(2026, 7, 29, tzinfo=UTC),
        lots=(lot,),
        complete_for_known_accounts=True,
        unknown_external_activity=True,
    )

    assert snapshot.unknown_external_activity


@pytest.mark.parametrize("policy", list(LotSelectionPolicy))
def test_lot_selection_policy_round_trips(policy: LotSelectionPolicy) -> None:
    assert LotSelectionPolicy(policy.value) is policy


def test_tax_lot_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValidationError):
        _ = TaxLot(
            lot_id="lot-1",
            account_id="taxable-1",
            instrument="AAPL",
            quantity=0.0,
            unit_cost=150.0,
            acquired_at=datetime(2025, 1, 2, tzinfo=UTC),
        )
