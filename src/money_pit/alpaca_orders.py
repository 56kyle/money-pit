"""Module exposing an alpaca-py-backed FillObserver for the money_pit package.

A single-shot order-status read: look an order up by its client_order_id and map the raw
alpaca-py order onto a typed FillObservation. Reads go via the alpaca-py SDK per ADR 0008,
never MCP. The get_order seam keeps the 404-vs-transport error mapping and the conversion
unit-testable without a live account.
"""

from collections.abc import Callable

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from money_pit.compute.fills import build_fill_observation
from money_pit.config import AlpacaCredentials
from money_pit.contracts import FillObserver
from money_pit.schemas.fills import FillObservation


_ORDER_NOT_FOUND_STATUS: int = 404


class OrderNotYetVisibleError(Exception):
    """Raised when the broker has not yet indexed a just-submitted order (a 404 on lookup by client_order_id).

    The poll loop treats this as a retryable in-flight signal, not a failure.
    """


class FillObservationError(Exception):
    """Raised on a transport or auth failure while observing an order status.

    The caller fails closed and never fabricates a fill.
    """


def _order_to_observation(order: object) -> FillObservation:
    """Map one alpaca-py order onto our frozen FillObservation, purely and without any broker call."""
    raw_status: object = getattr(order, "status", None)
    status: str = str(getattr(raw_status, "value", raw_status))
    raw_filled_qty: object = getattr(order, "filled_qty", None)
    filled_qty: float | None = float(raw_filled_qty) if raw_filled_qty is not None else None  # pyright: ignore[reportArgumentType]
    raw_filled_avg_price: object = getattr(order, "filled_avg_price", None)
    filled_avg_price: float | None = (
        float(raw_filled_avg_price) if raw_filled_avg_price is not None else None  # pyright: ignore[reportArgumentType]
    )
    return build_fill_observation(status, filled_qty, filled_avg_price)


def make_alpaca_fill_observer(
    credentials: AlpacaCredentials,
    *,
    get_order: Callable[[str], object] | None = None,
) -> FillObserver:
    """Return a FillObserver backed by the alpaca-py TradingClient, routed to paper or live per credentials."""
    if get_order is None:
        client: TradingClient = TradingClient(
            api_key=credentials.api_key,
            secret_key=credentials.secret_key.get_secret_value(),
            paper=credentials.paper,
        )
        get_order = client.get_order_by_client_id  # pyright: ignore[reportUnknownMemberType]

    resolved_get_order: Callable[[str], object] = get_order

    def observe_fill(client_order_id: str) -> FillObservation:
        try:
            order: object = resolved_get_order(client_order_id)
        except APIError as err:
            if err.status_code == _ORDER_NOT_FOUND_STATUS:
                raise OrderNotYetVisibleError(
                    f"Order {client_order_id!r} is not yet visible to the broker (404 on lookup)."
                ) from err
            raise FillObservationError(f"Failed to observe order {client_order_id!r}: {err}") from err
        return _order_to_observation(order)

    return observe_fill
