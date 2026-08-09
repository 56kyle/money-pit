from datetime import UTC
from datetime import datetime
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest

from money_pit.research.errors import ResearchFetchError
from money_pit.research.errors import ResearchProviderMismatchError
from money_pit.research.errors import ResearchSearchError
from money_pit.research.providers import BraveResearchProvider
from money_pit.research.providers import BraveSearchBackend
from money_pit.research.providers import EdgarResearchProvider
from money_pit.research.providers import EdgarSearchBackend
from money_pit.research.providers import ReadOnlySnapshotResearchProvider
from money_pit.research.providers import SearchHit
from money_pit.research.providers import WebResearchProvider
from money_pit.schemas.research import ResearchDiscoveryResult
from money_pit.schemas.research import ResearchQuery
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources._shared import sha256_bytes
from money_pit.sources._shared import source_definition_hash
from money_pit.sources.errors import SourceFetchError
from money_pit.sources.http import HttpResponse


NOW = datetime(2026, 8, 9, tzinfo=UTC)


class _Backend:
    def __init__(self, hits: tuple[SearchHit, ...]) -> None:
        self.hits: tuple[SearchHit, ...] = hits
        self.calls: list[tuple[str, int]] = []

    def search(self, query_text: str, *, maximum_results: int) -> tuple[SearchHit, ...]:
        self.calls.append((query_text, maximum_results))
        return self.hits


class _Transport:
    def __init__(self, response: HttpResponse) -> None:
        self.response: HttpResponse = response
        self.calls: list[tuple[str, int, float]] = []

    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        self.calls.append((url, maximum_bytes, timeout_seconds))
        return self.response


class _FixedOriginTransport(_Transport):
    def __init__(self, response: HttpResponse | BaseException) -> None:
        super().__init__(
            response if isinstance(response, HttpResponse) else HttpResponse(b"", "text/plain", "https://unused.test"),
        )
        self.fixed_response: HttpResponse | BaseException = response
        self.fixed_calls: list[tuple[str, tuple[tuple[str, str], ...], int, float]] = []

    def get_fixed_origin(
        self,
        url: str,
        *,
        headers: tuple[tuple[str, str], ...],
        maximum_bytes: int,
        timeout_seconds: float,
    ) -> HttpResponse:
        self.fixed_calls.append((url, headers, maximum_bytes, timeout_seconds))
        if isinstance(self.fixed_response, BaseException):
            raise self.fixed_response
        return self.fixed_response


class _SnapshotReader:
    def __init__(self, content: bytes) -> None:
        self.content: bytes = content
        self.queries: list[str] = []

    def read(self, query_text: str) -> bytes:
        self.queries.append(query_text)
        return self.content


def _definition(source_id: str = "research.web") -> SourceDefinition:
    return SourceDefinition(
        source_id=source_id,
        adapter_name="research",
        locator="https://example.test",
        provenance_group="configured-publisher",
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.INDEPENDENT_SECONDARY),),
    )


def _query(provider: str = "web") -> ResearchQuery:
    return ResearchQuery(
        provider=provider,
        query_text="material fact",
        purpose="verify",
        requested_at=NOW,
        max_results=2,
    )


def test_web_research_provider_search_is_discovery_only_and_deduplicates_uris() -> None:
    backend = _Backend(
        (
            SearchHit(
                canonical_uri="https://publisher.test/report",
                title="Report",
                snippet="discovery text",
                provenance_group="publisher",
            ),
            SearchHit(canonical_uri="https://publisher.test/report", title="Duplicate"),
        ),
    )
    transport = _Transport(HttpResponse(b"unused", "text/plain", "https://unused.test"))
    provider = WebResearchProvider("web", _definition(), backend, transport)

    batch = provider.search(_query())

    assert tuple((result.canonical_uri, result.snippet, result.provenance_group) for result in batch.results) == (
        ("https://publisher.test/report", "discovery text", "publisher"),
    )
    assert transport.calls == []


