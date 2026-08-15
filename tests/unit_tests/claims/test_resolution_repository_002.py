from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from money_pit.claims.projection import ClaimFreshnessRule
from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.repository import ClaimRepository
from money_pit.claims.repository import deterministic_claim_key
from money_pit.contracts import ResolutionDraft
from money_pit.pipeline.synthesis import materialize_resolution
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import ClaimResolutionKind
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


_REQUESTED_AS_OF = datetime(2026, 8, 1, 12, tzinfo=UTC)
_SAME_RUN_TIME = _REQUESTED_AS_OF + timedelta(minutes=2)
_CONCURRENT_TIME = _REQUESTED_AS_OF + timedelta(minutes=1)


def _observation(
    observation_id: str,
    claim_text: str,
    *,
    known_at: datetime = _REQUESTED_AS_OF,
    instruments: tuple[str, ...] = ("NEW",),
) -> ClaimObservation:
    return ClaimObservation(
        observation_id=observation_id,
        claim_text=claim_text,
        claim_kind=ClaimKind.FACTUAL,
        category=ClaimCategory.FUNDAMENTAL,
        source_item_id=f"source:{observation_id}",
        evidence_fragment_ids=(f"fragment:{observation_id}",),
        asserted_at=known_at,
        known_at=known_at,
        effective_from=known_at,
        valid_until=known_at + timedelta(days=30),
        horizon_class=HorizonClass.TACTICAL,
        instruments=instruments,
    )


def _resolution(
    decision_id: str,
    subject_id: str,
    object_id: str | None,
    relation: ClaimResolutionKind,
    *,
    known_at: datetime = _REQUESTED_AS_OF,
) -> ClaimResolutionDecision:
    return ClaimResolutionDecision(
        decision_id=decision_id,
        subject_observation_id=subject_id,
        object_observation_id=object_id,
        relation=relation,
        decided_at=known_at,
        known_at=known_at,
        resolver_version="resolver-002",
        rationale="The relation was established from the two source interpretations.",
    )


def _verification(
    verification_id: str,
    observation_id: str,
    status: VerificationStatus,
    *,
    known_at: datetime,
) -> VerificationResult:
    return VerificationResult(
        verification_id=verification_id,
        observation_id=observation_id,
        status=status,
        checked_at=known_at,
        known_at=known_at,
        verifier_version="verifier-002",
    )


@pytest.fixture
def claim_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "claims.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def claim_refresh_policy() -> ClaimRefreshPolicy:
    rule = ClaimFreshnessRule(
        review_interval=timedelta(days=7),
        freshness_interval=timedelta(days=30),
    )
    return ClaimRefreshPolicy(
        policy_version="test-policy-002",
        rules={category: dict.fromkeys(HorizonClass, rule) for category in ClaimCategory},
    )


@pytest.fixture
def claim_repository(
    claim_database: Database,
    claim_refresh_policy: ClaimRefreshPolicy,
) -> ClaimRepository:
    return ClaimRepository(claim_database, refresh_policy=claim_refresh_policy)


def _insert_observations(
    database: Database,
    *observations: ClaimObservation,
) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        for observation in observations:
            _ = connection.execute(
                """
                INSERT INTO claim_observations (
                    observation_id, claim_text, source_item_id, asserted_at, known_at,
                    effective_from, event_at, review_at, valid_until, horizon_class,
                    observation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.claim_text,
                    observation.source_item_id,
                    observation.asserted_at.isoformat(),
                    observation.known_at.isoformat(),
                    observation.effective_from.isoformat() if observation.effective_from else None,
                    observation.event_at.isoformat() if observation.event_at else None,
                    observation.review_at.isoformat() if observation.review_at else None,
                    observation.valid_until.isoformat() if observation.valid_until else None,
                    observation.horizon_class.value,
                    observation.model_dump_json(),
                ),
            )
            _ = connection.execute(
                "INSERT INTO claim_observation_search (observation_id, claim_text) VALUES (?, ?)",
                (observation.observation_id, observation.claim_text),
            )


def _insert_verifications(
    database: Database,
    *verifications: VerificationResult,
) -> None:
    with database.transaction(TransactionMode.WRITE) as connection:
        for verification in verifications:
            _ = connection.execute(
                """
                INSERT INTO verification_results (
                    verification_id, observation_id, status, checked_at, known_at,
                    valid_until, verification_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verification.verification_id,
                    verification.observation_id,
                    verification.status.value,
                    verification.checked_at.isoformat(),
                    verification.known_at.isoformat(),
                    verification.valid_until.isoformat() if verification.valid_until is not None else None,
                    verification.model_dump_json(),
                ),
            )


def test_projections_as_of_requires_resolution_controlled_membership(
    claim_database: Database,
    claim_repository: ClaimRepository,
) -> None:
    _insert_observations(claim_database, _observation("unresolved", "Revenue increased ten percent"))

    projections = claim_repository.projections_as_of(as_of=_REQUESTED_AS_OF)

    assert projections == ()


