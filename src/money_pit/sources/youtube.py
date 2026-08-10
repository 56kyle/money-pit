"""Module containing YouTube uploads-playlist discovery contracts."""

import hashlib
import re
import tempfile
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from pathlib import Path
from time import monotonic
from typing import Callable
from typing import ClassVar
from typing import Protocol
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import ValidationError

from money_pit.evidence.media import MediaAnalyzer
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import BoundedConnectorConfig
from money_pit.sources._shared import parse_config
from money_pit.sources._shared import require_media_type
from money_pit.sources._shared import source_definition_hash
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.errors import InvalidYouTubeVideoUrlError
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.errors import SourceFetchError
from money_pit.sources.errors import YouTubeSourceMembershipError
from money_pit.sources.http import AddressPinnedHttpTransport
from money_pit.sources.http import HttpResponse
from money_pit.sources.http import HttpTransport


_YOUTUBE_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
_YOUTUBE_VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
_YOUTUBE_CHANNEL_ID_PATTERN = r"^UC[A-Za-z0-9_-]{22}$"
_YOUTUBE_UPLOADS_PLAYLIST_ID_PATTERN = r"^UU[A-Za-z0-9_-]{22}$"
_YOUTUBE_WATCH_HOSTS = frozenset({"youtube.com", "www.youtube.com"})


class YouTubeConnectorConfig(BoundedConnectorConfig):
    """Configuration for uploads-playlist discovery."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    max_results: int = Field(default=25, ge=1, le=50)
    max_sync_pages: int = Field(default=20, ge=2, le=100)
    max_media_bytes: int = Field(default=512 * 1024 * 1024, ge=1)


class YouTubeMediaAcquisition(BaseModel):
    """Bounded media bytes plus yt-dlp's verified YouTube identity metadata."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    content: bytes
    media_type: str = Field(min_length=1)
    filename: Path
    video_id: str = Field(pattern=_YOUTUBE_VIDEO_ID_PATTERN)
    channel_id: str = Field(pattern=_YOUTUBE_CHANNEL_ID_PATTERN)
    published_at: AwareDatetime | None = None


