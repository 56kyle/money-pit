"""Module exposing an alpaca-py-backed FillObserver for the money_pit package.

A single-shot order-status read: look an order up by its client_order_id and map the raw
alpaca-py order onto a typed FillObservation. Reads go via the alpaca-py SDK per ADR 0008,
never MCP. The get_order seam keeps the 404-vs-transport error mapping and the conversion
unit-testable without a live account.
"""

from collections.abc import Callable

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest

from money_pit.config import AlpacaCredentials
from money_pit.execution_control.errors import BrokerObservationUnavailableError
from money_pit.execution_control.errors import BrokerOrderNotVisibleError
from money_pit.execution_control.errors import BrokerSubmissionError
from money_pit.execution_control.fills import FillObservation
from money_pit.execution_control.fills import build_fill_observation
from money_pit.execution_control.gateway import OrderPlacer
from money_pit.execution_control.gateway import OrderPlacerFactory
from money_pit.execution_control.orders import OrderIntent
from money_pit.schemas.execution_policy import BrokerEnvironment


_ORDER_NOT_FOUND_STATUS: int = 404
CredentialProvider = Callable[[BrokerEnvironment], AlpacaCredentials]
OrderSubmitter = Callable[[MarketOrderRequest], object]
OrderSubmitterFactory = Callable[[AlpacaCredentials], OrderSubmitter]
FillObserver = Callable[[str], FillObservation]


class OrderNotYetVisibleError(BrokerOrderNotVisibleError):
    """Raised when the broker has not yet indexed a just-submitted order (a 404 on lookup by client_order_id).

    The poll loop treats this as a retryable in-flight signal, not a failure.
    """


class FillObservationError(BrokerObservationUnavailableError):
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
        get_order = client.get_order_by_client_id

    resolved_get_order: Callable[[str], object] = get_order

    def observe_fill(client_order_id: str) -> FillObservation:
        try:
            order: object = resolved_get_order(client_order_id)
        except APIError as err:
            status_code: object = getattr(err, "status_code", None)
            if status_code == _ORDER_NOT_FOUND_STATUS:
                raise OrderNotYetVisibleError(
                    f"Order {client_order_id!r} is not yet visible to the broker (404 on lookup)."
                ) from err
            raise FillObservationError(f"Failed to observe order {client_order_id!r}: {err}") from err
        return _order_to_observation(order)

    return observe_fill


def _market_order_request(parameters: OrderIntent) -> MarketOrderRequest:
    """Translate validated execution parameters to the official Alpaca request model."""
    return MarketOrderRequest(
        symbol=parameters.symbol,
        qty=parameters.qty,
        notional=parameters.notional,
        side=parameters.side,
        time_in_force=parameters.time_in_force,
        client_order_id=parameters.client_order_id,
    )


def _order_identifier(order: object) -> str:
    """Return a nonblank broker order identifier from an Alpaca response."""
    value: object = getattr(order, "id", None)
    if value is None:
        raise BrokerSubmissionError("Alpaca returned no broker order identifier.")
    identifier: str = str(value)
    if not identifier.strip():
        raise BrokerSubmissionError("Alpaca returned a blank broker order identifier.")
    return identifier


def _default_submitter_factory(credentials: AlpacaCredentials) -> OrderSubmitter:
    """Construct an Alpaca trading client only inside the scoped A6 writer factory."""
    client: TradingClient = TradingClient(
        api_key=credentials.api_key,
        secret_key=credentials.secret_key.get_secret_value(),
        paper=credentials.paper,
    )
    return client.submit_order


def make_alpaca_order_placer_factory(
    credentials_for: CredentialProvider,
    *,
    submitter_factory: OrderSubmitterFactory = _default_submitter_factory,
) -> OrderPlacerFactory:
    """Return a lazy environment-bound writer factory for the A6 gateway only."""

    def make_order_placer(environment: BrokerEnvironment) -> OrderPlacer:
        credentials: AlpacaCredentials = credentials_for(environment)
        if credentials.broker_environment is not environment:
            raise BrokerSubmissionError("Resolved Alpaca credentials target a different broker environment.")
        submit_order: OrderSubmitter = submitter_factory(credentials)

        def place_order(parameters: OrderIntent) -> str:
            request: MarketOrderRequest = _market_order_request(parameters)
            try:
                response: object = submit_order(request)
            except APIError as error:
                raise BrokerSubmissionError("Alpaca rejected or could not confirm the order.") from error
            return _order_identifier(response)

        return place_order

    return make_order_placer
