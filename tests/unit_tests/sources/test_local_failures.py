# pyright: reportPrivateUsage=false, reportUnusedCallResult=false

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from money_pit.schemas.evidence import PageLocator
from money_pit.schemas.sources import SourceDefinition
from money_pit.sources.errors import SourceExtractionError
from money_pit.sources.errors import SourceFetchError
from money_pit.sources.local import LocalFileConnector
from money_pit.sources.local import _decode_text
from money_pit.sources.local import _extract_email_text
from money_pit.sources.local import local_audio_connector
from money_pit.sources.local import local_email_connector
from money_pit.sources.local import local_text_connector


def _definition(path: Path, *, adapter_name: str = "local_text") -> SourceDefinition:
    return SourceDefinition(
        source_id="local",
        adapter_name=adapter_name,
        locator=str(path),
    )


def test_fetch_rejects_item_from_another_source(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("evidence", encoding="utf-8")
    connector = local_text_connector(_definition(source_path))
    item = connector.discover(None).items[0].model_copy(update={"source_id": "other"})

    with pytest.raises(SourceFetchError, match="does not belong"):
        connector.fetch(item)


def test_extract_binary_retains_no_inferred_fragments(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mp3"
    source_path.write_bytes(b"audio")
    connector = local_audio_connector(_definition(source_path, adapter_name="local_audio"))
    artifact = connector.fetch(connector.discover(None).items[0])

    document = connector.extract(artifact)

    assert document.fragments == ()


def test_extract_pdf_as_text_uses_page_locator(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_text("page", encoding="utf-8")
    connector = LocalFileConnector(
        _definition(source_path),
        ("application/pdf",),
        "application/pdf",
        "text",
    )
    artifact = connector.fetch(connector.discover(None).items[0])

    document = connector.extract(artifact)

    assert document.fragments[0].locator == PageLocator(page_number=1)


def test__decode_text_rejects_non_utf8() -> None:
    with pytest.raises(SourceExtractionError, match="not valid UTF-8"):
        _decode_text(b"\xff")


def test__extract_email_text_uses_declared_charset(tmp_path: Path) -> None:
    source_path = tmp_path / "message.eml"
    source_path.write_bytes(
        b"Content-Type: text/plain; charset=iso-8859-1\r\n\r\ncaf\xe9",
    )
    connector = local_email_connector(_definition(source_path, adapter_name="local_email"))
    artifact = connector.fetch(connector.discover(None).items[0])

    document = connector.extract(artifact)

    assert document.fragments[0].extracted_text == "caf\u00e9"


def test__extract_email_text_rejects_unknown_charset() -> None:
    message = b"Content-Type: text/plain; charset=unknown-charset\r\n\r\ntext"

    with pytest.raises(SourceExtractionError, match="cannot be decoded"):
        _extract_email_text(message)


def test__extract_email_text_ignores_non_plain_parts() -> None:
    message = b"Content-Type: text/html\r\n\r\n<p>ignored</p>"

    assert _extract_email_text(message) == ""


def test__extract_email_text_wraps_parser_failure(monkeypatch: MonkeyPatch) -> None:
    def fail(_content: bytes) -> object:
        raise ValueError("invalid")

    monkeypatch.setattr("money_pit.sources.local.email.message_from_bytes", fail)

    with pytest.raises(SourceExtractionError, match="cannot be parsed"):
        _extract_email_text(b"message")
