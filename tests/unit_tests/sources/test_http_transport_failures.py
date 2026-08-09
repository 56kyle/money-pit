import ssl
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from email.message import Message
from typing import ClassVar

import pytest
from typing_extensions import override

from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources.errors import SourceContentTooLargeError
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.errors import SourceFetchError
from money_pit.sources.http import AddressPinnedHttpTransport
from money_pit.sources.http import AddressPinnedRequest
from money_pit.sources.http import AddressPinnedResponse
from money_pit.sources.http import DnsPythonHttpNameResolver
from money_pit.sources.http import HttpClientAddressPinnedExchange
from money_pit.sources.http import WebConnector
from money_pit.sources.http import (
    _AddressPinnedHttpsConnection,  # pyright: ignore[reportPrivateUsage]  # Contract test pins TLS socket composition.
)


class _Resolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses: tuple[str, ...] = addresses
        self.requests: list[tuple[str, float]] = []

    def resolve(self, hostname: str, *, port: int, maximum_addresses: int, timeout_seconds: float) -> tuple[str, ...]:
        del port, maximum_addresses
        self.requests.append((hostname, timeout_seconds))
        return self.addresses


class _MappingResolver:
    def __init__(self, values: dict[str, tuple[str, ...]]) -> None:
        self.values: dict[str, tuple[str, ...]] = values
        self.requests: list[tuple[str, float]] = []

    def resolve(self, hostname: str, *, port: int, maximum_addresses: int, timeout_seconds: float) -> tuple[str, ...]:
        del port, maximum_addresses
        self.requests.append((hostname, timeout_seconds))
        return self.values[hostname]


class _Exchange:
    def __init__(self, outcomes: list[AddressPinnedResponse | BaseException]) -> None:
        self.outcomes: list[AddressPinnedResponse | BaseException] = outcomes
        self.requests: list[AddressPinnedRequest] = []

    def get(self, request: AddressPinnedRequest) -> AddressPinnedResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Record:
    def __init__(self, address: str) -> None:
        self.address: str = address


class _Dns:
    def __init__(self, values: dict[str, tuple[str, ...]]) -> None:
        self.values: dict[str, tuple[str, ...]] = values
        self.requests: list[tuple[str, float]] = []

    def resolve(self, hostname: str, record_type: str, *, lifetime: float, search: bool) -> tuple[_Record, ...]:
        del hostname, search
        self.requests.append((record_type, lifetime))
        return tuple(_Record(value) for value in self.values[record_type])


class _Clock:
    def __init__(self) -> None:
        self.value: float = -1.0

    def __call__(self) -> float:
        self.value += 1
        return self.value


def _ok(content: bytes = b"claim") -> AddressPinnedResponse:
    return AddressPinnedResponse(200, content, "text/plain", None)


def _redirect(location: str) -> AddressPinnedResponse:
    return AddressPinnedResponse(302, b"", "text/plain", location)


