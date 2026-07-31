"""Module containing YouTube-channel new-episode detection over the Atom RSS feed for the money_pit package."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from typing import Protocol
from typing import TypeAlias
from typing import cast
from typing import runtime_checkable

import requests
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring


_RSS_URL_TEMPLATE: str = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
_ATOM_NS: str = "http://www.w3.org/2005/Atom"
_YT_NS: str = "http://www.youtube.com/xml/schemas/2015"
_DEFAULT_TIMEOUT_SECONDS: float = 10.0

_ENTRY_PATH: str = f"{{{_ATOM_NS}}}entry"
_VIDEO_ID_PATH: str = f"{{{_YT_NS}}}videoId"


HttpGet: TypeAlias = Callable[[str], str]


@runtime_checkable
class XmlElement(Protocol):
    """Structural XML element boundary returned by the hardened parser."""

    text: str | None
    attrib: Mapping[str, str]

    def find(self, path: str) -> XmlElement | None:
        """Return the first matching child element."""
        ...

    def findall(self, path: str) -> list[XmlElement]:
        """Return all matching child elements."""
        ...


class EpisodeDetectionError(Exception):
    """Raised when the channel feed is empty or malformed so the scheduler fails loudly rather than acting on garbage."""


def _parse_latest_video_id(rss_xml: str) -> str:
    """Return the newest entry's videoId from the Atom feed, raising EpisodeDetectionError when it cannot be parsed."""
    try:
        parsed_root: object = cast("object", fromstring(rss_xml))
        if not isinstance(parsed_root, XmlElement):
            raise EpisodeDetectionError("Channel feed XML has an invalid root.")
        root: XmlElement = parsed_root
    except (ParseError, DefusedXmlException) as error:
        raise EpisodeDetectionError(f"Channel feed XML is unparseable: {error}") from error

    entry: XmlElement | None = root.find(_ENTRY_PATH)
    if entry is None:
        raise EpisodeDetectionError("Channel feed has no entries; cannot determine the newest video id.")

    video_id_element: XmlElement | None = entry.find(_VIDEO_ID_PATH)
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
