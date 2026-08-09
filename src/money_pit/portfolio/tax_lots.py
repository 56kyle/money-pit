"""Module containing deterministic tax-lot selection."""

import math
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.schemas.tax import LotSelectionPolicy
from money_pit.schemas.tax import TaxLot
from money_pit.schemas.tax import TaxLotSnapshot


class InsufficientTaxLotsError(Exception):
    """Raised when known lots cannot cover a requested sale."""


class InvalidSpecificLotSelectionError(Exception):
    """Raised when a specific-ID request is absent or references an invalid lot."""


class SelectedTaxLot(BaseModel):
    """Quantity selected from one immutable acquisition lot."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    lot_id: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    unit_cost: float = Field(ge=0)
    estimated_gain: float
    acquired_at: datetime


class TaxLotSelection(BaseModel):
    """Auditable lot allocation and completeness of its tax estimate."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    policy: LotSelectionPolicy
    selected_lots: tuple[SelectedTaxLot, ...]
    requested_quantity: float = Field(gt=0)
    estimated_gain: float
    tax_cost_known: bool
    snapshot_id: str = Field(min_length=1)


def select_tax_lots(
    snapshot: TaxLotSnapshot,
    *,
    instrument: str,
    quantity: float,
    sale_price: float,
    policy: LotSelectionPolicy,
    as_of: datetime,
    specific_lot_ids: tuple[str, ...] = (),
) -> TaxLotSelection:
    """Select known lots without using acquisitions unavailable at the cutoff."""
    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("sale quantity must be positive")
    if not math.isfinite(sale_price) or sale_price < 0:
        raise ValueError("sale price cannot be negative")
    canonical_instrument: str = instrument.strip().upper()
    available: list[TaxLot] = [
        lot
        for lot in snapshot.lots
        if lot.instrument.strip().upper() == canonical_instrument and lot.acquired_at <= as_of
    ]
    ordered: list[TaxLot] = _ordered_lots(available, policy=policy, specific_lot_ids=specific_lot_ids)

    remaining: float = quantity
    selected: list[SelectedTaxLot] = []
    for lot in ordered:
        if remaining <= 1e-12:
            break
        selected_quantity: float = min(remaining, lot.quantity)
        selected.append(
            SelectedTaxLot(
                lot_id=lot.lot_id,
                quantity=selected_quantity,
                unit_cost=lot.unit_cost,
                estimated_gain=(sale_price - lot.unit_cost) * selected_quantity,
                acquired_at=lot.acquired_at,
            )
        )
        remaining -= selected_quantity
    if remaining > 1e-9:
        raise InsufficientTaxLotsError("known tax lots do not cover the requested sale")
    estimated_gain: float = sum(item.estimated_gain for item in selected)
    return TaxLotSelection(
        instrument=canonical_instrument,
        policy=policy,
        selected_lots=tuple(selected),
        requested_quantity=quantity,
        estimated_gain=estimated_gain,
        tax_cost_known=snapshot.complete_for_known_accounts and not snapshot.unknown_external_activity,
        snapshot_id=snapshot.snapshot_id,
    )


def _ordered_lots(
    available: list[TaxLot],
    *,
    policy: LotSelectionPolicy,
    specific_lot_ids: tuple[str, ...],
) -> list[TaxLot]:
    if policy is LotSelectionPolicy.SPECIFIC_ID:
        if not specific_lot_ids:
            raise InvalidSpecificLotSelectionError("specific-ID policy requires ordered lot IDs")
        by_id: dict[str, TaxLot] = {lot.lot_id: lot for lot in available}
        invalid_ids: bool = len(specific_lot_ids) != len(set(specific_lot_ids)) or any(
            lot_id not in by_id for lot_id in specific_lot_ids
        )
        if invalid_ids:
            raise InvalidSpecificLotSelectionError("specific lot IDs must be unique known lots for the instrument")
        return [by_id[lot_id] for lot_id in specific_lot_ids]
    if policy is LotSelectionPolicy.FIFO:
        return sorted(available, key=lambda lot: (lot.acquired_at, lot.lot_id))
    if policy is LotSelectionPolicy.LIFO:
        return sorted(available, key=lambda lot: (lot.acquired_at, lot.lot_id), reverse=True)
    return sorted(available, key=lambda lot: (-lot.unit_cost, lot.acquired_at, lot.lot_id))
