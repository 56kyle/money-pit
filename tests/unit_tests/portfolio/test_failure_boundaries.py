"""Remaining portfolio state, thesis, and universe failure contracts."""

from datetime import datetime
from datetime import timedelta

import pytest
from pydantic import ValidationError

from money_pit.portfolio.repository import MalformedSnapshotRecordError
from money_pit.portfolio.repository import SnapshotNotFoundError
from money_pit.portfolio.repository import SnapshotRepository
from money_pit.portfolio.snapshots import MarketQuote
from money_pit.portfolio.snapshots import MarketStatePayload
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.portfolio.theses import MalformedThesisRecordError
from money_pit.portfolio.theses import ThesisNotFoundError
from money_pit.portfolio.theses import ThesisRepository
from money_pit.portfolio.theses import _datetime_from_database
from money_pit.portfolio.theses import _encode_subject
from money_pit.portfolio.theses import _json_object_list
from money_pit.portfolio.theses import _json_string_tuple
from money_pit.portfolio.theses import _number_from_database
from money_pit.portfolio.theses import _optional_datetime_from_database
from money_pit.portfolio.universe import CandidateReference
from money_pit.portfolio.universe import build_layered_universe
from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import Thesis
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.theses import ThesisStatus
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode


def _market_payload(now: datetime) -> MarketStatePayload:
    return MarketStatePayload(
        captured_at=now,
        quotes=(
            MarketQuote(
                instrument="AAPL",
                price=200.0,
                observed_at=now,
                source="market-data",
            ),
        ),
    )


def _thesis(now: datetime) -> Thesis:
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
        created_at=now,
        reviewed_at=now,
        expires_at=now + timedelta(days=90),
    )


def test_portfolio_state_payload_rejects_unsorted_open_orders(now: datetime) -> None:
    with pytest.raises(ValidationError, match="open_order_ids"):
        PortfolioStatePayload(
            account_id="account-1",
            captured_at=now,
            available_cash=1_000.0,
            positions=(),
            open_order_ids=("order-2", "order-1"),
        )


def test_portfolio_state_snapshot_rejects_wrong_fingerprint(now: datetime) -> None:
    payload = PortfolioStatePayload(
        account_id="account-1",
        captured_at=now,
        available_cash=1_000.0,
        positions=(),
        open_order_ids=(),
    )

    with pytest.raises(ValidationError, match="does not match"):
        PortfolioStateSnapshot(snapshot_id="0" * 64, payload=payload)


def test_market_state_snapshot_rejects_wrong_fingerprint(now: datetime) -> None:
    with pytest.raises(ValidationError, match="does not match"):
        MarketStateSnapshot(snapshot_id="0" * 64, payload=_market_payload(now))


def test_snapshot_repository_missing_portfolio_raises(database: Database) -> None:
    with pytest.raises(SnapshotNotFoundError):
        SnapshotRepository(database).get_portfolio("missing")


def test_snapshot_repository_malformed_market_fails_closed(
    database: Database,
    now: datetime,
) -> None:
    repository = SnapshotRepository(database)
    snapshot = MarketStateSnapshot.from_payload(_market_payload(now))
    repository.append_market(snapshot)
    with database.transaction(TransactionMode.WRITE) as connection:
        connection.execute(
            "UPDATE market_state_snapshots SET payload_json = ? WHERE snapshot_id = ?",
            ("{}", snapshot.snapshot_id),
        )

    with pytest.raises(MalformedSnapshotRecordError):
        repository.get_market(snapshot.snapshot_id)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(
            {"instrument": None, "tradable": None, "observation_reason": None, "proxy_for": None},
            id="unresolved-reason",
        ),
        pytest.param(
            {"instrument": "AAPL", "tradable": None, "observation_reason": None, "proxy_for": None},
            id="resolved-decision",
        ),
        pytest.param(
            {"instrument": "AAPL", "tradable": False, "observation_reason": None, "proxy_for": None},
            id="nontradable-reason",
        ),
        pytest.param(
            {"instrument": None, "tradable": None, "observation_reason": "unresolved", "proxy_for": "Foreign Co."},
            id="proxy-instrument",
        ),
    ],
)
def test_validate_resolution_rejects_incomplete_reference(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CandidateReference(reference="source", **values)


def test_build_layered_universe_observation_restriction_overrides_tradable_candidate() -> None:
    tradable = CandidateReference(
        reference="holding",
        instrument="AAPL",
        tradable=True,
        observation_reason=None,
        proxy_for=None,
    )
    restricted = CandidateReference(
        reference="restriction",
        instrument="aapl",
        tradable=False,
        observation_reason="not approved",
        proxy_for=None,
    )

    universe = build_layered_universe(holdings=(tradable,), source_mentions=(restricted,))

    assert universe.instruments == ()


def test_transition_missing_thesis_fails_closed(database: Database, now: datetime) -> None:
    with pytest.raises(ThesisNotFoundError):
        ThesisRepository(database).transition(
            "missing",
            next_status=ThesisStatus.ACTIVE,
            reviewed_at=now,
        )


def test_transition_naive_expiry_fails_closed(database: Database, now: datetime) -> None:
    repository = ThesisRepository(database)
    thesis = _thesis(now)
    repository.append(thesis)

    with pytest.raises(ValueError, match="timezone-aware"):
        repository.transition(
            thesis.thesis_id,
            next_status=ThesisStatus.ACTIVE,
            reviewed_at=now,
            expires_at=datetime(2026, 8, 1),  # noqa: DTZ001 - intentionally naive boundary input
        )


def test__encode_subject_rejects_ambiguous_subject(now: datetime) -> None:
    malformed = _thesis(now).model_copy(update={"theme": "AI"})

    with pytest.raises(MalformedThesisRecordError):
        _encode_subject(malformed)


@pytest.mark.parametrize("value", ["{}", "[1]"])
def test__json_string_tuple_rejects_non_text_array(value: str) -> None:
    with pytest.raises(TypeError, match="array of text"):
        _json_string_tuple(value)


@pytest.mark.parametrize("value", ["{}", "[1]"])
def test__json_object_list_rejects_non_object_array(value: str) -> None:
    with pytest.raises(TypeError, match="array of objects"):
        _json_object_list(value)


def test__number_from_database_rejects_text() -> None:
    with pytest.raises(TypeError, match="numeric"):
        _number_from_database("0.7")


def test__datetime_from_database_rejects_non_text() -> None:
    with pytest.raises(TypeError, match="must be text"):
        _datetime_from_database(1)


def test__optional_datetime_from_database_preserves_missing_value() -> None:
    assert _optional_datetime_from_database(None) is None
