"""Module containing YouTube uploads-playlist discovery contracts."""

import os
from typing import ClassVar
from urllib.parse import urlencode

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import ValidationError

from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import BoundedConnectorConfig
from money_pit.sources._shared import parse_config
from money_pit.sources._shared import require_media_type
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.http import HttpResponse
from money_pit.sources.http import HttpTransport
from money_pit.sources.http import UrllibHttpTransport
from money_pit.sources.http import WebConnector


_YOUTUBE_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"


class YouTubeConnectorConfig(BoundedConnectorConfig):
    """Configuration for uploads-playlist discovery."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    api_key_env: str = Field(min_length=1)
    max_results: int = Field(default=25, ge=1, le=50)


class _YouTubeResourceId(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    video_id: str = Field(alias="videoId")


class _YouTubeSnippet(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    published_at: AwareDatetime = Field(alias="publishedAt")
    resource_id: _YouTubeResourceId = Field(alias="resourceId")


class _YouTubePlaylistItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    id: str
    snippet: _YouTubeSnippet


class _YouTubePlaylistResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    next_page_token: str | None = Field(default=None, alias="nextPageToken")
    items: tuple[_YouTubePlaylistItem, ...] = ()


class YouTubeConnector:
    """Discovers videos through a channel's configured uploads playlist."""

    def __init__(
        self,
        definition: SourceDefinition,
        transport: HttpTransport | None = None,
    ) -> None:
        """Bind playlist discovery to runtime credentials and transport."""
        self._definition: SourceDefinition = definition
        self._config: YouTubeConnectorConfig = YouTubeConnectorConfig.model_validate(
            parse_config(definition, YouTubeConnectorConfig).model_dump(),
        )
        api_key_value: str | None = os.environ.get(self._config.api_key_env)
        if not api_key_value:
            raise ConnectorConfigurationError(
                f"Environment variable {self._config.api_key_env!r} is not configured",
            )
        self._api_key: SecretStr = SecretStr(api_key_value)
        self._transport: HttpTransport = transport or UrllibHttpTransport()
        web_definition: SourceDefinition = definition.model_copy(
            update={
                "locator": "https://www.youtube.com/",
                "adapter_config": {"max_content_bytes": self._config.max_content_bytes},
            },
        )
        self._web: WebConnector = WebConnector(web_definition, self._transport)

    def discover(self, cursor: SourceCursor | None) -> DiscoveryBatch:
        """Return one bounded YouTube playlist page and its next-page cursor."""
        query_params: dict[str, str | int] = {
            "part": "snippet",
            "playlistId": self._definition.locator,
            "maxResults": self._config.max_results,
            "key": self._api_key.get_secret_value(),
        }
        if cursor is not None:
            query_params["pageToken"] = cursor.value
        query: str = urlencode(query_params)
        response: HttpResponse = self._transport.get(
            f"{_YOUTUBE_PLAYLIST_ITEMS_URL}?{query}",
            maximum_bytes=self._config.max_content_bytes,
            timeout_seconds=self._config.timeout_seconds,
        )
        require_media_type(response.media_type, ("application/json",))
        try:
            parsed: _YouTubePlaylistResponse = _YouTubePlaylistResponse.model_validate_json(
                response.content,
            )
        except ValidationError as error:
            raise SourceDiscoveryError("YouTube playlist response is malformed") from error
        items: tuple[SourceItem, ...] = tuple(
            SourceItem(
                source_item_id=f"{self._definition.source_id}:{item.id}",
                source_id=self._definition.source_id,
                canonical_uri=f"https://www.youtube.com/watch?v={item.snippet.resource_id.video_id}",
                published_at=item.snippet.published_at,
                updated_at=item.snippet.published_at,
                discovered_at=utc_now(),
                content_version=item.id,
            )
            for item in parsed.items
        )
        next_cursor: SourceCursor | None = (
            SourceCursor(value=parsed.next_page_token) if parsed.next_page_token else None
        )
        return DiscoveryBatch(items=items, next_cursor=next_cursor, discovered_at=utc_now())

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Fetch the public watch page while video media remains adapter-neutral."""
        return self._web.fetch(item)

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract watch-page evidence; media enrichment remains a later stage."""
        return self._web.extract(artifact)
