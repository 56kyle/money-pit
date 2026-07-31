"""Module containing bounded HTTP transport and webpage extraction."""

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar
from typing import Protocol
from typing import cast
from typing import runtime_checkable
from urllib.parse import urljoin
from urllib.parse import urlsplit

import dns.exception
import dns.resolver
from pydantic import ConfigDict
from typing_extensions import override

from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import BoundedConnectorConfig
from money_pit.sources._shared import evidence_asset
from money_pit.sources._shared import parse_config
from money_pit.sources._shared import require_media_type
from money_pit.sources._shared import sha256_bytes
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import SourceContentTooLargeError
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.errors import SourceFetchError


_MAXIMUM_RESOLVED_ADDRESSES: int = 32
_MAXIMUM_REDIRECTS: int = 5
_USER_AGENT: str = "money-pit/1 source-ingestion"
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


class HttpResponse:
    """Bounded HTTP response returned by an injected transport."""

    def __init__(self, content: bytes, media_type: str, final_url: str) -> None:
        """Create a response from already bounded bytes."""
        self.content: bytes = content
        self.media_type: str = media_type
        self.final_url: str = final_url


class HttpTransport(Protocol):
    """Transport seam used by network-backed connectors."""

    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        """Fetch one URL under explicit byte and time limits."""
        ...


