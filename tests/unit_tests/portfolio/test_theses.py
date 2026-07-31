"""Tests for durable living-thesis transitions and relevance."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast

import pytest
from pydantic import ValidationError


if TYPE_CHECKING:
    import sqlite3

from money_pit.portfolio.theses import InvalidThesisTransitionError
from money_pit.portfolio.theses import MalformedThesisRecordError
from money_pit.portfolio.theses import ThesisAlreadyExistsError
from money_pit.portfolio.theses import ThesisNotFoundError
from money_pit.portfolio.theses import ThesisRepository
from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import Thesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.theses import ThesisStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


@pytest.fixture
def thesis_now() -> datetime:
    return datetime(2026, 7, 29, 15, 0, tzinfo=UTC)


@pytest.fixture
def thesis(thesis_now: datetime) -> Thesis:
    return Thesis(
        thesis_id="thesis-1",
        instrument="AAPL",
        direction=ThesisDirection.LONG,
        horizon="three months",
        status=ThesisStatus.CANDIDATE,
        supporting_claim_keys=("claim-1",),
        contradicting_claim_keys=(),
        scenario_distribution=(ScenarioOutcome(name="base", probability=1.0, expected_return=0.08),),
        invalidation_rules=("guidance withdrawn",),
        confidence=0.7,
        created_at=thesis_now,
        reviewed_at=thesis_now,
        expires_at=thesis_now + timedelta(days=90),
    )


@pytest.fixture
def thesis_repository(database: Database) -> ThesisRepository:
    return ThesisRepository(database)


def test_append_round_trips_thesis(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
) -> None:
    thesis_repository.append(thesis)
    assert thesis_repository.get(thesis.thesis_id) == thesis


def test_thesis_with_review_before_creation_rejects_value(
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    with pytest.raises(ValidationError):
        Thesis.model_validate(
            thesis.model_dump()
            | {
                "created_at": thesis_now,
                "reviewed_at": thesis_now - timedelta(microseconds=1),
            }
        )


def test_thesis_with_review_at_creation_allows_value(
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    constructed: Thesis = Thesis.model_validate(
        thesis.model_dump()
        | {
            "created_at": thesis_now,
            "reviewed_at": thesis_now,
        }
    )
    assert constructed.reviewed_at == constructed.created_at


def test_append_with_duplicate_identifier_rejects_thesis(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
) -> None:
    thesis_repository.append(thesis)
    with pytest.raises(ThesisAlreadyExistsError):
        thesis_repository.append(thesis)


def test_get_with_missing_identifier_raises(
    thesis_repository: ThesisRepository,
) -> None:
    with pytest.raises(ThesisNotFoundError):
        thesis_repository.get("missing")


def test_transition_candidate_to_active_succeeds(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    thesis_repository.append(thesis)
    transitioned: Thesis = thesis_repository.transition(
        thesis.thesis_id,
        next_status=ThesisStatus.ACTIVE,
        reviewed_at=thesis_now + timedelta(days=1),
    )
    assert transitioned.status is ThesisStatus.ACTIVE


def test_transition_with_disallowed_edge_fails_closed(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    thesis_repository.append(thesis)
    with pytest.raises(InvalidThesisTransitionError):
        thesis_repository.transition(
            thesis.thesis_id,
            next_status=ThesisStatus.WEAKENED,
            reviewed_at=thesis_now + timedelta(days=1),
        )


def test_transition_with_naive_review_time_rejects_value(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
) -> None:
    thesis_repository.append(thesis)
    with pytest.raises(ValueError, match="datetime must be timezone-aware"):
        thesis_repository.transition(
            thesis.thesis_id,
            next_status=ThesisStatus.ACTIVE,
            reviewed_at=datetime(2026, 7, 30),  # noqa: DTZ001 - intentionally naive boundary input
        )


def test_active_returns_only_unexpired_active_theses(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    thesis_repository.append(thesis)
    active: Thesis = thesis_repository.transition(
        thesis.thesis_id,
        next_status=ThesisStatus.ACTIVE,
        reviewed_at=thesis_now + timedelta(days=1),
    )
    assert thesis_repository.active(as_of=thesis_now + timedelta(days=2)) == (active,)


def test_active_replays_status_before_and_after_activation_and_invalidation(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    thesis_repository.append(thesis)
    active: Thesis = thesis_repository.transition(
        thesis.thesis_id,
        next_status=ThesisStatus.ACTIVE,
        reviewed_at=thesis_now + timedelta(days=1),
    )
    invalidated: Thesis = thesis_repository.transition(
        thesis.thesis_id,
        next_status=ThesisStatus.INVALIDATED,
        reviewed_at=thesis_now + timedelta(days=3),
    )

    assert thesis_repository.active(as_of=thesis_now) == ()
    assert thesis_repository.active(as_of=thesis_now + timedelta(days=2)) == (active,)
    assert thesis_repository.active(as_of=thesis_now + timedelta(days=4)) == ()
    assert invalidated.status is ThesisStatus.INVALIDATED


def test_transition_with_backdated_review_time_preserves_history(
    database: Database,
    thesis_repository: ThesisRepository,
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    thesis_repository.append(thesis)

    with pytest.raises(InvalidThesisTransitionError):
        thesis_repository.transition(
            thesis.thesis_id,
            next_status=ThesisStatus.ACTIVE,
            reviewed_at=thesis_now - timedelta(seconds=1),
        )

    with database.transaction() as connection:
        history_row: sqlite3.Row | None = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT COUNT(*) FROM thesis_status_history WHERE thesis_id = ?",
                (thesis.thesis_id,),
            ).fetchone(),
        )
    assert history_row is not None
    history_count: int = cast("int", history_row[0])
    assert history_count == 1
    assert thesis_repository.get(thesis.thesis_id) == thesis


def test_expired_returns_thesis_at_expiry_boundary(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
) -> None:
    thesis_repository.append(thesis)
    assert thesis.expires_at is not None
    assert thesis_repository.expired(as_of=thesis.expires_at) == (thesis,)


def test_expired_replays_expiry_declared_at_each_transition(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    thesis_repository.append(thesis)
    active: Thesis = thesis_repository.transition(
        thesis.thesis_id,
        next_status=ThesisStatus.ACTIVE,
        reviewed_at=thesis_now + timedelta(days=1),
        expires_at=thesis_now + timedelta(days=5),
    )
    _ = thesis_repository.transition(
        thesis.thesis_id,
        next_status=ThesisStatus.WEAKENED,
        reviewed_at=thesis_now + timedelta(days=7),
        expires_at=thesis_now + timedelta(days=30),
    )

    assert thesis_repository.expired(as_of=thesis_now + timedelta(days=6)) == (active,)
    assert thesis_repository.expired(as_of=thesis_now + timedelta(days=8)) == ()


def test_append_round_trips_theme_subject(
    thesis_repository: ThesisRepository,
    thesis: Thesis,
) -> None:
    themed: Thesis = thesis.model_copy(update={"instrument": None, "theme": "AI infrastructure"})
    thesis_repository.append(themed)
    assert thesis_repository.get(themed.thesis_id).theme == "AI infrastructure"


def test_get_with_malformed_subject_fails_closed(
    database: Database,
    thesis_repository: ThesisRepository,
    thesis: Thesis,
) -> None:
    thesis_repository.append(thesis)
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            "UPDATE theses SET instrument_or_theme = ? WHERE thesis_id = ?",
            ("ambiguous", thesis.thesis_id),
        )
    with pytest.raises(MalformedThesisRecordError):
        thesis_repository.get(thesis.thesis_id)


def test_get_with_naive_durable_review_time_fails_closed(
    database: Database,
    thesis_repository: ThesisRepository,
    thesis: Thesis,
    thesis_now: datetime,
) -> None:
    thesis_repository.append(thesis)
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            "UPDATE theses SET reviewed_at = ? WHERE thesis_id = ?",
            (thesis_now.replace(tzinfo=None).isoformat(), thesis.thesis_id),
        )

    with pytest.raises(MalformedThesisRecordError):
        thesis_repository.get(thesis.thesis_id)