def test_unresolved_observation_page_excludes_resolved_and_uses_stable_cursor(
    claim_database: Database,
    claim_repository: ClaimRepository,
) -> None:
    anchor = _observation("anchor", "Revenue increased ten percent")
    same = _observation("same", "Revenue rose by ten percent")
    unresolved = _observation("unresolved", "Operating margin declined")
    unresolved_second = _observation("unresolved-second", "Free cash flow improved")
    _insert_observations(claim_database, anchor, same, unresolved, unresolved_second)
    claim_repository.append_resolution(_resolution("same-decision", "same", "anchor", ClaimResolutionKind.SAME))

    first_page = claim_repository.unresolved_observation_page(
        requested_as_of=_REQUESTED_AS_OF,
        limit=1,
    )
    assert first_page.next_cursor is not None
    second_page = claim_repository.unresolved_observation_page(
        requested_as_of=_REQUESTED_AS_OF,
        after=first_page.next_cursor,
        limit=1,
    )

    assert tuple(item.observation_id for item in first_page.items) == ("unresolved",)
    assert tuple(item.observation_id for item in second_page.items) == ("unresolved-second",)
    assert second_page.next_cursor is None


def test_resolution_candidates_returns_only_bounded_visible_matches(
    claim_database: Database,
    claim_repository: ClaimRepository,
) -> None:
    subject = _observation("subject", "Acme revenue growth accelerated", known_at=_SAME_RUN_TIME)
    first = _observation("first", "Acme revenue growth improved")
    second = _observation("second", "Acme reported revenue growth")
    unrelated = _observation("unrelated", "Policy rates remained unchanged")
    future = _observation("future", "Acme revenue growth slowed", known_at=_CONCURRENT_TIME)
    _insert_observations(claim_database, subject, first, second, unrelated, future)

    candidates = claim_repository.resolution_candidates(
        "subject",
        requested_as_of=_REQUESTED_AS_OF,
        limit=1,
    )

    assert tuple(item.observation_id for item in candidates) == ("first",)


def test_resolution_candidates_rejects_common_word_only_matches(
    claim_database: Database,
    claim_repository: ClaimRepository,
) -> None:
    subject = _observation(
        "subject",
        "Michael Burry added to a short position in Nebius, Micron, and Oracle",
        instruments=("NEBIUS", "MICRON", "ORACLE"),
    )
    relevant = _observation(
        "relevant",
        "Burry increased bearish positions in Nebius and Oracle",
        instruments=("NEBIUS", "ORACLE"),
    )
    common_words_only = _observation(
        "common-words-only",
        "Investors also added to positions while rates remained unchanged",
        instruments=("BONDS",),
    )
    _insert_observations(claim_database, subject, relevant, common_words_only)

    candidates = claim_repository.resolution_candidates(
        subject.observation_id,
        requested_as_of=_REQUESTED_AS_OF,
        limit=20,
    )

    assert tuple(item.observation_id for item in candidates) == (relevant.observation_id,)


def test_latest_verifications_for_observations_unions_only_exact_same_run_delta(
    claim_database: Database,
    claim_repository: ClaimRepository,
) -> None:
    first = _observation("first", "First statement")
    second = _observation("second", "Second statement")
    _insert_observations(claim_database, first, second)
    baseline = _verification(
        "baseline",
        first.observation_id,
        VerificationStatus.SUPPORTED,
        known_at=_REQUESTED_AS_OF,
    )
    same_run = _verification(
        "same-run",
        first.observation_id,
        VerificationStatus.CONTRADICTED,
        known_at=_SAME_RUN_TIME,
    )
    concurrent = _verification(
        "concurrent",
        second.observation_id,
        VerificationStatus.CONTRADICTED,
        known_at=_CONCURRENT_TIME,
    )
    _insert_verifications(claim_database, baseline, same_run, concurrent)

    latest = claim_repository.latest_verifications_for_observations(
        (first.observation_id, second.observation_id),
        requested_as_of=_REQUESTED_AS_OF,
        same_run_verification_ids=(same_run.verification_id,),
    )

    assert latest == {first.observation_id: same_run}


