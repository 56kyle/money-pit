"""Module containing immutable portfolio-decision persistence."""

import json
import sqlite3
from typing import cast

from pydantic import ValidationError

from money_pit.schemas.snapshots import DecisionSnapshot
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.errors import StorageError


class DecisionSnapshotRepositoryError(StorageError):
    """Base class for invalid durable decision-snapshot state."""


class ImmutableDecisionCollisionError(DecisionSnapshotRepositoryError):
    """Raised when a decision identifier or hash is bound to different content."""


class MalformedDecisionSnapshotRecordError(DecisionSnapshotRepositoryError):
    """Raised when stored decision content disagrees with indexed authority."""


class DecisionSnapshotRepository:
    """Append and retrieve hash-validated portfolio decisions."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to an initialized database."""
        self._database: Database = database

    def append(self, snapshot: DecisionSnapshot) -> None:
        """Persist one immutable decision idempotently."""
        payload = snapshot.payload
        with self._database.transaction(TransactionMode.WRITE) as connection:
            _ = connection.execute(
                """
                INSERT INTO decision_snapshots (
                    decision_snapshot_id, decision_hash, run_id, requested_as_of,
                    decision_at, known_at,
                    portfolio_snapshot_id, market_snapshot_id, risk_snapshot_id,
                    liquidity_snapshot_id, tax_snapshot_id, source_config_hash,
                    strategy_config_hash, execution_config_hash, policy_version,
                    claim_freshness_policy_version,
                    verification_result_ids_json, canonical_projection_hashes_json,
                    universe_fingerprint, processor_versions_json, calibration_version,
                    optimizer_version, trade_generation_version, execution_eligible,
                    model_versions_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    snapshot.decision_snapshot_id,
                    snapshot.decision_hash,
                    payload.run_id,
                    payload.requested_as_of.isoformat(),
                    payload.decision_at.isoformat(),
                    payload.known_at.isoformat(),
                    payload.portfolio_snapshot.snapshot_id,
                    payload.market_snapshot.snapshot_id,
                    payload.risk_snapshot.snapshot_id,
                    payload.liquidity_snapshot.snapshot_id,
                    payload.tax_snapshot.snapshot_id,
                    payload.source_config_hash,
                    payload.strategy_config_hash,
                    payload.execution_config_hash,
                    payload.policy_version,
                    payload.claim_freshness_policy_version,
                    json.dumps(payload.verification_result_ids),
                    json.dumps(payload.canonical_projection_hashes, sort_keys=True),
                    payload.universe_fingerprint,
                    json.dumps(payload.processor_versions, sort_keys=True),
                    payload.calibration_version,
                    payload.optimizer_version,
                    payload.trade_generation_version,
                    int(payload.execution_eligible),
                    json.dumps(payload.model_versions, sort_keys=True),
                    snapshot.model_dump_json(),
                ),
            )
            row: sqlite3.Row | None = _select_decision(connection, snapshot.decision_snapshot_id)
            if row is None:
                raise ImmutableDecisionCollisionError("decision hash is already bound to a different identifier")
            if _decision_from_row(row) != snapshot:
                raise ImmutableDecisionCollisionError("decision identifier is already bound to different content")

    def get(self, decision_snapshot_id: str) -> DecisionSnapshot | None:
        """Return one hash-validated decision snapshot when present."""
        with self._database.transaction() as connection:
            row: sqlite3.Row | None = _select_decision(connection, decision_snapshot_id)
        return None if row is None else _decision_from_row(row)


def _select_decision(connection: sqlite3.Connection, decision_snapshot_id: str) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT * FROM decision_snapshots WHERE decision_snapshot_id = ?",
            (decision_snapshot_id,),
        ).fetchone(),
    )


def _decision_from_row(row: sqlite3.Row) -> DecisionSnapshot:
    try:
        snapshot: DecisionSnapshot = DecisionSnapshot.model_validate_json(_text(row, "payload_json"))
        payload = snapshot.payload
        indexed: tuple[tuple[object, object], ...] = (
            (_text(row, "decision_snapshot_id"), snapshot.decision_snapshot_id),
            (_text(row, "decision_hash"), snapshot.decision_hash),
            (_text(row, "run_id"), payload.run_id),
            (_text(row, "requested_as_of"), payload.requested_as_of.isoformat()),
            (_text(row, "decision_at"), payload.decision_at.isoformat()),
            (_text(row, "known_at"), payload.known_at.isoformat()),
            (_text(row, "portfolio_snapshot_id"), payload.portfolio_snapshot.snapshot_id),
            (_text(row, "market_snapshot_id"), payload.market_snapshot.snapshot_id),
            (_text(row, "risk_snapshot_id"), payload.risk_snapshot.snapshot_id),
            (_text(row, "liquidity_snapshot_id"), payload.liquidity_snapshot.snapshot_id),
            (_text(row, "tax_snapshot_id"), payload.tax_snapshot.snapshot_id),
            (_text(row, "source_config_hash"), payload.source_config_hash),
            (_text(row, "strategy_config_hash"), payload.strategy_config_hash),
            (_optional_text(row, "execution_config_hash"), payload.execution_config_hash),
            (_text(row, "policy_version"), payload.policy_version),
            (_text(row, "claim_freshness_policy_version"), payload.claim_freshness_policy_version),
            (_text(row, "verification_result_ids_json"), json.dumps(payload.verification_result_ids)),
            (
                _text(row, "canonical_projection_hashes_json"),
                json.dumps(payload.canonical_projection_hashes, sort_keys=True),
            ),
            (_text(row, "universe_fingerprint"), payload.universe_fingerprint),
            (_text(row, "processor_versions_json"), json.dumps(payload.processor_versions, sort_keys=True)),
            (_text(row, "calibration_version"), payload.calibration_version),
            (_text(row, "optimizer_version"), payload.optimizer_version),
            (_text(row, "trade_generation_version"), payload.trade_generation_version),
            (_text(row, "model_versions_json"), json.dumps(payload.model_versions, sort_keys=True)),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise MalformedDecisionSnapshotRecordError("stored decision snapshot is malformed") from error
    if any(stored != canonical for stored, canonical in indexed):
        raise MalformedDecisionSnapshotRecordError("stored decision metadata disagrees with its payload")
    if bool(_integer(row, "execution_eligible")) is not payload.execution_eligible:
        raise MalformedDecisionSnapshotRecordError("stored execution eligibility disagrees with its payload")
    return snapshot


def _text(row: sqlite3.Row, name: str) -> str:
    value: object = cast("object", row[name])
    if not isinstance(value, str):
        raise TypeError(f"stored {name} must be text")
    return value


def _integer(row: sqlite3.Row, name: str) -> int:
    value: object = cast("object", row[name])
    if not isinstance(value, int):
        raise TypeError(f"stored {name} must be an integer")
    return value


def _optional_text(row: sqlite3.Row, name: str) -> str | None:
    value: object = cast("object", row[name])
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"stored {name} must be text or null")
    return value
