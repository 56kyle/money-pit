"""Module deriving deterministic identifiers for immutable intelligence records."""

import hashlib
import json
import re
from collections.abc import Mapping
from collections.abc import Sequence


_WHITESPACE = re.compile(r"\s+")


def stable_identifier(namespace: str, values: Mapping[str, object]) -> str:
    """Return a namespaced SHA-256 identifier over canonical JSON values."""
    encoded: bytes = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()}"


def interpretation_policy_version(*, prompt_version: str, model: str) -> str:
    """Bind a durable interpreter version to its prompt and model policy."""
    return hashlib.sha256(f"{prompt_version}\0{model}".encode()).hexdigest()


def ordered_work_fingerprint(identifiers: Sequence[str]) -> str:
    """Bind incremental work to the exact ordered durable inputs."""
    encoded = json.dumps(tuple(identifiers), separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def interpretation_bundle_id(
    *,
    source_item_id: str,
    content_version: str,
    interpreter_version: str,
    input_fingerprint: str,
) -> str:
    """Return the shared semantic identity for one interpretation bundle."""
    return stable_identifier(
        "interpretation-bundle",
        {
            "source_item_id": source_item_id,
            "content_version": content_version,
            "interpreter_version": interpreter_version,
            "input_fingerprint": input_fingerprint,
        },
    )


def normalized_claim_text(value: str) -> str:
    """Normalize claim text for deterministic provisional canonical grouping."""
    return _WHITESPACE.sub(" ", value.strip()).casefold()
