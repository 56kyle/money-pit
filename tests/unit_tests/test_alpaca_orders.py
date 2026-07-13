"""Tests for money_pit.alpaca_orders' pure order mapper and the injected-seam FillObserver.

_order_to_observation is called directly with duck-typed SimpleNamespace fakes standing in for
alpaca-py orders, exercising the str/float coercions and the getattr(status, "value", status)
fallback. make_alpaca_fill_observer is driven through its get_order seam with authored callables, so
no TradingClient is built and no live account is touched; the APIError status-code branching is pinned
by asserting on the OrderNotYetVisibleError / FillObservationError TYPES. Phases are compared by enum
identity.
"""

from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError

from money_pit.alpaca_orders import FillObservationError
from money_pit.alpaca_orders import OrderNotYetVisibleError
from money_pit.alpaca_orders import _order_to_observation
from money_pit.alpaca_orders import make_alpaca_fill_observer
from money_pit.config import AlpacaCredentials
from money_pit.schemas.enums import ExecutionPhase
from money_pit.schemas.fills import FillObservation


@pytest.fixture
def credentials() -> AlpacaCredentials:
    return AlpacaCredentials(api_key="k", secret_key="s", paper=True)


@pytest.fixture
def filled_order() -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(value="filled"),
        filled_qty="8",
        filled_avg_price="184.5",
    )


def test__order_to_observation_with_filled(filled_order: SimpleNamespace) -> None:
    observation = _order_to_observation(filled_order)

    assert observation == FillObservation(
        phase=ExecutionPhase.FILLED,
        status="filled",
        filled_qty=8.0,
        filled_avg_price=184.5,
        realized_notional=1476.0,
    )


def test__order_to_observation_with_rejected() -> None:
    order = SimpleNamespace(
        status=SimpleNamespace(value="rejected"),
        filled_qty=None,
        filled_avg_price=None,
    )

    observation = _order_to_observation(order)

    assert observation.phase is ExecutionPhase.REJECTED
    assert observation.status == "rejected"
    assert observation.filled_qty is None
    assert observation.filled_avg_price is None
    assert observation.realized_notional is None


def test__order_to_observation_with_open() -> None:
    order = SimpleNamespace(
        status=SimpleNamespace(value="accepted"),
        filled_qty=None,
        filled_avg_price=None,
    )

    observation = _order_to_observation(order)

    assert observation.phase is ExecutionPhase.SUBMITTED
    assert observation.status == "accepted"
    assert observation.filled_qty is None
    assert observation.realized_notional is None


def test__order_to_observation_with_terminal_partial() -> None:
    order = SimpleNamespace(
        status=SimpleNamespace(value="done_for_day"),
        filled_qty="3",
        filled_avg_price="100.0",
    )

    observation = _order_to_observation(order)

    assert observation == FillObservation(
        phase=ExecutionPhase.PARTIALLY_FILLED,
        status="done_for_day",
        filled_qty=3.0,
        filled_avg_price=100.0,
        realized_notional=300.0,
    )


def test__order_to_observation_with_bare_string_status() -> None:
    order = SimpleNamespace(status="filled", filled_qty="8", filled_avg_price="184.5")

    observation = _order_to_observation(order)

    assert observation.phase is ExecutionPhase.FILLED
    assert observation.status == "filled"


def test_make_alpaca_fill_observer_with_visible_order(
    credentials: AlpacaCredentials, filled_order: SimpleNamespace
) -> None:
    seen: list[str] = []

    def get_order(client_order_id: str) -> object:
        seen.append(client_order_id)
        return filled_order

    observe_fill = make_alpaca_fill_observer(credentials, get_order=get_order)

    observation = observe_fill("2026-01-01_00-00-00:A001")

    assert seen == ["2026-01-01_00-00-00:A001"]
    assert observation == FillObservation(
        phase=ExecutionPhase.FILLED,
        status="filled",
        filled_qty=8.0,
        filled_avg_price=184.5,
        realized_notional=1476.0,
    )


def test_make_alpaca_fill_observer_with_not_found(credentials: AlpacaCredentials) -> None:
    def get_order(_client_order_id: str) -> object:
        raise APIError("not found", SimpleNamespace(response=SimpleNamespace(status_code=404)))

    observe_fill = make_alpaca_fill_observer(credentials, get_order=get_order)

    with pytest.raises(OrderNotYetVisibleError):
        _ = observe_fill("2026-01-01_00-00-00:A001")


def test_make_alpaca_fill_observer_with_transport_error(credentials: AlpacaCredentials) -> None:
    def get_order(_client_order_id: str) -> object:
        raise APIError("server error", SimpleNamespace(response=SimpleNamespace(status_code=500)))

    observe_fill = make_alpaca_fill_observer(credentials, get_order=get_order)

    with pytest.raises(FillObservationError):
        _ = observe_fill("2026-01-01_00-00-00:A001")


def test_make_alpaca_fill_observer_with_status_code_none(credentials: AlpacaCredentials) -> None:
    def get_order(_client_order_id: str) -> object:
        raise APIError("boom")

    observe_fill = make_alpaca_fill_observer(credentials, get_order=get_order)

    with pytest.raises(FillObservationError):
        _ = observe_fill("2026-01-01_00-00-00:A001")