def _web_definition() -> SourceDefinition:
    return SourceDefinition(
        source_id="web",
        adapter_name="web",
        locator="https://example.com",
        provenance_group="example-web",
        allowed_uses=(AllowedUse.INTERPRETATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
    )


def test_dns_python_http_name_resolver_resolves_a_and_aaaa_under_shared_deadline() -> None:
    dns = _Dns({"A": ("93.184.216.34",), "AAAA": ("2606:2800:220:1:248:1893:25c8:1946",)})

    addresses = DnsPythonHttpNameResolver(dns, monotonic_clock=_Clock()).resolve(  # pyright: ignore[reportArgumentType]  # dnspython's resolver protocol is not statically exported.
        "example.com", port=443, maximum_addresses=32, timeout_seconds=5
    )

    assert addresses == ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    assert dns.requests == [("A", 4.0), ("AAAA", 3.0)]


def test_dns_python_http_name_resolver_bounds_combined_a_and_aaaa() -> None:
    dns = _Dns({"A": tuple(f"8.8.8.{i}" for i in range(1, 33)), "AAAA": ("2001:4860:4860::8888",)})
    with pytest.raises(SourceFetchError):
        _ = DnsPythonHttpNameResolver(dns).resolve(  # pyright: ignore[reportArgumentType]  # dnspython's resolver protocol is not statically exported.
            "example.com", port=443, maximum_addresses=32, timeout_seconds=5
        )


def test_address_pinned_transport_rejects_if_any_address_is_non_global() -> None:
    exchange = _Exchange([_ok()])
    with pytest.raises(SourceDiscoveryError):
        _ = AddressPinnedHttpTransport(exchange, resolver=_Resolver("93.184.216.34", "127.0.0.1")).get(
            "https://attacker.example/source", maximum_bytes=5, timeout_seconds=1
        )
    assert exchange.requests == []


def test_address_pinned_transport_rejects_more_than_32_addresses() -> None:
    exchange = _Exchange([_ok()])
    with pytest.raises(SourceFetchError):
        _ = AddressPinnedHttpTransport(exchange, resolver=_Resolver(*("93.184.216.34" for _ in range(33)))).get(
            "https://attacker.example/source", maximum_bytes=5, timeout_seconds=1
        )
    assert exchange.requests == []


def test_address_pinned_transport_conveys_validated_ip_and_hostname_identity() -> None:
    exchange = _Exchange([_ok()])
    _ = AddressPinnedHttpTransport(exchange, resolver=_Resolver("93.184.216.34")).get(
        "https://example.com:8443/source?q=1", maximum_bytes=5, timeout_seconds=10
    )
    request = exchange.requests[0]
    assert (request.address, request.hostname, request.port, request.target, request.host_header) == (
        "93.184.216.34",
        "example.com",
        8443,
        "/source?q=1",
        "example.com:8443",
    )


def test_address_pinned_transport_falls_back_across_validated_addresses() -> None:
    exchange = _Exchange([OSError("unavailable"), _ok()])
    response = AddressPinnedHttpTransport(exchange, resolver=_Resolver("93.184.216.34", "8.8.8.8")).get(
        "https://example.com/source", maximum_bytes=5, timeout_seconds=10
    )
    assert response.content == b"claim"
    assert [request.address for request in exchange.requests] == ["93.184.216.34", "8.8.8.8"]


def test_address_pinned_transport_reresolves_redirect_under_shared_deadline() -> None:
    resolver = _MappingResolver(
        {
            "example.com": ("93.184.216.34",),
            "redirect.example": ("8.8.8.8",),
        }
    )
    exchange = _Exchange([_redirect("https://redirect.example/final"), _ok()])
    response = AddressPinnedHttpTransport(exchange, resolver=resolver, monotonic_clock=_Clock()).get(
        "https://example.com/source", maximum_bytes=5, timeout_seconds=10
    )
    assert response.final_url == "https://redirect.example/final"
    assert [host for host, _ in resolver.requests] == ["example.com", "redirect.example"]
    assert resolver.requests[1][1] < resolver.requests[0][1]


def test_address_pinned_transport_rejects_sixth_redirect() -> None:
    exchange = _Exchange([_redirect(f"/redirect/{i}") for i in range(6)])
    with pytest.raises(SourceFetchError):
        _ = AddressPinnedHttpTransport(exchange, resolver=_Resolver("93.184.216.34")).get(
            "https://example.com/source", maximum_bytes=5, timeout_seconds=10
        )
    assert len(exchange.requests) == 6


def test_address_pinned_transport_rejects_non_success_status() -> None:
    with pytest.raises(SourceFetchError):
        _ = AddressPinnedHttpTransport(
            _Exchange([AddressPinnedResponse(503, b"", "text/plain", None)]),
            resolver=_Resolver("93.184.216.34"),
        ).get("https://example.com/source", maximum_bytes=5, timeout_seconds=10)


class _RawSocket:
    def close(self) -> None:
        return None


class _Tls:
    def __init__(self) -> None:
        self.server_hostname: str | None = None
        self.verify_mode: ssl.VerifyMode = ssl.CERT_REQUIRED
        self.check_hostname: bool = True
        self.post_handshake_auth: bool = False

    def wrap_socket(self, raw_socket: _RawSocket, *, server_hostname: str | None) -> _RawSocket:
        self.server_hostname = server_hostname
        return raw_socket


def test__address_pinned_https_connection_uses_ip_for_socket_and_hostname_for_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets: list[tuple[str, int]] = []
    context = _Tls()

    def create_connection(target: tuple[str, int], timeout: object, source_address: object) -> _RawSocket:
        del timeout, source_address
        targets.append(target)
        return _RawSocket()

    monkeypatch.setattr("money_pit.sources.http.socket.create_connection", create_connection)
    connection = _AddressPinnedHttpsConnection(
        "example.com",
        "93.184.216.34",
        443,
        timeout_seconds=5,
        context=context,  # pyright: ignore[reportArgumentType]  # The private seam intentionally accepts a minimal SSLContext test double at runtime.
    )
    connection.connect()
    assert targets == [("93.184.216.34", 443)]
    assert context.server_hostname == "example.com"


class _Response:
    def __init__(self, content: bytes, content_length: str | None = None) -> None:
        self.content: bytes = content
        self.offset: int = 0
        self.status: int = 200
        self.headers: Message[str, str] = Message()
        self.headers["Content-Type"] = "text/plain"
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read(self, amount: int = -1) -> bytes:
        return self.content[:amount]

    def read1(self, amount: int = -1) -> bytes:
        part: bytes = self.content[self.offset : self.offset + amount]
        self.offset += len(part)
        return part


class _TimeoutSocket:
    def settimeout(self, timeout_seconds: float) -> None:
        del timeout_seconds


class _Connection:
    instances: ClassVar[list["_Connection"]] = []
    response: ClassVar[_Response] = _Response(b"")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.request_data: tuple[str, str, dict[str, str]] | None = None
        self.sock: _TimeoutSocket | None = None
        self.__class__.instances.append(self)

    def connect(self) -> None:
        self.sock = _TimeoutSocket()

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.request_data = (method, target, headers)

    def getresponse(self) -> _Response:
        return self.__class__.response

    def close(self) -> None:
        return None


@pytest.fixture
def patched_connection(monkeypatch: pytest.MonkeyPatch) -> type[_Connection]:
    _Connection.instances = []
    monkeypatch.setattr("money_pit.sources.http._AddressPinnedHttpsConnection", _Connection)
    return _Connection


def _request(
    *,
    deadline: float = 100,
    monotonic_clock: Callable[[], float] = lambda: 0,
    headers: tuple[tuple[str, str], ...] = (),
) -> AddressPinnedRequest:
    return AddressPinnedRequest(
        "93.184.216.34",
        "example.com",
        443,
        True,
        "/source",
        "example.com",
        5,
        deadline,
        monotonic_clock,
        headers,
    )


def test_http_client_address_pinned_exchange_preserves_host_header(
    patched_connection: type[_Connection],
) -> None:
    patched_connection.response = _Response(b"claim")
    _ = HttpClientAddressPinnedExchange(ssl.create_default_context()).get(_request())
    assert patched_connection.instances[0].request_data == (
        "GET",
        "/source",
        {
            "Host": "example.com",
            "User-Agent": "money-pit/1 source-ingestion",
            "Connection": "close",
        },
    )


def test_http_client_address_pinned_exchange_forwards_custom_headers(
    patched_connection: type[_Connection],
) -> None:
    patched_connection.response = _Response(b"claim")

    _ = HttpClientAddressPinnedExchange().get(
        _request(headers=(("Accept", "application/json"), ("X-Subscription-Token", "token-value"))),
    )

    assert patched_connection.instances[0].request_data == (
        "GET",
        "/source",
        {
            "Host": "example.com",
            "User-Agent": "money-pit/1 source-ingestion",
            "Connection": "close",
            "Accept": "application/json",
            "X-Subscription-Token": "token-value",
        },
    )


def test_address_pinned_transport_get_fixed_origin_preserves_validated_headers() -> None:
    exchange = _Exchange([_ok()])

    _ = AddressPinnedHttpTransport(exchange, resolver=_Resolver("93.184.216.34")).get_fixed_origin(
        "https://example.com/source",
        headers=(("Accept", "application/json"), ("X-Api-Key", "token-value")),
        maximum_bytes=5,
        timeout_seconds=10,
    )

    assert exchange.requests[0].headers == (
        ("Accept", "application/json"),
        ("X-Api-Key", "token-value"),
    )


def test_address_pinned_transport_get_fixed_origin_rejects_redirect() -> None:
    exchange = _Exchange([_redirect("https://example.com/other")])

    with pytest.raises(SourceFetchError):
        _ = AddressPinnedHttpTransport(exchange, resolver=_Resolver("93.184.216.34")).get_fixed_origin(
            "https://example.com/source",
            headers=(("Authorization", "secret-value"),),
            maximum_bytes=5,
            timeout_seconds=10,
        )

    assert len(exchange.requests) == 1


@pytest.mark.parametrize(
    "headers",
    [
        (("Bad Header", "secret-value"),),
        (("Host", "secret-value"),),
        (("X-Api-Key", "secret-value"), ("x-api-key", "other-secret")),
        (("X-Api-Key", "secret-value\r\nInjected: yes"),),
    ],
    ids=("invalid-name", "reserved-name", "duplicate-name", "crlf-value"),
)
def test_address_pinned_transport_get_fixed_origin_rejects_invalid_headers_without_secret_text(
    headers: tuple[tuple[str, str], ...],
) -> None:
    exchange = _Exchange([_ok()])

    with pytest.raises(SourceFetchError) as raised:
        _ = AddressPinnedHttpTransport(exchange, resolver=_Resolver("93.184.216.34")).get_fixed_origin(
            "https://example.com/source",
            headers=headers,
            maximum_bytes=5,
            timeout_seconds=10,
        )

    assert "secret" not in str(raised.value)
    assert exchange.requests == []


@pytest.mark.parametrize("content_length", ["invalid", "-1"])
def test_http_client_address_pinned_exchange_rejects_invalid_content_length(
    patched_connection: type[_Connection], content_length: str
) -> None:
    patched_connection.response = _Response(b"claim", content_length)
    with pytest.raises(SourceFetchError):
        _ = HttpClientAddressPinnedExchange().get(_request())


def test_http_client_address_pinned_exchange_rejects_declared_oversize(
    patched_connection: type[_Connection],
) -> None:
    patched_connection.response = _Response(b"claim", "6")
    with pytest.raises(SourceContentTooLargeError):
        _ = HttpClientAddressPinnedExchange().get(_request())


def test_http_client_address_pinned_exchange_rejects_streamed_oversize(
    patched_connection: type[_Connection],
) -> None:
    patched_connection.response = _Response(b"claims")
    with pytest.raises(SourceContentTooLargeError):
        _ = HttpClientAddressPinnedExchange().get(_request())


class _SlowDripResponse(_Response):
    def __init__(self) -> None:
        super().__init__(b"claim")
        self.offset: int = 0

    @override
    def read1(self, amount: int = -1) -> bytes:
        del amount
        part: bytes = self.content[self.offset : self.offset + 1]
        self.offset += len(part)
        return part


def test_http_client_address_pinned_exchange_rejects_slow_drip_past_absolute_deadline(
    patched_connection: type[_Connection],
) -> None:
    patched_connection.response = _SlowDripResponse()

    with pytest.raises(SourceFetchError):
        _ = HttpClientAddressPinnedExchange().get(
            _request(deadline=8.5, monotonic_clock=_Clock()),
        )


def test_web_connector_rejects_item_from_another_source() -> None:
    connector = WebConnector(_web_definition())
    item = connector.discover(None).items[0].model_copy(update={"source_id": "other"})
    with pytest.raises(SourceFetchError):
        _ = connector.fetch(item)


def test_web_connector_rejects_non_utf8_content() -> None:
    connector = WebConnector(_web_definition())
    item = connector.discover(None).items[0]
    raw = RawArtifact(
        source_item=item,
        content=b"\xff",
        media_type="text/plain",
        retrieved_at=datetime(2026, 7, 29, tzinfo=UTC),
        canonical_uri=item.canonical_uri,
        content_hash="a" * 64,
    )
    with pytest.raises(SourceFetchError):
        _ = connector.extract(raw)