class HttpNameResolver(Protocol):
    """Bounded hostname-resolution seam used by the HTTP transport."""

    def resolve(
        self,
        hostname: str,
        *,
        port: int,
        maximum_addresses: int,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        """Return resolved A and AAAA address strings under explicit bounds."""
        ...


@runtime_checkable
class _DnsAddressRecord(Protocol):
    """Typed view of an A or AAAA record returned by dnspython."""

    address: str


class DnsPythonHttpNameResolver:
    """Resolve HTTP hostnames under an enforceable shared deadline."""

    def __init__(
        self,
        resolver: dns.resolver.Resolver | None = None,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a resolver with injectable DNS and clock seams."""
        self._resolver: dns.resolver.Resolver = resolver or dns.resolver.Resolver()
        self._monotonic_clock: Callable[[], float] = monotonic_clock

    def resolve(
        self,
        hostname: str,
        *,
        port: int,
        maximum_addresses: int,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        """Resolve A and AAAA records within one deadline and address bound."""
        del port
        if maximum_addresses < 1:
            raise SourceFetchError("HTTP resolver address limit must be positive")
        deadline: float = self._monotonic_clock() + timeout_seconds
        addresses: list[str] = []
        for record_type in ("A", "AAAA"):
            resolved_addresses: tuple[str, ...] = self._resolve_record_type(
                hostname,
                record_type=record_type,
                deadline=deadline,
            )
            _append_unique_addresses(
                addresses,
                resolved_addresses,
                maximum_addresses=maximum_addresses,
            )
        if not addresses:
            raise SourceFetchError("HTTP hostname did not resolve to an A or AAAA address")
        return tuple(addresses)

    def _resolve_record_type(
        self,
        hostname: str,
        *,
        record_type: str,
        deadline: float,
    ) -> tuple[str, ...]:
        """Resolve one DNS record type within the shared resolution deadline."""
        remaining_seconds: float = _remaining_seconds(
            deadline,
            monotonic_clock=self._monotonic_clock,
            timeout_message="HTTP hostname resolution timed out",
        )
        try:
            raw_answer: object = self._resolver.resolve(
                hostname,
                record_type,
                lifetime=remaining_seconds,
                search=False,
            )
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return ()
        except dns.exception.Timeout as error:
            raise SourceFetchError("HTTP hostname resolution timed out") from error
        except dns.exception.DNSException as error:
            raise SourceFetchError("HTTP hostname resolution failed") from error
        answer: Iterable[object] = cast(
            "Iterable[object]",
            raw_answer,
        )
        return tuple(_dns_record_address(record) for record in answer)


def _dns_record_address(record: object) -> str:
    """Validate and return the address carried by one DNS record."""
    if not isinstance(record, _DnsAddressRecord):
        raise SourceFetchError("HTTP resolver returned an invalid IP address")
    return record.address


def _append_unique_addresses(
    destination: list[str],
    candidates: tuple[str, ...],
    *,
    maximum_addresses: int,
) -> None:
    """Append unique DNS answers without exceeding the configured bound."""
    for address in candidates:
        if address in destination:
            continue
        if len(destination) >= maximum_addresses:
            raise SourceFetchError("HTTP hostname resolved to too many addresses")
        destination.append(address)


@dataclass(frozen=True)
class AddressPinnedRequest:
    """One HTTP request whose connection target is an already validated IP."""

    address: str
    hostname: str
    port: int
    use_tls: bool
    target: str
    host_header: str
    maximum_bytes: int
    deadline: float
    monotonic_clock: Callable[[], float]


@dataclass(frozen=True)
class AddressPinnedResponse:
    """One response hop returned by an address-pinned exchange."""

    status: int
    content: bytes
    media_type: str
    location: str | None


class HttpAddressPinnedExchange(Protocol):
    """Injectable seam for one connection to one validated address."""

    def get(self, request: AddressPinnedRequest) -> AddressPinnedResponse:
        """Connect to the supplied address while preserving hostname identity."""
        ...


class _AddressPinnedHttpConnection(http.client.HTTPConnection):
    """HTTP connection that opens its socket to a validated address."""

    sock: socket.socket | None

    def __init__(
        self,
        hostname: str,
        address: str,
        port: int,
        *,
        timeout_seconds: float,
    ) -> None:
        super().__init__(hostname, port=port, timeout=timeout_seconds)
        self._validated_address: str = address

    @override
    def connect(self) -> None:
        connected_socket: socket.socket = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            None,
        )
        self.sock = connected_socket


class _AddressPinnedHttpsConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an IP while verifying the URL hostname."""

    sock: socket.socket | None

    def __init__(
        self,
        hostname: str,
        address: str,
        port: int,
        *,
        timeout_seconds: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(
            hostname,
            port=port,
            timeout=timeout_seconds,
            context=context,
        )
        self._validated_address: str = address
        self._tls_context: ssl.SSLContext = context

    @override
    def connect(self) -> None:
        raw_socket: socket.socket = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            None,
        )
        try:
            tls_socket: ssl.SSLSocket = self._tls_context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
            self.sock = tls_socket
        except BaseException:
            raw_socket.close()
            raise


class HttpClientAddressPinnedExchange:
    """Production address-pinned exchange backed by http.client."""

    def __init__(self, tls_context: ssl.SSLContext | None = None) -> None:
        """Create an exchange with normal certificate and hostname validation."""
        self._tls_context: ssl.SSLContext = tls_context or ssl.create_default_context()

    def get(self, request: AddressPinnedRequest) -> AddressPinnedResponse:
        """Perform one bounded GET under the request's absolute deadline."""
        connection: http.client.HTTPConnection = self._create_connection(request)
        try:
            _connect_under_deadline(connection, request)
            _send_request_under_deadline(connection, request)
            response: http.client.HTTPResponse = _read_headers_under_deadline(
                connection,
                request,
            )
            location: str | None = response.getheader("Location")
            if response.status in _REDIRECT_STATUSES:
                return AddressPinnedResponse(
                    status=response.status,
                    content=b"",
                    media_type=response.headers.get_content_type(),
                    location=location,
                )
            _validate_declared_content_length(response, request.maximum_bytes)
            content: bytes = _read_body_under_deadline(
                response,
                connection=connection,
                request=request,
            )
            return AddressPinnedResponse(
                status=response.status,
                content=content,
                media_type=response.headers.get_content_type(),
                location=location,
            )
        finally:
            connection.close()

    def _create_connection(
        self,
        request: AddressPinnedRequest,
    ) -> http.client.HTTPConnection:
        """Create a pinned connection with only the current remaining timeout."""
        timeout_seconds: float = _request_remaining_seconds(request)
        if request.use_tls:
            return _AddressPinnedHttpsConnection(
                request.hostname,
                request.address,
                request.port,
                timeout_seconds=timeout_seconds,
                context=self._tls_context,
            )
        return _AddressPinnedHttpConnection(
            request.hostname,
            request.address,
            request.port,
            timeout_seconds=timeout_seconds,
        )


def _connect_under_deadline(
    connection: http.client.HTTPConnection,
    request: AddressPinnedRequest,
) -> None:
    """Connect and immediately reject an exhausted absolute deadline."""
    _set_connection_remaining_timeout(connection, request)
    connection.connect()
    _set_connection_remaining_timeout(connection, request)


def _send_request_under_deadline(
    connection: http.client.HTTPConnection,
    request: AddressPinnedRequest,
) -> None:
    """Send request headers within the absolute request deadline."""
    _set_connection_remaining_timeout(connection, request)
    connection.request(
        "GET",
        request.target,
        headers={
            "Host": request.host_header,
            "User-Agent": _USER_AGENT,
            "Connection": "close",
        },
    )
    _set_connection_remaining_timeout(connection, request)


def _read_headers_under_deadline(
    connection: http.client.HTTPConnection,
    request: AddressPinnedRequest,
) -> http.client.HTTPResponse:
    """Read response headers and reject completion after the deadline."""
    _set_connection_remaining_timeout(connection, request)
    response: http.client.HTTPResponse = connection.getresponse()
    _set_connection_remaining_timeout(connection, request)
    return response


def _validate_declared_content_length(
    response: http.client.HTTPResponse,
    maximum_bytes: int,
) -> None:
    """Reject invalid or excessive declared response sizes."""
    declared_length: str | None = response.getheader("Content-Length")
    if declared_length is None:
        return
    try:
        parsed_length: int = int(declared_length)
    except ValueError as error:
        raise SourceFetchError(
            "HTTP response contained an invalid Content-Length",
        ) from error
    if parsed_length < 0:
        raise SourceFetchError("HTTP response contained an invalid Content-Length")
    if parsed_length > maximum_bytes:
        raise SourceContentTooLargeError(
            f"HTTP response exceeds {maximum_bytes} bytes",
        )


def _read_body_under_deadline(
    response: http.client.HTTPResponse,
    *,
    connection: http.client.HTTPConnection,
    request: AddressPinnedRequest,
) -> bytes:
    """Read a bounded body while reapplying the absolute deadline per read."""
    content_parts: list[bytes] = []
    remaining_bytes: int = request.maximum_bytes + 1
    while remaining_bytes > 0:
        _set_connection_remaining_timeout(connection, request)
        part: bytes = response.read1(remaining_bytes)
        _set_connection_remaining_timeout(connection, request)
        if not part:
            break
        content_parts.append(part)
        remaining_bytes -= len(part)
    content: bytes = b"".join(content_parts)
    if len(content) > request.maximum_bytes:
        raise SourceContentTooLargeError(
            f"HTTP response exceeds {request.maximum_bytes} bytes",
        )
    return content


def _set_connection_remaining_timeout(
    connection: http.client.HTTPConnection,
    request: AddressPinnedRequest,
) -> None:
    """Set socket inactivity timeout to the absolute deadline remainder."""
    timeout_seconds: float = _request_remaining_seconds(request)
    connection.timeout = timeout_seconds
    connected_socket: socket.socket | None = connection.sock
    if connected_socket is not None:
        connected_socket.settimeout(timeout_seconds)


def _request_remaining_seconds(request: AddressPinnedRequest) -> float:
    """Return the request's absolute deadline remainder or fail closed."""
    return _remaining_seconds(
        request.deadline,
        monotonic_clock=request.monotonic_clock,
        timeout_message="HTTP fetch timed out",
    )


class UrllibHttpTransport:
    """Compatibility-named HTTP transport with address-pinned connections."""

    def __init__(
        self,
        exchange: HttpAddressPinnedExchange | None = None,
        *,
        resolver: HttpNameResolver | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a transport with injectable DNS, exchange, and clock seams."""
        self._exchange: HttpAddressPinnedExchange = exchange or HttpClientAddressPinnedExchange()
        self._resolver: HttpNameResolver = resolver or DnsPythonHttpNameResolver(
            monotonic_clock=monotonic_clock,
        )
        self._monotonic_clock: Callable[[], float] = monotonic_clock

    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        """Fetch one public HTTP(S) resource through validated pinned addresses."""
        _validate_http_limits(
            maximum_bytes=maximum_bytes,
            timeout_seconds=timeout_seconds,
        )
        try:
            return self._get_redirect_chain(
                url,
                maximum_bytes=maximum_bytes,
                deadline=self._monotonic_clock() + timeout_seconds,
            )
        except (SourceContentTooLargeError, SourceFetchError, SourceDiscoveryError):
            raise
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise SourceFetchError(f"HTTP fetch failed for {_safe_url_label(url)}") from error

    def _get_redirect_chain(
        self,
        url: str,
        *,
        maximum_bytes: int,
        deadline: float,
    ) -> HttpResponse:
        """Follow a bounded redirect chain under one request deadline."""
        current_url: str = url
        for redirect_count in range(_MAXIMUM_REDIRECTS + 1):
            response: AddressPinnedResponse = self._fetch_url(
                current_url,
                maximum_bytes=maximum_bytes,
                deadline=deadline,
            )
            redirect_target: str | None = _redirect_target(
                response,
                current_url=current_url,
                redirect_count=redirect_count,
            )
            if redirect_target is not None:
                current_url = redirect_target
                continue
            return _bounded_http_response(response, final_url=current_url)
        raise SourceFetchError("HTTP redirect handling ended unexpectedly")

    def _fetch_url(
        self,
        url: str,
        *,
        maximum_bytes: int,
        deadline: float,
    ) -> AddressPinnedResponse:
        """Resolve one URL and fetch it only through its validated addresses."""
        request_parts: _ValidatedHttpRequestParts = _validated_request_parts(url)
        addresses: tuple[str, ...] = _resolve_public_http_addresses(
            url,
            resolver=self._resolver,
            maximum_addresses=_MAXIMUM_RESOLVED_ADDRESSES,
            timeout_seconds=_remaining_seconds(
                deadline,
                monotonic_clock=self._monotonic_clock,
                timeout_message="HTTP fetch timed out",
            ),
        )
        return self._fetch_from_validated_addresses(
            request_parts,
            addresses=addresses,
            maximum_bytes=maximum_bytes,
            deadline=deadline,
        )

    def _fetch_from_validated_addresses(
        self,
        parts: "_ValidatedHttpRequestParts",
        *,
        addresses: tuple[str, ...],
        maximum_bytes: int,
        deadline: float,
    ) -> AddressPinnedResponse:
        """Try only the validated addresses until one returns a response."""
        last_error: OSError | http.client.HTTPException | None = None
        for address in addresses:
            request: AddressPinnedRequest = AddressPinnedRequest(
                address=address,
                hostname=parts.hostname,
                port=parts.port,
                use_tls=parts.use_tls,
                target=parts.target,
                host_header=parts.host_header,
                maximum_bytes=maximum_bytes,
                deadline=deadline,
                monotonic_clock=self._monotonic_clock,
            )
            try:
                return self._exchange.get(request)
            except SourceContentTooLargeError:
                raise
            except SourceFetchError:
                raise
            except (OSError, http.client.HTTPException) as error:
                last_error = error
        raise SourceFetchError("HTTP connection failed for every validated address") from last_error


def _validate_http_limits(*, maximum_bytes: int, timeout_seconds: float) -> None:
    """Validate caller-supplied HTTP resource limits."""
    if maximum_bytes < 0:
        raise SourceFetchError("HTTP response byte limit must not be negative")
    if timeout_seconds <= 0:
        raise SourceFetchError("HTTP timeout must be positive")


def _redirect_target(
    response: AddressPinnedResponse,
    *,
    current_url: str,
    redirect_count: int,
) -> str | None:
    """Return a validated redirect candidate or identify a final response."""
    if response.status not in _REDIRECT_STATUSES:
        return None
    if response.location is None:
        raise SourceFetchError("HTTP redirect did not contain a Location header")
    if redirect_count == _MAXIMUM_REDIRECTS:
        raise SourceFetchError("HTTP redirect limit exceeded")
    return urljoin(current_url, response.location)


def _bounded_http_response(
    response: AddressPinnedResponse,
    *,
    final_url: str,
) -> HttpResponse:
    """Convert a successful final hop into the public response contract."""
    if response.status < 200 or response.status >= 300:
        url_label: str = _safe_url_label(final_url)
        raise SourceFetchError(
            f"HTTP fetch returned status {response.status} for {url_label}",
        )
    return HttpResponse(
        content=response.content,
        media_type=response.media_type,
        final_url=final_url,
    )


@dataclass(frozen=True)
class _ValidatedHttpRequestParts:
    hostname: str
    port: int
    use_tls: bool
    target: str
    host_header: str


def _validated_request_parts(url: str) -> _ValidatedHttpRequestParts:
    """Validate a URL and derive its connection-independent request fields."""
    validate_public_http_url(url)
    parts = urlsplit(url)
    hostname: str = cast("str", parts.hostname).encode("idna").decode("ascii")
    try:
        explicit_port: int | None = parts.port
    except ValueError as error:
        raise SourceDiscoveryError("Source URL must contain a valid port") from error
    use_tls: bool = parts.scheme == "https"
    default_port: int = 443 if use_tls else 80
    port: int = explicit_port if explicit_port is not None else default_port
    target: str = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    host_name_for_header: str
    try:
        parsed_address = ipaddress.ip_address(hostname)
    except ValueError:
        host_name_for_header = hostname
    else:
        host_name_for_header = f"[{hostname}]" if parsed_address.version == 6 else hostname
    host_header: str = host_name_for_header
    if explicit_port is not None and explicit_port != default_port:
        host_header = f"{host_name_for_header}:{explicit_port}"
    return _ValidatedHttpRequestParts(
        hostname=hostname,
        port=port,
        use_tls=use_tls,
        target=target,
        host_header=host_header,
    )


def _remaining_seconds(
    deadline: float,
    *,
    monotonic_clock: Callable[[], float],
    timeout_message: str,
) -> float:
    """Return the remaining shared deadline or fail closed."""
    remaining_seconds: float = deadline - monotonic_clock()
    if remaining_seconds <= 0:
        raise SourceFetchError(timeout_message)
    return remaining_seconds


class WebConnectorConfig(BoundedConnectorConfig):
    """Configuration accepted by web connectors."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class WebConnector:
    """Connector for a manually configured webpage or URL."""

    def __init__(
        self,
        definition: SourceDefinition,
        transport: HttpTransport | None = None,
    ) -> None:
        """Bind the connector to one definition and an optional transport."""
        self._definition: SourceDefinition = definition
        self._config: WebConnectorConfig = WebConnectorConfig.model_validate(
            parse_config(definition, WebConnectorConfig).model_dump(),
        )
        self._transport: HttpTransport = transport or UrllibHttpTransport()
        validate_public_http_url(definition.locator)

    def discover(self, cursor: SourceCursor | None) -> DiscoveryBatch:
        """Discover the configured URL without performing a speculative fetch."""
        now = utc_now()
        version: str = cursor.value if cursor is not None else "unfetched"
        item: SourceItem = SourceItem(
            source_item_id=f"{self._definition.source_id}:{version}",
            source_id=self._definition.source_id,
            canonical_uri=self._definition.locator,
            discovered_at=now,
            content_version=version,
        )
        return DiscoveryBatch(items=(item,), next_cursor=cursor, discovered_at=now)

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Fetch HTML or plain text and derive its actual content version."""
        if item.source_id != self._definition.source_id:
            raise SourceFetchError("Source item does not belong to this connector")
        response: HttpResponse = self._transport.get(
            item.canonical_uri,
            maximum_bytes=self._config.max_content_bytes,
            timeout_seconds=self._config.timeout_seconds,
        )
        require_media_type(response.media_type, ("text/html", "text/plain"))
        digest: str = sha256_bytes(response.content)
        versioned_item: SourceItem = item.model_copy(
            update={
                "canonical_uri": response.final_url,
                "content_version": digest,
            },
        )
        return RawArtifact(
            source_item=versioned_item,
            content=response.content,
            media_type=response.media_type,
            retrieved_at=utc_now(),
            canonical_uri=response.final_url,
            content_hash=digest,
        )

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract visible text while retaining offsets into that extraction."""
        try:
            source: str = artifact.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceFetchError("Web content is not valid UTF-8") from error
        text: str
        if artifact.media_type.partition(";")[0].lower() == "text/html":
            parser: _VisibleTextParser = _VisibleTextParser()
            parser.feed(source)
            text = parser.text()
        else:
            text = source
        fragment: EvidenceFragment = EvidenceFragment(
            fragment_id=f"{artifact.content_hash}:text",
            asset_id=artifact.content_hash,
            kind="web_span",
            locator=TextLocator(start_offset=0, end_offset=len(text)),
            extracted_text=text,
            extraction_method="html.parser",
        )
        return EvidenceDocument(asset=evidence_asset(artifact), fragments=(fragment,))


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth: int = 0
        self._parts: list[str] = []

    @override
    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.lower() in {"script", "style", "noscript"}:
            self._hidden_depth += 1

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._hidden_depth > 0:
            self._hidden_depth -= 1

    @override
    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        """Return normalized visible text."""
        return "\n".join(self._parts)


def _safe_url_label(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _resolve_public_http_addresses(
    url: str,
    *,
    resolver: HttpNameResolver,
    maximum_addresses: int,
    timeout_seconds: float,
) -> tuple[str, ...]:
    """Return only globally routable addresses for one validated HTTP URL."""
    validate_public_http_url(url)
    parts = urlsplit(url)
    hostname: str = cast("str", parts.hostname)
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return (str(literal_address),)
    try:
        explicit_port: int | None = parts.port
    except ValueError as error:
        raise SourceDiscoveryError("Source URL must contain a valid port") from error
    port: int = explicit_port if explicit_port is not None else (443 if parts.scheme == "https" else 80)
    addresses: tuple[str, ...] = _resolve_http_hostname(
        resolver,
        hostname=hostname,
        port=port,
        maximum_addresses=maximum_addresses,
        timeout_seconds=timeout_seconds,
    )
    _require_global_addresses(addresses)
    return addresses


def _resolve_http_hostname(
    resolver: HttpNameResolver,
    *,
    hostname: str,
    port: int,
    maximum_addresses: int,
    timeout_seconds: float,
) -> tuple[str, ...]:
    """Return a nonempty resolver result that respects the address bound."""
    try:
        addresses: tuple[str, ...] = resolver.resolve(
            hostname,
            port=port,
            maximum_addresses=maximum_addresses,
            timeout_seconds=timeout_seconds,
        )
    except SourceFetchError:
        raise
    except OSError as error:
        raise SourceFetchError("HTTP hostname resolution failed") from error
    if not addresses:
        raise SourceFetchError("HTTP hostname did not resolve to an A or AAAA address")
    if len(addresses) > maximum_addresses:
        raise SourceFetchError("HTTP hostname resolved to too many addresses")
    return addresses


def _require_global_addresses(addresses: tuple[str, ...]) -> None:
    """Reject invalid or non-global resolver address strings."""
    for resolved_address in addresses:
        try:
            address = ipaddress.ip_address(resolved_address)
        except ValueError as error:
            raise SourceFetchError("HTTP resolver returned an invalid IP address") from error
        if not address.is_global:
            raise SourceDiscoveryError(
                "Private or non-global source URLs are not permitted",
            )


def validate_public_http_url(url: str) -> None:
    """Reject non-HTTP, credential-bearing, and literal private-network URLs."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SourceDiscoveryError("Source URL must use HTTP or HTTPS")
    if parts.username is not None or parts.password is not None:
        raise SourceDiscoveryError("Source URL must not contain credentials")
    hostname: str = parts.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise SourceDiscoveryError("Localhost source URLs are not permitted")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise SourceDiscoveryError("Private or non-global source URLs are not permitted")
