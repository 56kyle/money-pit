"""Module containing persistent claim CLI workflows."""

from datetime import UTC
from datetime import datetime

import typer

from money_pit.claims.repository import ClaimNotFoundError
from money_pit.claims.repository import ClaimRepository
from money_pit.composition import execute_harness_run
from money_pit.config import ConfigurationScope
from money_pit.config import claim_refresh_policy
from money_pit.config import load_application_config
from money_pit.constants import APP_VERSION
from money_pit.constants import DATA_ROOT
from money_pit.constants import STATE_DATABASE_FILENAME
from money_pit.pipeline.chain import Stage
from money_pit.runs.paths import RepositoryPaths
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import VerificationResult
from money_pit.storage.database import Database
from money_pit.storage.errors import StorageError


claims_app = typer.Typer(help="Inspect and refresh persistent claim memory.")


def _claim_repository() -> ClaimRepository:
    database = Database(DATA_ROOT / STATE_DATABASE_FILENAME)
    database.initialize()
    configuration = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    return ClaimRepository(database, refresh_policy=claim_refresh_policy(configuration.intelligence))


@claims_app.command(name="list")
def list_claims() -> None:
    """List current canonical claim projections."""
    try:
        projections: tuple[CanonicalClaim, ...] = _claim_repository().projections_as_of(as_of=datetime.now(tz=UTC))
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
        as_of = datetime.now(tz=UTC)
        projection: CanonicalClaim | None = next(
            (
                item
                for item in repository.projections_as_of(as_of=as_of)
                if item.canonical_claim_key == canonical_claim_key
            ),
            None,
        )
        observations: tuple[ClaimObservation, ...]
        verifications: tuple[VerificationResult, ...]
        observations, verifications = repository.history_as_of(canonical_claim_key, as_of=as_of)
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
    try:
        repository: ClaimRepository = _claim_repository()
        projections: tuple[CanonicalClaim, ...] = repository.materialize_projections(as_of=as_of)
        if canonical_claim_key is not None:
            projections = tuple(item for item in projections if item.canonical_claim_key == canonical_claim_key)
            if not projections:
                raise ClaimNotFoundError(f"Canonical claim not found: {canonical_claim_key}")
    except (ClaimNotFoundError, StorageError) as error:
        typer.echo(f"Cannot refresh claims: {error}", err=True)
        raise typer.Exit(code=1) from error
    for projection in projections:
        typer.echo(projection.model_dump_json())


@claims_app.command()
def verify(as_of: datetime | None = None) -> None:
    """Run bounded research and adversarial verification through A4."""
    database = Database(DATA_ROOT / STATE_DATABASE_FILENAME)
    database.initialize()
    result = execute_harness_run(
        database=database,
        config=load_application_config(scope=ConfigurationScope.INTELLIGENCE),
        paths=RepositoryPaths.from_data_root(DATA_ROOT),
        source_id=None,
        requested_as_of=as_of,
        through=Stage.A4,
        implementation_version=APP_VERSION,
    )
    typer.echo(str(result))
