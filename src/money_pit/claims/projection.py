"""Module deriving current claim projections and refresh schedules."""

from datetime import timedelta
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus


class ClaimProjectionError(Exception):
    """Base class for invalid claim-projection inputs."""


class EmptyClaimHistoryError(ClaimProjectionError):
    """Raised when a projection is requested without observations."""


class MixedCanonicalClaimError(ClaimProjectionError):
    """Raised when observations for different canonical keys are mixed."""


class NaiveProjectionTimeError(ClaimProjectionError):
    """Raised when a projection boundary has no UTC offset."""


class ClaimRefreshPolicy(BaseModel):
    """Deterministic refresh intervals by claim kind."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    factual: timedelta = timedelta(days=7)
    forecast: timedelta = timedelta(days=1)
    opinion: timedelta = timedelta(days=30)
    strategy: timedelta = timedelta(days=30)

    def interval_for(self, claim_kind: ClaimKind) -> timedelta:
        """Return the configured refresh interval for a claim kind."""
        return {
            ClaimKind.FACTUAL: self.factual,
            ClaimKind.FORECAST: self.forecast,
            ClaimKind.OPINION: self.opinion,
            ClaimKind.STRATEGY: self.strategy,
        }[claim_kind]


def project_canonical_claim(
    observations: tuple[ClaimObservation, ...],
    verifications: tuple[VerificationResult, ...],
    *,
    as_of: AwareDatetime,
    refresh_policy: ClaimRefreshPolicy,
) -> CanonicalClaim:
    """Derive one point-in-time current projection from immutable records."""
    _require_aware_as_of(as_of)
    if not observations:
        raise EmptyClaimHistoryError("Cannot project a claim without observations")
    canonical_key: str = observations[0].canonical_claim_key
    if any(observation.canonical_claim_key != canonical_key for observation in observations):
        raise MixedCanonicalClaimError("Claim observations use different canonical keys")

    visible: tuple[ClaimObservation, ...] = tuple(
        observation
        for observation in observations
        if observation.recorded_at <= as_of and observation.asserted_at <= as_of
    )
    if not visible:
        raise EmptyClaimHistoryError("No claim observations are visible at the projection time")
    superseded_ids: set[str] = {
        observation.supersedes_observation_id
        for observation in visible
        if observation.supersedes_observation_id is not None
        and (observation.valid_from is None or observation.valid_from <= as_of)
    }
    active: tuple[ClaimObservation, ...] = tuple(
        observation
        for observation in visible
        if observation.observation_id not in superseded_ids
        and (observation.valid_from is None or observation.valid_from <= as_of)
        and (observation.expires_at is None or observation.expires_at > as_of)
    )
    status: ClaimStatus = _projection_status(active, verifications, as_of=as_of)
    refresh_times: tuple[AwareDatetime, ...] = tuple(
        _next_refresh(observation, refresh_policy) for observation in active
    )
    return CanonicalClaim(
        canonical_claim_key=canonical_key,
        current_status=status,
        active_observation_ids=tuple(observation.observation_id for observation in active),
        last_material_change_at=max(observation.asserted_at for observation in visible),
        next_refresh_at=min(refresh_times) if refresh_times else None,
    )


def due_claim_keys(
    projections: tuple[CanonicalClaim, ...],
    *,
    as_of: AwareDatetime,
) -> tuple[str, ...]:
    """Return stable claim keys whose refresh time has arrived."""
    _require_aware_as_of(as_of)
    return tuple(
        sorted(
            projection.canonical_claim_key
            for projection in projections
            if projection.next_refresh_at is not None
            and projection.next_refresh_at <= as_of
            and projection.current_status in {ClaimStatus.ACTIVE, ClaimStatus.DISPUTED}
        ),
    )


def _projection_status(
    active: tuple[ClaimObservation, ...],
    verifications: tuple[VerificationResult, ...],
    *,
    as_of: AwareDatetime,
) -> ClaimStatus:
    if not active:
        return ClaimStatus.EXPIRED
    active_ids: set[str] = {observation.observation_id for observation in active}
    current_verifications: tuple[VerificationResult, ...] = tuple(
        verification
        for verification in verifications
        if verification.observation_id in active_ids
        and verification.recorded_at <= as_of
        and verification.checked_at <= as_of
        and (verification.valid_until is None or verification.valid_until > as_of)
    )
    if any(
        verification.status in {VerificationStatus.CONTRADICTED, VerificationStatus.MIXED}
        for verification in current_verifications
    ):
        return ClaimStatus.DISPUTED
    return ClaimStatus.ACTIVE


def _next_refresh(
    observation: ClaimObservation,
    refresh_policy: ClaimRefreshPolicy,
) -> AwareDatetime:
    interval_refresh: AwareDatetime = observation.asserted_at + refresh_policy.interval_for(
        observation.claim_kind,
    )
    if observation.expires_at is None:
        return interval_refresh
    return min(interval_refresh, observation.expires_at)


def _require_aware_as_of(as_of: AwareDatetime) -> None:
    if as_of.utcoffset() is None:
        raise NaiveProjectionTimeError("as_of must be timezone-aware")
