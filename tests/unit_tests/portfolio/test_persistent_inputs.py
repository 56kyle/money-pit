import math
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.portfolio.eligibility import MaterialEvidenceAnchor
from money_pit.portfolio.eligibility import VerificationState
from money_pit.portfolio.persistent_inputs import _anchors_for_claim_keys  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.persistent_inputs import _tax_cost_inputs  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.snapshots import PortfolioStatePosition
from money_pit.schemas.tax import TaxLot
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.schemas.tax import WashSaleStatus


_AS_OF = datetime(2026, 8, 9, tzinfo=UTC)
_POSITION = PortfolioStatePosition(
    instrument="NEW",
    quantity=3,
    market_price=100,
    market_value=300,
)


def test__anchors_for_claim_keys_preserves_present_and_materializes_missing_as_unresolved() -> None:
    present = MaterialEvidenceAnchor(
        claim_key="claim-present",
        status=VerificationState.SUPPORTED,
        known_at=_AS_OF,
        valid_until=_AS_OF + timedelta(days=7),
        allowed_for_portfolio=True,
        authoritative_primary=True,
        independent_provenance_groups=("issuer",),
    )

    anchors = _anchors_for_claim_keys(
        frozenset({"claim-present", "claim-missing"}),
        {"claim-present": (present,)},
        known_at=_AS_OF,
    )

    assert anchors == (
        MaterialEvidenceAnchor(
            claim_key="claim-missing",
            status=VerificationState.UNRESOLVED,
            known_at=_AS_OF,
            valid_until=None,
            allowed_for_portfolio=False,
            authoritative_primary=False,
            independent_provenance_groups=(),
        ),
        present,
    )


def _lot(lot_id: str, *, quantity: float, unit_cost: float, age_days: int) -> TaxLot:
    return TaxLot(
        lot_id=lot_id,
        account_id="taxable",
        instrument="NEW",
        quantity=quantity,
        unit_cost=unit_cost,
        acquired_at=_AS_OF - timedelta(days=age_days),
    )


def _snapshot(*, lots: tuple[TaxLot, ...], complete: bool = True) -> TaxLotSnapshot:
    return TaxLotSnapshot(
        snapshot_id="tax-snapshot",
        captured_at=_AS_OF,
        lots=lots,
        complete_for_known_accounts=complete,
        unknown_external_activity=False,
        short_term_tax_rate=0.30,
        long_term_tax_rate=0.15,
        wash_sale_status={"NEW": WashSaleStatus.CLEAR},
    )


def test__tax_cost_inputs_uses_conservative_lot_gain_rate_and_ignores_losses() -> None:
    snapshot = _snapshot(
        lots=(
            _lot("long-gain", quantity=1, unit_cost=50, age_days=366),
            _lot("short-gain", quantity=1, unit_cost=80, age_days=30),
            _lot("short-loss", quantity=1, unit_cost=110, age_days=30),
        )
    )

    costs, known = _tax_cost_inputs(
        snapshot,
        positions={"NEW": _POSITION},
        prices={"NEW": 100},
        as_of=_AS_OF,
    )

    assert known == {"NEW": True}
    assert math.isclose(costs["NEW"], 0.075)


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(
            _snapshot(lots=(_lot("partial", quantity=1, unit_cost=80, age_days=30),)),
            id="partial-lot-coverage",
        ),
        pytest.param(
            _snapshot(
                lots=(_lot("lot-1", quantity=3, unit_cost=80, age_days=30),),
                complete=False,
            ),
            id="incomplete-account-state",
        ),
    ],
)
def test__tax_cost_inputs_fails_closed_when_tax_cost_is_not_fully_known(
    snapshot: TaxLotSnapshot,
) -> None:
    costs, known = _tax_cost_inputs(
        snapshot,
        positions={"NEW": _POSITION},
        prices={"NEW": 100},
        as_of=_AS_OF,
    )

    assert (costs, known) == ({"NEW": 0.0}, {"NEW": False})
