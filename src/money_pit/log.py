"""Module containing logic for logging used throughout the money_pit package."""
from pathlib import Path

from loguru import logger

from money_pit.constants import APP_START_TIME
from money_pit.constants import USER_LOG_FOLDER
from money_pit.constants import _FILE_SAFE_DATETIME_FORMAT


_FILE_SAFE_DATETIME_SLUG: str = APP_START_TIME.strftime(_FILE_SAFE_DATETIME_FORMAT)

LOG_PATH: Path = USER_LOG_FOLDER / f"log_{_FILE_SAFE_DATETIME_SLUG}.log"


logger.add(LOG_PATH, serialize=True)
