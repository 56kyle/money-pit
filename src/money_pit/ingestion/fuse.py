"""Module containing pure assembly of ingestion artifacts into an A1-ready VideoPayload for the money_pit package."""

from money_pit.adapters.video_llm import VideoPayload
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.ingestion.artifacts import TranscriptResult
from money_pit.schemas.provenance import SourceRef


_LOCATOR_PREFIX_TEMPLATE: str = "[{locator}] {value}"
_CITED_SOURCE_PREFIX: str = "Source: "


def _flatten_on_screen_text(extractions: list[OnScreenExtraction]) -> list[str]:
    """Flatten locator-tagged on-screen lines and cited sources, de-duplicating exact repeats."""
    flattened: list[str] = []
    seen: set[str] = set()
    for extraction in extractions:
        lines = [
            _LOCATOR_PREFIX_TEMPLATE.format(locator=extraction.locator, value=line)
            for line in extraction.on_screen_text
        ]
        lines.extend(
            _LOCATOR_PREFIX_TEMPLATE.format(
                locator=extraction.locator, value=f"{_CITED_SOURCE_PREFIX}{source}"
            )
            for source in extraction.cited_sources
        )
        for line in lines:
            if line not in seen:
                seen.add(line)
                flattened.append(line)
    return flattened


def build_video_payload(
    slug: str,
    source_ref: SourceRef,
    transcript: TranscriptResult,
    extractions: list[OnScreenExtraction],
) -> VideoPayload:
    """Assemble a VideoPayload from a transcript and per-keyframe on-screen extractions.

    The flattened on-screen block surfaces both on-screen text and cited sources so the
    A1 classifier can attribute claims to on-screen chart footers. Per-claim cited_sources
    are deliberately not stamped here; deriving them from this block is the A1 model's job.
    """
    return VideoPayload(
        slug=slug,
        source_ref=source_ref,
        transcript=transcript.text,
        transcript_source=transcript.source,
        has_word_timestamps=transcript.has_word_timestamps,
        on_screen_text=_flatten_on_screen_text(extractions),
    )
