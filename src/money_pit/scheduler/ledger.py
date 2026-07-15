"""Module containing the persistent processed-episodes ledger for the money_pit package."""

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import TypeAdapter


_ENCODING: str = "utf-8"
_JSON_INDENT: int = 2


class ProcessedEpisode(BaseModel):
    """One ledger record marking an episode as already run, keyed by its `yt:<id>` source id."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    slug: str
    processed_at: str


_LEDGER_ADAPTER: TypeAdapter[list[ProcessedEpisode]] = TypeAdapter(list[ProcessedEpisode])


def _load_records(path: Path) -> list[ProcessedEpisode]:
    """Return the ledger records at `path`, or an empty list when no ledger exists yet.

    A malformed or schema-drifted ledger raises loudly via the adapter, failing closed rather than
    silently resetting the record of what has already been run.
    """
    if not path.exists():
        return []
    return _LEDGER_ADAPTER.validate_json(path.read_text(encoding=_ENCODING))


def load_processed_ids(path: Path) -> set[str]:
    """Return the set of source ids already recorded in the ledger at `path`."""
    return {record.source_id for record in _load_records(path)}


def record_processed(path: Path, source_id: str, slug: str, *, processed_at: str) -> None:
    """Append a processed-episode record to the ledger at `path`, creating the ledger's parent folder.

    `processed_at` is an injected ISO timestamp string, keeping the write deterministic and testable.
    """
    records = _load_records(path)
    records.append(ProcessedEpisode(source_id=source_id, slug=slug, processed_at=processed_at))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_LEDGER_ADAPTER.dump_json(records, indent=_JSON_INDENT).decode(_ENCODING), encoding=_ENCODING)
