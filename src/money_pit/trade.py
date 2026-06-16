"""Module containing the primary trading functionality used throughout the money_pit package."""
from functools import lru_cache

import keyring
from alpaca.trading import TradingClient

from money_pit.config import Config
from money_pit.config import load_config


config: Config = load_config()


@lru_cache(maxsize=1)
def get_client() -> TradingClient:
    """Returns an Alpaca trading client using the User's API key."""
    api_key: str = keyring.get_password(
        service_name=config.alpaca_service,
        username=config.alpaca_username,
    )
    return TradingClient(api_key=api_key)



