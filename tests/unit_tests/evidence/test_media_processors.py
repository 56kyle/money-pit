import hashlib
import importlib.util
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from money_pit.evidence.media import FrameReading
from money_pit.evidence.media import MediaAnalysis
from money_pit.evidence.media import MediaEvidenceProcessor
from money_pit.evidence.pdf import PdfEvidenceLimitError
from money_pit.evidence.pdf import PdfEvidenceProcessor
from money_pit.schemas.evidence import TimestampLocator
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceItem
from money_pit.sources.errors import SourceExtractionError


NOW = datetime(2026, 8, 9, tzinfo=UTC)
VIDEO = b"bounded-video"
FRAME_PNG = b"\x89PNG\r\n\x1a\nframe"


class _FrameAnalyzer:
    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        assert path.read_bytes() == VIDEO
        assert media_type == "video/mp4"
        return MediaAnalysis(
            transcript=(),
            frames=(
                FrameReading(
                    timestamp_seconds=12.5,
                    on_screen_text=("Revenue +20%", "Margin 18%"),
                    cited_sources=("SEC 10-Q", "Company presentation"),
                    bounding_box=(0.1, 0.2, 0.8, 0.9),
                    confidence=0.9,
                    extraction_model="vision-test",
                    image_png=FRAME_PNG,
                ),
            ),
        )


def _artifact(content: bytes, media_type: str, *, filename: str | None = None) -> RawArtifact:
    digest = hashlib.sha256(content).hexdigest()
    item = SourceItem(
        source_item_id="source:item",
        source_id="source",
        source_definition_hash="d" * 64,
        canonical_uri="https://example.test/item",
        discovered_at=NOW,
        content_version="version-1",
    )
    return RawArtifact(
        source_item=item,
        content=content,
        content_hash=digest,
        media_type=media_type,
        filename=Path(filename) if filename is not None else None,
        retrieved_at=NOW,
        canonical_uri=item.canonical_uri,
    )


def test_media_evidence_processor_groups_all_frame_entries_in_one_derived_png_document() -> None:
    bundle = MediaEvidenceProcessor(_FrameAnalyzer()).process_bundle(
        _artifact(VIDEO, "video/mp4", filename="video.mp4"),
    )

    assert len(bundle.derived) == 1
    derived = bundle.derived[0]
    frame_digest = hashlib.sha256(FRAME_PNG).hexdigest()
    assert (
        derived.content,
        derived.document.asset.asset_id,
        derived.document.asset.media_type,
    ) == (FRAME_PNG, frame_digest, "image/png")
    assert {(fragment.extracted_text, fragment.cited_source_text) for fragment in derived.document.fragments} == {
        ("Revenue +20%", None),
        ("Margin 18%", None),
        (None, "SEC 10-Q"),
        (None, "Company presentation"),
    }
    assert all(fragment.asset_id == frame_digest for fragment in derived.document.fragments)
    assert all(
        isinstance(fragment.locator, TimestampLocator)
        and fragment.locator.start_seconds == 12.5
        and fragment.locator.bounding_box == (0.1, 0.2, 0.8, 0.9)
        for fragment in derived.document.fragments
    )


def test_pdf_evidence_processor_enforces_its_byte_bound_before_optional_import() -> None:
    processor = PdfEvidenceProcessor(maximum_bytes=3)

    with pytest.raises(PdfEvidenceLimitError):
        _ = processor.process_bundle(_artifact(b"four", "application/pdf", filename="document.pdf"))


@pytest.mark.skipif(
    importlib.util.find_spec("pdfplumber") is not None,
    reason="This failure-seam pin applies when the optional PDF backend is unavailable.",
)
def test_pdf_evidence_processor_reports_the_unavailable_optional_backend() -> None:
    processor = PdfEvidenceProcessor(maximum_bytes=100)

    with pytest.raises(SourceExtractionError):
        _ = processor.process_bundle(_artifact(b"%PDF-minimal", "application/pdf", filename="document.pdf"))
