"""Module containing shared bounded connector behavior."""

import hashlib
import mimetypes
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.sources import RawArtifact
from money_pit.schemas.sources import SourceDefinition
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.errors import SourceContentTooLargeError
from money_pit.sources.errors import SourceMediaTypeError


DEFAULT_MAX_CONTENT_BYTES = 25 * 1024 * 1024


class BoundedConnectorConfig(BaseModel):
    """Shared connector limits parsed from adapter configuration."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    max_content_bytes: int = Field(default=DEFAULT_MAX_CONTENT_BYTES, gt=0)
    timeout_seconds: float = Field(default=20, gt=0, le=120)


def parse_config(
    definition: SourceDefinition,
    config_type: type[BoundedConnectorConfig],
) -> BoundedConnectorConfig:
    """Validate connector configuration at construction time."""
    try:
        return config_type.model_validate(definition.adapter_config)
    except ValueError as error:
        raise ConnectorConfigurationError(
            f"Invalid configuration for source {definition.source_id!r}",
        ) from error


def sha256_bytes(content: bytes) -> str:
    """Return the lowercase SHA-256 digest of content."""
    return hashlib.sha256(content).hexdigest()


def read_bounded(path: Path, maximum_bytes: int) -> bytes:
    """Read a file only when its declared and observed size fit the limit."""
    try:
        size: int = path.stat().st_size
    except OSError as error:
        raise SourceContentTooLargeError(f"Cannot inspect local source {path}") from error
    if size > maximum_bytes:
        raise SourceContentTooLargeError(f"Local source exceeds {maximum_bytes} bytes")
    try:
        content: bytes = path.read_bytes()
    except OSError as error:
        raise SourceContentTooLargeError(f"Cannot read local source {path}") from error
    if len(content) > maximum_bytes:
        raise SourceContentTooLargeError(f"Local source exceeds {maximum_bytes} bytes")
    return content


def require_media_type(media_type: str, accepted: tuple[str, ...]) -> None:
    """Reject content whose normalized media type is not accepted."""
    normalized: str = media_type.partition(";")[0].strip().lower()
    if not any(
        normalized == candidate or (candidate.endswith("/*") and normalized.startswith(candidate[:-1]))
        for candidate in accepted
    ):
        raise SourceMediaTypeError(f"Unsupported media type: {normalized}")


def guessed_media_type(path: Path, fallback: str) -> str:
    """Return a MIME type inferred from a local filename."""
    guessed: str | None = mimetypes.guess_type(path.name)[0]
    return guessed or fallback


def evidence_asset(artifact: RawArtifact) -> EvidenceAsset:
    """Create a durable-asset identity from a fetched artifact."""
    local_path: Path = Path(artifact.content_hash[:2], artifact.content_hash)
    return EvidenceAsset(
        asset_id=artifact.content_hash,
        content_hash=artifact.content_hash,
        media_type=artifact.media_type,
        source_item_id=artifact.source_item.source_item_id,
        local_path=local_path,
        retrieved_at=artifact.retrieved_at,
    )


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(tz=UTC)
