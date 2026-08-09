"""Module containing bounded local-file source connectors."""

import email
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Literal


if TYPE_CHECKING:
    from email.message import Message

from pydantic import ConfigDict

from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import PageLocator
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.sources import DiscoveryBatch
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.sources._shared import BoundedConnectorConfig
from money_pit.sources._shared import evidence_asset
from money_pit.sources._shared import guessed_media_type
from money_pit.sources._shared import parse_config
from money_pit.sources._shared import read_bounded
from money_pit.sources._shared import require_media_type
from money_pit.sources._shared import sha256_bytes
from money_pit.sources._shared import source_definition_hash
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import SourceExtractionError
from money_pit.sources.errors import SourceFetchError


class LocalConnectorConfig(BoundedConnectorConfig):
    """Configuration accepted by local-file connectors."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class LocalFileConnector:
    """Bounded connector for one immutable local file version."""

    def __init__(
        self,
        definition: SourceDefinition,
        accepted_media_types: tuple[str, ...],
        fallback_media_type: str,
        extraction_kind: Literal["text", "email", "binary"],
    ) -> None:
        """Bind one local path to explicit media and extraction policy."""
        self._definition: SourceDefinition = definition
        self._path: Path = Path(definition.locator).expanduser().resolve()
        self._config: LocalConnectorConfig = LocalConnectorConfig.model_validate(
            parse_config(definition, LocalConnectorConfig).model_dump(),
        )
        self._accepted_media_types: tuple[str, ...] = accepted_media_types
        self._fallback_media_type: str = fallback_media_type
        self._extraction_kind: Literal["text", "email", "binary"] = extraction_kind

    def discover(
        self,
        cursor: SourceCursor | None,
        *,
        purpose: SourceCursorPurpose = SourceCursorPurpose.SYNC,
    ) -> DiscoveryBatch:
        """Discover the configured file when its content version is new."""
        del purpose
        content: bytes = read_bounded(self._path, self._config.max_content_bytes)
        digest: str = sha256_bytes(content)
        now: datetime = utc_now()
        if cursor is not None and cursor.value == digest:
            return DiscoveryBatch(items=(), next_cursor=cursor, discovered_at=now)
        modified_at: datetime = datetime.fromtimestamp(self._path.stat().st_mtime).astimezone()
        item: SourceItem = SourceItem(
            source_item_id=f"{self._definition.source_id}:{digest}",
            source_id=self._definition.source_id,
            source_definition_hash=source_definition_hash(self._definition),
            canonical_uri=self._path.as_uri(),
            published_at=modified_at,
            updated_at=modified_at,
            discovered_at=now,
            content_version=digest,
        )
        return DiscoveryBatch(
            items=(item,),
            next_cursor=SourceCursor(value=digest),
            discovered_at=now,
        )

    def fetch(self, item: SourceItem) -> RawArtifact:
        """Read and hash the discovered local file version."""
        if item.source_id != self._definition.source_id:
            raise SourceFetchError("Source item does not belong to this connector")
        content: bytes = read_bounded(self._path, self._config.max_content_bytes)
        digest: str = sha256_bytes(content)
        if digest != item.content_version:
            raise SourceExtractionError("Local source changed after discovery")
        media_type: str = guessed_media_type(self._path, self._fallback_media_type)
        require_media_type(media_type, self._accepted_media_types)
        return RawArtifact(
            source_item=item,
            content=content,
            media_type=media_type,
            retrieved_at=utc_now(),
            canonical_uri=self._path.as_uri(),
            filename=self._path,
            content_hash=digest,
        )

    def extract(self, artifact: RawArtifact) -> EvidenceDocument:
        """Extract text or email bodies while retaining the original asset."""
        if self._extraction_kind == "binary":
            return EvidenceDocument(asset=evidence_asset(artifact), fragments=())
        text: str = (
            _extract_email_text(artifact.content)
            if self._extraction_kind == "email"
            else _decode_text(artifact.content)
        )
        fragment: EvidenceFragment = EvidenceFragment(
            fragment_id=f"{artifact.content_hash}:text",
            asset_id=artifact.content_hash,
            kind="page" if artifact.media_type == "application/pdf" else "web_span",
            locator=(
                PageLocator(page_number=1)
                if artifact.media_type == "application/pdf"
                else TextLocator(start_offset=0, end_offset=len(text))
            ),
            extracted_text=text,
            extraction_method="stdlib",
        )
        return EvidenceDocument(asset=evidence_asset(artifact), fragments=(fragment,))


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceExtractionError("Source text is not valid UTF-8") from error


def _extract_email_text(content: bytes) -> str:
    try:
        message: Message = email.message_from_bytes(content)
    except (TypeError, ValueError) as error:
        raise SourceExtractionError("Email message cannot be parsed") from error
    parts: list[str] = []
    messages: tuple[Message, ...] = tuple(message.walk()) if message.is_multipart() else (message,)
    for part in messages:
        if part.get_content_type() != "text/plain":
            continue
        payload_object: object = part.get_payload(decode=True)
        if isinstance(payload_object, bytes):
            charset: str = part.get_content_charset() or "utf-8"
            try:
                parts.append(payload_object.decode(charset))
            except (LookupError, UnicodeDecodeError) as error:
                raise SourceExtractionError("Email text cannot be decoded") from error
    return "\n\n".join(parts)


def local_text_connector(definition: SourceDefinition) -> LocalFileConnector:
    """Build a UTF-8 local text connector."""
    return LocalFileConnector(definition, ("text/*",), "text/plain", "text")


def local_audio_connector(definition: SourceDefinition) -> LocalFileConnector:
    """Build a local audio asset connector without implicit transcription."""
    return LocalFileConnector(definition, ("audio/*",), "application/octet-stream", "binary")


def local_pdf_connector(definition: SourceDefinition) -> LocalFileConnector:
    """Build a local PDF asset connector without lossy text inference."""
    return LocalFileConnector(definition, ("application/pdf",), "application/pdf", "binary")


def local_email_connector(definition: SourceDefinition) -> LocalFileConnector:
    """Build an RFC 5322 local email connector."""
    return LocalFileConnector(definition, ("message/rfc822",), "message/rfc822", "email")
