"""Module containing evidence-preserving conversion of video ingestion artifacts."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from money_pit.ingestion.artifacts import CaptionSegment
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.evidence import TimestampLocator
from money_pit.schemas.provenance import SourceRef


_UTF8: str = "utf-8"
_SECONDS_PER_MINUTE: int = 60
_SECONDS_PER_HOUR: int = 3600


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _retrieved_at(source_ref: SourceRef) -> datetime:
    retrieved_at: datetime = datetime.fromisoformat(source_ref.retrieved_at.replace("Z", "+00:00"))
    if retrieved_at.tzinfo is None:
        raise ValueError("Evidence retrieval timestamps must include a timezone")
    return retrieved_at


def _artifact_content(path: Path | None, fallback: bytes) -> bytes:
    if path is not None and path.exists():
        return path.read_bytes()
    return fallback


def _transcript_media_type(transcript: TranscriptResult) -> str:
    path: Path | None = transcript.artifact_path
    if path is not None and path.suffix.lower() == ".vtt":
        return "text/vtt"
    if transcript.source.value == "whisper":
        return "audio/*"
    return "text/plain"


def _transcript_asset(
    source_ref: SourceRef,
    transcript: TranscriptResult,
) -> tuple[EvidenceAsset, str]:
    fallback: bytes = transcript.text.encode(_UTF8)
    content: bytes = _artifact_content(transcript.artifact_path, fallback)
    digest: str = _sha256(content)
    local_path: Path = transcript.artifact_path or Path("derived-transcripts", f"{digest}.txt")
    return (
        EvidenceAsset(
            asset_id=digest,
            content_hash=digest,
            media_type=_transcript_media_type(transcript),
            source_item_id=source_ref.source_id,
            local_path=local_path,
            retrieved_at=_retrieved_at(source_ref),
        ),
        digest,
    )


def _transcript_fragment(
    asset_id: str,
    transcript: TranscriptResult,
    segment: CaptionSegment,
    index: int,
) -> EvidenceFragment:
    return EvidenceFragment(
        fragment_id=(f"sha256:{asset_id}:transcript:{index:06d}:{segment.start:.3f}:{segment.end:.3f}"),
        asset_id=asset_id,
        kind="transcript",
        locator=TimestampLocator(
            start_seconds=segment.start,
            end_seconds=segment.end,
        ),
        extracted_text=segment.text,
        extraction_method=transcript.source.value,
    )


def _transcript_document(
    source_ref: SourceRef,
    transcript: TranscriptResult,
) -> EvidenceDocument:
    asset, asset_id = _transcript_asset(source_ref, transcript)
    fragments: tuple[EvidenceFragment, ...]
    if transcript.segments:
        fragments = tuple(
            _transcript_fragment(asset_id, transcript, segment, index)
            for index, segment in enumerate(transcript.segments)
        )
    else:
        fragments = (
            EvidenceFragment(
                fragment_id=f"sha256:{asset_id}:transcript:full",
                asset_id=asset_id,
                kind="transcript",
                locator=TextLocator(start_offset=0, end_offset=len(transcript.text)),
                extracted_text=transcript.text,
                extraction_method=transcript.source.value,
            ),
        )
    return EvidenceDocument(asset=asset, fragments=fragments)


def _seconds_from_locator(locator: str) -> float:
    try:
        hours, minutes, seconds = (int(part) for part in locator.split(":"))
    except (TypeError, ValueError):
        return 0.0
    return float(hours * _SECONDS_PER_HOUR + minutes * _SECONDS_PER_MINUTE + seconds)


def _frame_document(
    source_ref: SourceRef,
    extraction: OnScreenExtraction,
) -> EvidenceDocument:
    timestamp: float = extraction.timestamp or _seconds_from_locator(extraction.locator)
    fallback_shape: str = json.dumps(
        {
            "source_id": source_ref.source_id,
            "locator": extraction.locator,
            "on_screen_text": extraction.on_screen_text,
            "cited_sources": extraction.cited_sources,
        },
        sort_keys=True,
    )
    content: bytes = _artifact_content(extraction.image_path, fallback_shape.encode(_UTF8))
    asset_id: str = _sha256(content)
    local_path: Path = extraction.image_path or Path("derived-frames", f"{asset_id}.png")
    asset = EvidenceAsset(
        asset_id=asset_id,
        content_hash=asset_id,
        media_type="image/png",
        source_item_id=source_ref.source_id,
        local_path=local_path,
        retrieved_at=_retrieved_at(source_ref),
    )
    fragments: list[EvidenceFragment] = []
    entries: tuple[tuple[str, str], ...] = tuple(("text", text) for text in extraction.on_screen_text) + tuple(
        ("citation", source) for source in extraction.cited_sources
    )
    for index, (entry_kind, value) in enumerate(entries):
        value_digest: str = _sha256(value.encode(_UTF8))
        fragments.append(
            EvidenceFragment(
                fragment_id=(f"sha256:{asset_id}:frame:{timestamp:.3f}:{entry_kind}:{index:04d}:{value_digest}"),
                asset_id=asset_id,
                kind="frame",
                locator=TimestampLocator(
                    start_seconds=timestamp,
                    bounding_box=extraction.bounding_box,
                ),
                extracted_text=value if entry_kind == "text" else None,
                cited_source_text=value if entry_kind == "citation" else None,
                extraction_method=extraction.extraction_method,
                extraction_model=extraction.extraction_model,
                confidence=extraction.confidence,
            ),
        )
    return EvidenceDocument(asset=asset, fragments=tuple(fragments))


def build_evidence_documents(
    source_ref: SourceRef,
    transcript: TranscriptResult,
    extractions: list[OnScreenExtraction],
) -> tuple[EvidenceDocument, ...]:
    """Build immutable transcript and frame evidence with stable, full fragment IDs."""
    return (
        _transcript_document(source_ref, transcript),
        *(_frame_document(source_ref, extraction) for extraction in extractions),
    )
