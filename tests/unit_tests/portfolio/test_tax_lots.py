"""Tests for deterministic tax-lot selection."""

from datetime import UTC
from datetime import datetime

import pytest

from money_pit.portfolio.planning import _planned_tax_lot  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.planning import _tax_cost_is_known  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.tax_lots import InvalidSpecificLotSelectionError
from money_pit.portfolio.tax_lots import SelectedTaxLot
from money_pit.portfolio.tax_lots import select_tax_lots
from money_pit.schemas.portfolio_plan import PlannedTaxLot
from money_pit.schemas.tax import LotSelectionPolicy
from money_pit.schemas.tax import TaxLot
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.schemas.tax import WashSaleStatus


_AS_OF = datetime(2026, 7, 29, tzinfo=UTC)


@pytest.fixture
def tax_lot_snapshot() -> TaxLotSnapshot:
    return TaxLotSnapshot(
        snapshot_id="tax-snapshot-1",
        captured_at=_AS_OF,
        lots=(
            TaxLot(
                lot_id="old-low-cost",
                account_id="taxable-1",
                instrument="AAPL",
                quantity=2.0,
                unit_cost=100.0,
                acquired_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
            TaxLot(
                lot_id="new-high-cost",
                account_id="taxable-1",
                instrument="AAPL",
                quantity=3.0,
                unit_cost=180.0,
                acquired_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            TaxLot(
                lot_id="future-lot",
                account_id="taxable-1",
                instrument="AAPL",
                quantity=10.0,
                unit_cost=190.0,
                acquired_at=datetime(2027, 1, 2, tzinfo=UTC),
            ),
        ),
        complete_for_known_accounts=True,
        unknown_external_activity=False,
    )


@pytest.mark.parametrize(
    ("policy", "expected_lot_id"),
    [
        pytest.param(LotSelectionPolicy.FIFO, "old-low-cost", id="fifo"),
        pytest.param(LotSelectionPolicy.LIFO, "new-high-cost", id="lifo"),
        pytest.param(LotSelectionPolicy.HIGHEST_COST, "new-high-cost", id="highest-cost"),
    ],
)
def test_select_tax_lots_with_policy_orders_known_available_lots(
    tax_lot_snapshot: TaxLotSnapshot,
    policy: LotSelectionPolicy,
    expected_lot_id: str,
) -> None:
    selection = select_tax_lots(
        tax_lot_snapshot,
        instrument="aapl",
        quantity=1.0,
        sale_price=200.0,
        policy=policy,
        as_of=_AS_OF,
    )

    assert selection.selected_lots[0].lot_id == expected_lot_id


def test_select_tax_lots_with_specific_id_preserves_operator_order(
    tax_lot_snapshot: TaxLotSnapshot,
) -> None:
    selection = select_tax_lots(
        tax_lot_snapshot,
        instrument="AAPL",
        quantity=3.0,
        sale_price=200.0,
        policy=LotSelectionPolicy.SPECIFIC_ID,
        as_of=_AS_OF,
        specific_lot_ids=("old-low-cost", "new-high-cost"),
    )

    assert tuple(lot.lot_id for lot in selection.selected_lots) == (
        "old-low-cost",
        "new-high-cost",
    )


@pytest.mark.parametrize(
    "specific_lot_ids",
    [
        pytest.param((), id="missing"),
        pytest.param(("missing",), id="unknown"),
        pytest.param(("old-low-cost", "old-low-cost"), id="duplicate"),
        pytest.param(("future-lot",), id="not-known-at-cutoff"),
    ],
)
def test_select_tax_lots_with_invalid_specific_id_fails_closed(
    tax_lot_snapshot: TaxLotSnapshot,
    specific_lot_ids: tuple[str, ...],
) -> None:
    with pytest.raises(InvalidSpecificLotSelectionError):
        _ = select_tax_lots(
            tax_lot_snapshot,
            instrument="AAPL",
            quantity=1.0,
            sale_price=200.0,
            policy=LotSelectionPolicy.SPECIFIC_ID,
            as_of=_AS_OF,
            specific_lot_ids=specific_lot_ids,
        )


@pytest.mark.parametrize(
    ("complete_for_known_accounts", "unknown_external_activity"),
    [
        pytest.param(False, False, id="incomplete-known-accounts"),
        pytest.param(True, True, id="unknown-external-activity"),
    ],
)
def test_select_tax_lots_with_incomplete_state_marks_tax_cost_unknown(
    tax_lot_snapshot: TaxLotSnapshot,
    complete_for_known_accounts: bool,
    unknown_external_activity: bool,
) -> None:
    incomplete = tax_lot_snapshot.model_copy(
        update={
            "complete_for_known_accounts": complete_for_known_accounts,
            "unknown_external_activity": unknown_external_activity,
        }
    )

    selection = select_tax_lots(
        incomplete,
        instrument="AAPL",
        quantity=1.0,
        sale_price=200.0,
        policy=LotSelectionPolicy.FIFO,
        as_of=_AS_OF,
    )

    assert not selection.tax_cost_known


@pytest.mark.parametrize(
    ("acquired_at", "expected_long_term", "expected_tax_cost"),
    [
        pytest.param(
            datetime(2025, 7, 28, tzinfo=UTC),
            True,
            3.0,
            id="long-term",
        ),
        pytest.param(
            datetime(2026, 1, 2, tzinfo=UTC),
            False,
            7.0,
            id="short-term",
        ),
    ],
)
def test__planned_tax_lot_uses_holding_period_tax_rate(
    tax_lot_snapshot: TaxLotSnapshot,
    acquired_at: datetime,
    expected_long_term: bool,
    expected_tax_cost: float,
) -> None:
    snapshot = tax_lot_snapshot.model_copy(update={"short_term_tax_rate": 0.35, "long_term_tax_rate": 0.15})
    selected = SelectedTaxLot(
        lot_id="selected",
        quantity=1.0,
        unit_cost=180.0,
        estimated_gain=20.0,
        acquired_at=acquired_at,
    )

    planned = _planned_tax_lot(selected, snapshot, as_of=_AS_OF)

    assert (planned.long_term, planned.estimated_tax_cost) == (
        expected_long_term,
        expected_tax_cost,
    )


def test__planned_tax_lot_with_missing_rate_marks_tax_cost_unknown(
    tax_lot_snapshot: TaxLotSnapshot,
) -> None:
    selected = SelectedTaxLot(
        lot_id="selected",
        quantity=1.0,
        unit_cost=180.0,
        estimated_gain=20.0,
        acquired_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    planned = _planned_tax_lot(selected, tax_lot_snapshot, as_of=_AS_OF)

    assert planned.estimated_tax_cost is None


@pytest.mark.parametrize(
    ("selection_complete", "wash_status", "estimated_tax_cost", "expected_known"),
    [
        pytest.param(True, WashSaleStatus.CLEAR, 3.0, True, id="complete"),
        pytest.param(False, WashSaleStatus.CLEAR, 3.0, False, id="incomplete-selection"),
        pytest.param(True, WashSaleStatus.POSSIBLE, 3.0, False, id="possible-wash-sale"),
        pytest.param(True, WashSaleStatus.UNKNOWN, 3.0, False, id="unknown-wash-sale"),
        pytest.param(True, WashSaleStatus.CLEAR, None, False, id="unknown-tax-rate"),
    ],
)
def test__tax_cost_is_known_requires_complete_wash_sale_and_rate_state(
    selection_complete: bool,
    wash_status: WashSaleStatus,
    estimated_tax_cost: float | None,
    expected_known: bool,
) -> None:
    lot = PlannedTaxLot(
        lot_id="lot-1",
        quantity=1.0,
        unit_cost=180.0,
        estimated_gain=20.0,
        acquired_at=datetime(2025, 1, 2, tzinfo=UTC),
        long_term=True,
        estimated_tax_cost=estimated_tax_cost,
    )

    known = _tax_cost_is_known(selection_complete, wash_status, (lot,))

    assert known is expected_known
