"""Module containing SEC filing-feed source connectors."""

from urllib.parse import urlsplit

from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.feeds import FeedConnector
from money_pit.sources.http import HttpTransport


class SecFilingsConnector:
    """SEC-host-restricted filing discovery over the SEC Atom feed."""

    def __init__(
        self,
        definition: SourceDefinition,
        transport: HttpTransport | None = None,
    ) -> None:
        """Bind a feed connector after enforcing the SEC host boundary."""
        hostname: str | None = urlsplit(definition.locator).hostname
        if hostname is None or not (hostname.lower() == "sec.gov" or hostname.lower().endswith(".sec.gov")):
            raise ConnectorConfigurationError("SEC source locator must use an sec.gov host")
        self._feed: FeedConnector = FeedConnector(definition, transport)

    def discover(self, cursor: SourceCursor | None) -> DiscoveryBatch:
        """Discover bounded SEC filing entries."""
        return self._feed.discover(cursor)

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Fetch the SEC filing landing document."""
        return self._feed.fetch(item)

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract filing webpage text with its original locator."""
        return self._feed.extract(artifact)