def test_web_research_provider_fetch_creates_content_addressed_source_provenance() -> None:
    content = b"full fetched source"
    definition = _definition()
    transport = _Transport(HttpResponse(content, "text/plain", "https://publisher.test/final"))
    provider = WebResearchProvider("web", definition, _Backend(()), transport, maximum_bytes=100, timeout_seconds=3)
    result = ResearchDiscoveryResult(
        result_id="web:result",
        provider="web",
        canonical_uri="https://publisher.test/report",
        snippet="must not become content",
        discovered_at=NOW,
        provenance_group="publisher",
    )

    artifact = provider.fetch(result)

    digest = sha256_bytes(content)
    assert (
        artifact.content,
        artifact.content_hash,
        artifact.source_item.source_id,
        artifact.source_item.source_definition_hash,
        artifact.source_item.content_version,
    ) == (content, digest, definition.source_id, source_definition_hash(definition), digest)
    assert transport.calls == [(result.canonical_uri, 100, 3)]


def test_web_research_provider_uses_the_injected_application_clock_for_actual_times() -> None:
    hit = SearchHit(canonical_uri="https://publisher.test/report", title="Report")
    provider = WebResearchProvider(
        "web",
        _definition(),
        _Backend((hit,)),
        _Transport(HttpResponse(b"content", "text/plain", hit.canonical_uri)),
        clock=lambda: NOW,
    )

    batch = provider.search(_query())
    artifact = provider.fetch(batch.results[0])

    assert batch.searched_at == NOW
    assert batch.results[0].discovered_at == NOW
    assert artifact.retrieved_at == NOW


def test_web_research_provider_rejects_cross_provider_fetch() -> None:
    provider = WebResearchProvider(
        "web",
        _definition(),
        _Backend(()),
        _Transport(HttpResponse(b"content", "text/plain", "https://example.test")),
    )
    result = ResearchDiscoveryResult(
        result_id="other:result",
        provider="other",
        canonical_uri="https://example.test/result",
        discovered_at=NOW,
    )

    with pytest.raises(ResearchProviderMismatchError):
        _ = provider.fetch(result)


def test_snapshot_provider_does_not_read_material_until_fetch() -> None:
    reader = _SnapshotReader(b'{"price": 42}')
    provider = ReadOnlySnapshotResearchProvider("market", _definition("research.market"), reader)

    result = provider.search(_query("market")).results[0]
    artifact = provider.fetch(result)

    assert reader.queries == ["material fact"]
    assert artifact.content == b'{"price": 42}'


def test_brave_search_backend_sends_credentials_only_to_exact_endpoint() -> None:
    transport = _FixedOriginTransport(
        HttpResponse(b'{"web":{"results":[]}}', "application/json", "https://api.search.brave.com"),
    )

    _ = BraveSearchBackend("brave-secret", transport).search("material fact", maximum_results=2)

    url, headers, _, _ = transport.fixed_calls[0]
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "api.search.brave.com",
        "/res/v1/web/search",
    )
    assert headers == (("Accept", "application/json"), ("X-Subscription-Token", "brave-secret"))
    assert parse_qs(parts.query) == {"q": ["material fact"], "count": ["2"]}


def test_brave_search_backend_parses_only_requested_number_of_hits() -> None:
    transport = _FixedOriginTransport(
        HttpResponse(
            b"".join(
                (
                    b'{"web":{"results":[',
                    b'{"url":"https://one.test","title":"One","description":"First"},',
                    b'{"url":"https://two.test","title":"Two","description":"Second"},',
                    b'{"url":"https://three.test","title":"Three","description":"Third"}',
                    b"]}}",
                ),
            ),
            "application/json",
            "https://api.search.brave.com",
        ),
    )

    hits = BraveSearchBackend("brave-secret", transport).search("material fact", maximum_results=2)

    assert tuple((hit.canonical_uri, hit.title, hit.snippet) for hit in hits) == (
        ("https://one.test", "One", "First"),
        ("https://two.test", "Two", "Second"),
    )


def test_brave_search_backend_does_not_expose_key_in_failure() -> None:
    api_key = "brave-secret-value"
    backend = BraveSearchBackend(api_key, _FixedOriginTransport(SourceFetchError(f"failure: {api_key}")))

    with pytest.raises(ResearchSearchError) as raised:
        _ = backend.search("material fact", maximum_results=2)

    assert api_key not in str(raised.value)


