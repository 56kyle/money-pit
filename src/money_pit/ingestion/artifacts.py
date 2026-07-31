"""Module containing the typed video-ingestion intermediates and cached-on-disk artifact shapes for the money_pit package."""

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.schemas.provenance import SourceRef


class CaptionSegment(BaseModel):
    """A single WebVTT cue reduced to its time span and cleaned text; start/end in seconds."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    start: float
    end: float
    text: str


class TranscriptResult(BaseModel):
    """A resolved transcript together with its origin and word-timestamp availability."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    text: str
    source: TranscriptSource
    has_word_timestamps: bool
    segments: tuple[CaptionSegment, ...] = ()
    artifact_path: Path | None = None


class Keyframe(BaseModel):
    """A single extracted video frame located both in seconds and as an HH:MM:SS locator."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    timestamp: float
    image_path: Path
    locator: str


class OnScreenExtraction(BaseModel):
    """On-screen text and cited sources read from a single keyframe, tagged by its locator."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    locator: str
    on_screen_text: list[str]
    cited_sources: list[str]
    timestamp: float | None = Field(default=None, ge=0)
    image_path: Path | None = None
    extraction_method: str = "vision_llm"
    extraction_model: str | None = None
    bounding_box: tuple[float, float, float, float] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class VideoArtifacts(BaseModel):
    """The on-disk products of fetching a video: media, caption tracks, and metadata."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_ref: SourceRef
    audio_path: Path | None
    video_path: Path | None
    uploader_caption_path: Path | None
    auto_caption_path: Path | None
    metadata_path: Path
