"""Module responsible for handling config used throughout the money_pit package."""
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel

from money_pit.constants import DEFAULT_CONFIG_PATH


class Config(BaseModel):
    """The primary config for the money_pit package."""
    alpaca_service: str
    alpaca_username: str


@lru_cache
def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load the config for the money_pit package."""
    load_dotenv(path)
    alpaca_service: Optional[str] = os.environ.get("MONEY_PIT__ALPACA_SERVICE", None)

    if alpaca_service is None:
        raise ValueError(f"Failed to get alpaca_service from environment variable MONEY_PIT__ALPACA_SERVICE.")

    alpaca_username: Optional[str] = os.environ.get("MONEY_PIT__ALPACA_USERNAME", None)
    if alpaca_username is None:
        raise ValueError(f"Failed to get alpaca_username from environment variable MONEY_PIT__ALPACA_USERNAME.")

    return Config(
        alpaca_service=alpaca_service,
        alpaca_username=alpaca_username,
    )
