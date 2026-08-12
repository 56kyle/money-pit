"""Module containing immutable content-addressed assets for the money_pit package."""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from money_pit.storage.errors import AssetIntegrityError
from money_pit.storage.errors import AssetWriteError


_DIGEST_LENGTH: Final[int] = 64


@dataclass(frozen=True)
class StoredAsset:
    """A persisted asset identified by the SHA-256 of its complete content."""

    digest: str
    path: Path
    size_bytes: int


class AssetStore:
    """Persist immutable bytes under a full-SHA-256 content address."""

    def __init__(self, root: Path) -> None:
        """Bind the store to its immutable asset root."""
        self._root: Path = root

    @property
    def root(self) -> Path:
        """Return the asset-store root."""
        return self._root

    def path_for_digest(self, digest: str) -> Path:
        """Return the canonical path for a validated lowercase SHA-256 digest."""
        is_lowercase_hex: bool = all(character in "0123456789abcdef" for character in digest)
        if len(digest) != _DIGEST_LENGTH or not is_lowercase_hex:
            raise ValueError("digest must be a lowercase hexadecimal SHA-256 value.")
        return self._root / digest[:2] / digest

    def put_bytes(self, content: bytes) -> StoredAsset:
        """Atomically persist bytes and return their immutable content address."""
        digest: str = hashlib.sha256(content).hexdigest()
        destination: Path = self.path_for_digest(digest)
        if destination.exists():
            return self._verify_existing(destination, digest)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            file_descriptor: int
            temporary_name: str
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{digest}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                _ = temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            _ = temporary_path.replace(destination)
            temporary_path = None
        except OSError as error:
            raise AssetWriteError(f"Could not persist asset {digest}.") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return StoredAsset(digest=digest, path=destination, size_bytes=len(content))

    def read_bytes(self, digest: str, *, maximum_bytes: int) -> bytes | None:
        """Return verified bounded bytes, or ``None`` when the asset is absent."""
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive.")
        path: Path = self.path_for_digest(digest)
        if not path.exists():
            return None
        try:
            with path.open("rb") as asset_file:
                content: bytes = asset_file.read(maximum_bytes + 1)
        except OSError as error:
            raise AssetWriteError(f"Could not read existing asset {digest}.") from error
        if len(content) > maximum_bytes:
            raise AssetIntegrityError(
                f"Existing asset {digest} exceeds the configured byte bound.",
            )
        if hashlib.sha256(content).hexdigest() != digest:
            raise AssetIntegrityError(f"Existing asset at {path} does not match digest {digest}.")
        return content

    def _verify_existing(self, path: Path, digest: str) -> StoredAsset:
        """Return an existing asset after checking its full content."""
        try:
            existing_content: bytes = path.read_bytes()
        except OSError as error:
            raise AssetWriteError(f"Could not read existing asset {digest}.") from error
        existing_digest: str = hashlib.sha256(existing_content).hexdigest()
        if existing_digest != digest:
            raise AssetIntegrityError(f"Existing asset at {path} does not match digest {digest}.")
        return StoredAsset(digest=digest, path=path, size_bytes=len(existing_content))