class YouTubeMediaTransport(Protocol):
    """Read-only seam for acquiring one public video's immutable bytes."""

    def fetch(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> YouTubeMediaAcquisition:
        """Return bounded media and its verified YouTube identity metadata."""
        ...


class YtDlpMediaTransport:
    """Acquire bounded public video media without retaining downloader caches."""

    def fetch(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> YouTubeMediaAcquisition:
        """Download one video to an isolated temporary directory."""
        started_at = monotonic()

        def enforce_elapsed_bound(status: dict[str, object]) -> None:
            del status
            if monotonic() - started_at > timeout_seconds:
                raise TimeoutError("YouTube media acquisition exceeded its elapsed-time bound")

        try:
            import yt_dlp

            with tempfile.TemporaryDirectory(prefix="money-pit-youtube-") as temporary:
                output = str(Path(temporary, "media.%(ext)s"))
                options: dict[str, object] = {
                    "format": "bestvideo*+bestaudio/best",
                    "outtmpl": output,
                    "max_filesize": maximum_bytes,
                    "socket_timeout": timeout_seconds,
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "progress_hooks": [enforce_elapsed_bound],
                }
                with yt_dlp.YoutubeDL(options) as downloader:  # pyright: ignore[reportArgumentType]
                    raw_info = downloader.extract_info(url, download=True)
                try:
                    info = _YtDlpVideoInfo.model_validate(raw_info)
                except ValidationError as error:
                    raise SourceFetchError("YouTube media metadata is incomplete or malformed") from error
                paths = tuple(path for path in Path(temporary).iterdir() if path.is_file())
                if len(paths) != 1:
                    raise SourceFetchError("YouTube media acquisition produced an unexpected file set")
                path = paths[0]
                content = path.read_bytes()
                if monotonic() - started_at > timeout_seconds:
                    raise SourceFetchError("YouTube media acquisition exceeded its elapsed-time bound")
                if not content or len(content) > maximum_bytes:
                    raise SourceFetchError("YouTube media exceeds its configured byte bound")
                media_types = {
                    ".m4a": "audio/mp4",
                    ".mka": "audio/x-matroska",
                    ".mp3": "audio/mpeg",
                    ".mp4": "video/mp4",
                    ".mkv": "video/x-matroska",
                    ".webm": "video/webm",
                }
                try:
                    media_type = media_types[path.suffix.casefold()]
                except KeyError as error:
                    raise SourceFetchError("YouTube media container is unsupported") from error
                return YouTubeMediaAcquisition(
                    content=content,
                    media_type=media_type,
                    filename=Path(path.name),
                    video_id=info.video_id,
                    channel_id=info.channel_id,
                    published_at=_published_at(info),
                )
        except Exception as error:
            if isinstance(error, SourceFetchError):
                raise
            raise SourceFetchError("YouTube media acquisition failed") from error


class _YouTubeResourceId(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    video_id: str = Field(alias="videoId")


class _YtDlpVideoInfo(BaseModel):
    """Identity-bearing yt-dlp metadata required for source admission."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    video_id: str = Field(alias="id", pattern=_YOUTUBE_VIDEO_ID_PATTERN)
    channel_id: str = Field(pattern=_YOUTUBE_CHANNEL_ID_PATTERN)
    timestamp: float | None = Field(default=None, ge=0)
    upload_date: str | None = Field(default=None, pattern=r"^[0-9]{8}$")


def _published_at(info: _YtDlpVideoInfo) -> datetime | None:
    """Return yt-dlp publication metadata as a stable aware timestamp."""
    if info.timestamp is not None:
        try:
            return datetime.fromtimestamp(info.timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError) as error:
            raise SourceFetchError("YouTube publication timestamp is invalid") from error
    if info.upload_date is None:
        return None
    try:
        published_date = date.fromisoformat(
            f"{info.upload_date[:4]}-{info.upload_date[4:6]}-{info.upload_date[6:]}",
        )
    except ValueError as error:
        raise SourceFetchError("YouTube publication date is invalid") from error
    return datetime.combine(published_date, time.min, tzinfo=UTC)


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


class _YouTubeSyncRange(BaseModel):
    """A durable unfinished newest-to-watermark playlist interval."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    stop_video_id: str = Field(min_length=1)
    page_token: str = Field(min_length=1)


class _YouTubeSyncCursor(BaseModel):
    """Newest high-water identity plus bounded continuation recovery."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    high_water_video_id: str | None = None
    pending_ranges: tuple[_YouTubeSyncRange, ...] = ()


class _YouTubeVideoUrl(BaseModel):
    """Validated canonical identity for one public YouTube video URL."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    video_id: str = Field(pattern=_YOUTUBE_VIDEO_ID_PATTERN)

    @property
    def canonical_url(self) -> str:
        """Return the stable watch URL used for acquisition and provenance."""
        return f"https://www.youtube.com/watch?v={self.video_id}"


def _parse_youtube_video_url(url: str) -> _YouTubeVideoUrl:
    """Validate one HTTPS YouTube watch or youtu.be video URL."""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        username = parsed.username
        password = parsed.password
        fragment = parsed.fragment
        port = parsed.port
        hostname = parsed.hostname.casefold() if parsed.hostname is not None else ""
    except ValueError as error:
        raise InvalidYouTubeVideoUrlError("A canonical HTTPS YouTube video URL is required") from error
    if scheme != "https" or username is not None or password is not None or fragment:
        raise InvalidYouTubeVideoUrlError("A canonical HTTPS YouTube video URL is required")
    if port not in (None, 443):
        raise InvalidYouTubeVideoUrlError("A canonical HTTPS YouTube video URL is required")

    video_id: str | None = None
    if hostname in _YOUTUBE_WATCH_HOSTS and parsed.path == "/watch":
        values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
        if len(values) == 1:
            video_id = values[0]
    elif hostname == "youtu.be" and parsed.path.count("/") == 1:
        video_id = parsed.path.removeprefix("/")
    if video_id is None:
        raise InvalidYouTubeVideoUrlError("A canonical YouTube watch or youtu.be URL is required")
    try:
        return _YouTubeVideoUrl(video_id=video_id)
    except ValidationError as error:
        raise InvalidYouTubeVideoUrlError("YouTube video URL contains an invalid video ID") from error


class YouTubeConnector:
    """Discovers videos through a channel's configured uploads playlist."""

    def __init__(
        self,
        definition: SourceDefinition,
        discovery_api_key: Callable[[], SecretStr],
        analyzer: MediaAnalyzer,
        transport: HttpTransport | None = None,
        media_transport: YouTubeMediaTransport | None = None,
    ) -> None:
        """Bind playlist discovery to lazy credentials and bounded transports."""
        self._definition: SourceDefinition = definition
        self._config: YouTubeConnectorConfig = YouTubeConnectorConfig.model_validate(
            parse_config(definition, YouTubeConnectorConfig).model_dump(),
        )
        if re.fullmatch(_YOUTUBE_UPLOADS_PLAYLIST_ID_PATTERN, definition.locator) is None:
            raise ConnectorConfigurationError("YouTube source locator must be an uploads playlist ID")
        self._discovery_api_key: Callable[[], SecretStr] = discovery_api_key
        self._resolved_api_key: SecretStr | None = None
        self._analyzer: MediaAnalyzer = analyzer
        self._transport: HttpTransport = transport or AddressPinnedHttpTransport()
        self._media_transport: YouTubeMediaTransport = media_transport or YtDlpMediaTransport()

    def discover(
        self,
        cursor: SourceCursor | None,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> DiscoveryBatch:
        """Poll newest uploads or return one independently paged backfill batch."""
        if purpose is SourceCursorPurpose.BACKFILL:
            parsed = self._discover_page(None if cursor is None else cursor.value)
            return self._discovery_batch(
                parsed.items,
                next_cursor=(SourceCursor(value=parsed.next_page_token) if parsed.next_page_token else None),
            )

        state = self._sync_cursor(cursor)
        previous_high_water = state.high_water_video_id
        discovered: list[_YouTubePlaylistItem] = []
        newest_page = self._discover_page(None)
        newest_video_id = (
            newest_page.items[0].snippet.resource_id.video_id if newest_page.items else previous_high_water
        )
        reached_previous = previous_high_water is None
        for item in newest_page.items:
            if item.snippet.resource_id.video_id == previous_high_water:
                reached_previous = True
                break
            discovered.append(item)

        pending = list(state.pending_ranges)
        if not reached_previous and newest_page.next_page_token is not None and previous_high_water is not None:
            pending.append(
                _YouTubeSyncRange(
                    stop_video_id=previous_high_water,
                    page_token=newest_page.next_page_token,
                )
            )

        remaining_pages = self._config.max_sync_pages - 1
        while pending and remaining_pages > 0:
            current = pending[0]
            page = self._discover_page(current.page_token)
            remaining_pages -= 1
            reached_stop = False
            for item in page.items:
                if item.snippet.resource_id.video_id == current.stop_video_id:
                    reached_stop = True
                    break
                discovered.append(item)
            if reached_stop or page.next_page_token is None:
                _ = pending.pop(0)
            else:
                pending[0] = current.model_copy(update={"page_token": page.next_page_token})

        next_state = _YouTubeSyncCursor(
            high_water_video_id=newest_video_id,
            pending_ranges=tuple(pending),
        )
        next_cursor = SourceCursor(value=next_state.model_dump_json())
        return self._discovery_batch(tuple(discovered), next_cursor=next_cursor)

    @staticmethod
    def _sync_cursor(cursor: SourceCursor | None) -> _YouTubeSyncCursor:
        """Load the typed newest-watermark cursor or fail closed on corruption."""
        if cursor is None:
            return _YouTubeSyncCursor()
        try:
            return _YouTubeSyncCursor.model_validate_json(cursor.value)
        except ValidationError as error:
            raise SourceDiscoveryError("Stored YouTube sync cursor is malformed") from error

    def _discover_page(self, page_token: str | None) -> _YouTubePlaylistResponse:
        """Fetch and validate one bounded uploads-playlist page."""
        query_params: dict[str, str | int] = {
            "part": "snippet",
            "playlistId": self._definition.locator,
            "maxResults": self._config.max_results,
            "key": self._api_key().get_secret_value(),
        }
        if page_token is not None:
            query_params["pageToken"] = page_token
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
        return parsed

    def _api_key(self) -> SecretStr:
        """Resolve and cache the playlist-discovery credential on first discovery."""
        if self._resolved_api_key is None:
            resolved = self._discovery_api_key()
            if not resolved.get_secret_value().strip():
                raise ConnectorConfigurationError("YouTube API credential cannot be blank")
            self._resolved_api_key = resolved
        return self._resolved_api_key

    def source_item_from_url(self, url: str) -> SourceItem:
        """Materialize one validated direct video without playlist discovery."""
        parsed = _parse_youtube_video_url(url)
        discovered_at = utc_now()
        return SourceItem(
            source_item_id=f"{self._definition.source_id}:{parsed.video_id}",
            source_id=self._definition.source_id,
            source_definition_hash=source_definition_hash(self._definition),
            canonical_uri=parsed.canonical_url,
            discovered_at=discovered_at,
            content_version=parsed.video_id,
        )

    def _discovery_batch(
        self,
        page_items: tuple[_YouTubePlaylistItem, ...] | list[_YouTubePlaylistItem],
        *,
        next_cursor: SourceCursor | None,
    ) -> DiscoveryBatch:
        """Materialize stable source identities for validated playlist entries."""
        discovered_at = utc_now()
        unique_items = tuple({item.snippet.resource_id.video_id: item for item in page_items}.values())
        items: tuple[SourceItem, ...] = tuple(
            SourceItem(
                source_item_id=(f"{self._definition.source_id}:{item.snippet.resource_id.video_id}"),
                source_id=self._definition.source_id,
                source_definition_hash=source_definition_hash(self._definition),
                canonical_uri=f"https://www.youtube.com/watch?v={item.snippet.resource_id.video_id}",
                published_at=item.snippet.published_at,
                updated_at=item.snippet.published_at,
                discovered_at=discovered_at,
                content_version=item.snippet.resource_id.video_id,
            )
            for item in unique_items
        )
        return DiscoveryBatch(items=items, next_cursor=next_cursor, discovered_at=discovered_at)

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Acquire the actual public video for transcript and frame processing."""
        if item.source_id != self._definition.source_id or item.source_definition_hash != source_definition_hash(
            self._definition
        ):
            raise SourceFetchError("Cannot fetch a YouTube item from another source")
        acquisition = self._media_transport.fetch(
            str(item.canonical_uri),
            maximum_bytes=self._config.max_media_bytes,
            timeout_seconds=self._config.timeout_seconds,
        )
        expected_video_id = item.source_item_id.removeprefix(f"{self._definition.source_id}:")
        expected_playlist_id = f"UU{acquisition.channel_id[2:]}"
        if acquisition.video_id != expected_video_id or expected_playlist_id != self._definition.locator:
            raise YouTubeSourceMembershipError(
                "YouTube video does not belong to the configured uploads playlist",
            )
        digest = hashlib.sha256(acquisition.content).hexdigest()
        published_at = acquisition.published_at or item.published_at
        updated_at = acquisition.published_at or item.updated_at or published_at
        return RawArtifact(
            source_item=item.model_copy(
                update={
                    "content_version": digest,
                    "published_at": published_at,
                    "updated_at": updated_at,
                },
            ),
            content=acquisition.content,
            content_hash=digest,
            media_type=acquisition.media_type,
            filename=acquisition.filename,
            retrieved_at=utc_now(),
            canonical_uri=str(item.canonical_uri),
        )

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract timestamped media evidence when used outside a processor registry."""
        from money_pit.evidence.media import MediaEvidenceProcessor

        return MediaEvidenceProcessor(self._analyzer).process(artifact)
