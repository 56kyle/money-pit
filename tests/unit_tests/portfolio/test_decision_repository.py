from datetime import UTC
from datetime import datetime
from pathlib import Path

from money_pit.portfolio.decision_repository import DecisionSnapshotRepository
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.snapshots import DecisionSnapshot
from money_pit.schemas.snapshots import DecisionSnapshotPayload
from money_pit.schemas.snapshots import SnapshotBinding
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.runs import RunRepository


_RUN_ID = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
_DECISION_AT = datetime(2026, 8, 9, 15, tzinfo=UTC)


def test_decision_snapshot_round_trip_preserves_absent_execution_config(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    RunRepository(database).append_run(
        RunRecord(
            run_id=_RUN_ID,
            requested_as_of=_DECISION_AT,
            started_at=_DECISION_AT,
            known_at=_DECISION_AT,
            through_stage="A5",
            source_config_hash="a" * 64,
            intelligence_config_hash="b" * 64,
            portfolio_config_hash="c" * 64,
        )
    )
    snapshot_ids = {
        "portfolio_state_snapshots": "portfolio",
        "market_state_snapshots": "market",
        "risk_state_snapshots": "risk",
        "liquidity_state_snapshots": "liquidity",
        "tax_state_snapshots": "tax",
    }
    with database.transaction(TransactionMode.WRITE) as connection:
        for table, snapshot_id in snapshot_ids.items():
            _ = connection.execute(
                f"INSERT INTO {table} (snapshot_id, captured_at, payload_json) VALUES (?, ?, ?)",  # noqa: S608
                (snapshot_id, _DECISION_AT.isoformat(), "{}"),
            )
    payload = DecisionSnapshotPayload(
        run_id=_RUN_ID,
        requested_as_of=_DECISION_AT,
        decision_at=_DECISION_AT,
        known_at=_DECISION_AT,
        evidence_fragment_ids=(),
        claim_observation_ids=(),
        verification_result_ids=(),
        thesis_revision_ids=(),
        canonical_projection_hashes={},
        portfolio_snapshot=SnapshotBinding(snapshot_id="portfolio", captured_at=_DECISION_AT),
        market_snapshot=SnapshotBinding(snapshot_id="market", captured_at=_DECISION_AT),
        risk_snapshot=SnapshotBinding(snapshot_id="risk", captured_at=_DECISION_AT),
        liquidity_snapshot=SnapshotBinding(snapshot_id="liquidity", captured_at=_DECISION_AT),
        tax_snapshot=SnapshotBinding(snapshot_id="tax", captured_at=_DECISION_AT),
        source_config_hash="a" * 64,
        strategy_config_hash="c" * 64,
        execution_config_hash=None,
        policy_version="portfolio-policy-1",
        claim_freshness_policy_version="freshness-1",
        universe_fingerprint="d" * 64,
        calibration_version="calibration-1",
        optimizer_version="optimizer-1",
        trade_generation_version="trades-1",
        execution_eligible=False,
    )
    snapshot = DecisionSnapshot.from_payload("decision-1", payload)
    repository = DecisionSnapshotRepository(database)

    repository.append(snapshot)

    stored = repository.get(snapshot.decision_snapshot_id)
    assert stored == snapshot
    assert stored is not None
    assert stored.payload.execution_config_hash is None
