"""Module containing constants used throughout the money_pit package."""

import datetime
import re
from pathlib import Path

from platformdirs import user_cache_path
from platformdirs import user_config_path
from platformdirs import user_log_path
from platformdirs import user_state_path


FILE_SAFE_DATETIME_FORMAT: str = "%Y-%m-%d_%H-%M-%S"

APP_NAME: str = "money_pit"
APP_AUTHOR: str = "56kyle"
APP_START_TIME: datetime.datetime = datetime.datetime.now(tz=datetime.timezone.utc)

_CONFIG_FILENAME: str = ".env"
_INGEST_CACHE_DIRNAME: str = "ingest_cache"
_PROCESSED_EPISODES_FILENAME: str = "processed_episodes.json"


def user_config_folder() -> Path:
    """Return the per-user config folder, creating it on each call."""
    return user_config_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)


def user_cache_folder() -> Path:
    """Return the per-user cache folder, creating it on each call."""
    return user_cache_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)


def user_state_folder() -> Path:
    """Return the per-user state folder, creating it on each call."""
    return user_state_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)


def user_log_folder() -> Path:
    """Return the per-user log folder, creating it on each call."""
    return user_log_path(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=True)


def default_config_path() -> Path:
    """Return the default per-user config file path, creating its parent folder on each call."""
    return user_config_folder() / _CONFIG_FILENAME


def default_ingest_cache_dir() -> Path:
    """Return the default per-user ingest cache directory, creating its parent folder on each call."""
    return user_cache_folder() / _INGEST_CACHE_DIRNAME


def default_processed_episodes_path() -> Path:
    """Return the default per-user processed-episodes ledger path, creating its parent folder on each call."""
    return user_state_folder() / _PROCESSED_EPISODES_FILENAME


def source_id_to_dirname(source_id: str) -> str:
    """Return a filesystem-safe directory name for a logical source id.

    The logical source id keeps its colon on `SourceRef.source_id`; only the on-disk directory
    name is sanitized so ids like `yt:dQw4w9WgXcQ` do not raise on colon-hostile filesystems.
    """
    return re.sub(r"[^\w.\-]", "_", source_id)

OPENAI_MODEL_PREFIX: str = "openai:"

GMAIL_KEYRING_SERVICE: str = "money-pit-gmail"

DEFAULT_SMTP_HOST: str = "smtp.gmail.com"
DEFAULT_SMTP_PORT: int = 587

DAILY_SHOW_ROOT: Path = Path("data") / "daily_show"

SIGNALS_DIRNAME: str = "signals"

AGGREGATED_SIGNALS_JSON_FILENAME: str = "aggregated_signals.json"
AGGREGATED_SIGNALS_MD_FILENAME: str = "aggregated_signals.md"
PORTFOLIO_SNAPSHOT_FILENAME: str = "portfolio_snapshot.json"
INITIAL_QUESTIONS_JSON_FILENAME: str = "initial_questions.json"
INITIAL_QUESTIONS_MD_FILENAME: str = "initial_questions.md"
INITIAL_ANSWERS_JSON_FILENAME: str = "initial_answers.json"
INITIAL_ANSWERS_MD_FILENAME: str = "initial_answers.md"
ANALYSIS_JUDGMENT_JSON_FILENAME: str = "analysis_judgment.json"
ANALYSIS_MD_FILENAME: str = "analysis.md"
ACTION_STEPS_JSON_FILENAME: str = "action_steps.json"
ACTION_STEPS_MD_FILENAME: str = "action_steps.md"
ACTION_STEPS_VALIDATION_JSON_FILENAME: str = "action_steps_validation.json"
ACTION_STEPS_VALIDATION_MD_FILENAME: str = "action_steps_validation.md"
VALIDATION_STATUS_FILENAME: str = "validation_status.json"
DETERMINATION_JSON_FILENAME: str = "determination.json"
DETERMINATION_MD_FILENAME: str = "determination.md"
EXECUTION_JOURNAL_FILENAME: str = "execution_journal.json"
RECOVERY_JSON_FILENAME: str = "recovery.json"
