"""Module containing page- and table-preserving PDF evidence processing."""

# pdfplumber does not publish typing metadata; this module converts its output
# into strict scalar and tuple contracts before persistence.
# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from io import BytesIO
from time import monotonic
from typing import TYPE_CHECKING

from money_pit.evidence.results import EvidenceProcessingBundle
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import PageLocator
from money_pit.sources._shared import evidence_asset
from money_pit.sources.errors import SourceExtractionError


if TYPE_CHECKING:
    from money_pit.schemas.sources import RawArtifact


_DEFAULT_MAXIMUM_BYTES = 25 * 1024 * 1024
_DEFAULT_MAXIMUM_PAGES = 500
_DEFAULT_TIMEOUT_SECONDS = 30.0


class PdfEvidenceLimitError(SourceExtractionError):
    """Raised when a PDF exceeds an explicit processing bound."""


class PdfEvidenceProcessor:
    """Extract PDF pages and table-like rows with page locators."""

    name: str = "pdfplumber-pages"
    version: str = "1"

    def __init__(
        self,
        *,
        maximum_bytes: int = _DEFAULT_MAXIMUM_BYTES,
        maximum_pages: int = _DEFAULT_MAXIMUM_PAGES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Bind strict byte, page, and elapsed-time limits."""
        if maximum_bytes <= 0 or maximum_pages <= 0 or timeout_seconds <= 0:
            raise ValueError("PDF processing bounds must be positive")
        self._maximum_bytes: int = maximum_bytes
        self._maximum_pages: int = maximum_pages
        self._timeout_seconds: float = timeout_seconds

    def supports(self, media_type: str) -> bool:
        """Accept PDF documents."""
        return media_type.partition(";")[0].strip().casefold() == "application/pdf"

    def process(self, acquisition: RawArtifact) -> EvidenceDocument:
        """Extract ordered page text and preserve table-like rows separately."""
        if len(acquisition.content) > self._maximum_bytes:
            raise PdfEvidenceLimitError("PDF exceeds its byte limit")
        started_at: float = monotonic()
        try:
            import pdfplumber

            fragments: list[EvidenceFragment] = []
            with pdfplumber.open(BytesIO(acquisition.content)) as document:
                if len(document.pages) > self._maximum_pages:
                    raise PdfEvidenceLimitError("PDF exceeds its page limit")
                for page_number, page in enumerate(document.pages, start=1):
                    if monotonic() - started_at > self._timeout_seconds:
                        raise PdfEvidenceLimitError("PDF processing timed out")
                    text: str = page.extract_text() or ""
                    if text:
                        fragments.append(
                            EvidenceFragment(
                                fragment_id=f"{acquisition.content_hash}:page:{page_number:06d}",
                                asset_id=acquisition.content_hash,
                                kind="page",
                                locator=PageLocator(page_number=page_number),
                                extracted_text=text,
                                extraction_method=self.name,
                            ),
                        )
                    for table_index, table in enumerate(page.find_tables(), start=1):
                        rows: list[list[str | None]] = table.extract()
                        rendered_rows: tuple[str, ...] = tuple("\t".join(cell or "" for cell in row) for row in rows)
                        fragments.append(
                            EvidenceFragment(
                                fragment_id=(f"{acquisition.content_hash}:table:{page_number:06d}:{table_index:04d}"),
                                asset_id=acquisition.content_hash,
                                kind="table",
                                locator=PageLocator(
                                    page_number=page_number,
                                    bounding_box=(
                                        float(table.bbox[0]),
                                        float(table.bbox[1]),
                                        float(table.bbox[2]),
                                        float(table.bbox[3]),
                                    ),
                                ),
                                extracted_text="\n".join(rendered_rows),
                                extraction_method=f"{self.name}:table",
                            ),
                        )
        except (ImportError, OSError, ValueError) as error:
            raise SourceExtractionError("PDF evidence extraction failed") from error
        return EvidenceDocument(asset=evidence_asset(acquisition), fragments=tuple(fragments))

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Return the uniform processing result for this single-asset document."""
        return EvidenceProcessingBundle(primary=self.process(acquisition))
