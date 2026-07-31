# pyright: reportPrivateUsage=false, reportUnusedCallResult=false

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from money_pit.claims.repository import MalformedClaimRecordError
from money_pit.claims.repository import _datetime_from_database
from money_pit.claims.repository import _json_string_tuple
from money_pit.claims.repository import _load_observations
from money_pit.claims.repository import _load_verifications
from money_pit.claims.repository import _optional_text
from money_pit.claims.repository import _projection_from_row
from money_pit.claims.repository import _require_aware


def _row(values: dict[str, object]) -> sqlite3.Row:
    return cast("sqlite3.Row", cast("object", values))


@pytest.mark.parametrize("value", ["{}", '["valid", 1]'])
def test__json_string_tuple_rejects_non_string_array(value: str) -> None:
    with pytest.raises(TypeError):
        _json_string_tuple(value)


def test__datetime_from_database_rejects_non_text() -> None:
    with pytest.raises(TypeError, match="datetime must be text"):
        _datetime_from_database(1)


def test__optional_text_rejects_non_text() -> None:
    with pytest.raises(TypeError, match="optional text"):
        _optional_text(1)


def test__optional_text_returns_text() -> None:
    assert _optional_text("horizon") == "horizon"


def test__require_aware_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _require_aware(datetime(2026, 7, 29), field_name="test")  # noqa: DTZ001


def test__load_verifications_with_no_observations_returns_empty(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.sqlite3")
    try:
        result = _load_verifications(connection, ())
    finally:
        connection.close()

    assert result == ()


def test__projection_from_row_wraps_invalid_status() -> None:
    row = _row(
        {
            "canonical_claim_key": "claim",
            "current_status": "invalid",
            "active_observation_ids_json": "[]",
            "last_material_change_at": "2026-07-29T00:00:00+00:00",
            "next_refresh_at": None,
        }
    )

    with pytest.raises(MalformedClaimRecordError, match="canonical claim"):
        _projection_from_row(row)


def test__load_observations_wraps_malformed_durable_row(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE claim_observations (
                observation_id TEXT, canonical_claim_key TEXT, claim_text TEXT,
                claim_kind TEXT, subjects_json TEXT, instruments_json TEXT,
                source_item_id TEXT, evidence_fragment_ids_json TEXT,
                asserted_at TEXT, recorded_at TEXT, valid_from TEXT,
                horizon TEXT, expires_at TEXT, supersedes_observation_id TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO claim_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "observation",
                "claim",
                "text",
                "not-a-kind",
                "[]",
                "[]",
                "source:item",
                "[]",
                "2026-07-29T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
                None,
                None,
                None,
                None,
            ),
        )

        with pytest.raises(MalformedClaimRecordError, match="claim observation"):
            _load_observations(connection, "claim")
    finally:
        connection.close()


def test__load_verifications_wraps_malformed_durable_row(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE verification_results (
                verification_id TEXT, observation_id TEXT, status TEXT,
                supporting_evidence_ids_json TEXT,
                contradicting_evidence_ids_json TEXT, checked_at TEXT,
                recorded_at TEXT, valid_until TEXT, verifier_version TEXT,
                limitations_json TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO verification_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "verification",
                "observation",
                "not-a-status",
                "[]",
                "[]",
                "2026-07-29T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
                None,
                "v1",
                "[]",
            ),
        )

        with pytest.raises(MalformedClaimRecordError, match="verification result"):
            _load_verifications(connection, ("observation",))
    finally:
        connection.close()
