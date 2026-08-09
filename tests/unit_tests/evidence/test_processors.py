from datetime import UTC
from datetime import datetime

import pytest

from money_pit.evidence.errors import EvidenceProcessorAlreadyRegisteredError
from money_pit.evidence.errors import EvidenceProcessorNotFoundError
from money_pit.evidence.processors import EvidenceProcessorRegistry
from money_pit.evidence.processors import HtmlEvidenceProcessor
from money_pit.evidence.processors import JsonEvidenceProcessor
from money_pit.evidence.processors import Rfc5322EvidenceProcessor
from money_pit.evidence.processors import TextEvidenceProcessor
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import sha256_bytes
from money_pit.sources.errors import SourceExtractionError


NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _artifact(content: bytes, media_type: str) -> RawArtifact:
    digest = sha256_bytes(content)
    item = SourceItem(
        source_item_id="source:item",
        source_id="source",
        source_definition_hash="d" * 64,
        canonical_uri="https://example.test/item",
        discovered_at=NOW,
        content_version="v1",
    )
    return RawArtifact(
        source_item=item,
        content=content,
        media_type=media_type,
        retrieved_at=NOW,
        canonical_uri=item.canonical_uri,
        content_hash=digest,
    )


def test_text_evidence_processor_normalizes_a_parameterized_media_type() -> None:
    artifact = _artifact(b"plain text", " Text/Plain; charset=UTF-8 ")

    document = TextEvidenceProcessor().process(artifact)

    assert document.fragments[0].extracted_text == "plain text"


def test_html_evidence_processor_excludes_nonvisible_elements() -> None:
    artifact = _artifact(
        b"<h1>Visible</h1><script>secret()</script><style>hidden</style><p>Body</p>",
        "text/html",
    )

    document = HtmlEvidenceProcessor().process(artifact)

    assert document.fragments[0].extracted_text == "Visible\nBody"


def test_rfc5322_evidence_processor_excludes_attachments() -> None:
    content = (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nmessage body\r\n"
        b"--b\r\nContent-Type: text/plain\r\nContent-Disposition: attachment\r\n\r\nattachment\r\n"
        b"--b--\r\n"
    )

    document = Rfc5322EvidenceProcessor().process(_artifact(content, "message/rfc822"))

    assert document.fragments[0].extracted_text == "message body"


def test_json_evidence_processor_rejects_invalid_json() -> None:
    with pytest.raises(SourceExtractionError):
        _ = JsonEvidenceProcessor().process(_artifact(b"{invalid", "application/json"))


def test_evidence_processor_registry_rejects_duplicate_stable_names() -> None:
    registry = EvidenceProcessorRegistry()
    registry.register(TextEvidenceProcessor())

    with pytest.raises(EvidenceProcessorAlreadyRegisteredError):
        registry.register(TextEvidenceProcessor())


def test_evidence_processor_registry_rejects_ambiguous_media_support() -> None:
    registry = EvidenceProcessorRegistry()
    registry.register(TextEvidenceProcessor())
    duplicate = TextEvidenceProcessor()
    duplicate.name = "other-text"
    registry.register(duplicate)

    with pytest.raises(EvidenceProcessorNotFoundError):
        _ = registry.select("text/plain")
