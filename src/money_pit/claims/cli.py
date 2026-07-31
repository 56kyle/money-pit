"""Module containing persistent claim CLI workflows."""

from datetime import UTC
from datetime import datetime
from pathlib import Path

import typer
from pydantic import ValidationError

from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.repository import ClaimNotFoundError
from money_pit.claims.repository import ClaimRepository
from money_pit.constants import DATA_ROOT
from money_pit.constants import STATE_DATABASE_FILENAME
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import VerificationResult
from money_pit.storage.database import Database
from money_pit.storage.errors import StorageError


claims_app = typer.Typer(help="Inspect and refresh persistent claim memory.")


def _claim_repository() -> ClaimRepository:
    database = Database(DATA_ROOT / STATE_DATABASE_FILENAME)
    database.initialize()
    return ClaimRepository(database)


@claims_app.command(name="append-observation")
def append_observation(input_path: Path) -> None:
    """Validate and append one ClaimObservation JSON document."""
    try:
        observation: ClaimObservation = ClaimObservation.model_validate_json(input_path.read_text(encoding="utf-8"))
        _claim_repository().append_observation(observation)
    except (OSError, ValidationError, StorageError) as error:
        typer.echo(f"Cannot append claim observation: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Appended claim observation {observation.observation_id}.")


@claims_app.command(name="append-verification")
def append_verification(input_path: Path) -> None:
    """Validate and append one VerificationResult JSON document."""
    try:
        verification: VerificationResult = VerificationResult.model_validate_json(
            input_path.read_text(encoding="utf-8")
        )
        _claim_repository().append_verification(verification)
    except (OSError, ValidationError, StorageError) as error:
        typer.echo(f"Cannot append verification result: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Appended verification result {verification.verification_id}.")


@claims_app.command(name="list")
def list_claims() -> None:
    """List current canonical claim projections."""
    try:
        projections: tuple[CanonicalClaim, ...] = _claim_repository().list_projections()
    except StorageError as error:
        typer.echo(f"Cannot list claims: {error}", err=True)
        raise typer.Exit(code=1) from error
    for projection in projections:
        refresh: str = projection.next_refresh_at.isoformat() if projection.next_refresh_at is not None else "none"
        typer.echo(
            f"{projection.canonical_claim_key}\t{projection.current_status.value}\t{refresh}",
        )


@claims_app.command()
def show(canonical_claim_key: str) -> None:
    """Show one projection and its immutable observation and verification history."""
    try:
        repository: ClaimRepository = _claim_repository()
        projection: CanonicalClaim | None = repository.get_projection(canonical_claim_key)
        observations: tuple[ClaimObservation, ...]
        verifications: tuple[VerificationResult, ...]
        observations, verifications = repository.history(canonical_claim_key)
    except (ClaimNotFoundError, StorageError) as error:
        typer.echo(f"Cannot show claim {canonical_claim_key}: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        CanonicalClaim.model_validate(projection).model_dump_json(indent=2)
        if projection is not None
        else '{"projection": null}',
    )
    for observation in observations:
        typer.echo(observation.model_dump_json())
    for verification in verifications:
        typer.echo(verification.model_dump_json())


@claims_app.command()
def refresh(canonical_claim_key: str | None = None) -> None:
    """Recompute current projections from durable point-in-time claim history."""
    as_of: datetime = datetime.now(tz=UTC)
    policy = ClaimRefreshPolicy()
    try:
        repository: ClaimRepository = _claim_repository()
        projections: tuple[CanonicalClaim, ...] = (
            (repository.refresh(canonical_claim_key, as_of=as_of, policy=policy),)
            if canonical_claim_key is not None
            else repository.refresh_all(as_of=as_of, policy=policy)
        )
    except (ClaimNotFoundError, StorageError) as error:
        typer.echo(f"Cannot refresh claims: {error}", err=True)
        raise typer.Exit(code=1) from error
    for projection in projections:
        typer.echo(projection.model_dump_json())
