"""Module containing media-neutral evidence processor contracts and registry."""

from __future__ import annotations

import email
import json
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from typing import Protocol
from typing import cast

from typing_extensions import override

from money_pit.evidence.errors import EvidenceProcessorAlreadyRegisteredError
from money_pit.evidence.errors import EvidenceProcessorNotFoundError
from money_pit.evidence.media import MediaEvidenceProcessor
from money_pit.evidence.pdf import PdfEvidenceProcessor
from money_pit.evidence.results import EvidenceProcessingBundle
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.sources._shared import evidence_asset
from money_pit.sources.errors import SourceExtractionError


if TYPE_CHECKING:
    from collections.abc import Iterable
    from email.message import Message

    from money_pit.schemas.sources import RawArtifact


class EvidenceProcessor(Protocol):
    """Converts one bounded acquisition into provenance-linked evidence."""

    @property
    def name(self) -> str:
        """Return the stable processor identity recorded with attempts."""
        ...

    @property
    def version(self) -> str:
        """Return the implementation version recorded with attempts."""
        ...

    def supports(self, media_type: str) -> bool:
        """Return whether this processor accepts the normalized media type."""
        ...

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Extract evidence without changing or persisting the acquisition."""
        ...


class EvidenceProcessorRegistry:
    """Ordered processor registry with explicit duplicate and ambiguity rules."""

    def __init__(self) -> None:
        """Create an empty processor registry."""
        self._processors: list[EvidenceProcessor] = []
        self._names: set[str] = set()

    def register(self, processor: EvidenceProcessor) -> None:
        """Register one uniquely named processor."""
        if processor.name in self._names:
            raise EvidenceProcessorAlreadyRegisteredError(
                f"Evidence processor already registered: {processor.name}",
            )
        self._processors.append(processor)
        self._names.add(processor.name)

    def select(self, media_type: str) -> EvidenceProcessor:
        """Select the sole processor that supports a media type."""
        matches: list[EvidenceProcessor] = [
            processor for processor in self._processors if processor.supports(media_type)
        ]
        if len(matches) != 1:
            names: str = ", ".join(processor.name for processor in matches)
            detail: str = f"; matches: {names}" if names else ""
            raise EvidenceProcessorNotFoundError(
                f"Expected exactly one processor for {media_type!r}{detail}",
            )
        return matches[0]

    def process(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Process an acquisition using its media type."""
        return self.select(acquisition.media_type).process_bundle(acquisition)


class TextEvidenceProcessor:
    """Extract UTF-8 text from text-like acquisitions."""

    name: str = "text-utf8"
    version: str = "1"

    def supports(self, media_type: str) -> bool:
        """Accept plain text and source-code-like text media."""
        normalized: str = _normalized_media_type(media_type)
        return normalized.startswith("text/") and normalized != "text/html"

    def process(self, acquisition: RawArtifact) -> EvidenceDocument:
        """Decode one complete bounded text acquisition."""
        try:
            text: str = acquisition.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceExtractionError("Source text is not valid UTF-8") from error
        return _text_document(acquisition, text, extraction_method=self.name)

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Wrap plain-text evidence in the uniform processor result."""
        return EvidenceProcessingBundle(primary=self.process(acquisition))


class Rfc5322EvidenceProcessor:
    """Extract visible plain-text bodies from an RFC 5322 message."""

    name: str = "rfc5322-text"
    version: str = "1"

    def supports(self, media_type: str) -> bool:
        """Accept RFC 5322 message acquisitions."""
        return _normalized_media_type(media_type) == "message/rfc822"

    def process(self, acquisition: RawArtifact) -> EvidenceDocument:
        """Extract ordered non-attachment plain-text message parts."""
        try:
            message: Message = email.message_from_bytes(acquisition.content)
        except (TypeError, ValueError) as error:
            raise SourceExtractionError("Email message cannot be parsed") from error
        text: str = "\n\n".join(_plain_text_parts(message))
        return _text_document(acquisition, text, extraction_method=self.name)

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Wrap email evidence in the uniform processor result."""
        return EvidenceProcessingBundle(primary=self.process(acquisition))


