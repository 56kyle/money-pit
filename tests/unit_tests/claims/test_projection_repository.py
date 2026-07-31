import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.projection import EmptyClaimHistoryError
from money_pit.claims.projection import MixedCanonicalClaimError
from money_pit.claims.projection import NaiveProjectionTimeError
from money_pit.claims.projection import due_claim_keys
from money_pit.claims.projection import project_canonical_claim
from money_pit.claims.repository import ClaimNotFoundError
from money_pit.claims.repository import ClaimRepository
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _observation(
    observation_id: str = "observation",
    *,
    expires_at=None,
    recorded_at: datetime = NOW,
    supersedes=None,
) -> ClaimObservation:
    return ClaimObservation(
        observation_id=observation_id,
        canonical_claim_key="claim",
        claim_text="Revenue grew.",
        claim_kind=ClaimKind.FACTUAL,
        source_item_id="source:item",
        asserted_at=NOW,
        recorded_at=recorded_at,
        expires_at=expires_at,
        supersedes_observation_id=supersedes,
    )


def test_project_canonical_claim_marks_current_contradiction_disputed():
    verification = VerificationResult(
        verification_id="verification",
        observation_id="observation",
        status=VerificationStatus.CONTRADICTED,
        checked_at=NOW,
        recorded_at=NOW,
        verifier_version="v1",
    )

    projection = project_canonical_claim(
        (_observation(),),
        (verification,),
        as_of=NOW,
        refresh_policy=ClaimRefreshPolicy(),
    )

    assert projection.current_status is ClaimStatus.DISPUTED


def test_project_canonical_claim_expires_inactive_history():
    projection = project_canonical_claim(
        (_observation(expires_at=NOW),),
        (),
        as_of=NOW,
        refresh_policy=ClaimRefreshPolicy(),
    )

    assert projection.current_status is ClaimStatus.EXPIRED
    assert projection.active_observation_ids == ()


def test_project_canonical_claim_rejects_future_only_history():
    future = _observation().model_copy(update={"asserted_at": NOW + timedelta(days=1)})

    with pytest.raises(EmptyClaimHistoryError):
        project_canonical_claim(
            (future,),
            (),
            as_of=NOW,
            refresh_policy=ClaimRefreshPolicy(),
        )


def test_project_canonical_claim_excludes_late_backdated_observation():
    late_recorded_at = NOW + timedelta(days=1)
    late_correction = _observation(
        "late-correction",
        recorded_at=late_recorded_at,
        supersedes="observation",
    ).model_copy(update={"asserted_at": NOW - timedelta(days=1)})

    before_recording = project_canonical_claim(
        (_observation(), late_correction),
        (),
        as_of=NOW,
        refresh_policy=ClaimRefreshPolicy(),
    )
    after_recording = project_canonical_claim(
        (_observation(), late_correction),
        (),
        as_of=late_recorded_at,
        refresh_policy=ClaimRefreshPolicy(),
    )

    assert before_recording.active_observation_ids == ("observation",)
    assert after_recording.active_observation_ids == ("late-correction",)


def test_project_canonical_claim_delays_supersession_until_successor_is_effective():
    valid_from = NOW + timedelta(days=1)
    successor = _observation(
        "successor",
        supersedes="observation",
    ).model_copy(update={"valid_from": valid_from})

    before_validity = project_canonical_claim(
        (_observation(), successor),
        (),
        as_of=NOW,
        refresh_policy=ClaimRefreshPolicy(),
    )
    at_validity = project_canonical_claim(
        (_observation(), successor),
        (),
        as_of=valid_from,
        refresh_policy=ClaimRefreshPolicy(),
    )

    assert before_validity.active_observation_ids == ("observation",)
    assert at_validity.active_observation_ids == ("successor",)


def test_project_canonical_claim_excludes_late_backdated_verification():
    late_recorded_at = NOW + timedelta(days=1)
    verification = VerificationResult(
        verification_id="verification",
        observation_id="observation",
        status=VerificationStatus.CONTRADICTED,
        checked_at=NOW - timedelta(days=1),
        recorded_at=late_recorded_at,
        verifier_version="v1",
    )

    before_recording = project_canonical_claim(
        (_observation(),),
        (verification,),
        as_of=NOW,
        refresh_policy=ClaimRefreshPolicy(),
    )
    after_recording = project_canonical_claim(
        (_observation(),),
        (verification,),
        as_of=late_recorded_at,
        refresh_policy=ClaimRefreshPolicy(),
    )

    assert before_recording.current_status is ClaimStatus.ACTIVE
    assert after_recording.current_status is ClaimStatus.DISPUTED


def test_project_canonical_claim_rejects_naive_as_of():
    with pytest.raises(NaiveProjectionTimeError):
        project_canonical_claim(
            (_observation(),),
            (),
            as_of=datetime(2026, 7, 29),  # noqa: DTZ001
            refresh_policy=ClaimRefreshPolicy(),
        )


def test_project_canonical_claim_rejects_empty_history():
    with pytest.raises(EmptyClaimHistoryError):
        project_canonical_claim(
            (),
            (),
            as_of=NOW,
            refresh_policy=ClaimRefreshPolicy(),
        )


def test_project_canonical_claim_rejects_mixed_canonical_keys():
    other = _observation("other").model_copy(update={"canonical_claim_key": "other"})

    with pytest.raises(MixedCanonicalClaimError):
        project_canonical_claim(
            (_observation(), other),
            (),
            as_of=NOW,
            refresh_policy=ClaimRefreshPolicy(),
        )