def test_brave_provider_resolves_configured_publishers_and_syndication_groups() -> None:
    gateway = _definition("research.brave")
    primary = _definition("publisher.primary").model_copy(
        update={
            "locator": "https://issuer.example/",
            "provenance_group": "issuer-upstream",
        },
    )
    syndicated = _definition("publisher.syndicated").model_copy(
        update={
            "locator": "https://wire-copy.example/",
            "provenance_group": "issuer-upstream",
        },
    )
    independent = _definition("publisher.independent").model_copy(
        update={
            "locator": "https://independent.example/",
            "provenance_group": "independent-publisher",
        },
    )
    provider = BraveResearchProvider(
        gateway,
        _Backend(()),
        _Transport(HttpResponse(b"content", "text/plain", "https://unused.test")),
        publisher_policies=(primary, syndicated, independent),
    )

    resolved = tuple(
        provider.source_definition_for(
            ResearchDiscoveryResult(
                result_id=f"brave:{index}",
                provider="brave",
                canonical_uri=uri,
                discovered_at=NOW,
            ),
        )
        for index, uri in enumerate(
            (
                "https://issuer.example/release",
                "https://wire-copy.example/release",
                "https://independent.example/report",
            ),
        )
    )

    assert tuple(item.source_id for item in resolved) == (
        primary.source_id,
        syndicated.source_id,
        independent.source_id,
    )
    assert tuple(item.provenance_group for item in resolved) == (
        "issuer-upstream",
        "issuer-upstream",
        "independent-publisher",
    )


def test_brave_provider_rejects_an_unconfigured_destination_publisher() -> None:
    provider = BraveResearchProvider(
        _definition("research.brave"),
        _Backend(()),
        _Transport(HttpResponse(b"content", "text/plain", "https://unused.test")),
        publisher_policies=(_definition("publisher.configured"),),
    )
    result = ResearchDiscoveryResult(
        result_id="brave:unknown",
        provider="brave",
        canonical_uri="https://unknown.example/report",
        discovered_at=NOW,
    )

    with pytest.raises(ResearchFetchError):
        _ = provider.fetch(result)


@pytest.mark.parametrize(("query_text", "maximum_results"), [("", 1), ("   ", 1), ("fact", 0), ("fact", 21)])
def test_brave_search_backend_rejects_invalid_query_bounds_before_io(
    query_text: str,
    maximum_results: int,
) -> None:
    transport = _FixedOriginTransport(HttpResponse(b"{}", "application/json", "https://api.search.brave.com"))

    with pytest.raises(ResearchSearchError):
        _ = BraveSearchBackend("brave-api-key", transport).search(
            query_text,
            maximum_results=maximum_results,
        )

    assert transport.fixed_calls == []


@pytest.mark.parametrize("user_agent", ["", "money-pit", "   "])
def test_edgar_search_backend_requires_contact_user_agent(user_agent: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        _ = EdgarSearchBackend(
            user_agent,
            _FixedOriginTransport(HttpResponse(b"{}", "application/json", "https://efts.sec.gov")),
        )


def test_edgar_search_backend_sends_contact_identity_to_exact_endpoint() -> None:
    transport = _FixedOriginTransport(
        HttpResponse(b'{"hits":{"hits":[]}}', "application/json", "https://efts.sec.gov"),
    )

    _ = EdgarSearchBackend("money-pit contact@example.com", transport).search(
        "10-K revenue",
        maximum_results=3,
    )

    url, headers, _, _ = transport.fixed_calls[0]
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "efts.sec.gov",
        "/LATEST/search-index",
    )
    assert headers == (("Accept", "application/json"), ("User-Agent", "money-pit contact@example.com"))
    assert parse_qs(parts.query) == {"q": ["10-K revenue"], "from": ["0"], "size": ["3"]}


