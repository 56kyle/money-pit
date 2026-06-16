"""Module containing constants used throughout the money_pit package."""
import datetime
from pathlib import Path

from platformdirs import user_config_path
from platformdirs import user_state_path
from platformdirs import user_log_path


_FILE_SAFE_DATETIME_FORMAT: str = "%Y-%m-%d_%H-%M-%S"

APP_NAME: str = "money_pit"
APP_AUTHOR: str = "56kyle"
APP_START_TIME: datetime.datetime = datetime.datetime.now(tz=datetime.timezone.utc)

USER_CONFIG_FOLDER: Path = user_config_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)
USER_STATE_FOLDER: Path = user_state_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)
USER_LOG_FOLDER: Path = user_log_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)

DEFAULT_CONFIG_PATH: Path = USER_CONFIG_FOLDER / ".env"
