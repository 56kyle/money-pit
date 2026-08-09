"""Module containing built-in bounded read-only research providers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Protocol
from typing import cast
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from typing_extensions import override

from money_pit.research.errors import HistoricalResearchUnavailableError
from money_pit.research.errors import ResearchFetchError
from money_pit.research.errors import ResearchProviderMismatchError
from money_pit.research.errors import ResearchSearchError
from money_pit.research.protocol import ResearchProviderCapabilities
from money_pit.schemas.research import ResearchDiscoveryBatch
from money_pit.schemas.research import ResearchDiscoveryResult
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import sha256_bytes
from money_pit.sources._shared import source_definition_hash
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import SourceError


if TYPE_CHECKING:
    from collections.abc import Callable

    from money_pit.schemas.research import ResearchQuery
    from money_pit.schemas.sources import SourceDefinition
    from money_pit.sources.http import FixedOriginHttpTransport
    from money_pit.sources.http import HttpTransport


_DEFAULT_MAXIMUM_BYTES = 25 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 20.0
_FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_FRED_SERIES_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_EDGAR_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_EDGAR_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class _BraveResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    url: str = Field(min_length=1)
    title: str | None = None
    description: str | None = None


class _BraveWeb(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    results: tuple[_BraveResult, ...] = ()


class _BraveResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    web: _BraveWeb = _BraveWeb()


class _EdgarSource(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    file_url: str | None = None
    file_date: str | None = None
    display_names: tuple[str, ...] = ()
    form: str | None = None
    ciks: tuple[str, ...] = ()


class _EdgarHit(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    hit_id: str | None = Field(default=None, alias="_id")
    source: _EdgarSource = Field(alias="_source")


class _EdgarHits(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    hits: tuple[_EdgarHit, ...] = ()


class _EdgarResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    hits: _EdgarHits = _EdgarHits()


@dataclass(frozen=True)
class SearchHit:
    """Validated provider-neutral metadata from a search backend."""

    canonical_uri: str
    title: str | None = None
    snippet: str | None = None
    provenance_group: str | None = None
    fetch_token: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    available_at: datetime | None = None


class SearchBackend(Protocol):
    """Read-only search seam used by web and filing providers."""

    def search(self, query_text: str, *, maximum_results: int) -> tuple[SearchHit, ...]:
        """Return at most the requested number of discovery hits."""
        ...


class PublisherPolicyResolver(Protocol):
    """Resolve configured destination-publisher policy without inferring trust."""

    def resolve(self, canonical_uri: str) -> SourceDefinition:
        """Return the exact configured policy for a fetched destination."""
        ...


class ConfiguredPublisherPolicyResolver:
    """Resolve immutable publisher definitions by exact normalized hostname."""

    def __init__(self, definitions: tuple[SourceDefinition, ...]) -> None:
        """Index explicitly configured publisher policies and reject ambiguity."""
        by_hostname: dict[str, SourceDefinition] = {}
        for definition in definitions:
            hostname: str | None = urlsplit(definition.locator).hostname
            if hostname is None:
                raise ValueError("Publisher policy locator must identify an HTTP origin")
            normalized: str = hostname.casefold()
            existing: SourceDefinition | None = by_hostname.get(normalized)
            if existing is not None and existing != definition:
                raise ValueError("Publisher hostname has multiple source policies")
            by_hostname[normalized] = definition
        if not by_hostname:
            raise ValueError("At least one publisher source policy is required")
        self._by_hostname: dict[str, SourceDefinition] = by_hostname

    def resolve(self, canonical_uri: str) -> SourceDefinition:
        """Resolve an exact destination host; unknown publishers fail closed."""
        hostname: str | None = urlsplit(canonical_uri).hostname
        definition: SourceDefinition | None = None if hostname is None else self._by_hostname.get(hostname.casefold())
        if definition is None:
            raise ResearchFetchError("Research result publisher has no configured source policy")
        return definition


class BraveSearchBackend:
    """Bounded Brave web search using a fixed-origin credentialed request."""

    def __init__(
        self,
        api_key: str,
        transport: FixedOriginHttpTransport,
        *,
        maximum_bytes: int = 2 * 1024 * 1024,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Bind a secret and explicit response bounds without logging the secret."""
        if not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise ValueError("Brave API key must not be empty")
        _require_positive_provider_bounds(maximum_bytes, timeout_seconds)
        self._api_key: str = api_key
        self._transport: FixedOriginHttpTransport = transport
        self._maximum_bytes: int = maximum_bytes
        self._timeout_seconds: float = timeout_seconds

    def search(self, query_text: str, *, maximum_results: int) -> tuple[SearchHit, ...]:
        """Return Brave discovery metadata; snippets remain non-evidence."""
        _require_search_request(query_text, maximum_results=maximum_results, upper_bound=20)
        query = urlencode({"q": query_text, "count": maximum_results})
        try:
            response = self._transport.get_fixed_origin(
                f"{_BRAVE_SEARCH_URL}?{query}",
                headers=(("Accept", "application/json"), ("X-Subscription-Token", self._api_key)),
                maximum_bytes=self._maximum_bytes,
                timeout_seconds=self._timeout_seconds,
            )
            parsed = _BraveResponse.model_validate_json(response.content)
        except (SourceError, ValidationError):
            raise ResearchSearchError("Brave search failed") from None
        return tuple(
            SearchHit(canonical_uri=item.url, title=item.title, snippet=item.description)
            for item in parsed.web.results[:maximum_results]
        )


