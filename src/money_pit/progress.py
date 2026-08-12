"""Module containing typed progress events for long-running ingestion work."""

from collections.abc import Callable
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class IngestionProgressStage(StrEnum):
    """Stable stages emitted while acquiring and processing source media."""

    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    CACHE_REFRESH = "cache_refresh"
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_PROGRESS = "download_progress"
    DOWNLOAD_COMPLETED = "download_completed"
    TRANSCRIPTION_BACKEND = "transcription_backend"
    TRANSCRIPTION_FALLBACK = "transcription_fallback"
    TRANSCRIPTION_PROGRESS = "transcription_progress"
    SCENE_DETECTION = "scene_detection"
    FRAME_PROCESSING = "frame_processing"
    PERSISTENCE = "persistence"
    COMPLETED = "completed"


class IngestionProgressEvent(BaseModel):
    """One secret-free progress update from a long-running ingestion."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    stage: IngestionProgressStage
    detail: str | None = None
    current: float | None = Field(default=None, ge=0)
    total: float | None = Field(default=None, gt=0)


IngestionProgressCallback = Callable[[IngestionProgressEvent], None]


def ignore_ingestion_progress(event: IngestionProgressEvent) -> None:
    """Discard one progress event for non-interactive library callers."""
    del event
