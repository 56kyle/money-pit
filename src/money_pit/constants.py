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

DAILY_SHOW_ROOT: Path = Path("data") / "daily_show"

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