class HtmlEvidenceProcessor:
    """Extract visible text from bounded HTML acquisitions."""

    name: str = "html-visible-text"
    version: str = "1"

    def supports(self, media_type: str) -> bool:
        """Accept HTML documents."""
        return _normalized_media_type(media_type) == "text/html"

    def process(self, acquisition: RawArtifact) -> EvidenceDocument:
        """Decode HTML and retain only model-visible text."""
        try:
            source: str = acquisition.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceExtractionError("Web content is not valid UTF-8") from error
        parser = _VisibleTextParser()
        parser.feed(source)
        return _text_document(acquisition, parser.text(), extraction_method=self.name)

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Wrap HTML evidence in the uniform processor result."""
        return EvidenceProcessingBundle(primary=self.process(acquisition))


class JsonEvidenceProcessor:
    """Validate and expose structured JSON acquisitions as bounded text."""

    name: str = "json-text"
    version: str = "1"

    def supports(self, media_type: str) -> bool:
        """Accept JSON documents and JSON-derived API responses."""
        normalized: str = _normalized_media_type(media_type)
        return normalized == "application/json" or normalized.endswith("+json")

    def process(self, acquisition: RawArtifact) -> EvidenceDocument:
        """Validate UTF-8 JSON before exposing its exact source text."""
        try:
            text: str = acquisition.content.decode("utf-8")
            _ = cast("object", json.loads(text))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceExtractionError("JSON evidence cannot be decoded") from error
        return _text_document(acquisition, text, extraction_method=self.name)

    def process_bundle(self, acquisition: RawArtifact) -> EvidenceProcessingBundle:
        """Wrap JSON evidence in the uniform processor result."""
        return EvidenceProcessingBundle(primary=self.process(acquisition))


class _VisibleTextParser(HTMLParser):
    """Minimal visible-text projection that drops executable and styling elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth: int = 0
        self._parts: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter content hidden from a rendered page."""
        del attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self._hidden_depth += 1

    @override
    def handle_endtag(self, tag: str) -> None:
        """Leave content hidden from a rendered page."""
        if tag.casefold() in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1

    @override
    def handle_data(self, data: str) -> None:
        """Collect visible nonblank text nodes."""
        stripped: str = data.strip()
        if not self._hidden_depth and stripped:
            self._parts.append(stripped)

    def text(self) -> str:
        """Return visible nodes separated by stable newlines."""
        return "\n".join(self._parts)


def _plain_text_parts(message: Message) -> Iterable[str]:
    messages: tuple[Message, ...] = tuple(message.walk()) if message.is_multipart() else (message,)
    for part in messages:
        if part.get_content_type() != "text/plain" or part.get_content_disposition() == "attachment":
            continue
        payload: object = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset: str = part.get_content_charset() or "utf-8"
        try:
            yield payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as error:
            raise SourceExtractionError("Email text cannot be decoded") from error


def _text_document(
    acquisition: RawArtifact,
    text: str,
    *,
    extraction_method: str,
) -> EvidenceDocument:
    fragment: EvidenceFragment = EvidenceFragment(
        fragment_id=f"{acquisition.content_hash}:text",
        asset_id=acquisition.content_hash,
        kind="web_span",
        locator=TextLocator(start_offset=0, end_offset=len(text)),
        extracted_text=text,
        extraction_method=extraction_method,
    )
    return EvidenceDocument(asset=evidence_asset(acquisition), fragments=(fragment,))


def _normalized_media_type(media_type: str) -> str:
    return media_type.partition(";")[0].strip().casefold()


def builtin_evidence_processors() -> EvidenceProcessorRegistry:
    """Return processors whose extraction semantics are media-generic."""
    registry = EvidenceProcessorRegistry()
    registry.register(TextEvidenceProcessor())
    registry.register(Rfc5322EvidenceProcessor())
    registry.register(HtmlEvidenceProcessor())
    registry.register(JsonEvidenceProcessor())
    registry.register(PdfEvidenceProcessor())
    registry.register(MediaEvidenceProcessor())
    return registry
