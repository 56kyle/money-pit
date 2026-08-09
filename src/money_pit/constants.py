"""Module containing shared constants for the money_pit package."""

import datetime
import re
from pathlib import Path
from typing import Final

from platformdirs import user_config_path
from platformdirs import user_log_path


APP_NAME: Final[str] = "money_pit"
APP_AUTHOR: Final[str] = "56kyle"
APP_VERSION: Final[str] = "0.0.2"
APP_START_TIME: Final[datetime.datetime] = datetime.datetime.now(tz=datetime.timezone.utc)

DATA_ROOT: Final[Path] = Path("data")
ASSETS_DIRNAME: Final[str] = "assets"
RUNS_DIRNAME: Final[str] = "runs"
REPORTS_DIRNAME: Final[str] = "reports"
STATE_DATABASE_FILENAME: Final[str] = "intelligence.sqlite3"
RUN_MANIFEST_FILENAME: Final[str] = "run.json"

SOURCES_CONFIG_FILENAME: Final[str] = "sources.toml"
STRATEGY_CONFIG_FILENAME: Final[str] = "strategy.toml"
EXECUTION_CONFIG_FILENAME: Final[str] = "execution.toml"
ENV_CONFIG_FILENAME: Final[str] = ".env"


def user_config_folder() -> Path:
    """Return the per-user configuration folder."""
    return user_config_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)


def user_log_folder() -> Path:
    """Return the per-user log folder."""
    return user_log_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)


def default_config_path() -> Path:
    """Return the default secrets-only environment file."""
    return Path(ENV_CONFIG_FILENAME)


def default_sources_config_path() -> Path:
    """Return the default source-registry path."""
    return Path(SOURCES_CONFIG_FILENAME)


def default_strategy_config_path() -> Path:
    """Return the default strategy configuration path."""
    return Path(STRATEGY_CONFIG_FILENAME)


def default_execution_config_path() -> Path:
    """Return the default execution configuration path."""
    return Path(EXECUTION_CONFIG_FILENAME)


def source_id_to_dirname(source_id: str) -> str:
    """Return a filesystem-safe directory name for a logical source identifier."""
    return re.sub(r"[^\w.\-]", "_", source_id)
