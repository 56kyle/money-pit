"""Module containing the pinned Alpaca order schema path, loader, and fail-closed errors for the money_pit package."""
import json
from pathlib import Path
from typing import cast

ALPACA_ORDER_SCHEMA_PATH: Path = Path(__file__).parent / "alpaca_order_schema.json"


class AlpacaOrderSchemaError(Exception):
    """Base error for the pinned Alpaca order schema."""


class AlpacaOrderSchemaMissingError(AlpacaOrderSchemaError):
    """Raised when the pinned Alpaca order schema file is absent — not yet pinned."""


class AlpacaOrderSchemaMalformedError(AlpacaOrderSchemaError):
    """Raised when the pinned Alpaca order schema is present but unparseable as JSON or not a top-level object."""


def load_order_schema(path: Path = ALPACA_ORDER_SCHEMA_PATH) -> dict[str, object]:
    """Load the pinned Alpaca order JSON schema, failing closed if absent or malformed."""
    if not path.exists():
        raise AlpacaOrderSchemaMissingError(
            f"Alpaca order schema not yet pinned. Expected at: {path}."
            + " Run the integration step to fetch it from the live Alpaca MCP server."
        )
    try:
        raw: object = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        raise AlpacaOrderSchemaMalformedError(
            f"Could not parse JSON in {path}: {error}."
        ) from error
    if not isinstance(raw, dict):
        raise AlpacaOrderSchemaMalformedError(
            f"Expected JSON object in {path}, got {type(raw).__name__}."
        )
    return cast(dict[str, object], raw)
