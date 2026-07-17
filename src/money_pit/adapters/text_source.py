"""Module containing the deterministic thesis-file front-end that mints a TextPayload before LLM classification for the money_pit package."""

import hashlib
from pathlib import Path

from money_pit.adapters.text_llm import TextPayload
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


class ThesisFileNotReadableError(Exception):
    """Raised when the thesis path is missing or cannot be read."""


class EmptyThesisError(Exception):
    """Raised when the thesis file is empty or whitespace-only."""


def read_thesis(path: Path) -> str:
    """Read a UTF-8 thesis file, raising on unreadable paths or empty content."""
    try:
        raw: str = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ThesisFileNotReadableError(str(path)) from error
    body: str = raw.strip()
    if not body:
        raise EmptyThesisError(str(path))
    return body


def text_source_id(body: str) -> str:
    """Derive a deterministic, clock-free source id from the thesis body."""
    return f"note:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:8]}"


def build_text_payload(
    body: str,
    *,
    slug: str,
    title: str,
    retrieved_at: str,
) -> TextPayload:
    """Mint a TextPayload with a content-addressed manual-note SourceRef."""
    source_ref: SourceRef = SourceRef(
        source_id=text_source_id(body),
        source_type=SourceType.MANUAL_NOTE,
        title=title,
        url=None,
        published_at=None,
        retrieved_at=retrieved_at,
        locator=None,
    )
    return TextPayload(slug=slug, source_ref=source_ref, body=body)