def test_project_canonical_claim_refreshes_before_expiry():
    expires_at = NOW + timedelta(days=1)

    projection = project_canonical_claim(
        (_observation(expires_at=expires_at),),
        (),
        as_of=NOW,
        refresh_policy=ClaimRefreshPolicy(factual=timedelta(days=7)),
    )

    assert projection.next_refresh_at == expires_at


def test_due_claim_keys_filters_and_sorts():
    due = (
        CanonicalClaim(
            canonical_claim_key="b",
            current_status=ClaimStatus.ACTIVE,
            active_observation_ids=("b",),
            last_material_change_at=NOW,
            next_refresh_at=NOW,
        ),
        CanonicalClaim(
            canonical_claim_key="a",
            current_status=ClaimStatus.DISPUTED,
            active_observation_ids=("a",),
            last_material_change_at=NOW,
            next_refresh_at=NOW,
        ),
        CanonicalClaim(
            canonical_claim_key="expired",
            current_status=ClaimStatus.EXPIRED,
            active_observation_ids=(),
            last_material_change_at=NOW,
            next_refresh_at=NOW,
        ),
    )

    assert due_claim_keys(due, as_of=NOW) == ("a", "b")


@pytest.fixture
def claim_repository(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    return ClaimRepository(database), database


def _insert_observation(database: Database, observation: ClaimObservation) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            """
            INSERT INTO claim_observations (
                observation_id, canonical_claim_key, claim_text, claim_kind,
                source_item_id, asserted_at, recorded_at, valid_from, horizon,
                expires_at, supersedes_observation_id, subjects_json,
                instruments_json, evidence_fragment_ids_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.observation_id,
                observation.canonical_claim_key,
                observation.claim_text,
                observation.claim_kind.value,
                observation.source_item_id,
                observation.asserted_at.isoformat(),
                observation.recorded_at.isoformat(),
                None,
                None,
                None,
                None,
                json.dumps(observation.subjects),
                json.dumps(observation.instruments),
                json.dumps(observation.evidence_fragment_ids),
            ),
        )


def test_refresh_persists_projection_and_history(claim_repository):
    repository, database = claim_repository
    _insert_observation(database, _observation())

    refreshed = repository.refresh("claim", as_of=NOW, policy=ClaimRefreshPolicy())
    observations, verifications = repository.history("claim")

    assert repository.get_projection("claim") == refreshed
    assert observations == (_observation(),)
    assert verifications == ()


def test_replay_does_not_overwrite_materialized_projection(claim_repository):
    repository, database = claim_repository
    late_recorded_at = NOW + timedelta(days=1)
    _insert_observation(database, _observation())
    _insert_observation(
        database,
        _observation(
            "late-correction",
            recorded_at=late_recorded_at,
            supersedes="observation",
        ).model_copy(update={"asserted_at": NOW - timedelta(days=1)}),
    )
    materialized = repository.refresh(
        "claim",
        as_of=late_recorded_at,
        policy=ClaimRefreshPolicy(),
    )

    replayed = repository.replay("claim", as_of=NOW, policy=ClaimRefreshPolicy())

    assert replayed.active_observation_ids == ("observation",)
    assert repository.get_projection("claim") == materialized


def test_refresh_all_excludes_claims_recorded_after_projection_time(claim_repository):
    repository, database = claim_repository
    _insert_observation(database, _observation())
    _insert_observation(
        database,
        _observation(
            "future",
            recorded_at=NOW + timedelta(days=1),
        ).model_copy(update={"canonical_claim_key": "future"}),
    )

    _ = repository.refresh(
        "future",
        as_of=NOW + timedelta(days=1),
        policy=ClaimRefreshPolicy(),
    )

    refreshed = repository.refresh_all(as_of=NOW, policy=ClaimRefreshPolicy())

    assert tuple(projection.canonical_claim_key for projection in refreshed) == ("claim",)
    assert repository.get_projection("future") is None


def test_refresh_all_uses_stable_claim_key_order(claim_repository):
    repository, database = claim_repository
    _insert_observation(database, _observation().model_copy(update={"canonical_claim_key": "z"}))
    _insert_observation(
        database,
        _observation("other").model_copy(update={"canonical_claim_key": "a"}),
    )

    refreshed = repository.refresh_all(as_of=NOW, policy=ClaimRefreshPolicy())

    assert tuple(projection.canonical_claim_key for projection in refreshed) == ("a", "z")


def test_history_with_unknown_claim_raises(claim_repository):
    repository, _ = claim_repository

    with pytest.raises(ClaimNotFoundError):
        repository.history("missing")


def test_refresh_with_unknown_claim_raises(claim_repository):
    repository, _ = claim_repository

    with pytest.raises(ClaimNotFoundError):
        repository.refresh("missing", as_of=NOW, policy=ClaimRefreshPolicy())


def test_list_projections_returns_stable_persisted_order(claim_repository):
    repository, database = claim_repository
    _insert_observation(database, _observation().model_copy(update={"canonical_claim_key": "z"}))
    _insert_observation(
        database,
        _observation("other").model_copy(update={"canonical_claim_key": "a"}),
    )
    repository.refresh_all(as_of=NOW, policy=ClaimRefreshPolicy())

    projections = repository.list_projections()

    assert tuple(projection.canonical_claim_key for projection in projections) == ("a", "z")
