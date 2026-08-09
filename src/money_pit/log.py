"""Module containing logic for logging used throughout the money_pit package."""

import functools
from pathlib import Path

from loguru import logger

from money_pit.constants import APP_START_TIME
from money_pit.constants import user_log_folder


_LOG_FILENAME_TEMPLATE: str = "log_{slug}.log"
_FILE_SAFE_DATETIME_FORMAT: str = "%Y%m%d_%H%M%S"


@functools.cache
def log_path() -> Path:
    """Return the process-start log file path, creating the log folder on first call."""
    slug: str = APP_START_TIME.strftime(_FILE_SAFE_DATETIME_FORMAT)
    return user_log_folder() / _LOG_FILENAME_TEMPLATE.format(slug=slug)


@functools.cache
def configure_file_logging() -> Path:
    """Register the serialized file sink exactly once, returning the log path; call from process entrypoints."""
    path: Path = log_path()
    _ = logger.add(path, serialize=True)
    return path
