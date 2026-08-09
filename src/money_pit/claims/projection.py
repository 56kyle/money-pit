"""Module deriving configured point-in-time canonical claim projections."""

from datetime import timedelta
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimStatus
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.claims import VerificationStatus


class ClaimProjectionError(Exception):
    """Base class for invalid claim-projection inputs."""


class EmptyClaimHistoryError(ClaimProjectionError):
    """Raised when a projection is requested without visible observations."""


class NaiveProjectionTimeError(ClaimProjectionError):
    """Raised when a projection boundary has no UTC offset."""


class ClaimFreshnessRule(BaseModel):
    """Review and hard freshness intervals for one category and horizon."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    review_interval: timedelta = Field(gt=timedelta(0))
    freshness_interval: timedelta = Field(gt=timedelta(0))


class ClaimRefreshPolicy(BaseModel):
    """Complete versioned freshness rules keyed by category and horizon."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    policy_version: str = Field(min_length=1)
    rules: dict[ClaimCategory, dict[HorizonClass, ClaimFreshnessRule]]

    @model_validator(mode="after")
    def validate_complete_matrix(self) -> "ClaimRefreshPolicy":
        """Require every category and horizon to be explicitly configured."""
        if set(self.rules) != set(ClaimCategory):
            raise ValueError("freshness policy must define every claim category.")
        if any(set(horizons) != set(HorizonClass) for horizons in self.rules.values()):
            raise ValueError("freshness policy must define every horizon for each category.")
        return self

    def rule_for(self, observation: ClaimObservation) -> ClaimFreshnessRule:
        """Return the exact rule for an observation's category and horizon."""
        return self.rules[observation.category][observation.horizon_class]


def project_canonical_claim(
    observations: tuple[ClaimObservation, ...],
    verifications: tuple[VerificationResult, ...],
    *,
    canonical_claim_key: str,
    as_of: AwareDatetime,
    refresh_policy: ClaimRefreshPolicy,
    resolution_change_times: tuple[AwareDatetime, ...] = (),
) -> CanonicalClaim:
    """Derive one point-in-time projection from resolved immutable records."""
    _require_aware_as_of(as_of)
    visible = tuple(
        observation
        for observation in observations
        if observation.known_at <= as_of and observation.asserted_at <= as_of
    )
    if not visible:
        raise EmptyClaimHistoryError("No claim observations are visible at the projection time.")
    superseded_ids = {
        observation.supersedes_observation_id
        for observation in visible
        if observation.supersedes_observation_id is not None
        and (observation.effective_from is None or observation.effective_from <= as_of)
    }
    active = tuple(
        observation
        for observation in visible
        if observation.observation_id not in superseded_ids
        and (observation.effective_from is None or observation.effective_from <= as_of)
        and (observation.valid_until is None or observation.valid_until > as_of)
        and observation.known_at + refresh_policy.rule_for(observation).freshness_interval > as_of
    )
    visible_verifications = tuple(
        verification
        for verification in verifications
        if verification.known_at <= as_of and verification.checked_at <= as_of
    )
    latest_by_observation: dict[str, VerificationResult] = {}
    for verification in visible_verifications:
        prior = latest_by_observation.get(verification.observation_id)
        if prior is None or (
            verification.known_at,
            verification.checked_at,
            verification.verification_id,
        ) > (prior.known_at, prior.checked_at, prior.verification_id):
            latest_by_observation[verification.observation_id] = verification
    latest_verifications = tuple(latest_by_observation.values())
    current_verifications = tuple(
        verification
        for verification in latest_verifications
        if verification.valid_until is None or verification.valid_until > as_of
    )
    active_ids = {item.observation_id for item in active}
    if not active:
        status = ClaimStatus.EXPIRED
    elif any(
        verification.status in {VerificationStatus.CONTRADICTED, VerificationStatus.MIXED}
        and verification.observation_id in active_ids
        for verification in current_verifications
    ):
        status = ClaimStatus.DISPUTED
    else:
        status = ClaimStatus.ACTIVE
    material_change_at = max(
        (
            *(observation.known_at for observation in visible),
            *(verification.known_at for verification in latest_verifications),
            *resolution_change_times,
        )
    )
    refresh_times = tuple(
        _next_refresh(observation, refresh_policy, material_change_at=material_change_at) for observation in active
    )
    verification_expiries = tuple(
        verification.valid_until
        for verification in latest_verifications
        if verification.valid_until is not None and verification.valid_until > as_of
    )
    return CanonicalClaim(
        canonical_claim_key=canonical_claim_key,
        current_status=status,
        active_observation_ids=tuple(observation.observation_id for observation in active),
        projected_as_of=as_of,
        last_material_change_at=material_change_at,
        next_refresh_at=min((*refresh_times, *verification_expiries))
        if refresh_times or verification_expiries
        else None,
        freshness_policy_version=refresh_policy.policy_version,
    )


def due_claim_keys(projections: tuple[CanonicalClaim, ...], *, as_of: AwareDatetime) -> tuple[str, ...]:
    """Return stable claim keys whose configured refresh time has arrived."""
    _require_aware_as_of(as_of)
    return tuple(
        sorted(
            item.canonical_claim_key
            for item in projections
            if item.next_refresh_at is not None
            and item.next_refresh_at <= as_of
            and item.current_status in {ClaimStatus.ACTIVE, ClaimStatus.DISPUTED}
        )
    )


def _next_refresh(
    observation: ClaimObservation,
    policy: ClaimRefreshPolicy,
    *,
    material_change_at: AwareDatetime,
) -> AwareDatetime:
    rule = policy.rule_for(observation)
    candidates = [
        material_change_at + rule.review_interval,
        observation.known_at + rule.freshness_interval,
    ]
    if observation.review_at is not None:
        candidates.append(observation.review_at)
    if observation.valid_until is not None:
        candidates.append(observation.valid_until)
    return min(candidates)


def _require_aware_as_of(as_of: AwareDatetime) -> None:
    if as_of.utcoffset() is None:
        raise NaiveProjectionTimeError("as_of must be timezone-aware.")
