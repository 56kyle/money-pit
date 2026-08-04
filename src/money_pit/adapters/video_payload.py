"""Module containing the durable video classification payload contract."""

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.provenance import SourceRef


class TranscriptSource(str, Enum):
    """Origin of the transcript text delivered with a VideoPayload."""

    UPLOADER_CAPTIONS = "uploader_captions"
    AUTO_CAPTIONS = "auto_captions"
    WHISPER = "whisper"


class VideoPayload(BaseModel):
    """Typed boundary between deterministic video ingestion and A1 classification."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    source_ref: SourceRef
    transcript: str
    transcript_source: TranscriptSource
    has_word_timestamps: bool
    on_screen_text: list[str]
    evidence_assets: tuple[EvidenceAsset, ...] = ()
    evidence_fragments: tuple[EvidenceFragment, ...] = ()
