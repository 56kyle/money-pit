from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
import typer
from pytest import CaptureFixture
from pytest import MonkeyPatch

from money_pit.claims import cli
from money_pit.claims.repository import ClaimNotFoundError
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus
from money_pit.storage.errors import StorageTransactionError


NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _projection(*, next_refresh_at: datetime | None = NOW) -> CanonicalClaim:
    return CanonicalClaim(
        canonical_claim_key="claim",
        current_status=ClaimStatus.ACTIVE,
        active_observation_ids=("observation",),
        last_material_change_at=NOW,
        next_refresh_at=next_refresh_at,
    )


def _observation() -> ClaimObservation:
    return ClaimObservation(
        observation_id="observation",
        canonical_claim_key="claim",
        claim_text="Revenue grew.",
        claim_kind=ClaimKind.FACTUAL,
        source_item_id="source:item",
        asserted_at=NOW,
        recorded_at=NOW,
    )


def _verification() -> VerificationResult:
    return VerificationResult(
        verification_id="verification",
        observation_id="observation",
        status=VerificationStatus.SUPPORTED,
        checked_at=NOW,
        recorded_at=NOW,
        verifier_version="v1",
    )


class _ClaimRepository:
    def __init__(self, *, projection: CanonicalClaim | None = None) -> None:
        self.projection: CanonicalClaim | None = projection
        self.observations: list[ClaimObservation] = []
        self.verifications: list[VerificationResult] = []

    def append_observation(self, observation: ClaimObservation) -> None:
        self.observations.append(observation)

    def append_verification(self, verification: VerificationResult) -> None:
        self.verifications.append(verification)

    def list_projections(self) -> tuple[CanonicalClaim, ...]:
        return (_projection(), _projection(next_refresh_at=None))

    def get_projection(self, canonical_claim_key: str) -> CanonicalClaim | None:
        _ = canonical_claim_key
        return self.projection

    def history(
        self,
        canonical_claim_key: str,
    ) -> tuple[tuple[ClaimObservation, ...], tuple[VerificationResult, ...]]:
        _ = canonical_claim_key
        return (_observation(),), (_verification(),)

    def refresh(self, canonical_claim_key: str, **_kwargs: object) -> CanonicalClaim:
        _ = canonical_claim_key
        return _projection()

    def refresh_all(self, **_kwargs: object) -> tuple[CanonicalClaim, ...]:
        return (_projection(),)


def test_append_observation_reads_validated_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository = _ClaimRepository()
    input_path: Path = tmp_path / "observation.json"
    _ = input_path.write_text(_observation().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cli, "_claim_repository", lambda: repository)

    cli.append_observation(input_path)

    assert repository.observations == [_observation()]


def test_append_verification_reads_validated_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository = _ClaimRepository()
    input_path: Path = tmp_path / "verification.json"
    _ = input_path.write_text(_verification().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cli, "_claim_repository", lambda: repository)

    cli.append_verification(input_path)

    assert repository.verifications == [_verification()]


@pytest.mark.parametrize("next_refresh", [NOW, None])
def test_list_claims_prints_refresh_state(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    next_refresh: datetime | None,
) -> None:
    repository = _ClaimRepository()
    monkeypatch.setattr(
        repository,
        "list_projections",
        lambda: (_projection(next_refresh_at=next_refresh),),
    )
    monkeypatch.setattr(cli, "_claim_repository", lambda: repository)

    cli.list_claims()

    assert capsys.readouterr().out.rstrip().endswith(next_refresh.isoformat() if next_refresh is not None else "none")


def test_list_claims_wraps_storage_failure(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def fail() -> tuple[CanonicalClaim, ...]:
        raise StorageTransactionError("offline")

    repository = _ClaimRepository()
    monkeypatch.setattr(repository, "list_projections", fail)
    monkeypatch.setattr(cli, "_claim_repository", lambda: repository)

    with pytest.raises(typer.Exit) as raised:
        cli.list_claims()

    assert (raised.value.exit_code, capsys.readouterr().err.strip()) == (
        1,
        "Cannot list claims: offline",
    )


@pytest.mark.parametrize("projection", [_projection(), None])
def test_show_prints_projection_and_history(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    projection: CanonicalClaim | None,
) -> None:
    monkeypatch.setattr(cli, "_claim_repository", lambda: _ClaimRepository(projection=projection))

    cli.show("claim")

    output = capsys.readouterr().out
    assert ('"canonical_claim_key": "claim"' if projection is not None else '{"projection": null}') in output
    assert '"observation_id":"observation"' in output
    assert '"verification_id":"verification"' in output


def test_show_wraps_missing_claim(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    repository = _ClaimRepository()

    def fail(_canonical_claim_key: str) -> CanonicalClaim | None:
        raise ClaimNotFoundError("missing")

    monkeypatch.setattr(repository, "get_projection", fail)
    monkeypatch.setattr(cli, "_claim_repository", lambda: repository)

    with pytest.raises(typer.Exit) as raised:
        cli.show("claim")

    assert (raised.value.exit_code, capsys.readouterr().err.strip()) == (
        1,
        "Cannot show claim claim: missing",
    )


@pytest.mark.parametrize("canonical_claim_key", ["claim", None])
def test_refresh_prints_projection(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    canonical_claim_key: str | None,
) -> None:
    monkeypatch.setattr(cli, "_claim_repository", _ClaimRepository)

    cli.refresh(canonical_claim_key)

    assert '"canonical_claim_key":"claim"' in capsys.readouterr().out


def test_refresh_wraps_repository_failure(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    repository = _ClaimRepository()

    def fail(**_kwargs: object) -> tuple[CanonicalClaim, ...]:
        raise ClaimNotFoundError("missing")

    monkeypatch.setattr(repository, "refresh_all", fail)
    monkeypatch.setattr(cli, "_claim_repository", lambda: repository)

    with pytest.raises(typer.Exit) as raised:
        cli.refresh()

    assert (raised.value.exit_code, capsys.readouterr().err.strip()) == (
        1,
        "Cannot refresh claims: missing",
    )
