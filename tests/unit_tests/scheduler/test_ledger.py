"""Unit tests for money_pit.scheduler.ledger — the persistent processed-episodes ledger (tmp-file I/O)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from money_pit.constants import default_processed_episodes_path
from money_pit.scheduler.ledger import ProcessedEpisode
from money_pit.scheduler.ledger import _load_records
from money_pit.scheduler.ledger import load_processed_ids
from money_pit.scheduler.ledger import record_processed


_SOURCE_ID: str = "yt:abc"
_SLUG: str = "2026-06-18_14-30-00"
_PROCESSED_AT: str = "2026-06-18T14:30:05Z"

_OTHER_SOURCE_ID: str = "yt:def"
_OTHER_SLUG: str = "2026-06-19_09-00-00"
_OTHER_PROCESSED_AT: str = "2026-06-19T09:00:03Z"


def test_load_processed_ids_with_missing_file(tmp_path: Path) -> None:
    assert load_processed_ids(tmp_path / "nope.json") == set()


def test_record_processed_then_load(tmp_path: Path) -> None:
    ledger = tmp_path / "state" / "processed_episodes.json"

    record_processed(ledger, _SOURCE_ID, _SLUG, processed_at=_PROCESSED_AT)

    assert ledger.parent.is_dir()
    assert load_processed_ids(ledger) == {_SOURCE_ID}


def test_record_processed_appends(tmp_path: Path) -> None:
    ledger = tmp_path / "processed_episodes.json"

    record_processed(ledger, _SOURCE_ID, _SLUG, processed_at=_PROCESSED_AT)
    record_processed(ledger, _OTHER_SOURCE_ID, _OTHER_SLUG, processed_at=_OTHER_PROCESSED_AT)

    assert load_processed_ids(ledger) == {_SOURCE_ID, _OTHER_SOURCE_ID}

    raw = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(raw) == 2

    records = [ProcessedEpisode.model_validate(entry) for entry in raw]
    assert records == [
        ProcessedEpisode(source_id=_SOURCE_ID, slug=_SLUG, processed_at=_PROCESSED_AT),
        ProcessedEpisode(source_id=_OTHER_SOURCE_ID, slug=_OTHER_SLUG, processed_at=_OTHER_PROCESSED_AT),
    ]


def test_processed_episode_is_frozen() -> None:
    episode = ProcessedEpisode(source_id=_SOURCE_ID, slug=_SLUG, processed_at=_PROCESSED_AT)

    with pytest.raises(ValidationError):
        episode.source_id = _OTHER_SOURCE_ID


def test_processed_episode_json_round_trip() -> None:
    episode = ProcessedEpisode(source_id=_SOURCE_ID, slug=_SLUG, processed_at=_PROCESSED_AT)

    assert ProcessedEpisode.model_validate_json(episode.model_dump_json()) == episode


@pytest.mark.parametrize("corrupt", ["{ not json", '{"source_id": "yt:abc"}'])
def test__load_records_with_malformed_ledger(tmp_path: Path, corrupt: str) -> None:
    ledger = tmp_path / "processed_episodes.json"
    ledger.write_text(corrupt, encoding="utf-8")

    with pytest.raises(ValidationError):
        _load_records(ledger)


@pytest.mark.parametrize("corrupt", ["{ not json", '{"source_id": "yt:abc"}'])
def test_load_processed_ids_with_malformed_ledger(tmp_path: Path, corrupt: str) -> None:
    ledger = tmp_path / "processed_episodes.json"
    ledger.write_text(corrupt, encoding="utf-8")

    with pytest.raises(ValidationError):
        load_processed_ids(ledger)


def test_default_processed_episodes_path() -> None:
    assert default_processed_episodes_path().name == "processed_episodes.json"