def test_edgar_search_backend_parses_filing_title_and_date() -> None:
    transport = _FixedOriginTransport(
        HttpResponse(
            b"".join(
                (
                    b'{"hits":{"hits":[{"_source":{',
                    b'"file_url":"/Archives/edgar/data/1/filing.htm",',
                    b'"file_date":"2026-01-02","display_names":["Acme Corp"],"form":"10-K"}}]}}',
                ),
            ),
            "application/json",
            "https://efts.sec.gov",
        ),
    )

    hit = EdgarSearchBackend("money-pit contact@example.com", transport).search(
        "Acme",
        maximum_results=1,
    )[0]

    assert (hit.canonical_uri, hit.title, hit.published_at, hit.available_at) == (
        "https://www.sec.gov/Archives/edgar/data/1/filing.htm",
        "10-K: Acme Corp",
        datetime(2026, 1, 2, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_edgar_search_backend_rejects_non_sec_result_url() -> None:
    transport = _FixedOriginTransport(
        HttpResponse(
            b'{"hits":{"hits":[{"_source":{"file_url":"https://attacker.test/filing.htm"}}]}}',
            "application/json",
            "https://efts.sec.gov",
        ),
    )

    with pytest.raises(ResearchSearchError):
        _ = EdgarSearchBackend("money-pit contact@example.com", transport).search("Acme", maximum_results=1)


def test_edgar_search_backend_constructs_filing_url_from_efts_identity() -> None:
    transport = _FixedOriginTransport(
        HttpResponse(
            b'{"hits":{"hits":[{"_id":"0000320193-24-000123:aapl-20240928.htm",'
            + b'"_source":{"ciks":["0000789019","0000320193"],'
            + b'"display_names":["Co-filer","Apple Inc."],"form":"10-K"}}]}}',
            "application/json",
            "https://efts.sec.gov",
        ),
    )

    hit = EdgarSearchBackend("money-pit contact@example.com", transport).search("Apple", maximum_results=1)[0]

    assert hit.canonical_uri == ("https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm")


@pytest.mark.parametrize(("query_text", "maximum_results"), [("", 1), ("fact", 0), ("fact", 101)])
def test_edgar_search_backend_rejects_invalid_query_bounds_before_io(
    query_text: str,
    maximum_results: int,
) -> None:
    transport = _FixedOriginTransport(HttpResponse(b"{}", "application/json", "https://efts.sec.gov"))

    with pytest.raises(ResearchSearchError):
        _ = EdgarSearchBackend("money-pit contact@example.com", transport).search(
            query_text,
            maximum_results=maximum_results,
        )

    assert transport.fixed_calls == []


def test_edgar_research_provider_fetch_sends_user_agent_through_fixed_origin_transport() -> None:
    transport = _FixedOriginTransport(
        HttpResponse(b"filing", "text/html", "https://www.sec.gov/Archives/filing.htm"),
    )
    provider = EdgarResearchProvider(
        _definition("research.edgar"),
        _Backend(()),
        transport,
        user_agent="money-pit contact@example.com",
        maximum_bytes=123,
        timeout_seconds=4,
    )
    result = ResearchDiscoveryResult(
        result_id="edgar:filing",
        provider="edgar",
        canonical_uri="https://www.sec.gov/Archives/filing.htm",
        discovered_at=NOW,
    )

    artifact = provider.fetch(result)

    assert transport.fixed_calls == [
        (
            result.canonical_uri,
            (("User-Agent", "money-pit contact@example.com"),),
            123,
            4,
        ),
    ]
    assert artifact.content == b"filing"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/Archives/filing.htm",
        "https://attacker.test/Archives/filing.htm",
        "https://user@example.com@www.sec.gov/Archives/filing.htm",
        "https://www.sec.gov:8443/Archives/filing.htm",
    ],
)
def test_edgar_research_provider_rejects_non_sec_filing_origin_before_io(url: str) -> None:
    transport = _FixedOriginTransport(HttpResponse(b"filing", "text/html", url))
    provider = EdgarResearchProvider(
        _definition("research.edgar"),
        _Backend(()),
        transport,
        user_agent="money-pit contact@example.com",
    )
    result = ResearchDiscoveryResult(
        result_id="edgar:filing",
        provider="edgar",
        canonical_uri=url,
        discovered_at=NOW,
    )

    with pytest.raises(ResearchFetchError):
        _ = provider.fetch(result)

    assert transport.fixed_calls == []


def test_edgar_research_provider_fetch_wraps_transport_failure() -> None:
    provider = EdgarResearchProvider(
        _definition("research.edgar"),
        _Backend(()),
        _FixedOriginTransport(SourceFetchError("unavailable")),
        user_agent="money-pit contact@example.com",
    )
    result = ResearchDiscoveryResult(
        result_id="edgar:filing",
        provider="edgar",
        canonical_uri="https://www.sec.gov/Archives/filing.htm",
        discovered_at=NOW,
    )

    with pytest.raises(ResearchFetchError):
        _ = provider.fetch(result)