@pytest.mark.parametrize(
    ("relation", "expected_key", "expected_active_ids", "expected_status"),
    [
        (
            ClaimResolutionKind.SAME,
            deterministic_claim_key("Anchor statement"),
            ("anchor", "subject"),
            ClaimStatus.ACTIVE,
        ),
        (
            ClaimResolutionKind.CONTRADICTS,
            deterministic_claim_key("Anchor statement"),
            ("anchor", "subject"),
            ClaimStatus.DISPUTED,
        ),
        (
            ClaimResolutionKind.DISTINCT,
            deterministic_claim_key("Subject statement"),
            ("subject",),
            ClaimStatus.ACTIVE,
        ),
        (
            ClaimResolutionKind.UPDATES,
            deterministic_claim_key("Anchor statement"),
            ("subject",),
            ClaimStatus.ACTIVE,
        ),
    ],
)
def test_append_resolution_with_relation_semantics(
    claim_database: Database,
    claim_repository: ClaimRepository,
    relation: ClaimResolutionKind,
    expected_key: str,
    expected_active_ids: tuple[str, ...],
    expected_status: ClaimStatus,
) -> None:
    _insert_observations(
        claim_database,
        _observation("anchor", "Anchor statement"),
        _observation("subject", "Subject statement"),
    )
    object_id = None if relation is ClaimResolutionKind.DISTINCT else "anchor"
    claim_repository.append_resolution(_resolution("decision", "subject", object_id, relation))

    projections = claim_repository.projections_as_of(as_of=_REQUESTED_AS_OF)

    assert tuple(
        (item.canonical_claim_key, item.active_observation_ids, item.current_status) for item in projections
    ) == ((expected_key, expected_active_ids, expected_status),)


def test_append_resolution_with_first_observation_unary_distinct_creates_projection(
    claim_database: Database,
    claim_repository: ClaimRepository,
) -> None:
    _insert_observations(claim_database, _observation("first", "First canonical statement"))
    claim_repository.append_resolution(_resolution("first-decision", "first", None, ClaimResolutionKind.DISTINCT))

    projections = claim_repository.projections_as_of(as_of=_REQUESTED_AS_OF)

    assert tuple(item.active_observation_ids for item in projections) == (("first",),)


def test_claim_resolution_decision_rejects_binary_self_pair() -> None:
    with pytest.raises(ValidationError):
        _ = _resolution("self-decision", "subject", "subject", ClaimResolutionKind.SAME)


def test_materialize_resolution_with_first_observation_returns_unary_distinct() -> None:
    observation = _observation("first", "First canonical statement")
    decision = materialize_resolution(
        ResolutionDraft(
            subject_observation_id=observation.observation_id,
            relation=ClaimResolutionKind.DISTINCT,
            rationale="No bounded candidate represents the same canonical statement.",
        ),
        observations={observation.observation_id: observation},
        candidate_ids_by_subject={observation.observation_id: frozenset()},
        decision_at=_REQUESTED_AS_OF,
        known_at=_REQUESTED_AS_OF,
        resolver_version="resolver-002",
    )

    assert decision.object_observation_id is None


def test_materialize_resolution_rejects_binary_self_pair() -> None:
    observation = _observation("subject", "Subject statement")
    with pytest.raises(ValidationError):
        _ = materialize_resolution(
            ResolutionDraft(
                subject_observation_id=observation.observation_id,
                object_observation_id=observation.observation_id,
                relation=ClaimResolutionKind.SAME,
                rationale="Invalid self-comparison.",
            ),
            observations={observation.observation_id: observation},
            candidate_ids_by_subject={
                observation.observation_id: frozenset({observation.observation_id}),
            },
            decision_at=_REQUESTED_AS_OF,
            known_at=_REQUESTED_AS_OF,
            resolver_version="resolver-002",
        )


def test_projections_with_deltas_uses_only_exact_same_run_records(
    claim_database: Database,
    claim_repository: ClaimRepository,
) -> None:
    anchor = _observation("anchor", "Revenue increased")
    baseline = _observation("baseline", "Revenue rose")
    same_run = _observation("same-run", "Revenue growth accelerated", known_at=_SAME_RUN_TIME)
    concurrent = _observation("concurrent", "Revenue growth slowed", known_at=_CONCURRENT_TIME)
    _insert_observations(claim_database, anchor, baseline, same_run, concurrent)
    claim_repository.append_resolution(
        _resolution("baseline-resolution", "baseline", "anchor", ClaimResolutionKind.SAME)
    )
    claim_repository.append_resolution(
        _resolution(
            "concurrent-resolution",
            "concurrent",
            "anchor",
            ClaimResolutionKind.SAME,
            known_at=_CONCURRENT_TIME,
        )
    )
    claim_repository.append_resolution(
        _resolution(
            "same-run-resolution",
            "same-run",
            "anchor",
            ClaimResolutionKind.SAME,
            known_at=_SAME_RUN_TIME,
        )
    )
    _insert_verifications(
        claim_database,
        _verification(
            "concurrent-verification",
            "concurrent",
            VerificationStatus.CONTRADICTED,
            known_at=_CONCURRENT_TIME,
        ),
        _verification(
            "same-run-verification",
            "same-run",
            VerificationStatus.CONTRADICTED,
            known_at=_SAME_RUN_TIME,
        ),
    )

    projections = claim_repository.projections_with_deltas(
        requested_as_of=_REQUESTED_AS_OF,
        observation_ids=("same-run",),
        resolution_ids=("same-run-resolution",),
        verification_ids=("same-run-verification",),
    )

    assert tuple((item.active_observation_ids, item.current_status, item.projected_as_of) for item in projections) == (
        (("anchor", "baseline", "same-run"), ClaimStatus.DISPUTED, _SAME_RUN_TIME),
    )