class EdgarSearchBackend:
    """Bounded SEC EDGAR full-text search with the required caller identity."""

    def __init__(
        self,
        user_agent: str,
        transport: FixedOriginHttpTransport,
        *,
        maximum_bytes: int = 4 * 1024 * 1024,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Bind the SEC-required product/contact identity and response bounds."""
        _require_sec_user_agent(user_agent)
        _require_positive_provider_bounds(maximum_bytes, timeout_seconds)
        self._user_agent: str = user_agent
        self._transport: FixedOriginHttpTransport = transport
        self._maximum_bytes: int = maximum_bytes
        self._timeout_seconds: float = timeout_seconds

    def search(self, query_text: str, *, maximum_results: int) -> tuple[SearchHit, ...]:
        """Return filing metadata from SEC's fixed-origin full-text index."""
        _require_search_request(query_text, maximum_results=maximum_results, upper_bound=100)
        query = urlencode({"q": query_text, "from": 0, "size": maximum_results})
        try:
            response = self._transport.get_fixed_origin(
                f"{_EDGAR_SEARCH_URL}?{query}",
                headers=(("Accept", "application/json"), ("User-Agent", self._user_agent)),
                maximum_bytes=self._maximum_bytes,
                timeout_seconds=self._timeout_seconds,
            )
            parsed = _EdgarResponse.model_validate_json(response.content)
        except (SourceError, ValidationError):
            raise ResearchSearchError("EDGAR search failed") from None
        return tuple(_edgar_search_hit(item) for item in parsed.hits.hits[:maximum_results])


class ReadOnlySnapshotReader(Protocol):
    """Read-only seam for market or portfolio point-in-time snapshots."""

    def read(self, query_text: str) -> bytes:
        """Return a complete JSON snapshot for one validated query."""
        ...


class WebResearchProvider:
    """Search a backend and fetch selected public web results."""

    def __init__(
        self,
        name: str,
        source_definition: SourceDefinition,
        backend: SearchBackend,
        transport: HttpTransport,
        *,
        maximum_bytes: int = _DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        capabilities: ResearchProviderCapabilities | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind configured source policy, search, and SSRF-safe fetch seams."""
        if maximum_bytes <= 0 or timeout_seconds <= 0:
            raise ValueError("Research fetch bounds must be positive")
        self._name: str = name
        self._source_definition: SourceDefinition = source_definition
        self._backend: SearchBackend = backend
        self._transport: HttpTransport = transport
        self._maximum_bytes: int = maximum_bytes
        self._timeout_seconds: float = timeout_seconds
        self._capabilities: ResearchProviderCapabilities = capabilities or ResearchProviderCapabilities()
        self._clock: Callable[[], datetime] = clock

    @property
    def name(self) -> str:
        """Return the provider name."""
        return self._name

    @property
    def source_definition(self) -> SourceDefinition:
        """Return the configured provenance policy."""
        return self._source_definition

    def source_definition_for(self, result: ResearchDiscoveryResult) -> SourceDefinition:
        """Return the provider's fixed source policy for one result."""
        _require_result_provider(result.provider, self.name)
        return self._source_definition

    @property
    def capabilities(self) -> ResearchProviderCapabilities:
        """Return configured historical-retrieval guarantees."""
        return self._capabilities

    def search(self, query: ResearchQuery) -> ResearchDiscoveryBatch:
        """Return deduplicated discovery-only results."""
        _require_query_provider(query.provider, self.name)
        try:
            hits: tuple[SearchHit, ...] = self._backend.search(
                query.query_text,
                maximum_results=query.max_results,
            )
        except (OSError, ValueError) as error:
            raise ResearchSearchError(f"{self.name} search failed") from error
        searched_at = self._clock()
        results: list[ResearchDiscoveryResult] = []
        seen_uris: set[str] = set()
        for hit in hits:
            if hit.canonical_uri in seen_uris:
                continue
            seen_uris.add(hit.canonical_uri)
            results.append(
                _discovery_result(
                    self.name,
                    hit,
                    query_key=query.model_dump_json(),
                    discovered_at=searched_at,
                    capabilities=self.capabilities,
                ),
            )
            if len(results) == query.max_results:
                break
        return ResearchDiscoveryBatch(query=query, results=tuple(results), searched_at=searched_at)

    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        """Fetch a selected result; its snippet is deliberately not included."""
        _require_result_provider(result.provider, self.name)
        try:
            response = self._transport.get(
                result.canonical_uri,
                maximum_bytes=self._maximum_bytes,
                timeout_seconds=self._timeout_seconds,
            )
        except SourceError as error:
            raise ResearchFetchError(f"{self.name} result fetch failed") from error
        return _raw_artifact(
            self.source_definition_for(result),
            result,
            content=response.content,
            media_type=response.media_type,
            retrieved_at=self._clock(),
        )

    def search_as_of(self, query: ResearchQuery, *, requested_as_of: datetime) -> ResearchDiscoveryBatch:
        """Reject historical access because the web backend is current-only."""
        del query, requested_as_of
        raise HistoricalResearchUnavailableError(f"{self.name} has no certified historical index")

    def fetch_as_of(self, result: ResearchDiscoveryResult, *, requested_as_of: datetime) -> RawArtifact:
        """Reject historical fetch because current URLs do not pin a version."""
        del result, requested_as_of
        raise HistoricalResearchUnavailableError(f"{self.name} has no certified version fetch")


class BraveResearchProvider(WebResearchProvider):
    """General web research provider backed by an injected Brave search client."""

    def __init__(
        self,
        source_definition: SourceDefinition,
        backend: SearchBackend,
        transport: HttpTransport,
        *,
        publisher_policies: tuple[SourceDefinition, ...],
        maximum_bytes: int = _DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        capabilities: ResearchProviderCapabilities | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the stable Brave provider identity."""
        super().__init__(
            "brave",
            source_definition,
            backend,
            transport,
            maximum_bytes=maximum_bytes,
            timeout_seconds=timeout_seconds,
            capabilities=capabilities,
            clock=clock,
        )
        self._publisher_policies: PublisherPolicyResolver = ConfiguredPublisherPolicyResolver(
            publisher_policies,
        )

    @override
    def source_definition_for(self, result: ResearchDiscoveryResult) -> SourceDefinition:
        """Resolve destination publisher and upstream group from configured policy."""
        _require_result_provider(result.provider, self.name)
        return self._publisher_policies.resolve(result.canonical_uri)


class EdgarResearchProvider(WebResearchProvider):
    """SEC filing research provider backed by a bounded EDGAR search seam."""

    def __init__(
        self,
        source_definition: SourceDefinition,
        backend: SearchBackend,
        transport: FixedOriginHttpTransport,
        *,
        user_agent: str,
        maximum_bytes: int = _DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        capabilities: ResearchProviderCapabilities | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the stable EDGAR provider identity."""
        _require_sec_user_agent(user_agent)
        super().__init__(
            "edgar",
            source_definition,
            backend,
            transport,
            maximum_bytes=maximum_bytes,
            timeout_seconds=timeout_seconds,
            capabilities=capabilities,
            clock=clock,
        )
        self._edgar_transport: FixedOriginHttpTransport = transport
        self._user_agent: str = user_agent

    @override
    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        """Fetch one SEC filing with the required fixed-origin caller identity."""
        _require_result_provider(result.provider, self.name)
        _require_sec_https_url(result.canonical_uri)
        try:
            response = self._edgar_transport.get_fixed_origin(
                result.canonical_uri,
                headers=(("User-Agent", self._user_agent),),
                maximum_bytes=self._maximum_bytes,
                timeout_seconds=self._timeout_seconds,
            )
        except SourceError:
            raise ResearchFetchError("EDGAR filing fetch failed") from None
        return _raw_artifact(
            self.source_definition,
            result,
            content=response.content,
            media_type=response.media_type,
            retrieved_at=self._clock(),
        )


class FredResearchProvider:
    """Fetch FRED series observations as durable JSON evidence."""

    name: str = "fred"

    def __init__(
        self,
        source_definition: SourceDefinition,
        transport: HttpTransport,
        *,
        api_key: str,
        maximum_bytes: int = _DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind configured FRED policy, credentials, and fetch bounds."""
        if not api_key:
            raise ValueError("FRED API key must not be empty")
        self._source_definition: SourceDefinition = source_definition
        self._transport: HttpTransport = transport
        self._api_key: str = api_key
        self._maximum_bytes: int = maximum_bytes
        self._timeout_seconds: float = timeout_seconds
        self._clock: Callable[[], datetime] = clock

    @property
    def source_definition(self) -> SourceDefinition:
        """Return the configured FRED source policy."""
        return self._source_definition

    def source_definition_for(self, result: ResearchDiscoveryResult) -> SourceDefinition:
        """Return configured FRED policy for its exact result."""
        _require_result_provider(result.provider, self.name)
        return self._source_definition

    @property
    def capabilities(self) -> ResearchProviderCapabilities:
        """Current FRED calls are not certified for historical replay."""
        return ResearchProviderCapabilities()

    def search(self, query: ResearchQuery) -> ResearchDiscoveryBatch:
        """Interpret a validated query as an exact FRED series identifier."""
        _require_query_provider(query.provider, self.name)
        series_id: str = query.query_text.strip().upper()
        if _FRED_SERIES_ID.fullmatch(series_id) is None:
            raise ResearchSearchError("FRED research requires an exact series identifier")
        searched_at = self._clock()
        hit = SearchHit(
            canonical_uri=f"https://fred.stlouisfed.org/series/{quote(series_id, safe='')}",
            title=f"FRED series {series_id}",
            provenance_group="fred",
            fetch_token=series_id,
        )
        return ResearchDiscoveryBatch(
            query=query,
            results=(
                _discovery_result(
                    self.name,
                    hit,
                    query_key=query.model_dump_json(),
                    discovered_at=searched_at,
                    capabilities=self.capabilities,
                ),
            ),
            searched_at=searched_at,
        )

    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        """Fetch observations while keeping the credential out of durable URIs."""
        _require_result_provider(result.provider, self.name)
        series_id: str | None = result.fetch_token
        if series_id is None or _FRED_SERIES_ID.fullmatch(series_id) is None:
            raise ResearchFetchError("FRED result has no valid series token")
        query: str = urlencode(
            {
                "series_id": series_id,
                "api_key": self._api_key,
                "file_type": "json",
                "sort_order": "desc",
            },
        )
        try:
            response = self._transport.get(
                f"{_FRED_OBSERVATIONS_URL}?{query}",
                maximum_bytes=self._maximum_bytes,
                timeout_seconds=self._timeout_seconds,
            )
        except SourceError as error:
            raise ResearchFetchError("FRED series fetch failed") from error
        return _raw_artifact(
            self.source_definition,
            result,
            content=response.content,
            media_type=response.media_type,
            retrieved_at=self._clock(),
        )

    def search_as_of(self, query: ResearchQuery, *, requested_as_of: datetime) -> ResearchDiscoveryBatch:
        """Reject until FRED vintage parameters are implemented and verified."""
        del query, requested_as_of
        raise HistoricalResearchUnavailableError("FRED vintage research is not configured")

    def fetch_as_of(self, result: ResearchDiscoveryResult, *, requested_as_of: datetime) -> RawArtifact:
        """Reject current observations for explicit historical research."""
        del result, requested_as_of
        raise HistoricalResearchUnavailableError("FRED vintage research is not configured")


class ReadOnlySnapshotResearchProvider:
    """Turn injected market or portfolio reads into fetch-required evidence."""

    def __init__(
        self,
        name: str,
        source_definition: SourceDefinition,
        reader: ReadOnlySnapshotReader,
        *,
        maximum_bytes: int = _DEFAULT_MAXIMUM_BYTES,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind a read-only snapshot seam and explicit byte bound."""
        if name not in {"market", "portfolio"}:
            raise ValueError("Snapshot provider name must be 'market' or 'portfolio'")
        self._name: str = name
        self._source_definition: SourceDefinition = source_definition
        self._reader: ReadOnlySnapshotReader = reader
        self._maximum_bytes: int = maximum_bytes
        self._clock: Callable[[], datetime] = clock

    @property
    def name(self) -> str:
        """Return the provider name."""
        return self._name

    @property
    def source_definition(self) -> SourceDefinition:
        """Return the configured snapshot source policy."""
        return self._source_definition

    def source_definition_for(self, result: ResearchDiscoveryResult) -> SourceDefinition:
        """Return configured snapshot policy for its exact result."""
        _require_result_provider(result.provider, self.name)
        return self._source_definition

    @property
    def capabilities(self) -> ResearchProviderCapabilities:
        """Current snapshot readers are not certified historical stores."""
        return ResearchProviderCapabilities()

    def search(self, query: ResearchQuery) -> ResearchDiscoveryBatch:
        """Create one discovery record without reading the underlying snapshot."""
        _require_query_provider(query.provider, self.name)
        searched_at = self._clock()
        token: str = sha256_bytes(query.query_text.encode("utf-8"))
        hit = SearchHit(
            canonical_uri=f"money-pit-{self.name}://snapshot/{token}",
            title=f"{self.name.title()} snapshot",
            provenance_group=self.source_definition.provenance_group,
            fetch_token=query.query_text,
        )
        return ResearchDiscoveryBatch(
            query=query,
            results=(
                _discovery_result(
                    self.name,
                    hit,
                    query_key=query.model_dump_json(),
                    discovered_at=searched_at,
                    capabilities=self.capabilities,
                ),
            ),
            searched_at=searched_at,
        )

    def fetch(self, result: ResearchDiscoveryResult) -> RawArtifact:
        """Read and bound the selected snapshot only during fetch."""
        _require_result_provider(result.provider, self.name)
        if result.fetch_token is None:
            raise ResearchFetchError(f"{self.name} result has no snapshot query token")
        try:
            content: bytes = self._reader.read(result.fetch_token)
            _: object = cast("object", json.loads(content))
        except (OSError, ValueError) as error:
            raise ResearchFetchError(f"{self.name} snapshot read failed") from error
        if len(content) > self._maximum_bytes:
            raise ResearchFetchError(f"{self.name} snapshot exceeds its byte limit")
        return _raw_artifact(
            self.source_definition,
            result,
            content=content,
            media_type="application/json",
            retrieved_at=self._clock(),
        )

    def search_as_of(self, query: ResearchQuery, *, requested_as_of: datetime) -> ResearchDiscoveryBatch:
        """Reject current snapshot readers for explicit historical research."""
        del query, requested_as_of
        raise HistoricalResearchUnavailableError(f"{self.name} has no certified historical snapshots")

    def fetch_as_of(self, result: ResearchDiscoveryResult, *, requested_as_of: datetime) -> RawArtifact:
        """Reject current snapshot reads for explicit historical research."""
        del result, requested_as_of
        raise HistoricalResearchUnavailableError(f"{self.name} has no certified historical snapshots")


def _discovery_result(
    provider: str,
    hit: SearchHit,
    *,
    query_key: str,
    discovered_at: datetime,
    capabilities: ResearchProviderCapabilities | None = None,
) -> ResearchDiscoveryResult:
    identity: str = sha256_bytes(
        f"{provider}\0{query_key}\0{hit.canonical_uri}".encode("utf-8"),
    )
    return ResearchDiscoveryResult(
        result_id=f"{provider}:{identity}",
        provider=provider,
        canonical_uri=hit.canonical_uri,
        title=hit.title,
        snippet=hit.snippet,
        discovered_at=discovered_at,
        provenance_group=hit.provenance_group,
        fetch_token=hit.fetch_token,
        published_at=hit.published_at,
        updated_at=hit.updated_at,
        available_at=hit.available_at,
        metadata={
            "provider_capabilities": (capabilities or ResearchProviderCapabilities()).model_dump(mode="json"),
        },
    )


def _edgar_search_hit(hit: _EdgarHit) -> SearchHit:
    source: _EdgarSource = hit.source
    canonical_uri: str = _edgar_filing_url(hit)
    if canonical_uri.startswith("/"):
        canonical_uri = f"https://www.sec.gov{canonical_uri}"
    if not _is_sec_https_url(canonical_uri):
        raise ResearchSearchError("EDGAR search returned a non-SEC filing URL")
    published_at: datetime | None = None
    if source.file_date is not None:
        try:
            published_at = datetime.fromisoformat(source.file_date).replace(tzinfo=UTC)
        except ValueError as error:
            raise ResearchSearchError("EDGAR search returned an invalid filing date") from error
    issuer = ", ".join(source.display_names) or "SEC filing"
    title = f"{source.form}: {issuer}" if source.form else issuer
    return SearchHit(
        canonical_uri=canonical_uri,
        title=title,
        provenance_group="sec-edgar",
        published_at=published_at,
        available_at=published_at,
    )


def _edgar_filing_url(hit: _EdgarHit) -> str:
    source: _EdgarSource = hit.source
    if source.file_url:
        return source.file_url
    if hit.hit_id is None:
        raise ResearchSearchError("EDGAR search result has no filing URL identity")
    accession, separator, filename = hit.hit_id.partition(":")
    cik: str = accession[:10].lstrip("0") or "0"
    if (
        not separator
        or _EDGAR_ACCESSION.fullmatch(accession) is None
        or _EDGAR_FILENAME.fullmatch(filename) is None
        or not cik.isdigit()
        or filename in {".", ".."}
    ):
        raise ResearchSearchError("EDGAR search result has an invalid filing identity")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{filename}"


def _require_sec_https_url(url: str) -> None:
    if not _is_sec_https_url(url):
        raise ResearchFetchError("EDGAR filing URL must use an SEC HTTPS origin")


def _is_sec_https_url(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port: int | None = parsed.port
    except ValueError:
        return False
    return not (
        parsed.scheme != "https"
        or parsed.hostname not in {"sec.gov", "www.sec.gov"}
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    )


def _require_positive_provider_bounds(maximum_bytes: int, timeout_seconds: float) -> None:
    if maximum_bytes <= 0 or timeout_seconds <= 0:
        raise ValueError("Research provider bounds must be positive")


def _require_sec_user_agent(user_agent: str) -> None:
    if (
        not user_agent.strip()
        or "@" not in user_agent
        or "\r" in user_agent
        or "\n" in user_agent
        or len(user_agent) > 256
    ):
        raise ValueError("SEC User-Agent must include a valid contact email address")


def _require_search_request(
    query_text: str,
    *,
    maximum_results: int,
    upper_bound: int,
) -> None:
    if not query_text.strip():
        raise ResearchSearchError("Research query must not be empty")
    if maximum_results < 1 or maximum_results > upper_bound:
        raise ResearchSearchError("Research result limit is outside provider bounds")


def _raw_artifact(
    definition: SourceDefinition,
    result: ResearchDiscoveryResult,
    *,
    content: bytes,
    media_type: str,
    retrieved_at: datetime,
) -> RawArtifact:
    digest: str = sha256_bytes(content)
    item = SourceItem(
        source_item_id=f"{definition.source_id}:{result.result_id}:{digest}",
        source_id=definition.source_id,
        source_definition_hash=source_definition_hash(definition),
        canonical_uri=result.canonical_uri,
        published_at=result.published_at,
        updated_at=result.updated_at,
        discovered_at=result.discovered_at,
        content_version=digest,
    )
    return RawArtifact(
        source_item=item,
        content=content,
        media_type=media_type,
        retrieved_at=retrieved_at,
        canonical_uri=result.canonical_uri,
        content_hash=digest,
    )


def _require_query_provider(actual: str, expected: str) -> None:
    if actual != expected:
        raise ResearchProviderMismatchError(
            f"Query provider {actual!r} does not match {expected!r}",
        )


def _require_result_provider(actual: str, expected: str) -> None:
    if actual != expected:
        raise ResearchProviderMismatchError(
            f"Result provider {actual!r} does not match {expected!r}",
        )
