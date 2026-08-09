from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from money_pit.claims.projection import ClaimFreshnessRule
from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.repository import ClaimRepository
from money_pit.claims.repository import deterministic_claim_key
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import ClaimResolutionKind
from money_pit.schemas.claims import HorizonClass
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


_CUTOFF = datetime(2026, 8, 1, tzinfo=UTC)


def _observation(identifier: str, *, known_at: datetime) -> ClaimObservation:
    return ClaimObservation(
        observation_id=identifier,
        claim_text=f"Observation {identifier}",
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.FUNDAMENTAL,
        source_item_id=f"source:{identifier}",
        evidence_fragment_ids=(f"fragment-{identifier}",),
        asserted_at=_CUTOFF,
        known_at=known_at,
        effective_from=_CUTOFF,
        valid_until=_CUTOFF + timedelta(days=30),
        horizon_class=HorizonClass.TACTICAL,
        instruments=("NEW",),
    )


@pytest.fixture
def repository_with_concurrent_records(tmp_path: Path) -> ClaimRepository:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()
    observations = (
        _observation("baseline", known_at=_CUTOFF),
        _observation("same-run", known_at=_CUTOFF + timedelta(minutes=2)),
        _observation("concurrent", known_at=_CUTOFF + timedelta(minutes=1)),
    )
    canonical_key = deterministic_claim_key(observations[0].claim_text)
    with database.transaction(TransactionMode.WRITE) as connection:
        for observation in observations:
            _ = connection.execute(
                """INSERT INTO claim_observations (
                    observation_id, claim_text, source_item_id, asserted_at,
                    known_at, effective_from, event_at, review_at, valid_until,
                    horizon_class, observation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.observation_id,
                    observation.claim_text,
                    observation.source_item_id,
                    observation.asserted_at.isoformat(),
                    observation.known_at.isoformat(),
                    observation.effective_from.isoformat() if observation.effective_from else None,
                    None,
                    None,
                    observation.valid_until.isoformat() if observation.valid_until else None,
                    observation.horizon_class,
                    observation.model_dump_json(),
                ),
            )
        resolution_specs = (
            ("resolution-baseline", "baseline", None, ClaimResolutionKind.DISTINCT, _CUTOFF),
            (
                "resolution-same-run",
                "same-run",
                "baseline",
                ClaimResolutionKind.SAME,
                _CUTOFF + timedelta(minutes=2),
            ),
            (
                "resolution-concurrent",
                "concurrent",
                "baseline",
                ClaimResolutionKind.SAME,
                _CUTOFF + timedelta(minutes=1),
            ),
        )
        for identifier, subject, object_identifier, relation, known_at in resolution_specs:
            decision = ClaimResolutionDecision(
                decision_id=identifier,
                subject_observation_id=subject,
                object_observation_id=object_identifier,
                relation=relation,
                decided_at=known_at,
                known_at=known_at,
                resolver_version="resolver-1",
                rationale="Fixture membership",
            )
            _ = connection.execute(
                """INSERT INTO claim_resolution_decisions (
                    decision_id, subject_observation_id, object_observation_id,
                    relation, resolved_canonical_claim_key, decided_at, known_at,
                    decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.decision_id,
                    subject,
                    object_identifier,
                    relation,
                    canonical_key,
                    known_at.isoformat(),
                    known_at.isoformat(),
                    decision.model_dump_json(),
                ),
            )
    rule = ClaimFreshnessRule(
        review_interval=timedelta(days=1),
        freshness_interval=timedelta(days=7),
    )
    return ClaimRepository(
        database,
        refresh_policy=ClaimRefreshPolicy(
            policy_version="freshness-1",
            rules={category: dict.fromkeys(HorizonClass, rule) for category in ClaimCategory},
        ),
    )


def test_projections_with_observation_deltas_admits_only_named_same_run_records(
    repository_with_concurrent_records: ClaimRepository,
) -> None:
    projections = repository_with_concurrent_records.projections_with_deltas(
        requested_as_of=_CUTOFF,
        observation_ids=("same-run",),
        resolution_ids=("resolution-same-run",),
        verification_ids=(),
    )

    assert projections[0].active_observation_ids == ("baseline", "same-run")
