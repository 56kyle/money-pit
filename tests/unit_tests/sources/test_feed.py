from dataclasses import dataclass

import pytest

from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.feeds import FeedConnector
from money_pit.sources.http import HttpResponse


@dataclass
class StaticTransport:
    response: HttpResponse

    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        del url, maximum_bytes, timeout_seconds
        return self.response


def _connector(content: bytes, media_type: str = "application/rss+xml") -> FeedConnector:
    definition = SourceDefinition(
        source_id="feed",
        adapter_name="rss",
        locator="https://example.com/feed.xml",
        provenance_group="example-feed",
        allowed_uses=(AllowedUse.INTERPRETATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
    )
    transport = StaticTransport(HttpResponse(content, media_type, "https://example.com/feed.xml"))
    return FeedConnector(definition, transport)


def test_discover_stops_at_cursor_and_advances_to_newest_item() -> None:
    connector = _connector(
        b"""<rss><channel>
        <item><guid>new</guid><link>https://example.com/new</link></item>
        <item><guid>old</guid><link>https://example.com/old</link></item>
        </channel></rss>"""
    )

    batch = connector.discover(SourceCursor(value="feed:old"))

    assert tuple(item.source_item_id for item in batch.items) == ("feed:new",)
    assert batch.next_cursor == SourceCursor(value="feed:new")


def test_discover_recovers_when_cursor_is_absent() -> None:
    connector = _connector(
        b"""<rss><channel>
        <item><guid>new</guid><link>https://example.com/new</link></item>
        <item><guid>old</guid><link>https://example.com/old</link></item>
        </channel></rss>"""
    )

    batch = connector.discover(SourceCursor(value="feed:missing"))

    assert tuple(item.source_item_id for item in batch.items) == ("feed:new", "feed:old")


@pytest.mark.parametrize(
    "content",
    [
        b"<rss><unclosed>",
        b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///secret">]><rss>&xxe;</rss>',
    ],
)
def test_discover_rejects_unsafe_or_malformed_xml(content: bytes) -> None:
    connector = _connector(content)

    with pytest.raises(SourceDiscoveryError):
        _ = connector.discover(None)
