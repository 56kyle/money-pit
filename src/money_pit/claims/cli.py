"""Module containing persistent claim CLI workflows."""

from datetime import UTC
from datetime import datetime

import typer

from money_pit.claims.repository import ClaimNotFoundError
from money_pit.claims.repository import ClaimRepository
from money_pit.cli_support import run_operator_command
from money_pit.config import ConfigurationScope
from money_pit.config import claim_refresh_policy
from money_pit.config import load_application_config
from money_pit.constants import DATA_ROOT
from money_pit.constants import STATE_DATABASE_FILENAME
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import VerificationResult
from money_pit.storage.database import Database


claims_app = typer.Typer(help="Inspect and refresh persistent claim memory.")


def _claim_repository() -> ClaimRepository:
    database = Database(DATA_ROOT / STATE_DATABASE_FILENAME)
    database.initialize()
    configuration = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    return ClaimRepository(database, refresh_policy=claim_refresh_policy(configuration.intelligence))


@claims_app.command(name="list")
def list_claims() -> None:
    """List current canonical claim projections."""
    _ = run_operator_command(
        lambda: _claim_repository().projections_as_of(as_of=datetime.now(tz=UTC)),
        heading="Claims",
    )


@claims_app.command()
def show(canonical_claim_key: str) -> None:
    """Show one projection and its immutable observation and verification history."""

    def run() -> dict[str, object]:
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
        return {
            "projection": projection,
            "observations": observations,
            "verifications": verifications,
        }

    _ = run_operator_command(run, heading="Claim")


@claims_app.command()
def refresh(canonical_claim_key: str | None = None) -> None:
    """Recompute current projections from durable point-in-time claim history."""
    as_of: datetime = datetime.now(tz=UTC)

    def run() -> tuple[CanonicalClaim, ...]:
        repository: ClaimRepository = _claim_repository()
        projections: tuple[CanonicalClaim, ...] = repository.materialize_projections(as_of=as_of)
        if canonical_claim_key is not None:
            projections = tuple(item for item in projections if item.canonical_claim_key == canonical_claim_key)
            if not projections:
                raise ClaimNotFoundError(f"Canonical claim not found: {canonical_claim_key}")
        return projections

    _ = run_operator_command(run, heading="Refreshed claims")
