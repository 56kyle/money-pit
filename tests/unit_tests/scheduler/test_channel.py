"""Unit tests for money_pit.scheduler.channel — pure Atom-feed parsing and the injected-http_get fetch seam."""

import pytest

from money_pit.scheduler.channel import EpisodeDetectionError
from money_pit.scheduler.channel import _parse_latest_video_id
from money_pit.scheduler.channel import fetch_latest_video_id


_NEWEST_VIDEO_ID: str = "NEWEST00001"
_CHANNEL_ID: str = "UCtest123"
_EXPECTED_URL: str = "https://www.youtube.com/feeds/videos.xml?channel_id=UCtest123"

_ATOM_NS: str = "http://www.w3.org/2005/Atom"
_YT_NS: str = "http://www.youtube.com/xml/schemas/2015"

_FEED_NO_ENTRIES: str = (
    f'<feed xmlns="{_ATOM_NS}" xmlns:yt="{_YT_NS}">'
    f"<yt:channelId>{_CHANNEL_ID}</yt:channelId>"
    "<title>Empty Channel</title>"
    "</feed>"
)

_FEED_EMPTY_VIDEO_ID: str = (
    f'<feed xmlns="{_ATOM_NS}" xmlns:yt="{_YT_NS}">'
    "<entry>"
    "<yt:videoId></yt:videoId>"
    "<title>No Id Episode</title>"
    "</entry>"
    "</feed>"
)

_FEED_ABSENT_VIDEO_ID: str = (
    f'<feed xmlns="{_ATOM_NS}" xmlns:yt="{_YT_NS}">'
    "<entry>"
    "<title>No Id Episode</title>"
    "</entry>"
    "</feed>"
)


def test__parse_latest_video_id_with_valid(channel_feed_xml: str) -> None:
    assert _parse_latest_video_id(channel_feed_xml) == _NEWEST_VIDEO_ID


def test__parse_latest_video_id_with_unparseable_xml() -> None:
    with pytest.raises(EpisodeDetectionError):
        _parse_latest_video_id("not xml <<<")


def test__parse_latest_video_id_with_no_entries() -> None:
    with pytest.raises(EpisodeDetectionError):
        _parse_latest_video_id(_FEED_NO_ENTRIES)


@pytest.mark.parametrize("rss_xml", [_FEED_EMPTY_VIDEO_ID, _FEED_ABSENT_VIDEO_ID])
def test__parse_latest_video_id_with_missing_video_id(rss_xml: str) -> None:
    with pytest.raises(EpisodeDetectionError):
        _parse_latest_video_id(rss_xml)


def test_fetch_latest_video_id_with_valid(channel_feed_xml: str) -> None:
    recorded_urls: list[str] = []

    def fake_http_get(url: str) -> str:
        recorded_urls.append(url)
        return channel_feed_xml

    result = fetch_latest_video_id(_CHANNEL_ID, fake_http_get)

    assert result == _NEWEST_VIDEO_ID
    assert recorded_urls == [_EXPECTED_URL]
