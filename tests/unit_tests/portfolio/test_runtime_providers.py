from collections.abc import Mapping
from datetime import UTC
from datetime import datetime

import pytest
from pydantic import SecretStr

from money_pit.portfolio.runtime import AlpacaPortfolioStateProvider
from money_pit.portfolio.runtime import PortfolioProviderError
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.secrets import AlpacaCredentials


class _MalformedAlpacaTransport:
    def get_json(self, path: str, *, query: Mapping[str, str] | None = None) -> object:
        del query
        if path == "/account":
            return {"id": "paper-account", "cash": "not-a-number"}
        return []


def test_alpaca_portfolio_provider_converts_malformed_read_state_to_typed_error() -> None:
    provider = AlpacaPortfolioStateProvider(
        AlpacaCredentials(
            api_key=SecretStr("test-key"),
            secret_key=SecretStr("test-secret"),
            broker_environment=BrokerEnvironment.PAPER,
        ),
        transport=_MalformedAlpacaTransport(),
        clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
    )

    with pytest.raises(PortfolioProviderError):
        _ = provider.snapshot()
