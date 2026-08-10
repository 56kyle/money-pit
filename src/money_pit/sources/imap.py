"""Module containing bounded IMAP message discovery and acquisition."""

from __future__ import annotations

import imaplib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Protocol
from typing import cast
from urllib.parse import quote

from pydantic import ConfigDict
from pydantic import Field

from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import BoundedConnectorConfig
from money_pit.sources._shared import parse_config
from money_pit.sources._shared import sha256_bytes
from money_pit.sources._shared import source_definition_hash
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.errors import SourceFetchError


if TYPE_CHECKING:
    from datetime import datetime

    from money_pit.schemas.evidence import EvidenceDocument
    from money_pit.secrets import ImapCredentials


class ImapConnectorConfig(BoundedConnectorConfig):
    """Configuration for one bounded IMAP mailbox."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    mailbox: str = Field(default="INBOX", min_length=1)
    port: int = Field(default=993, ge=1, le=65535)
    maximum_messages: int = Field(default=25, ge=1, le=100)


@dataclass(frozen=True)
class ImapDiscovery:
    """Stable mailbox UID identity returned by an IMAP transport."""

    uid_validity: str
    uid: int


class ImapTransport(Protocol):
    """Network seam for bounded mailbox discovery and fetches."""

    def discover(self, cursor: SourceCursor | None, *, maximum_messages: int) -> tuple[ImapDiscovery, ...]:
        """Return ascending message UIDs after a cursor."""
        ...

    def fetch(self, uid: int, *, uid_validity: str, maximum_bytes: int) -> bytes:
        """Return one complete RFC 5322 message under a byte limit."""
        ...


class ImapLibTransport:
    """TLS IMAP transport that opens a bounded connection per operation."""

    def __init__(
        self,
        host: str,
        config: ImapConnectorConfig,
        *,
        username: str,
        password: str,
    ) -> None:
        """Bind validated connection settings and credentials."""
        self._host: str = host
        self._config: ImapConnectorConfig = config
        self._username: str = username
        self._password: str = password

    def discover(self, cursor: SourceCursor | None, *, maximum_messages: int) -> tuple[ImapDiscovery, ...]:
        """Discover ascending UIDs without downloading message bodies."""
        with self._connected() as client:
            uid_validity: str = _uid_validity(client)
            cursor_validity, cursor_uid = _parse_cursor(cursor)
            start_uid: int = cursor_uid + 1 if cursor_validity == uid_validity else 1
            status, payload = client.uid("search", f"UID {start_uid}:*")
            if status != "OK" or not payload or not isinstance(payload[0], bytes):
                raise SourceDiscoveryError("IMAP UID search failed")
            uids: list[int] = [int(value) for value in payload[0].split() if value.isdigit()]
            return tuple(ImapDiscovery(uid_validity=uid_validity, uid=uid) for uid in sorted(uids)[:maximum_messages])

    def fetch(self, uid: int, *, uid_validity: str, maximum_bytes: int) -> bytes:
        """Fetch one message and enforce its observed content bound."""
        with self._connected() as client:
            if _uid_validity(client) != uid_validity:
                raise SourceFetchError("IMAP UIDVALIDITY changed after discovery")
            status, payload = client.uid("fetch", str(uid), "(RFC822)")
            if status != "OK":
                raise SourceFetchError("IMAP message fetch failed")
            payload_items: list[object] = cast("list[object]", payload)
            content: bytes | None = _imap_message_content(payload_items)
            if content is None:
                raise SourceFetchError("IMAP message fetch returned no RFC 5322 body")
            if len(content) > maximum_bytes:
                raise SourceFetchError(f"IMAP message exceeds {maximum_bytes} bytes")
            return content

    def _connected(self) -> imaplib.IMAP4_SSL:
        """Open, authenticate, and select the configured mailbox."""
        try:
            client = imaplib.IMAP4_SSL(
                self._host,
                self._config.port,
                timeout=self._config.timeout_seconds,
            )
            status, _ = client.login(self._username, self._password)
            if status != "OK":
                raise SourceDiscoveryError("IMAP login failed")
            status, _ = client.select(self._config.mailbox, readonly=True)
            if status != "OK":
                raise SourceDiscoveryError("IMAP mailbox selection failed")
            return client
        except OSError as error:
            raise SourceDiscoveryError("IMAP connection failed") from error


class ImapConnector:
    """Discover and fetch messages from one bounded read-only IMAP mailbox."""

    def __init__(
        self,
        definition: SourceDefinition,
        transport: ImapTransport | None = None,
        *,
        credentials: ImapCredentials | None = None,
    ) -> None:
        """Bind an IMAP source to credentials and an optional transport."""
        self._definition: SourceDefinition = definition
        self._config: ImapConnectorConfig = ImapConnectorConfig.model_validate(
            parse_config(definition, ImapConnectorConfig).model_dump(),
        )
        if transport is None:
            if credentials is None:
                raise TypeError("credentials are required when no IMAP transport is injected")
            transport = ImapLibTransport(
                definition.locator,
                self._config,
                username=credentials.username.get_secret_value(),
                password=credentials.password.get_secret_value(),
            )
        self._transport: ImapTransport = transport

    def discover(
        self,
        cursor: SourceCursor | None,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> DiscoveryBatch:
        """Return one bounded page of unseen mailbox UIDs."""
        del purpose
        discoveries: tuple[ImapDiscovery, ...] = self._transport.discover(
            cursor,
            maximum_messages=self._config.maximum_messages,
        )
        now: datetime = utc_now()
        items: tuple[SourceItem, ...] = tuple(
            SourceItem(
                source_item_id=f"{self._definition.source_id}:{item.uid_validity}:{item.uid}",
                source_id=self._definition.source_id,
                source_definition_hash=source_definition_hash(self._definition),
                canonical_uri=(f"imap://{self._definition.locator}/{quote(self._config.mailbox, safe='')}/{item.uid}"),
                discovered_at=now,
                content_version=f"{item.uid_validity}:{item.uid}",
            )
            for item in discoveries
        )
        next_cursor: SourceCursor | None = (
            SourceCursor(value=f"{discoveries[-1].uid_validity}:{discoveries[-1].uid}") if discoveries else cursor
        )
        return DiscoveryBatch(items=items, next_cursor=next_cursor, discovered_at=now)

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Fetch and hash one discovered RFC 5322 message."""
        if item.source_id != self._definition.source_id:
            raise SourceFetchError("Source item does not belong to this connector")
        uid_validity, separator, uid_text = item.content_version.partition(":")
        if not separator:
            raise SourceFetchError("IMAP source item has no UIDVALIDITY")
        try:
            uid: int = int(uid_text)
        except ValueError as error:
            raise SourceFetchError("IMAP source item has an invalid UID") from error
        content: bytes = self._transport.fetch(
            uid,
            uid_validity=uid_validity,
            maximum_bytes=self._config.max_content_bytes,
        )
        digest: str = sha256_bytes(content)
        return RawArtifact(
            source_item=item,
            content=content,
            media_type="message/rfc822",
            retrieved_at=utc_now(),
            canonical_uri=item.canonical_uri,
            content_hash=digest,
        )

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract the message through the generic RFC 5322 processor."""
        from money_pit.evidence.processors import Rfc5322EvidenceProcessor

        return Rfc5322EvidenceProcessor().process(artifact)


def _parse_cursor(cursor: SourceCursor | None) -> tuple[str | None, int]:
    if cursor is None:
        return None, 0
    validity, separator, uid_text = cursor.value.partition(":")
    if not separator:
        return None, 0
    try:
        return validity, int(uid_text)
    except ValueError:
        return None, 0


def _uid_validity(client: imaplib.IMAP4_SSL) -> str:
    _, values = client.response("UIDVALIDITY")
    if not values or not isinstance(values[0], bytes):
        raise SourceDiscoveryError("IMAP mailbox did not report UIDVALIDITY")
    return values[0].decode("ascii")


def _imap_message_content(payload: list[object]) -> bytes | None:
    for part in payload:
        if not isinstance(part, tuple):
            continue
        values: tuple[object, ...] = cast("tuple[object, ...]", part)
        if len(values) < 2:
            continue
        content: object = values[1]
        if isinstance(content, bytes):
            return content
    return None
