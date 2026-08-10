from datetime import UTC
from datetime import datetime
from types import TracebackType

import pytest
from pydantic import SecretStr
from pytest import MonkeyPatch

from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.secrets import ImapCredentials
from money_pit.sources.errors import SourceFetchError
from money_pit.sources.imap import ImapConnector
from money_pit.sources.imap import ImapConnectorConfig
from money_pit.sources.imap import ImapDiscovery
from money_pit.sources.imap import ImapLibTransport


NOW = datetime(2026, 8, 9, tzinfo=UTC)


class _ImapClient:
    def __init__(self, uid_validity: str, search_payload: bytes = b"") -> None:
        self.uid_validity: str = uid_validity
        self.search_payload: bytes = search_payload
        self.uid_calls: list[tuple[str, ...]] = []

    def __enter__(self) -> "_ImapClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def response(self, name: str) -> tuple[str, list[bytes]]:
        assert name == "UIDVALIDITY"
        return "OK", [self.uid_validity.encode("ascii")]

    def uid(self, *arguments: str) -> tuple[str, list[object]]:
        self.uid_calls.append(arguments)
        if arguments[0] == "search":
            return "OK", [self.search_payload]
        return "OK", [(b"header", b"message")]


class _ConnectorTransport:
    def __init__(self) -> None:
        self.discover_calls: list[tuple[SourceCursor | None, int]] = []
        self.fetch_calls: list[tuple[int, str, int]] = []

    def discover(self, cursor: SourceCursor | None, *, maximum_messages: int) -> tuple[ImapDiscovery, ...]:
        self.discover_calls.append((cursor, maximum_messages))
        return (ImapDiscovery(uid_validity="validity-1", uid=7),)

    def fetch(self, uid: int, *, uid_validity: str, maximum_bytes: int) -> bytes:
        self.fetch_calls.append((uid, uid_validity, maximum_bytes))
        return b"From: sender@example.test\r\n\r\nBody"


def _definition() -> SourceDefinition:
    return SourceDefinition(
        source_id="mail",
        adapter_name="imap",
        locator="mail.example.test",
        provenance_group="mailbox",
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
        adapter_config={
            "maximum_messages": 2,
            "max_content_bytes": 64,
        },
    )


def _lib_transport() -> ImapLibTransport:
    config = ImapConnectorConfig(
        maximum_messages=2,
    )
    credential = chr(120)
    return ImapLibTransport("mail.example.test", config, username="user", password=credential)


def test_imap_lib_transport_resets_uid_search_after_uidvalidity_changes(monkeypatch: MonkeyPatch) -> None:
    client = _ImapClient("new-validity", b"5 4 3")
    transport = _lib_transport()
    monkeypatch.setattr(transport, "_connected", lambda: client)

    discoveries = transport.discover(SourceCursor(value="old-validity:99"), maximum_messages=2)

    assert discoveries == (
        ImapDiscovery(uid_validity="new-validity", uid=3),
        ImapDiscovery(uid_validity="new-validity", uid=4),
    )
    assert client.uid_calls == [("search", "UID 1:*")]


def test_imap_lib_transport_rejects_fetch_after_uidvalidity_changes(monkeypatch: MonkeyPatch) -> None:
    client = _ImapClient("new-validity")
    transport = _lib_transport()
    monkeypatch.setattr(transport, "_connected", lambda: client)

    with pytest.raises(SourceFetchError):
        _ = transport.fetch(7, uid_validity="old-validity", maximum_bytes=64)


def test_imap_connector_propagates_configured_discovery_and_fetch_bounds() -> None:
    transport = _ConnectorTransport()
    connector = ImapConnector(_definition(), transport)

    batch = connector.discover(None)
    artifact = connector.fetch(batch.items[0])

    assert transport.discover_calls == [(None, 2)]
    assert transport.fetch_calls == [(7, "validity-1", 64)]
    assert artifact.media_type == "message/rfc822"


def test_imap_connector_injects_explicit_credentials_without_ambient_names() -> None:
    credentials = ImapCredentials(username=SecretStr("mail-user"), password=SecretStr("mail-password"))

    connector = ImapConnector(_definition(), credentials=credentials)
    transport = connector._transport  # pyright: ignore[reportPrivateUsage]  # Pins the credential injection seam.
    if not isinstance(transport, ImapLibTransport):
        pytest.fail("IMAP connector did not construct its production transport")

    assert (transport._username, transport._password) == (  # pyright: ignore[reportPrivateUsage]  # Values must cross only into the transport boundary.
        "mail-user",
        "mail-password",
    )
