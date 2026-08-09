from pathlib import Path

import pytest

from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources.errors import SourceContentTooLargeError
from money_pit.sources.errors import SourceExtractionError
from money_pit.sources.errors import SourceMediaTypeError
from money_pit.sources.local import local_text_connector


def _definition(path: Path, *, maximum_bytes: int = 1024) -> SourceDefinition:
    return SourceDefinition(
        source_id="local",
        adapter_name="local_text",
        locator=str(path),
        provenance_group="local-test",
        allowed_uses=(AllowedUse.INTERPRETATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
        adapter_config={"max_content_bytes": maximum_bytes},
    )


def test_discover_is_idempotent_for_matching_cursor(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    _ = source_path.write_text("evidence", encoding="utf-8")
    connector = local_text_connector(_definition(source_path))
    first = connector.discover(None)

    second = connector.discover(first.next_cursor)

    assert second.items == ()
    assert second.next_cursor == first.next_cursor


def test_discover_creates_new_content_version_after_edit(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    _ = source_path.write_text("first", encoding="utf-8")
    connector = local_text_connector(_definition(source_path))
    first = connector.discover(None)
    _ = source_path.write_text("second", encoding="utf-8")

    second = connector.discover(first.next_cursor)

    assert second.items[0].content_version != first.items[0].content_version


def test_fetch_rejects_edit_after_discovery(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    _ = source_path.write_text("first", encoding="utf-8")
    connector = local_text_connector(_definition(source_path))
    item = connector.discover(None).items[0]
    _ = source_path.write_text("second", encoding="utf-8")

    with pytest.raises(SourceExtractionError):
        _ = connector.fetch(item)


def test_discover_rejects_oversized_content(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    _ = source_path.write_bytes(b"too large")
    connector = local_text_connector(_definition(source_path, maximum_bytes=3))

    with pytest.raises(SourceContentTooLargeError):
        _ = connector.discover(None)


def test_fetch_rejects_unaccepted_media_type(tmp_path: Path) -> None:
    source_path = tmp_path / "source.bin"
    _ = source_path.write_bytes(b"plain bytes")
    connector = local_text_connector(_definition(source_path))
    item = connector.discover(None).items[0]

    with pytest.raises(SourceMediaTypeError):
        _ = connector.fetch(item)


def test_extract_retains_asset_and_text_locator(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    _ = source_path.write_text("traceable claim", encoding="utf-8")
    connector = local_text_connector(_definition(source_path))
    item = connector.discover(None).items[0]

    evidence = connector.extract(connector.fetch(item))

    assert evidence.asset.source_item_id == item.source_item_id
    assert evidence.asset.local_path == Path(
        evidence.asset.content_hash[:2],
        evidence.asset.content_hash,
    )
    assert evidence.fragments[0].extracted_text == "traceable claim"
    locator = evidence.fragments[0].locator
    assert isinstance(locator, TextLocator)
    assert locator.end_offset == len("traceable claim")
