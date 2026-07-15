"""Module containing YouTube-channel new-episode detection over the Atom RSS feed for the money_pit package."""

from collections.abc import Callable
from typing import TypeAlias
from xml.etree import ElementTree

import requests


_RSS_URL_TEMPLATE: str = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
_ATOM_NS: str = "http://www.w3.org/2005/Atom"
_YT_NS: str = "http://www.youtube.com/xml/schemas/2015"
_DEFAULT_TIMEOUT_SECONDS: float = 10.0

_ENTRY_PATH: str = f"{{{_ATOM_NS}}}entry"
_VIDEO_ID_PATH: str = f"{{{_YT_NS}}}videoId"


HttpGet: TypeAlias = Callable[[str], str]


class EpisodeDetectionError(Exception):
    """Raised when the channel feed is empty or malformed so the scheduler fails loudly rather than acting on garbage."""


def _parse_latest_video_id(rss_xml: str) -> str:
    """Return the newest entry's videoId from the Atom feed, raising EpisodeDetectionError when it cannot be parsed."""
    try:
        root: ElementTree.Element = ElementTree.fromstring(rss_xml)
    except ElementTree.ParseError as error:
        raise EpisodeDetectionError(f"Channel feed XML is unparseable: {error}") from error

    entry: ElementTree.Element | None = root.find(_ENTRY_PATH)
    if entry is None:
        raise EpisodeDetectionError("Channel feed has no entries; cannot determine the newest video id.")

    video_id_element: ElementTree.Element | None = entry.find(_VIDEO_ID_PATH)
    video_id: str | None = video_id_element.text if video_id_element is not None else None
    if not video_id:
        raise EpisodeDetectionError("Newest channel feed entry has no non-empty videoId.")
    return video_id


def fetch_latest_video_id(channel_id: str, http_get: HttpGet) -> str:
    """Return the newest video id for a channel, fetching the feed via the injected http_get seam."""
    url: str = _RSS_URL_TEMPLATE.format(channel_id=channel_id)
    return _parse_latest_video_id(http_get(url))


def make_requests_http_get(*, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> HttpGet:
    """Return an HttpGet backed by requests, raising for HTTP error status before returning the response body."""

    def http_get(url: str) -> str:  # pragma: no cover
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.text

    return http_get
