"""Module containing bounded RSS and Atom discovery connectors."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Protocol
from typing import cast
from typing import runtime_checkable

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring
from pydantic import ConfigDict

from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import BoundedConnectorConfig
from money_pit.sources._shared import parse_config
from money_pit.sources._shared import require_media_type
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.http import HttpResponse
from money_pit.sources.http import HttpTransport
from money_pit.sources.http import UrllibHttpTransport
from money_pit.sources.http import WebConnector


if TYPE_CHECKING:
    from collections.abc import Mapping

    from money_pit.schemas.evidence import EvidenceDocument


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


class FeedConnectorConfig(BoundedConnectorConfig):
    """Configuration accepted by RSS and Atom connectors."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class FeedConnector:
    """Discovers linked entries from one RSS or Atom feed."""

    def __init__(
        self,
        definition: SourceDefinition,
        transport: HttpTransport | None = None,
    ) -> None:
        """Bind one feed definition to an optional HTTP transport."""
        self._definition: SourceDefinition = definition
        self._config: FeedConnectorConfig = FeedConnectorConfig.model_validate(
            parse_config(definition, FeedConnectorConfig).model_dump(),
        )
        self._transport: HttpTransport = transport or UrllibHttpTransport()
        self._web: WebConnector = WebConnector(
            definition.model_copy(update={"adapter_config": definition.adapter_config}),
            self._transport,
        )

    def discover(self, cursor: SourceCursor | None) -> DiscoveryBatch:
        """Fetch and parse a bounded feed, returning only entries after the cursor."""
        response: HttpResponse = self._transport.get(
            self._definition.locator,
            maximum_bytes=self._config.max_content_bytes,
            timeout_seconds=self._config.timeout_seconds,
        )
        require_media_type(
            response.media_type,
            ("application/atom+xml", "application/rss+xml", "application/xml", "text/xml"),
        )
        try:
            parsed_root: object = cast("object", fromstring(response.content))
            if not isinstance(parsed_root, XmlElement):
                raise SourceDiscoveryError("Feed XML has an invalid root")
            root: XmlElement = parsed_root
        except (ParseError, DefusedXmlException) as error:
            raise SourceDiscoveryError("Feed XML cannot be parsed") from error
        entries: list[SourceItem] = _parse_feed_entries(root, self._definition.source_id)
        unseen: tuple[SourceItem, ...] = _entries_before_cursor(entries, cursor)
        next_cursor: SourceCursor | None = SourceCursor(value=entries[0].source_item_id) if entries else cursor
        return DiscoveryBatch(items=unseen, next_cursor=next_cursor, discovered_at=utc_now())

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Fetch an entry's canonical webpage."""
        return self._web.fetch(item)

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract the fetched entry through the webpage extractor."""
        return self._web.extract(artifact)


def _parse_feed_entries(root: XmlElement, source_id: str) -> list[SourceItem]:
    now: datetime = utc_now()
    entries: list[SourceItem] = []
    for element in root.findall(".//item") + root.findall(".//{*}entry"):
        identifier: str | None = _child_text(element, ("guid", "{*}id"))
        link: str | None = _entry_link(element)
        if link is None:
            continue
        stable_id: str = identifier or link
        published: datetime | None = _parse_datetime(
            _child_text(element, ("pubDate", "{*}published", "{*}updated")),
        )
        entries.append(
            SourceItem(
                source_item_id=f"{source_id}:{stable_id}",
                source_id=source_id,
                canonical_uri=link,
                published_at=published,
                updated_at=published,
                discovered_at=now,
                content_version=stable_id,
            ),
        )
    return entries


def _entries_before_cursor(
    entries: list[SourceItem],
    cursor: SourceCursor | None,
) -> tuple[SourceItem, ...]:
    if cursor is None:
        return tuple(entries)
    unseen: list[SourceItem] = []
    for entry in entries:
        if entry.source_item_id == cursor.value:
            break
        unseen.append(entry)
    return tuple(unseen)


def _child_text(element: XmlElement, names: tuple[str, ...]) -> str | None:
    for name in names:
        child: XmlElement | None = element.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return None


def _entry_link(element: XmlElement) -> str | None:
    text_link: str | None = _child_text(element, ("link",))
    if text_link:
        return text_link
    atom_link: XmlElement | None = element.find("{*}link")
    return atom_link.attrib.get("href") if atom_link is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed: datetime = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
