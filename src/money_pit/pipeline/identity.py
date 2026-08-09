"""Module deriving deterministic identifiers for immutable intelligence records."""

import hashlib
import json
import re
from collections.abc import Mapping


_WHITESPACE = re.compile(r"\s+")


def stable_identifier(namespace: str, values: Mapping[str, object]) -> str:
    """Return a namespaced SHA-256 identifier over canonical JSON values."""
    encoded: bytes = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()}"


def normalized_claim_text(value: str) -> str:
    """Normalize claim text for deterministic provisional canonical grouping."""
    return _WHITESPACE.sub(" ", value.strip()).casefold()
