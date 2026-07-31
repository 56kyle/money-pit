import hashlib

import pytest

from money_pit.storage.assets import AssetStore
from money_pit.storage.errors import AssetIntegrityError


INVALID_DIGEST_ERROR = "digest must be a lowercase hexadecimal SHA-256 value"


def test_put_bytes_is_content_addressed_and_idempotent(tmp_path):
    store = AssetStore(tmp_path / "assets")
    content = b"immutable evidence"
    expected_digest = hashlib.sha256(content).hexdigest()

    first = store.put_bytes(content)
    second = store.put_bytes(content)

    assert first == second
    assert first.digest == expected_digest
    assert first.path == tmp_path / "assets" / expected_digest[:2] / expected_digest
    assert first.path.read_bytes() == content


def test_put_bytes_rejects_tampered_existing_asset(tmp_path):
    store = AssetStore(tmp_path / "assets")
    stored = store.put_bytes(b"original")
    stored.path.write_bytes(b"tampered")

    with pytest.raises(AssetIntegrityError):
        store.put_bytes(b"original")


@pytest.mark.parametrize("digest", ["ABC", "g" * 64, "0" * 63, "A" * 64])
def test_path_for_digest_with_invalid_digest(tmp_path, digest):
    store = AssetStore(tmp_path / "assets")

    with pytest.raises(ValueError, match=INVALID_DIGEST_ERROR):
        store.path_for_digest(digest)
